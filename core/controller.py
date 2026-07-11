"""
Центральный контроллер управления Webbles Fix.
Содержит всю бизнес-логику, работу с конфигурацией, запуск конвейера и бота.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Подхватываем .env, если установлен python-dotenv.
# Если не установлен — переменные окружения всё равно работают, просто
# их надо выставить вручную (через PowerShell или GUI Windows).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from reporters.telegram import TelegramReporter

# P0.6: ВСЕ пути к runtime/конфиг-файлам теперь АБСОЛЮТНЫЕ относительно
# корня webbles_fix. Раньше `Path("webles_config.json")` был относительным
# и подхватывал чужую CWD — конфиг создавался в папке ремонтируемого
# проекта (например, `C:\Users\zov31\test_python\webles_config.json`).
# Пользователь явно требует, чтобы в проекте была ТОЛЬКО `.webbles_backups/`.
_WEBBLES_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = _WEBBLES_ROOT / "webles_config.json"
PROMPT_FILE = _WEBBLES_ROOT / "prompts" / "fix_prompt.txt"
DEFAULT_PROMPT_FILE = _WEBBLES_ROOT / "prompts" / "default.txt"
logger = logging.getLogger(__name__)


def _apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """Перекрывает секреты из конфига значениями из переменных окружения.

    Поддерживаемые переменные:
      WEBBLES_LLM_API_KEY        — ключ для основного LLM-провайдера
      WEBBLES_TELEGRAM_TOKEN     — токен telegram-бота
      WEBBLES_TELEGRAM_CHAT_ID   — id чата для уведомлений

    Это путь миграции с открытых ключей в webles_config.json/webles_secrets.json
    на безопасный .env (см. .env.example). Если ENV-переменная не задана —
    остаётся значение из JSON, чтобы не ломать существующие установки.
    """
    llm = config.setdefault("llm", {})
    api_key_env = os.getenv("WEBBLES_LLM_API_KEY")
    if api_key_env:
        # Новый формат: llm.providers — массив. Перебиваем api_key у первого.
        providers = llm.get("providers")
        if isinstance(providers, list) and providers:
            providers[0]["api_key"] = api_key_env
        # Старый формат: ключ лежит сразу под llm.api_key
        if "api_key" in llm or providers is None:
            llm["api_key"] = api_key_env

    tg = config.setdefault("telegram", {})
    if os.getenv("WEBBLES_TELEGRAM_TOKEN"):
        tg["token"] = os.getenv("WEBBLES_TELEGRAM_TOKEN")
    if os.getenv("WEBBLES_TELEGRAM_CHAT_ID"):
        tg["chat_id"] = os.getenv("WEBBLES_TELEGRAM_CHAT_ID")

    return config


def _warn_if_inline_key(config: Dict[str, Any]) -> None:
    """Если в конфиге сидит реально похожий на боевой API-ключ — напомнить
    пользователю перенести в .env. Помогает не залить ключ в git.
    """
    llm = config.get("llm", {})
    providers = llm.get("providers") or []
    candidates = []
    for p in providers:
        if isinstance(p, dict):
            candidates.append(p.get("api_key"))
    candidates.append(llm.get("api_key"))
    for k in candidates:
        if isinstance(k, str) and len(k) >= 16 and k.startswith(("sk-", "key-", "claude-")):
            logger.warning(
                "Обнаружен API-ключ в webles_config.json. Рекомендуется "
                "перенести его в .env (WEBBLES_LLM_API_KEY=...) и удалить "
                "из коммитимого файла. См. .env.example."
            )
            break

# Прогресс-бар: каждой стадии соответствует процент выполнения
STAGE_PROGRESS = {
    "IDLE": 0,
    "ANALYZING": 10,
    "PRIORITIZING": 20,
    "ROOT_CAUSE": 30,
    "GENERATING_PATCH": 40,
    "APPLYING_PATCH": 50,
    "VALIDATING": 60,
    "DECIDING": 70,
    "NEXT_ERROR": 80,
    "COMPLETED": 100,
    "FAILED": 100,
    "CIRCUIT_OPEN": 100,
}


class Controller:
    """Единый контроллер для CLI и бота."""

    def __init__(self):
        self.bot_thread: Optional[threading.Thread] = None
        self.pipeline_running = False
        self.pipeline_engine = None
        config = self.load_config()
        tg = config.get("telegram", {})
        self.reporter = TelegramReporter(
            bot_token=tg.get("token"),
            chat_id=tg.get("chat_id")
        )
        # Уровень логирования из конфига
        if config.get("verbose", False):
            logging.getLogger().setLevel(logging.DEBUG)
            logger.debug("Verbose mode enabled")

    def config_exists(self) -> bool:
        return CONFIG_FILE.exists()

    def load_config(self) -> Dict[str, Any]:
        if not CONFIG_FILE.exists():
            return _apply_env_overrides(self._default_config())
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            return _apply_env_overrides(self._default_config())
        # Перекрытие секретов из .env / переменных окружения.
        config = _apply_env_overrides(config)
        # Однократное предупреждение если ключи остались в JSON.
        _warn_if_inline_key(config)
        return config

    def save_config(self, config: Dict[str, Any]) -> None:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            tg = config.get("telegram", {})
            self.reporter = TelegramReporter(
                bot_token=tg.get("token"),
                chat_id=tg.get("chat_id")
            )
            if config.get("verbose", False):
                logging.getLogger().setLevel(logging.DEBUG)
            else:
                logging.getLogger().setLevel(logging.INFO)
            logger.info("Конфигурация сохранена")
        except Exception as e:
            logger.error(f"Ошибка сохранения конфигурации: {e}")

    def reset_to_defaults(self) -> None:
        self.save_config(self._default_config())

    def _default_config(self) -> Dict[str, Any]:
        return {
            "llm": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "api_key": None,
                "base_url": "https://api.deepseek.com/v1",
                "max_tokens": 8000,
                "enable_web_search": False,
            },
            "telegram": {
                "token": None,
                "chat_id": None,
                "notifications": False,
            },
            "pipeline": {
                "use_planning": True,
                "max_iterations": 10,
                "planning_depth": 2,
                "beam_width": 3,
                "strictness": 1.0,
                "baseline_file": None,
                "reset_baseline": False,
            },
            "dry_run": False,
            "verbose": False,
            "log_file": "webbles_fix.log",
            "report_dir": ".webbles_reports",
        }

    def set_llm_config(self, provider=None, model=None, api_key=None, base_url=None,
                       max_tokens=None, enable_web_search=None) -> None:
        config = self.load_config()
        llm = config.setdefault("llm", {})
        for key, val in (("provider", provider), ("model", model), ("api_key", api_key),
                         ("base_url", base_url), ("max_tokens", max_tokens), ("enable_web_search", enable_web_search)):
            if val is not None:
                llm[key] = val
        self.save_config(config)

    def set_telegram_config(self, token=None, chat_id=None) -> None:
        config = self.load_config()
        tg = config.setdefault("telegram", {})
        if token is not None:
            tg["token"] = token
        if chat_id is not None:
            tg["chat_id"] = chat_id
        self.save_config(config)

    def set_pipeline_config(self, use_planning=None, max_iterations=None, planning_depth=None,
                            beam_width=None, strictness=None, dry_run=None) -> None:
        config = self.load_config()
        pipeline = config.setdefault("pipeline", {})
        for key, val in (("use_planning", use_planning), ("max_iterations", max_iterations),
                         ("planning_depth", planning_depth), ("beam_width", beam_width),
                         ("strictness", strictness)):
            if val is not None:
                pipeline[key] = val
        if dry_run is not None:
            config["dry_run"] = dry_run
        self.save_config(config)

    def set_baseline_config(self, baseline_file=None, reset_baseline=None) -> None:
        config = self.load_config()
        pipeline = config.setdefault("pipeline", {})
        if baseline_file is not None:
            pipeline["baseline_file"] = baseline_file
        if reset_baseline is not None:
            pipeline["reset_baseline"] = reset_baseline
        self.save_config(config)

    def prompt_exists(self) -> bool:
        return PROMPT_FILE.exists()

    def save_prompt(self, text: str) -> None:
        PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROMPT_FILE.write_text(text, encoding="utf-8")
        logger.info("Промпт сохранён")

    def ensure_prompt_file(self) -> None:
        if self.prompt_exists():
            return
        print("Файл промпта не найден.")
        print("1. Использовать стандартный промпт из prompts/default.txt")
        print("2. Ввести свой текст промпта")
        choice = input("Ваш выбор (1 или 2): ").strip()
        if choice == "1":
            if DEFAULT_PROMPT_FILE.exists():
                self.save_prompt(DEFAULT_PROMPT_FILE.read_text(encoding="utf-8"))
                print("✅ Стандартный промпт скопирован.")
            else:
                print("❌ Стандартный промпт не найден. Создаю пустой.")
                self.save_prompt("")
        else:
            print("Введите текст промпта (многострочный ввод, пустая строка завершает):")
            lines = []
            while True:
                line = input()
                if line == "":
                    break
                lines.append(line)
            self.save_prompt("\n".join(lines))
            print("✅ Промпт сохранён.")

    def run_pipeline(
        self,
        project_path: Path,
        language: str,
        config: Optional[Dict[str, Any]] = None,
        *,
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        if config is None:
            config = self.load_config()
        from core.pipeline_engine import PipelineEngine
        from fixers.llm_client import LLMClient

        # 2026-06-24 (control series на 10 проектах: pluggy/jsonschema/tenacity
        # застряли с HTTP timeout=120s/3 retries вместо настроенных 60s/2 —
        # расследование показало, что LLMClient(config.get("llm", {})) передавал
        # ТОЛЬКО распакованный под-словарь "llm" как ВЕСЬ config. Внутри
        # __init__ код читает self.config.get("llm", {}).get("timeout", 120) —
        # ищет ключ "llm" ВНУТРИ уже распакованного словаря "llm", не находит,
        # и ВСЕГДА возвращает дефолты (120s/3 retries/unresponsive_threshold=5),
        # независимо от настроек webles_config.json. _load_providers() не задет
        # только благодаря отдельному defensive-fallback на self.config.get(
        # "providers", []) — для timeout/max_retries/unresponsive_threshold
        # такого fallback'а нет. ВЕСЬ продакшен-путь (run_agent.py →
        # run_fix_agent → этот метод) игнорировал llm.timeout/llm.max_retries
        # с момента появления этих настроек. Передаём ПОЛНЫЙ config — так же,
        # как PipelineEngine.__init__ делает в собственном fallback-конструкторе
        # (core/pipeline_engine.py: LLMClient(config=self.config, ...)).
        llm_client = LLMClient(config)
        engine = PipelineEngine(
            project_path=project_path,
            language=language,
            llm_client=llm_client,
            config=config,
            resume=resume,
            dry_run=config.get("dry_run", False),
            reporter=self.reporter,
            event_emitter=event_emitter,
        )
        self.pipeline_engine = engine
        self.pipeline_running = True

        if config.get("telegram", {}).get("notifications", False):
            self.reporter.send(f"🚀 Конвейер запущен для {project_path} (язык: {language})")

        # 2026-07-02 (решение Алекса, вариант «в»): dependency recovery
        # переехал из controller в PipelineEngine._recover_dependencies_sandbox
        # — раньше он писал requirements.txt/Cargo.toml ПРЯМО в проект
        # пользователя ДО создания sandbox, мимо accept-цикла и вопреки
        # правилу «в проекте пользователя только .webbles_backups/». Теперь
        # правка манифеста делается в sandbox и доставляется обычным
        # accept-потоком (регистрируется в accepted_patches → аудит →
        # копирование с бэкапом).
        self._extract_secrets(project_path, config, event_emitter=event_emitter)

        try:
            return engine.run()
        finally:
            self.pipeline_running = False
            self.pipeline_engine = None

    # 2026-07-02: _recover_dependencies УДАЛЁН из controller — писал манифесты
    # (requirements.txt/Cargo.toml/...) прямо в проект пользователя ДО
    # sandbox, мимо accept-цикла. Логика живёт в
    # PipelineEngine._recover_dependencies_sandbox (работает в sandbox,
    # доставка — обычным accept-потоком). Оставлять здесь мёртвую
    # project_path-версию нельзя — тот же класс риска, что L6 в
    # PROJECT_AUDIT_REPORT (мёртвый код, мутирующий оригинал).

    def _extract_secrets(
        self,
        project_path: Path,
        config: Dict[str, Any],
        event_emitter=None,
    ) -> None:
        """Пред-пайплайн хук: детерминированное извлечение захардкоженных секретов.

        Запускается под флагом `pipeline.secrets_extract` (default False).
        Значения секретов НИКОГДА не попадают в логи или event-payload.
        Никогда не роняет пайплайн.
        """
        try:
            pipeline_cfg = config.get("pipeline", {})
            if not pipeline_cfg.get("secrets_extract", False):
                return
            from analysis.secrets_extractor import SecretsExtractor
            dry_run = bool(pipeline_cfg.get("secrets_extract_dry_run", False))
            result = SecretsExtractor().scan_and_apply(project_path, dry_run=dry_run)
            if result.count == 0:
                return
            names_str = ", ".join(result.names[:10])  # имена — не значения
            suffix = " (dry run)" if dry_run else ""
            msg = (
                f"Secrets extracted: {result.count} ({names_str}) "
                f"-> secrets.txt{suffix}"
            )
            logger.info(msg)
            try:
                self.reporter.send(f"🔐 {msg}")
            except Exception:
                pass
            if callable(event_emitter):
                event_emitter("secrets_extracted", {
                    "count": result.count,
                    "names": result.names,   # имена только — значений нет
                    "dry_run": dry_run,
                })
        except Exception as e:
            logger.warning("Secrets extraction пропущен из-за ошибки: %s", e)

    def start_bot(self) -> bool:
        if self.bot_thread and self.bot_thread.is_alive():
            return False
        try:
            config = self.load_config()
            token = config.get("telegram", {}).get("token")
            if not token:
                logger.error("Токен бота не найден в конфигурации")
                return False
            from reporters.telegram_bot import run_bot
            self.bot_thread = threading.Thread(target=run_bot, args=(token,), daemon=True)
            self.bot_thread.start()
            return True
        except Exception as e:
            logger.error(f"Не удалось запустить бота: {e}")
            return False

    def is_bot_running(self) -> bool:
        return self.bot_thread is not None and self.bot_thread.is_alive()

    def get_pipeline_state(self) -> Optional[str]:
        if self.pipeline_engine and self.pipeline_running:
            return self.pipeline_engine.context.current_state.name
        return None

    def get_pipeline_progress(self) -> str:
        if not self.pipeline_engine or not self.pipeline_running:
            return ""
        state = self.pipeline_engine.context.current_state.name
        pct = STAGE_PROGRESS.get(state, 0)
        bar_len = 10
        filled = int(bar_len * pct / 100)
        bar = "■" * filled + "□" * (bar_len - filled)
        return f"[{bar}] {pct}%"

    def detect_language(self, project_path: Path) -> Optional[str]:
        indicators = {
            "rust": ["Cargo.toml", "Cargo.lock"],
            "python": ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile"],
            "javascript": ["package.json", "package-lock.json", "yarn.lock"],
            "typescript": ["tsconfig.json", "package.json"],
        }
        scores = {lang: 0 for lang in indicators}
        for lang, files in indicators.items():
            for file in files:
                if (project_path / file).exists():
                    scores[lang] += 1
        for ext, lang in [(".py", "python"), (".rs", "rust"), (".js", "javascript"), (".ts", "typescript")]:
            scores[lang] += len(list(project_path.rglob(f"*{ext}")))
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else None
