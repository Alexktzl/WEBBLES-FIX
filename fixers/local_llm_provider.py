"""
LocalLLMProvider — единый провайдер для локальных LLM-серверов
(llama.cpp /v1, LM Studio, Ollama OpenAI-compat, vLLM, koboldcpp, etc.)

Профили:
  patch     — JSON mode, temperature 0.1  (генерация EditSet / unified diff)
  review    — JSON mode, temperature 0.2  (LLM-ревью патча)
  chat      — text mode, temperature 0.7  (чат с пользователем)
  architect — text mode, temperature 0.4  (высокоуровневые вопросы)

Для patch/review добавляется response_format={"type":"json_object"}.
Для chat/architect — обычный text completion без JSON-принуждения.

Fallback: если сервер вернул JSON-режим с лишним текстом вокруг объекта,
_extract_first_json() находит и возвращает первый валидный JSON.

Контекстное окно:
  Дефолт LOCAL_LLM_CONTEXT_WINDOW=248000 токенов (Gemma 4 26B-A4B через
  llama.cpp, RTX 3060 Ti + 32 GB RAM, ~22 t/s). Провайдер хранит context_window
  и через max_input_tokens / max_input_chars раскрывает budget для входных промптов.
  LLMClient использует это при расчёте radius для _trim_around_line.

Используется двумя способами:
  1. В pipeline (LLMClient): provider="local" в конфиге провайдеров.
  2. В чате (ChatSession): make_local_provider(profile="chat").as_chat_callable()
     или напрямую через обновлённый make_default_chat() при WEBBLES_LOCAL_LLM_URL.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Контекстное окно и бюджет токенов
# ---------------------------------------------------------------------------

# Проверенное значение: Gemma 4 26B-A4B-it через llama.cpp ("-cmoe -ngl 999"),
# RTX 3060 Ti 8 GB + 32 GB RAM, скорость ~22 t/s, window стабильно до 248 000.
LOCAL_LLM_CONTEXT_WINDOW: int = 248_000

# GBNF grammar for guaranteed-valid JSON output (grammar sampling).
# Embedded here so it works without depending on the llama.cpp grammars/ folder.
_JSON_GRAMMAR = r"""root   ::= object
value  ::= object | array | string | number | ("true" | "false" | "null") ws

object ::=
  "{" ws (
            string ":" ws value
    ("," ws string ":" ws value)*
  )? "}" ws

array  ::=
  "[" ws (
            value
    ("," ws value)*
  )? "]" ws

string ::=
  "\"" (
    [^"\\\x7F\x00-\x1F] |
    "\\" (["\\bfnrt] | "u" [0-9a-fA-F]{4})
  )* "\"" ws

number ::= ("-"? ([0-9] | [1-9] [0-9]{0,15})) ("." [0-9]+)? ([eE] [-+]? [0-9] [1-9]{0,15})? ws

ws ::= | " " | "\n" [ \t]{0,20}
"""

# Консервативная оценка: 1 токен ≈ 3.5 символа (код + текст на латинице/кириллице)
_CHARS_PER_TOKEN: float = 3.5

# Overhead системного сообщения (шаблон + схема JSON): ~512 токенов для patch/review,
# ~256 для chat/architect (без JSON-схемы).
_PROFILE_SYS_OVERHEAD: Dict[str, int] = {
    "patch":     512,
    "review":    512,
    "chat":      256,
    "architect": 512,
}

# --- Thinking/reasoning control ----------------------------------------
#
# Для patch/review reasoning бесполезен и удваивает latency. Три слоя защиты:
#
#   1. chat_template_kwargs {"enable_thinking": False} — Gemma 4 / llama.cpp b9614+.
#      Шаблон вставляет <|channel>thought\n<channel|> сразу после заголовка модели,
#      закрывая thinking-канал до первого токена рассуждений. Latency: 1–3s vs 87s.
#      Тест b9614: r_len=0, elapsed=1.3s. Это основной механизм.
#
#   2. reasoning_format="none" — скрывает reasoning_content из API-ответа даже если
#      CTK не сработал (другой сервер/билд). Сама по себе не ускоряет (4.9s), но
#      гарантирует отсутствие reasoning_content в теле ответа.
#
#   3. Системный префикс — инструктирует модель текстом (belt-and-suspenders).
#
# Проверено на b9614: --reasoning off / --reasoning-budget 0 в payload не работают
# (это параметры запуска сервера, не per-request поля; игнорируются как unknown).
# reasoning_format="none" в payload — работает как фильтр ответа, но не отключает
# генерацию reasoning токенов. Два endpoint'а НЕ нужны: CTK решает задачу per-request.
#
# chat/architect: thinking включён — улучшает качество рассуждений.

_NO_THINKING_SYSTEM_PREFIX: str = (
    "Do NOT use extended thinking or chain-of-thought reasoning. "
    "Respond immediately and directly without any preamble.\n"
)

# chat_template_kwargs для llama.cpp — переменные в Jinja2-шаблон чата.
_CTK_THINKING_OFF: Dict[str, Any] = {"enable_thinking": False}
_CTK_THINKING_ON: Dict[str, Any] = {"enable_thinking": True}

# reasoning_format per-request поведение (проверено b9614 + Gemma 4):
#   "none"    → channel-теги НЕ извлекаются, утекают в content как сырой текст — ХУЖЕ
#   "deepseek"→ channel-теги → reasoning_content (мы игнорируем), content чистый
#   default   → server default (reasoning_in_content=false), content чистый — ЛУЧШИЙ вариант
# Поэтому reasoning_format в payload НЕ передаём — пусть работает серверный дефолт.


# ---------------------------------------------------------------------------
# Профили
# ---------------------------------------------------------------------------

PROFILE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "patch": {
        "json_mode": True,
        "temperature": 0.1,
        "max_tokens": 8000,
        "disable_thinking": True,   # reasoning не нужен, только замедляет
        "system": (
            "You are a precise code-fixing assistant. "
            "Return ONLY a JSON object describing the minimal set of edits."
        ),
    },
    "review": {
        "json_mode": True,
        "temperature": 0.2,
        "max_tokens": 8000,
        "disable_thinking": True,   # то же: нужен только структурированный JSON
        "system": (
            "You are a strict code reviewer. "
            "Return ONLY a JSON object with verdict, reasons, and confidence_adjustment."
        ),
    },
    "chat": {
        "json_mode": False,
        "temperature": 0.7,
        "max_tokens": 1500,
        "disable_thinking": True,   # reasoning adds 60-90s latency; not needed for chat
        "system": "You are a helpful coding assistant.",
    },
    "architect": {
        "json_mode": False,
        "temperature": 0.4,
        "max_tokens": 4000,
        "disable_thinking": False,  # reasoning полезен для архитектурных вопросов
        "system": "You are a software architect and senior developer.",
    },
}

VALID_PROFILES: frozenset = frozenset(PROFILE_DEFAULTS)


# ---------------------------------------------------------------------------
# Fallback JSON extraction
# ---------------------------------------------------------------------------

def _extract_content(message: Dict[str, Any]) -> str:
    """Возвращает только choices[0].message.content.

    Явно игнорирует поля reasoning_content (DeepSeek-R1, некоторые Gemma-конфиги
    llama.cpp) и __verbose, которые некоторые серверы добавляют рядом с content.
    Всегда возвращает str — никогда не возвращает None или не-str значения.
    """
    if "reasoning_content" in message:
        logger.debug(
            "LocalLLMProvider: поле reasoning_content проигнорировано (len=%d)",
            len(str(message["reasoning_content"] or "")),
        )
    if "__verbose" in message:
        logger.debug("LocalLLMProvider: поле __verbose проигнорировано")
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "" if content is None else str(content)


def _extract_first_json(text: str) -> Optional[str]:
    """Извлекает первый валидный JSON-объект или массив из произвольного текста.

    Порядок попыток:
      1. Весь текст целиком (быстро).
      2. Блок ```json ... ``` или ``` ... ```.
      3. Сканирование посимвольно по открывающим { / [.
    """
    if not text:
        return None

    stripped = text.strip()
    try:
        json.loads(stripped)
        return stripped
    except (json.JSONDecodeError, ValueError):
        pass

    # Fenced code blocks
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```', text)
    if m:
        candidate = m.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, ValueError):
            pass

    # Посимвольный поиск первого полного объекта или массива
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        depth = 0
        obj_start = -1
        in_string = False
        escape_next = False
        for i, ch in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_ch:
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == close_ch:
                if depth > 0:
                    depth -= 1
                    if depth == 0 and obj_start >= 0:
                        candidate = text[obj_start : i + 1]
                        try:
                            json.loads(candidate)
                            return candidate
                        except (json.JSONDecodeError, ValueError):
                            obj_start = -1  # start over for next occurrence

    return None


# ---------------------------------------------------------------------------
# Провайдер
# ---------------------------------------------------------------------------

class LocalLLMProvider:
    """OpenAI-compatible провайдер для локально запущенных моделей.

    Один экземпляр = один профиль. Создавай отдельные экземпляры для
    patch/review и chat если нужно разное поведение (напр. разная температура).
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        profile: str = "patch",
        api_key: str = "",
        timeout: float = 120.0,
        context_window: int = LOCAL_LLM_CONTEXT_WINDOW,
        json_mode_override: Optional[bool] = None,
        temperature_override: Optional[float] = None,
        max_tokens_override: Optional[int] = None,
        disable_thinking_override: Optional[bool] = None,
    ) -> None:
        if profile not in VALID_PROFILES:
            raise ValueError(
                f"Неизвестный профиль: {profile!r}. "
                f"Допустимые: {sorted(VALID_PROFILES)}"
            )
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or "local"
        self.profile = profile
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        self.context_window: int = int(context_window)

        defaults = PROFILE_DEFAULTS[profile]
        self._json_mode: bool = (
            json_mode_override if json_mode_override is not None
            else defaults["json_mode"]
        )
        self._temperature: float = (
            temperature_override if temperature_override is not None
            else defaults["temperature"]
        )
        self._max_tokens: int = (
            max_tokens_override if max_tokens_override is not None
            else defaults["max_tokens"]
        )
        self._disable_thinking: bool = (
            disable_thinking_override if disable_thinking_override is not None
            else defaults["disable_thinking"]
        )
        self._default_system: str = defaults["system"]

    @property
    def max_input_tokens(self) -> int:
        """Токены доступные для входного промпта (context_window − output − overhead)."""
        sys_overhead = _PROFILE_SYS_OVERHEAD.get(self.profile, 512)
        return max(1, self.context_window - self._max_tokens - sys_overhead)

    @property
    def max_input_chars(self) -> int:
        """Приблизительный лимит символов входного промпта (токены × 3.5)."""
        return int(self.max_input_tokens * _CHARS_PER_TOKEN)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def complete_json(self, system_message: str, user_prompt: str) -> Optional[str]:
        """Запрашивает JSON-ответ; применяет response_format + fallback.

        Возвращает строку с валидным JSON или None если сервер недоступен
        или JSON не удалось извлечь.
        """
        raw = self._post_chat(
            system_message=system_message,
            user_prompt=user_prompt,
            force_json=True,
        )
        if raw is None:
            return None
        extracted = _extract_first_json(raw)
        if extracted is None:
            logger.warning(
                "LocalLLMProvider[%s].complete_json: не удалось извлечь JSON "
                "из ответа len=%d: %.120s",
                self.profile, len(raw), raw,
            )
        return extracted

    def complete_text(
        self,
        prompt: str,
        *,
        system_message: Optional[str] = None,
    ) -> Optional[str]:
        """Запрашивает текстовый ответ (без JSON-принуждения)."""
        return self._post_chat(
            system_message=system_message or self._default_system,
            user_prompt=prompt,
            force_json=False,
        )

    def health_check(self, timeout: float = 5.0) -> bool:
        """Pre-flight проверка доступности сервера (empty_response диагностика,
        2026-06-22, категория "transport_error"): лёгкий GET на /models с
        коротким таймаутом, БЕЗ долгого _do_request retry-цикла (10/30/60с).
        Вызывающий код (LLMClient.__init__) использует это, чтобы не тратить
        целую серию retry-попыток на мёртвый локальный сервер — узнать об
        этом за 5с, а не за десятки минут project_timeout."""
        url = self.base_url + "/models"
        req = urllib.request.Request(url, method="GET")
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)

        # Изолируем системные прокси, как _do_request.
        proxy_keys = (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )
        old_env: Dict[str, str] = {}
        for k in proxy_keys:
            if k in os.environ:
                old_env[k] = os.environ.pop(k)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            logger.warning(
                "LocalLLMProvider[%s].health_check: сервер %s недоступен: %s: %s",
                self.profile, self.base_url, type(exc).__name__, exc,
            )
            return False
        finally:
            os.environ.update(old_env)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Полный OpenAI chat/completions вызов, совместимый с HTTPChatLLM.chat().

        Используется ChatSession напрямую. Для профилей с json_mode=True
        автоматически добавляется response_format.
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [],          # заполним после thinking control
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # Применяем thinking control: модифицирует payload["thinking"]
        # и системное сообщение (первый элемент messages если role=system).
        out_messages: List[Dict[str, Any]] = list(messages)
        if messages and messages[0].get("role") == "system":
            new_sys = self._apply_thinking_control(payload, messages[0]["content"])
            out_messages = [{"role": "system", "content": new_sys}] + list(messages[1:])
        else:
            # Нет системного сообщения — применяем thinking control без системного текста
            if self._disable_thinking:
                payload["chat_template_kwargs"] = _CTK_THINKING_OFF
            else:
                payload["chat_template_kwargs"] = _CTK_THINKING_ON
        payload["messages"] = out_messages

        raw = self._do_request("/chat/completions", payload)
        if raw is None:
            from agent.chat_llm import ChatLLMError
            raise ChatLLMError("LocalLLMProvider.chat: пустой ответ от сервера")
        try:
            data = json.loads(raw)
            msg = data["choices"][0]["message"]
        except Exception as exc:
            from agent.chat_llm import ChatLLMError
            raise ChatLLMError(
                f"LocalLLMProvider.chat: неожиданная схема ответа: {exc}"
            ) from exc
        # Возвращаем нормализованное сообщение: content всегда str,
        # reasoning_content и __verbose явно отфильтрованы.
        msg["content"] = _extract_content(msg)
        return msg

    def as_chat_callable(self) -> Callable:
        """Возвращает callable совместимый с ChatCallable из agent/chat_llm.py."""

        def _call(
            messages: List[Dict[str, Any]],
            tools: Optional[List[Dict[str, Any]]] = None,
            **kw: Any,
        ) -> Dict[str, Any]:
            return self.chat(messages, tools=tools, **kw)

        return _call

    def as_pipeline_provider_config(self) -> Dict[str, Any]:
        """Возвращает dict провайдера для LLMClient._load_providers().

        Тип "local" обрабатывается специально в LLMClient — роутится через
        _call_local_json / _call_local_text вместо openai SDK.
        """
        return {
            "name": f"local:{self.profile}",
            "provider": "local",
            "model": self.model,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "profile": self.profile,
            "max_tokens": self._max_tokens,
            "timeout": self.timeout,
            "context_window": self.context_window,
        }

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _apply_thinking_control(
        self,
        payload: Dict[str, Any],
        system_message: str,
    ) -> str:
        """Управляет thinking-каналом Gemma 4 / llama.cpp через chat_template_kwargs.

        Возвращает (возможно изменённое) системное сообщение.
        """
        if self._disable_thinking:
            # Слой 1: chat_template_kwargs — Gemma 4 llama.cpp шаблон (b9614+).
            # enable_thinking=False вставляет <|channel>thought\n<channel|> сразу
            # после заголовка, закрывает thinking-канал до первого токена рассуждений.
            # ВАЖНО: reasoning_format НЕ передаём — "none" вызывает утечку channel-тегов
            # в content (проверено b9614). Серверный дефолт корректно обрабатывает теги.
            payload["chat_template_kwargs"] = _CTK_THINKING_OFF

            # Слой 2: системный префикс — текстовая инструкция (belt-and-suspenders
            # для серверов без поддержки chat_template_kwargs).
            if not system_message.startswith(_NO_THINKING_SYSTEM_PREFIX):
                system_message = _NO_THINKING_SYSTEM_PREFIX + system_message
        else:
            # chat/architect: явно включаем thinking (шаблон вставит <|think|>).
            payload["chat_template_kwargs"] = _CTK_THINKING_ON

        return system_message

    def _enforce_input_budget(self, system_message: str, user_prompt: str) -> Tuple[str, str]:
        """Guard против превышения context_window (empty_response диагностика,
        2026-06-22, категория "empty"): оценивает суммарную длину system+user
        в символах против max_input_chars и обрезает user_prompt с конца,
        если бюджет превышен. Без этого guard'а локальный сервер при слишком
        длинном промпте может молча обрезать контекст изнутри (теряя ВАЖНОЕ
        в начале — например сам текст ошибки) или вернуть пустой ответ без
        объяснения — мы предпочитаем предсказуемо потерять ХВОСТ промпта
        (обычно менее критичный контекст) с явным предупреждением в лог."""
        budget = self.max_input_chars
        total = len(system_message) + len(user_prompt)
        if total <= budget:
            return system_message, user_prompt
        overflow = total - budget
        keep = max(0, len(user_prompt) - overflow)
        truncated = user_prompt[:keep]
        logger.warning(
            "LocalLLMProvider[%s]: входной промпт обрезан на %d символов "
            "(было %d, бюджет %d при context_window=%d) — иначе сервер мог бы "
            "молча обрезать контекст или вернуть пустой ответ",
            self.profile, overflow, total, budget, self.context_window,
        )
        return system_message, truncated

    def _post_chat(
        self,
        *,
        system_message: str,
        user_prompt: str,
        force_json: bool,
    ) -> Optional[str]:
        system_message, user_prompt = self._enforce_input_budget(system_message, user_prompt)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [],          # заполним после apply_thinking_control
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if force_json and self._json_mode:
            # Grammar sampling: guarantees valid JSON without retries.
            # Falls back to response_format if grammar is somehow rejected.
            payload["grammar"] = _JSON_GRAMMAR
            payload["response_format"] = {"type": "json_object"}

        system_message = self._apply_thinking_control(payload, system_message)
        payload["messages"] = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_prompt},
        ]

        raw = self._do_request("/chat/completions", payload)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            msg = data["choices"][0]["message"]
        except Exception as exc:
            logger.warning(
                "LocalLLMProvider._post_chat: ошибка разбора ответа: %s: %.200s",
                exc, raw,
            )
            return None
        content = _extract_content(msg)
        if not content:
            finish = ""
            try:
                finish = data["choices"][0].get("finish_reason", "")
            except Exception:
                pass
            if finish == "length":
                logger.warning(
                    "LocalLLMProvider._post_chat[%s]: content пустой, "
                    "finish_reason=length — max_tokens (%d) слишком мал "
                    "для reasoning-модели; увеличь max_tokens в конфиге",
                    self.profile, self._max_tokens,
                )
            else:
                logger.debug(
                    "LocalLLMProvider._post_chat[%s]: content пустой "
                    "(finish_reason=%r)", self.profile, finish,
                )
        return content or None

    # Delays between retry attempts on timeout (seconds).
    _TIMEOUT_RETRY_DELAYS = (10, 30, 60)

    def _do_request(self, path: str, payload: Dict[str, Any]) -> Optional[str]:
        """Отправляет POST на base_url+path с JSON-телом.

        Изолирует системные прокси (как HTTPChatLLM и LLMClient).
        При TimeoutError — до 3 повторов с паузами 10/30/60 с.
        Возвращает тело ответа или None при любой ошибке.
        """
        url = self.base_url + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)

        # Изолируем системные прокси (SOCKS4 / SOCKS5 несовместимы с urllib).
        proxy_keys = (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )
        old_env: Dict[str, str] = {}
        for k in proxy_keys:
            if k in os.environ:
                old_env[k] = os.environ.pop(k)
        try:
            for attempt in range(len(self._TIMEOUT_RETRY_DELAYS) + 1):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return resp.read().decode("utf-8", errors="replace")
                except urllib.error.HTTPError as exc:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:400]
                    except Exception:
                        pass
                    logger.error(
                        "LocalLLMProvider HTTP %d%s",
                        exc.code, f": {detail or exc.reason}",
                    )
                    return None
                except (TimeoutError, socket.timeout) as exc:
                    if attempt < len(self._TIMEOUT_RETRY_DELAYS):
                        delay = self._TIMEOUT_RETRY_DELAYS[attempt]
                        logger.warning(
                            "LocalLLMProvider timeout (попытка %d/%d) — повтор через %ds",
                            attempt + 1, len(self._TIMEOUT_RETRY_DELAYS) + 1, delay,
                        )
                        time.sleep(delay)
                        continue
                    logger.error(
                        "LocalLLMProvider._do_request TimeoutError: timed out (все %d попытки исчерпаны)",
                        len(self._TIMEOUT_RETRY_DELAYS) + 1,
                    )
                    return None
                except urllib.error.URLError as exc:
                    if isinstance(exc.reason, (TimeoutError, socket.timeout)) and attempt < len(self._TIMEOUT_RETRY_DELAYS):
                        delay = self._TIMEOUT_RETRY_DELAYS[attempt]
                        logger.warning(
                            "LocalLLMProvider URLError/timeout (попытка %d/%d) — повтор через %ds",
                            attempt + 1, len(self._TIMEOUT_RETRY_DELAYS) + 1, delay,
                        )
                        time.sleep(delay)
                        continue
                    logger.error("LocalLLMProvider сеть: %s", exc.reason)
                    return None
                except Exception as exc:
                    logger.error(
                        "LocalLLMProvider._do_request %s: %s", type(exc).__name__, exc
                    )
                    return None
            return None
        finally:
            os.environ.update(old_env)


# ---------------------------------------------------------------------------
# Фабрика из ENV
# ---------------------------------------------------------------------------

def make_local_provider(
    *,
    profile: str = "patch",
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
    context_window: Optional[int] = None,
    json_mode_override: Optional[bool] = None,
    temperature_override: Optional[float] = None,
    max_tokens_override: Optional[int] = None,
) -> LocalLLMProvider:
    """Создаёт LocalLLMProvider из ENV + явных параметров.

    ENV (читаются только если параметр не задан явно):
      WEBBLES_LOCAL_LLM_URL     — base_url (например http://127.0.0.1:8080/v1)
      WEBBLES_LOCAL_LLM_MODEL   — название модели
      WEBBLES_LOCAL_LLM_KEY     — API key (опционально; нужен LM Studio и нек. др.)
      WEBBLES_LOCAL_LLM_CTX     — context_window (токенов; дефолт 248000)
    """
    env_ctx = os.environ.get("WEBBLES_LOCAL_LLM_CTX")
    resolved_ctx = (
        context_window
        if context_window is not None
        else (int(env_ctx) if env_ctx else LOCAL_LLM_CONTEXT_WINDOW)
    )
    return LocalLLMProvider(
        base_url=(
            base_url
            or os.environ.get("WEBBLES_LOCAL_LLM_URL", "http://localhost:8080/v1")
        ),
        model=model or os.environ.get("WEBBLES_LOCAL_LLM_MODEL", "local"),
        api_key=api_key or os.environ.get("WEBBLES_LOCAL_LLM_KEY", ""),
        profile=profile,
        timeout=float(timeout) if timeout is not None else 120.0,
        context_window=resolved_ctx,
        json_mode_override=json_mode_override,
        temperature_override=temperature_override,
        max_tokens_override=max_tokens_override,
    )
