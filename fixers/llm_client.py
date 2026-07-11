"""
Мульти-LLM клиент для Webbles Fix.
Поддерживает несколько провайдеров, но использует ОДНОГО основного.
Оркестрация убрана для стабильности и скорости.
Добавлены таймауты для всех API-вызовов.
"""

import concurrent.futures
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SYSTEM_MESSAGE = "You are a code fixing assistant. Output ONLY a unified diff."


class _DaemonFuture:
    """Минимальный Future-совместимый объект (.result(timeout=) поднимает
    concurrent.futures.TimeoutError) для _SingleDaemonWorker — см. там же."""

    def __init__(self):
        self._q: "queue.Queue" = queue.Queue(maxsize=1)

    def result(self, timeout: Optional[float] = None):
        try:
            status, payload = self._q.get(timeout=timeout)
        except queue.Empty:
            raise concurrent.futures.TimeoutError()
        if status == "err":
            raise payload
        return payload

    def _set_result(self, value) -> None:
        self._q.put(("ok", value))

    def _set_exception(self, exc: Exception) -> None:
        self._q.put(("err", exc))


class _SingleDaemonWorker:
    """Персистентный single-worker пул на DAEMON-потоке вместо
    `concurrent.futures.ThreadPoolExecutor`.

    Control series 2026-06-21, находка #2 (зависание после финального
    отчёта): `ThreadPoolExecutor` регистрирует свои рабочие потоки в
    `concurrent.futures.thread._python_exit` — atexit-хуке, который
    БЕЗУСЛОВНО join'ит все такие потоки перед завершением интерпретатора,
    НЕЗАВИСИМО от `daemon`-флага потока. Если зависший/очень медленный вызов
    LLM (например, `LocalLLMProvider._do_request`'s собственная retry-цепочка
    10/30/60с на повторяющийся parse-error) переживает наш foreground
    `hard_limit` (см. `_hard_timeout_call`), фоновый воркер продолжает
    выполнение ПОСЛЕ того, как foreground уже сдался и весь видимый Python-код
    (включая финальный отчёт run_agent.py) отработал — процесс не завершается,
    пока этот воркер не закончит (control series, AdsMCP/tiktok-ads-mcp-server,
    ~30 минут зависания с CPU≈0 после печати `=== ИТОГ ===`).

    Раздельные обычные `threading.Thread(daemon=True)` НЕ регистрируются в
    этом atexit-хуке — интерпретатор просто бросает их при выходе, не дожидаясь.
    Сохраняет тот же `.submit()`/single-worker контракт, что и раньше (один
    persistent воркер — не плодим зомби-потоки, бьющие по локальному
    LLM-серверу конкурентно, как было до фикса control series 12).
    """

    def __init__(self):
        self._task_q: "queue.Queue" = queue.Queue()
        self._max_workers = 1  # backward-compat: тесты читают этот атрибут
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="llm-hard-timeout-worker",
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            func, args, kwargs, fut = self._task_q.get()
            try:
                fut._set_result(func(*args, **kwargs))
            except Exception as e:
                fut._set_exception(e)

    def submit(self, func, *args, **kwargs) -> _DaemonFuture:
        fut = _DaemonFuture()
        self._task_q.put((func, args, kwargs, fut))
        return fut


class LLMClient:
    """Мульти-провайдерный LLM клиент (без оркестрации)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, language_provider=None):
        self.config = config or {}
        self.language_provider = language_provider

        self.providers = self._load_providers()
        # Таймаут для каждого запроса (секунды)
        self.per_request_timeout = self.config.get("llm", {}).get("timeout", 120)
        # Сколько раз повторять при неудачном вызове (1 = без retry).
        # Паузы между попытками — exponential backoff: 2, 4, 8 с.
        self.max_retries = int(self.config.get("llm", {}).get("max_retries", 3))
        # Count of hard-timeout events in this session (visible via PipelineEngine stats).
        self.hard_timeout_count: int = 0
        # 2026-07-07 (фаза Performance): счётчик исходящих LLM-вызовов за
        # сессию — главная метрика фазы (LLM ≈ 90% wall-time по perf-профилю;
        # оптимизации меряем в сэкономленных вызовах, не секундах). Логический
        # вызов = один вход в _call_with_retries/_call_for_json_with_retries
        # (внутренние network-retry не считаются — их число зависит от
        # нестабильности DeepSeek, а не от пайплайна).
        self.llm_call_count: int = 0
        # Consecutive hard-timeouts (resets to 0 on any successful response).
        # control series 12 (2026-06-20, cantools/textparser): _hard_timeout_call
        # used to spawn a FRESH ThreadPoolExecutor per call and abandon it
        # (Python threads can't be killed) — every failed attempt left a zombie
        # thread still hammering the (typically single-slot) local LLM server,
        # so each subsequent attempt got progressively slower until everything
        # hung. Fix: one persistent single-worker executor (serializes our own
        # outbound requests) + a circuit breaker that stops calling the LLM
        # entirely after too many consecutive failures, instead of retrying
        # forever.
        self.consecutive_hard_timeouts: int = 0
        self.llm_unresponsive_threshold: int = int(
            self.config.get("llm", {}).get("unresponsive_threshold", 5)
        )
        # 2026-07-10 (C++ серия, tinyxml2): подряд идущие «неответы» LLM НЕ по
        # hard-timeout, а по transport_error/empty (DeepSeek возвращает пустоту/
        # рвёт соединение) РАНЬШЕ не считались — breaker не срабатывал, прогон
        # жёг весь project_timeout (398 вызовов, 272 empty, 30 мин, 0 доставки),
        # а результат маскировался под «project_timeout / 0 delivery» (не
        # отличить от реального бага движка). Считаем их отдельным счётчиком с
        # более консервативным порогом (транзиентные блипы сети — норма, глушим
        # только устойчивую недоступность). Сброс — на первый реальный ответ.
        self.consecutive_no_response: int = 0
        self.no_response_threshold: int = int(
            self.config.get("llm", {}).get("no_response_threshold",
                                            max(self.llm_unresponsive_threshold * 3, 12))
        )
        self._executor = _SingleDaemonWorker()

        # 2026-06-24: PROJECT_DEADLINE — раньше проверялся только МЕЖДУ
        # вызовами llm_client (GeneratePatchStage._deadline_exceeded перед
        # каждой retry-итерацией СВОЕЙ петли), но НЕ внутри ОДНОГО вызова
        # _call_with_retries/_call_for_json_with_retries — их собственная
        # exponential-backoff retry-петля (до max_retries попыток по
        # hard_limit=per_request_timeout*1.5 секунд + 2/4/8с паузы между)
        # могла перерасходовать бюджет на десятки-сотни секунд за ОДИН
        # вызов, даже если дедлайн уже прошёл к началу второй попытки.
        # Вызывающий код (GeneratePatchStage.execute) выставляет это перед
        # обращением к LLM; None — поведение не меняется (нет дедлайна).
        self.project_deadline: Optional[float] = None

        # empty_response диагностика (2026-06-22): после КАЖДОГО вызова
        # generate_structured_fix/generate_fix, вернувшего None, эти два
        # поля содержат точную причину и сырой ответ модели — вызывающий
        # код (GeneratePatchStage) читает их немедленно после вызова, чтобы
        # не путать "LLM не ответил вовсе" (timeout/transport_error/empty)
        # с "LLM ответил, но результат не собрался в патч" (bad_format/
        # parser_fail/anchor_empty/anchor_mismatch/diff_fail) — раньше всё
        # это сваливалось в одну REJECT-причину "empty_response", что
        # инфлировало REJECT-счётчик решениями, для которых патча для
        # оценки никогда и не было (особенно заметно на локальной LLM).
        self.last_failure_category: Optional[str] = None
        self.last_raw_response: Optional[str] = None

        logger.info(f"Загружено {len(self.providers)} LLM-провайдеров")
        self._preflight_health_check()

    def _preflight_health_check(self) -> None:
        """Pre-flight проверка ТОЛЬКО для provider="local" (empty_response
        диагностика, 2026-06-22, категория "transport_error"): если сервер
        мёртв, узнаём об этом за 5с, а не после серии retry-попыток с
        exponential backoff (2/4/8с × max_retries) на каждую ошибку всего
        прогона. При недоступности — взводим ТОТ ЖЕ circuit breaker, что
        ловит подряд идущие hard-timeout (is_unresponsive), чтобы все
        последующие вызовы fail-fast'ились с категорией "timeout", не тратя
        реальные HTTP-таймауты на заведомо мёртвый сервер.

        Удалённые провайдеры (openai/deepseek/anthropic/ollama) НЕ
        проверяются — пинг чужого API без необходимости — лишний вызов,
        и у них уже есть собственная надёжная обработка ошибок.

        2026-06-24: проверяем ТОЛЬКО providers[0] — реально для вызовов
        используется именно он (см. generate_structured_fix/_call_llm и
        др., все берут self.providers[0]), остальные записи в списке сейчас
        ни на что не влияют (нет fallback-логики). Раньше цикл проверял
        ВСЕ провайдеры подряд — если где-то в списке оставался неиспользуемый
        "local"-провайдер (например, как запасной вариант "на будущее") и
        его сервер был выключен, health-check взводил ОБЩИЙ (на весь
        LLMClient) circuit breaker — и реально используемый providers[0]
        (например, облачный deepseek) после этого fail-fast'ился без единой
        попытки, даже будучи полностью исправным."""
        for provider in self.providers[:1]:
            if provider.get("provider") != "local":
                continue
            try:
                from fixers.local_llm_provider import LocalLLMProvider
                p = LocalLLMProvider(
                    base_url=provider.get("base_url", "http://localhost:8080/v1"),
                    model=provider.get("model", "local"),
                    api_key=provider.get("api_key", ""),
                    profile=provider.get("profile", "patch"),
                )
                if not p.health_check(timeout=5.0):
                    logger.error(
                        "LLM pre-flight: локальный сервер %s недоступен — "
                        "взводим circuit breaker заранее (fail-fast вместо "
                        "трат времени на retry в течение всего прогона)",
                        p.base_url,
                    )
                    self.consecutive_hard_timeouts = self.llm_unresponsive_threshold
                else:
                    logger.info("LLM pre-flight: локальный сервер %s доступен", p.base_url)
            except Exception as e:
                logger.debug("LLM pre-flight health-check упал (не критично): %s", e)

    def is_unresponsive(self) -> bool:
        """True если LLM накопил >= llm_unresponsive_threshold hard-timeout подряд
        ИЛИ >= no_response_threshold transport_error/empty подряд (2026-07-10).

        Once tripped, остаётся True до первого успешного ответа — намеренно без
        автоматического восстановления (recovery-проба — отдельная задача, не
        в этой сессии). Вызывающий код должен fail-fast вместо retry.
        """
        return (self.consecutive_hard_timeouts >= self.llm_unresponsive_threshold
                or self.consecutive_no_response >= self.no_response_threshold)

    # Категории «LLM не ответил вовсе» (инфраструктура) — считаются в
    # consecutive_no_response. bad_format/parser_fail/anchor_* НЕ входят: там
    # LLM ЖИВ и ответил, просто контент не собрался — это не недоступность.
    _NO_RESPONSE_CATEGORIES = frozenset({"transport_error", "empty", "timeout"})

    def _note_response_outcome(self, raw: Optional[str], category: Optional[str]) -> None:
        """Обновляет счётчик подряд идущих «неответов». Реальный ответ (raw
        непустой) — сброс; transport_error/empty/timeout — инкремент."""
        if raw:
            self.consecutive_no_response = 0
        elif category in self._NO_RESPONSE_CATEGORIES:
            self.consecutive_no_response += 1
            if self.consecutive_no_response == self.no_response_threshold:
                logger.warning(
                    "LLM недоступен: %d подряд '%s'-неответов (порог %d) — "
                    "дальнейшие вызовы fail-fast, прогон завершится быстро "
                    "вместо выжигания бюджета",
                    self.consecutive_no_response, category, self.no_response_threshold,
                )

    def _deadline_exceeded(self) -> bool:
        """project_timeout, выставленный вызывающим кодом в self.project_deadline
        (см. docstring в __init__). None — поведение не меняется (нет дедлайна,
        как раньше)."""
        if self.project_deadline is None:
            return False
        return time.monotonic() >= float(self.project_deadline)

    def _load_providers(self) -> List[Dict[str, Any]]:
        providers = []

        llm_config = self.config.get("llm", {})
        provider_list = llm_config.get("providers", [])

        if not provider_list:
            provider_list = self.config.get("providers", [])

        if provider_list:
            for p in provider_list:
                entry: Dict[str, Any] = {
                    "name": p.get("name", p.get("provider", "unknown")),
                    "provider": p.get("provider", "openai"),
                    "model": p.get("model", "gpt-4"),
                    "api_key": p.get("api_key") or os.environ.get("OPENAI_API_KEY"),
                    "base_url": p.get("base_url"),
                    "max_tokens": p.get("max_tokens", 8000),
                }
                # Поля local-провайдера — прозрачно проброшены
                if p.get("provider") == "local":
                    if "profile" in p:
                        entry["profile"] = p["profile"]
                    if "context_window" in p:
                        entry["context_window"] = int(p["context_window"])
                    if "timeout" in p:
                        entry["timeout"] = p["timeout"]
                providers.append(entry)
            return providers

        # Старый формат: одиночный провайдер
        if llm_config.get("provider"):
            providers.append({
                "name": llm_config.get("provider"),
                "provider": llm_config.get("provider"),
                "model": llm_config.get("model", "gpt-4"),
                "api_key": llm_config.get("api_key") or os.environ.get("OPENAI_API_KEY"),
                "base_url": llm_config.get("base_url"),
                "max_tokens": llm_config.get("max_tokens", 8000),
            })
            return providers

        return providers

    def _get_prompt_template(self, template_name: str) -> str:
        prompt_path = Path(__file__).parent.parent / "prompts" / template_name
        if not prompt_path.exists():
            logger.warning(f"Файл промпта не найден: {prompt_path}")
            return ""
        return prompt_path.read_text(encoding="utf-8")

    def _fill_prompt(self, template: str, **kwargs) -> str:
        result = template
        for key, value in kwargs.items():
            result = result.replace("{" + key + "}", str(value))
        return result

    def _hard_timeout_call(self, func, *args, **kwargs) -> Tuple[Optional[str], Optional[str]]:
        """Thread-based hard timeout wrapper for LLM calls.

        Runs `func(*args, **kwargs)` in a thread; if it doesn't finish within
        hard_limit seconds, returns (None, "hard_timeout_Xs") immediately.

        Uses `self._executor` — ONE persistent single-worker executor shared
        across all calls for the LLMClient's lifetime (NOT a fresh executor
        per call). Python threads can't be forcefully killed, so a stalled
        call's worker thread keeps running in the background regardless; the
        old per-call-executor design abandoned a fresh thread on every hard
        timeout, and those zombie threads kept hammering the (typically
        single-slot) local LLM server concurrently, making every subsequent
        call progressively slower (control series 12, cantools/textparser).
        A single shared worker caps our own outbound concurrency at 1,
        matching how a local single-slot LLM server actually processes
        requests, instead of piling up.
        """
        hard_limit = float(self.per_request_timeout) * 1.5  # 50% grace above HTTP timeout
        _fut = self._executor.submit(func, *args, **kwargs)
        try:
            result = _fut.result(timeout=hard_limit)
            self.consecutive_hard_timeouts = 0
            return result if isinstance(result, tuple) else (result, None)
        except concurrent.futures.TimeoutError:
            self.hard_timeout_count += 1
            self.consecutive_hard_timeouts += 1
            logger.error(
                "LLM hard timeout #%d (подряд: %d): %.0fs exceeded (HTTP timeout=%.0fs)",
                self.hard_timeout_count, self.consecutive_hard_timeouts,
                hard_limit, self.per_request_timeout,
            )
            return None, f"hard_timeout_{hard_limit:.0f}s"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def _call_with_retries(self, provider, prompt: str) -> Tuple[Optional[str], Optional[str]]:
        """Обёртка над _call_provider_with_prompt с exponential backoff.

        При временных сетевых сбоях (timeout, 429, 5xx и т.п.) делает до
        self.max_retries попыток с паузами 2, 4, 8 секунд. Возвращает первую
        успешную пару (response, None) либо итоговую (None, last_error).
        Каждая неудачная попытка логируется на уровне WARNING.

        Circuit breaker: если LLM уже накопил llm_unresponsive_threshold
        hard-timeout подряд (через любые предыдущие вызовы в рамках этого
        LLMClient), не пытаемся вообще — сразу fail-fast. Без этого пайплайн
        продолжал бы слать новые запросы зависшему серверу до конца
        project_timeout (control series 12, cantools/textparser: 20+ hard
        timeout подряд, ни одного ответа за ~45 минут).
        """
        self.llm_call_count += 1
        if self.is_unresponsive():
            logger.error(
                "LLM unresponsive (%d hard timeout подряд >= порог %d) — fail-fast без попытки",
                self.consecutive_hard_timeouts, self.llm_unresponsive_threshold,
            )
            return None, "llm_unresponsive"
        if self._deadline_exceeded():
            logger.warning("LLM: PROJECT_DEADLINE уже прошёл — fail-fast без попытки")
            return None, "deadline_exceeded"
        last_err: Optional[str] = None
        attempts = max(1, int(self.max_retries))
        for attempt in range(1, attempts + 1):
            response, err = self._hard_timeout_call(self._call_provider_with_prompt, provider, prompt)
            if response is not None:
                if attempt > 1:
                    logger.info("LLM: успех на попытке %d/%d", attempt, attempts)
                return response, None
            last_err = err
            if self.is_unresponsive():
                logger.error(
                    "LLM unresponsive (%d hard timeout подряд >= порог %d) — прекращаем retry досрочно",
                    self.consecutive_hard_timeouts, self.llm_unresponsive_threshold,
                )
                break
            if self._deadline_exceeded():
                logger.warning(
                    "LLM: PROJECT_DEADLINE прошёл после попытки %d/%d — прекращаем retry досрочно",
                    attempt, attempts,
                )
                last_err = "deadline_exceeded"
                break
            if attempt < attempts:
                wait = 2 ** attempt
                logger.warning(
                    "LLM попытка %d/%d не удалась (%s), пауза %dс",
                    attempt, attempts, err, wait,
                )
                time.sleep(wait)
        return None, last_err

    @staticmethod
    def _classify_call_err(err: Optional[str]) -> str:
        """Категоризирует причину провала вызова (err из _call_*_with_retries).
        "timeout" — hard-timeout или circuit-breaker (llm_unresponsive);
        "transport_error" — иное (соединение, неподдерживаемый провайдер,
        исключение SDK и т.п.)."""
        if err is None:
            return "empty"
        if err == "llm_unresponsive" or err.startswith("hard_timeout_"):
            return "timeout"
        return "transport_error"

    def _call_llm(self, prompt: str) -> Optional[str]:
        if not self.providers:
            logger.error("Нет доступных LLM-провайдеров")
            return None
        response, err = self._call_with_retries(self.providers[0], prompt)
        if response is None and err:
            logger.warning("_call_llm: пустой ответ после retry, причина: %s", err)
        return response

    # ------------------------------------------------------------------
    # Гибрид diff+JSON: получаем EditSet, а unified diff собираем сами.
    # См. fixers/structured_edit.py — там JSON_SCHEMA_HINT и сборщик.
    # ------------------------------------------------------------------
    def generate_structured_fix(
        self,
        error: Dict[str, Any],
        file_contents: Dict[str, str],
        language: str,
        extra_hint: str = "",
    ) -> Optional[Any]:
        """Запрашивает у LLM EditSet (json), а не текстовый unified diff.

        `file_contents` — {relative_path: text}, чтобы вызвать
        `EditSet.to_unified_diff` СНАРУЖИ. Сам метод EditSet НЕ применяет.

        Возвращает EditSet | None.
        """
        from fixers.structured_edit import EditSet, JSON_SCHEMA_HINT

        self.last_failure_category = None
        self.last_raw_response = None

        if not self.providers:
            logger.error("Нет доступных LLM-провайдеров")
            self.last_failure_category = "transport_error"
            return None

        provider = self.providers[0]

        target_file = error.get("file", "unknown")
        target_line = error.get("line", 0)
        error_code = error.get("code", "")
        error_msg = error.get("message", "")
        snippet = file_contents.get(target_file, "")
        # 2026-07-08 (loguru, 108 CRITICAL_SYNTAX-откатов за прогон): путь с
        # Windows-backslash в промпте ("loguru\_recattrs.py") LLM читает как
        # markdown-escape `\_` и возвращает "loguru_recattrs.py" — EditSet не
        # находит файл, патч отклоняется/мнётся. В промпт путь идёт ТОЛЬКО
        # forward-slash нормализованным; матчинг ответа дополнительно
        # страхуется в structured_edit (mangled-fallback).
        prompt_file = str(target_file).replace("\\", "/")

        # Для провайдеров с большим контекстным окном (≥100k) передаём файл
        # целиком; для остальных — окно ±radius строк вокруг ошибки.
        context_window = provider.get("context_window", 0)
        if context_window >= 100_000:
            snippet_view = snippet
            base_line = 1
        else:
            radius = 60
            snippet_view = _trim_around_line(snippet, target_line, radius=radius)
            base_line = _trim_base_line(snippet, target_line, radius=radius)

        system_message = (
            "You are a precise code-fixing assistant for "
            f"{language}. Return ONLY a JSON object describing the minimal "
            "set of edits to fix the given error.\n\n" + JSON_SCHEMA_HINT
        )

        user_parts = [
            f"FILE: {prompt_file}",
            f"LINE: {target_line}" if target_line else "LINE: <unknown>",
            f"ERROR CODE: {error_code or '<none>'}",
            f"ERROR MESSAGE: {error_msg}",
        ]
        if extra_hint:
            user_parts.append("\nCONTEXT:\n" + extra_hint)
        user_parts.append("\nFILE CONTENT (with line numbers):")
        user_parts.append(_numbered(snippet_view, base_line=base_line))
        user_prompt = "\n".join(user_parts)

        raw, err = self._call_for_json_with_retries(provider, system_message, user_prompt)
        self.last_raw_response = raw
        if not raw:
            self.last_failure_category = (
                self._classify_call_err(err) if raw is None else "empty"
            )
            self._note_response_outcome(None, self.last_failure_category)
            if err:
                logger.warning("generate_structured_fix: LLM не ответил (%s)", err)
            return None
        self._note_response_outcome(raw, None)  # LLM жив (ответил) — сброс счётчика

        diag: Dict[str, Any] = {}
        edit_set = EditSet.from_json(raw, diag=diag)
        if edit_set is None:
            self.last_failure_category = diag.get("reason", "bad_format")
            logger.warning(
                "generate_structured_fix: ответ не парсится как EditSet (%s), len=%d",
                self.last_failure_category, len(raw),
            )
            return None
        logger.info(
            "generate_structured_fix: получен EditSet intent=%r, edits=%d, confidence=%.2f",
            edit_set.intent[:60], len(edit_set.edits), edit_set.confidence,
        )
        return edit_set

    # =================================================================
    # D.3 — strict reviewer
    # =================================================================
    def review_patch(
        self,
        error: Dict[str, Any],
        structured_edit: Optional[Dict[str, Any]],
        patch: str,
        before_errors: List[Dict[str, Any]],
        after_errors: List[Dict[str, Any]],
        file_context: str,
        language: str,
    ) -> Optional[Dict[str, Any]]:
        """LLM-ревью применённого патча.

        Возвращает словарь `{verdict, reasons, confidence_adjustment}` или
        `None` если LLM не ответил / ответил невалидным JSON.

        verdict:
          - ``ok``    — фикс по существу, минимальный, ничего лишнего.
          - ``noisy`` — фиксит ошибку, но трогает не относящиеся к ней строки
            (или дублирует код / меняет стиль / правит неактивные участки).
          - ``wrong`` — НЕ фиксит, или ломает существенное.

        confidence_adjustment ∈ [-0.2, +0.2] — корректировка, которую
        DecideStage прибавит к текущему `metadata["confidence"]`. На вход
        попадает уже после жёсткого clamp.
        """
        if not self.providers:
            logger.error("review_patch: нет доступных LLM-провайдеров")
            return None

        provider = self.providers[0]

        JSON_SCHEMA = (
            "Output STRICTLY a JSON object with these keys (no markdown, no prose):\n"
            '  "verdict":               "ok" | "noisy" | "wrong"\n'
            '  "reasons":               array of short strings (each ≤120 chars), at most 6\n'
            '  "confidence_adjustment": number in [-0.2, +0.2]\n'
            "Rules:\n"
            "  - ok    : patch fixes exactly the target error, nothing extra.\n"
            "  - noisy : patch fixes the target but touches unrelated code,\n"
            "            duplicates blocks, adds dead imports or stylistic noise.\n"
            "  - wrong : patch does NOT fix the target error, or introduces\n"
            "            a regression visible in the AFTER errors.\n"
            "  - confidence_adjustment is your delta to the existing confidence:\n"
            "    +0.20 for very clean ok, +0.05 typical ok,\n"
            "     0.00 inconclusive,\n"
            "    -0.10 noisy, -0.20 wrong.\n"
            "  - Do not invent new errors; reason only from BEFORE and AFTER given."
        )

        system_message = (
            f"You are a STRICT code reviewer for {language}. "
            "Your only job is to evaluate whether the proposed patch is a clean, "
            "minimal, correct fix for the stated target error. "
            "Be skeptical: many LLM-generated patches look correct but include "
            "noisy unrelated changes.\n\n" + JSON_SCHEMA
        )

        def _summarise_errors(errs: List[Dict[str, Any]], limit: int = 20) -> str:
            if not errs:
                return "<none>"
            out: List[str] = []
            for e in errs[:limit]:
                msg = (e.get("message") or "")[:140].replace("\n", " ")
                out.append(
                    f"  [{e.get('code','')}] {e.get('file','')}:{e.get('line','')} {msg}"
                )
            if len(errs) > limit:
                out.append(f"  ... and {len(errs) - limit} more")
            return "\n".join(out)

        edit_summary = "<not provided — only raw patch available>"
        if structured_edit:
            try:
                edit_summary = (
                    f"intent: {structured_edit.get('intent', '')}\n"
                    f"self-reported confidence: {structured_edit.get('confidence', '')}\n"
                    f"risks: {structured_edit.get('risks', [])}\n"
                    "edits:\n"
                    + json.dumps(
                        structured_edit.get("edits", []),
                        indent=2, ensure_ascii=False,
                    )[:1500]
                )
            except Exception:
                edit_summary = "<unparseable structured_edit>"

        user_prompt = "\n".join([
            f"TARGET ERROR: [{error.get('code','')}] "
            f"{error.get('file','')}:{error.get('line','')}",
            f"MESSAGE: {(error.get('message') or '')[:300]}",
            "",
            "BEFORE (errors present before applying the patch):",
            _summarise_errors(before_errors),
            "",
            "AFTER  (errors present now, after applying):",
            _summarise_errors(after_errors),
            "",
            "STRUCTURED EDIT (from generate_structured_fix):",
            edit_summary,
            "",
            "RAW UNIFIED DIFF (truncated to 4000 chars):",
            (patch or "")[:4000],
            "",
            "FILE CONTEXT (snippet around target line, truncated):",
            (file_context or "")[:2000],
        ])

        raw, err = self._call_for_json_with_retries(
            provider, system_message, user_prompt,
        )
        if raw is None:
            if err:
                logger.warning("review_patch: LLM не ответил (%s)", err)
            return None

        try:
            data = json.loads(raw)
        except Exception as e:
            logger.warning(
                "review_patch: невалидный JSON (%s): %s", e, (raw or "")[:200],
            )
            return None

        # Валидация + нормализация. Сознательно мягкие на стороне ввода:
        # неизвестный verdict → 'noisy' (нейтрально-консервативно).
        verdict = str(data.get("verdict", "")).lower().strip()
        if verdict not in ("ok", "noisy", "wrong"):
            logger.warning(
                "review_patch: неизвестный verdict=%r — считаем 'noisy'",
                verdict,
            )
            verdict = "noisy"

        reasons = data.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        reasons = [str(r)[:200] for r in reasons][:6]

        try:
            adj = float(data.get("confidence_adjustment", 0.0))
        except (TypeError, ValueError):
            adj = 0.0
        # Жёсткий clamp по плану D.3.
        adj = max(-0.2, min(0.2, adj))

        logger.info(
            "review_patch: verdict=%s adj=%+.2f reasons=%d",
            verdict, adj, len(reasons),
        )
        return {
            "verdict": verdict,
            "reasons": reasons,
            "confidence_adjustment": adj,
        }

    def _call_for_json_with_retries(
        self, provider: Dict[str, Any], system_message: str, user_prompt: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Аналог _call_with_retries, но запрашивает JSON-режим у провайдера.

        Тот же circuit breaker, что и в _call_with_retries — этот метод
        вызывает _hard_timeout_call напрямую, минуя _call_with_retries, так
        что без отдельной проверки здесь LLM unresponsive не ловился бы для
        JSON-режима (generate_structured_fix).
        """
        self.llm_call_count += 1
        if self.is_unresponsive():
            logger.error(
                "LLM-JSON unresponsive (%d hard timeout подряд >= порог %d) — fail-fast без попытки",
                self.consecutive_hard_timeouts, self.llm_unresponsive_threshold,
            )
            return None, "llm_unresponsive"
        if self._deadline_exceeded():
            logger.warning("LLM-JSON: PROJECT_DEADLINE уже прошёл — fail-fast без попытки")
            return None, "deadline_exceeded"
        last_err: Optional[str] = None
        attempts = max(1, int(self.max_retries))
        for attempt in range(1, attempts + 1):
            response, err = self._hard_timeout_call(
                self._call_provider_for_json, provider, system_message, user_prompt
            )
            if response is not None:
                if attempt > 1:
                    logger.info("LLM-JSON: успех на попытке %d/%d", attempt, attempts)
                return response, None
            last_err = err
            if self.is_unresponsive():
                logger.error(
                    "LLM-JSON unresponsive (%d hard timeout подряд >= порог %d) — прекращаем retry досрочно",
                    self.consecutive_hard_timeouts, self.llm_unresponsive_threshold,
                )
                break
            if self._deadline_exceeded():
                logger.warning(
                    "LLM-JSON: PROJECT_DEADLINE прошёл после попытки %d/%d — прекращаем retry досрочно",
                    attempt, attempts,
                )
                last_err = "deadline_exceeded"
                break
            if attempt < attempts:
                wait = 2 ** attempt
                logger.warning(
                    "LLM-JSON попытка %d/%d не удалась (%s), пауза %dс",
                    attempt, attempts, err, wait,
                )
                time.sleep(wait)
        return None, last_err

    def _call_provider_for_json(
        self, config: Dict[str, Any], system_message: str, user_prompt: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        provider_name = config.get("provider", "unknown")
        model_name = config.get("model", "unknown")
        try:
            if provider_name == "openai":
                return self._call_openai_json(config, system_message, user_prompt), None
            elif provider_name == "deepseek":
                return self._call_deepseek_json(config, system_message, user_prompt), None
            elif provider_name == "anthropic":
                return self._call_anthropic_json(config, system_message, user_prompt), None
            elif provider_name == "ollama":
                # ollama: нет аппаратного json_object, обходимся system-инструкцией
                return self._call_ollama_json(config, system_message, user_prompt), None
            elif provider_name == "local":
                return self._call_local_json(config, system_message, user_prompt), None
            else:
                err = f"Неподдерживаемый провайдер: {provider_name}"
                logger.error("LLM-JSON вызов не выполнен: %s", err)
                return None, err
        except Exception as e:
            logger.error(
                "LLM-JSON вызов не выполнен (provider=%s, model=%s): %s: %s",
                provider_name, model_name, type(e).__name__, e,
            )
            return None, f"{type(e).__name__}: {e}"

    def _call_openai_json(self, config, system_message, user_prompt) -> str:
        import openai
        client = openai.OpenAI(
            api_key=config["api_key"],
            http_client=self._make_httpx_client(),
        )
        resp = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=config.get("max_tokens", 8000),
            timeout=self.per_request_timeout,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content

    def _call_deepseek_json(self, config, system_message, user_prompt) -> str:
        import openai
        client = openai.OpenAI(
            api_key=config["api_key"],
            base_url=config.get("base_url", "https://api.deepseek.com/v1"),
            http_client=self._make_httpx_client(),
        )
        # 2026-06-24: `extra_body` — провайдер-специфичные параметры за
        # пределами стандартного OpenAI-контракта (например, у deepseek-v4
        # отключение thinking: extra_body={"thinking": {"type": "disabled"}}).
        extra_body = config.get("extra_body")
        resp = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=config.get("max_tokens", 8000),
            timeout=self.per_request_timeout,
            response_format={"type": "json_object"},
            extra_body=extra_body,
        )
        return resp.choices[0].message.content

    def _call_anthropic_json(self, config, system_message, user_prompt) -> str:
        import anthropic
        client = anthropic.Anthropic(
            api_key=config["api_key"],
            http_client=self._make_httpx_client(),
        )
        # Claude нет жёсткого json_object — полагаемся на чёткие инструкции
        # в system_message + EditSet.from_json режет ```json fences.
        resp = client.messages.create(
            model=config["model"],
            max_tokens=config.get("max_tokens", 8000),
            temperature=0.1,
            system=system_message,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=self.per_request_timeout,
        )
        return resp.content[0].text

    def _call_ollama_json(self, config, system_message, user_prompt) -> str:
        import requests
        url = f"{config.get('base_url', 'http://localhost:11434')}/api/generate"
        prompt = f"{system_message}\n\n{user_prompt}"
        resp = requests.post(url, json={
            "model": config["model"],
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }, timeout=self.per_request_timeout, proxies={"http": "", "https": ""})
        if resp.status_code == 200:
            return resp.json().get("response", "")
        return ""

    def _call_local_json(self, config: Dict[str, Any], system_message: str, user_prompt: str) -> str:
        from fixers.local_llm_provider import LocalLLMProvider
        p = LocalLLMProvider(
            base_url=config.get("base_url", "http://localhost:8080/v1"),
            model=config["model"],
            api_key=config.get("api_key", ""),
            profile=config.get("profile", "patch"),
            timeout=config.get("timeout", self.per_request_timeout),
            context_window=int(config.get("context_window", 248000)),
            max_tokens_override=int(config["max_tokens"]) if config.get("max_tokens") else None,
        )
        return p.complete_json(system_message, user_prompt) or ""

    def _call_local_text(self, config: Dict[str, Any], prompt: str) -> str:
        from fixers.local_llm_provider import LocalLLMProvider
        p = LocalLLMProvider(
            base_url=config.get("base_url", "http://localhost:8080/v1"),
            model=config["model"],
            api_key=config.get("api_key", ""),
            profile=config.get("profile", "patch"),
            timeout=config.get("timeout", self.per_request_timeout),
            context_window=int(config.get("context_window", 248000)),
            max_tokens_override=int(config["max_tokens"]) if config.get("max_tokens") else None,
        )
        return p.complete_text(prompt) or ""

    def generate_full_file(self, error: Dict[str, Any], full_file_content: str, language: str) -> Optional[str]:
        return self._call_llm(full_file_content)

    def generate_fix(
        self,
        error: Dict[str, Any],
        context: str,
        language: str,
        failure_reason: str = "",
        attempt: int = 1,
        compiler_feedback: str = "",
        enriched_prompt: Optional[str] = None,
        project_dir: Optional[Path] = None,
        file_path_obj: Optional[Path] = None,
    ) -> Optional[str]:
        self.last_failure_category = None
        self.last_raw_response = None

        if not self.providers:
            logger.error("Нет доступных LLM-провайдеров")
            self.last_failure_category = "transport_error"
            return None

        provider = self.providers[0]

        if enriched_prompt:
            prompt = enriched_prompt
        else:
            template = self._get_prompt_template("fix_prompt.txt")
            prompt = self._fill_prompt(
                template,
                language=language,
                # 2026-07-08: только forward-slash в промпт — см. комментарий
                # у generate_structured_fix (markdown-escape `\_` ел путь).
                file_path=str(error.get("file", "unknown")).replace("\\", "/"),
                line=error.get("line", 0),
                error_code=error.get("code", ""),
                message=error.get("message", "unknown error"),
                context=context,
                example="",
                fix_version="N/A",
            )

        response, err = self._call_with_retries(provider, prompt)
        self.last_raw_response = response
        if not response:
            self.last_failure_category = (
                self._classify_call_err(err) if response is None else "empty"
            )
            self._note_response_outcome(None, self.last_failure_category)
            if err:
                logger.warning("generate_fix: LLM не ответил (%s)", err)
            return None
        self._note_response_outcome(response, None)  # LLM жив — сброс счётчика
        patch = self._extract_patch(response)
        if patch is None:
            # Ответ непустой, но не похож на diff/код-блок вовсе — отличаем
            # от "LLM не ответил" (empty/timeout/transport_error).
            self.last_failure_category = "bad_format"
        return patch

    def _call_provider_with_prompt(
        self, config: Dict[str, Any], prompt: str
    ) -> Tuple[Optional[str], Optional[str]]:
        provider_name = config.get("provider", "unknown")
        model_name = config.get("model", "unknown")
        try:
            if provider_name == "openai":
                return self._call_openai(config, prompt), None
            elif provider_name == "anthropic":
                return self._call_anthropic(config, prompt), None
            elif provider_name == "deepseek":
                return self._call_deepseek(config, prompt), None
            elif provider_name == "ollama":
                return self._call_ollama(config, prompt), None
            elif provider_name == "local":
                return self._call_local_text(config, prompt), None
            else:
                err = f"Неподдерживаемый провайдер: {provider_name}"
                logger.error("LLM-вызов не выполнен: %s", err)
                return None, err
        except Exception as e:
            # Раньше причину молча проглатывали — наружу было только "LLM
            # вернул пустой ответ". Теперь логируем тип и текст исключения,
            # чтобы можно было отличить 401/timeout/DNS/rate-limit и т.п.
            logger.error(
                "LLM-вызов не выполнен (provider=%s, model=%s): %s: %s",
                provider_name, model_name, type(e).__name__, e,
            )
            return None, f"{type(e).__name__}: {e}"

    def _make_httpx_client(self):
        """Создаёт httpx.Client с trust_env=False, чтобы SDK игнорировал
        системные прокси-переменные (HTTP_PROXY/HTTPS_PROXY/ALL_PROXY и т.п.).
        Это спасает от ситуаций вроде ALL_PROXY=socks4://... , которые httpx
        не поддерживает и которые ломают любой запрос ещё до отправки.
        """
        import httpx
        return httpx.Client(trust_env=False, timeout=self.per_request_timeout)

    def _call_openai(self, config: Dict[str, Any], prompt: str) -> str:
        import openai
        client = openai.OpenAI(
            api_key=config["api_key"],
            http_client=self._make_httpx_client(),
        )
        resp = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=config.get("max_tokens", 8000),
            timeout=self.per_request_timeout,   # ТАЙМАУТ
        )
        return resp.choices[0].message.content

    def _call_anthropic(self, config: Dict[str, Any], prompt: str) -> str:
        import anthropic
        client = anthropic.Anthropic(
            api_key=config["api_key"],
            http_client=self._make_httpx_client(),
        )
        resp = client.messages.create(
            model=config["model"],
            max_tokens=config.get("max_tokens", 8000),
            temperature=0.1,
            system=SYSTEM_MESSAGE,
            messages=[{"role": "user", "content": prompt}],
            timeout=self.per_request_timeout,   # ТАЙМАУТ
        )
        return resp.content[0].text

    def _call_deepseek(self, config: Dict[str, Any], prompt: str) -> str:
        import openai
        client = openai.OpenAI(
            api_key=config["api_key"],
            base_url=config.get("base_url", "https://api.deepseek.com/v1"),
            http_client=self._make_httpx_client(),
        )
        resp = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=config.get("max_tokens", 8000),
            timeout=self.per_request_timeout,   # ТАЙМАУТ
            extra_body=config.get("extra_body"),
        )
        return resp.choices[0].message.content

    def _call_ollama(self, config: Dict[str, Any], prompt: str) -> str:
        import requests
        url = f"{config.get('base_url', 'http://localhost:11434')}/api/generate"
        # proxies={} — отключаем чтение HTTP_PROXY/HTTPS_PROXY/ALL_PROXY из env.
        # Иначе системный socks4-прокси ронял бы и Ollama-запросы.
        resp = requests.post(url, json={
            "model": config["model"],
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1}
        }, timeout=self.per_request_timeout, proxies={"http": "", "https": ""})
        if resp.status_code == 200:
            return resp.json().get("response", "")
        return ""

    def _extract_patch(self, response: str) -> Optional[str]:
        if not response:
            return None
        clean = response.strip()
        if clean.startswith("```diff"):
            clean = clean[7:]
        elif clean.startswith("```"):
            clean = clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        lines = clean.splitlines()
        patch_lines = []
        in_patch = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("---") or stripped.startswith("+++") or stripped.startswith("@@"):
                in_patch = True
                patch_lines.append(line)
                continue
            if in_patch and (line.startswith(('+', '-', ' ')) or stripped.startswith('@@')):
                patch_lines.append(line)
            else:
                in_patch = False
        if not patch_lines:
            filtered = [l for l in lines if l.startswith(('---', '+++', '@@', '+', '-', ' '))]
            if filtered and any(l.strip().startswith('@@') for l in filtered):
                patch_lines = filtered
        if not patch_lines:
            return None
        candidate = "\n".join(patch_lines)
        if not re.search(r'^[-+]', candidate, re.MULTILINE):
            return None
        return candidate


# ---------------------------------------------------------------------------
# Помощники для structured-режима (используются в generate_structured_fix).
# ---------------------------------------------------------------------------

def _trim_base_line(text: str, target_line: int, radius: int = 60) -> int:
    """Возвращает 1-based номер строки, с которого начинается окно вывода
    в _trim_around_line. Нужен LLM-у, чтобы правильно проставлять anchor.line.
    """
    if not text or not target_line or target_line <= 0:
        return 1
    return max(1, target_line - radius)


def _trim_around_line(text: str, target_line: int, radius: int = 60) -> str:
    """Берёт окно ±radius строк вокруг target_line. Если файл маленький
    или target_line неизвестен — возвращает весь текст.
    """
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    if not target_line or target_line <= 0 or len(lines) <= 2 * radius:
        return text
    start = max(0, target_line - 1 - radius)
    end = min(len(lines), target_line - 1 + radius + 1)
    return "".join(lines[start:end])


def _numbered(text: str, base_line: int = 1) -> str:
    """Добавляет к каждой строке номер (`  42 | ...`). Нужно, чтобы LLM
    мог точно ссылаться на конкретные строки в anchor.line.
    """
    if not text:
        return ""
    lines = text.splitlines()
    width = len(str(base_line + len(lines)))
    return "\n".join(
        f"{(base_line + i):>{width}} | {line}" for i, line in enumerate(lines)
    )


def build_llm_client_pool(
    config: Dict[str, Any], language_provider=None,
    primary_client: Optional["LLMClient"] = None,
) -> List["LLMClient"]:
    """2026-06-24: при parallel_workers>1 все потоки раньше делили ОДИН
    LLMClient (один api_key, один circuit breaker) — конкурентные вызовы
    от нескольких потоков под одним ключом упираются в rate-limit
    провайдера и общий consecutive_hard_timeouts (одна "плохая" попытка
    одного потока могла взвести circuit breaker для ВСЕХ остальных).

    Читает WEBBLES_LLM_API_KEY_POOL (через запятую, доп. ключи) из env —
    первый клиент в пуле — `primary_client`, если передан (избегаем
    повторного конструирования + лишнего preflight-пинга), иначе строится
    свежий с основным api_key из config. Остальные — отдельные LLMClient
    с тем же provider/model, но своим api_key и собственным (изолированным)
    circuit breaker/consecutive_hard_timeouts.

    Без WEBBLES_LLM_API_KEY_POOL возвращает список из одного клиента —
    обратная совместимость, поведение не меняется."""
    pool_env = os.environ.get("WEBBLES_LLM_API_KEY_POOL", "")
    extra_keys = [k.strip() for k in pool_env.split(",") if k.strip()]

    primary = primary_client if primary_client is not None else LLMClient(
        config=config, language_provider=language_provider,
    )
    if not extra_keys:
        return [primary]

    base_llm_cfg = config.get("llm", {}) or {}
    base_providers = base_llm_cfg.get("providers") or []
    if not base_providers:
        # 2026-06-24: найдено при живом прогоне — конфиги старого формата
        # (provider/model/api_key прямо под "llm", без вложенного списка
        # "providers"; см. _load_providers "Старый формат" и минимальные
        # тестовые конфиги типа test_pipeline_engine_resume_config.py) не
        # дают шаблон для клонирования с другим api_key. Без этой проверки
        # копии строились бы с providers=[] — LLMClient с нулём провайдеров,
        # бесполезный, но тихо "успешный" (никогда бы не использовался для
        # реальных вызовов, просто занимал бы место в пуле).
        logger.debug(
            "build_llm_client_pool: llm.providers пуст/в старом формате — "
            "пул не строится, используется только primary_client",
        )
        return [primary]

    clients = [primary]
    for key in extra_keys:
        cfg_copy = dict(config)
        llm_copy = dict(base_llm_cfg)
        providers_copy = [dict(p) for p in base_providers]
        providers_copy[0] = dict(providers_copy[0])
        providers_copy[0]["api_key"] = key
        llm_copy["providers"] = providers_copy
        cfg_copy["llm"] = llm_copy
        clients.append(LLMClient(config=cfg_copy, language_provider=language_provider))

    logger.info("LLM client pool: %d клиент(ов) (1 основной + %d из WEBBLES_LLM_API_KEY_POOL)",
                len(clients), len(extra_keys))
    return clients