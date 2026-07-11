"""
Stage J — External example search: абстракция провайдера и оркестратор.

`Example`        — одна находка (источник, заголовок, сниппет, url, score).
`ExampleSearchProvider` — база для конкретных источников (rustc_explain,
                   github, stackoverflow). Контракт: `available()` + `search()`.
`ExampleSearchService`  — оркестратор: обходит провайдеров «дёшево → дорого»,
                   агрегирует находки, кэширует по error_code между сессиями.

Сетевые провайдеры (github / stackoverflow) — opt-in. По умолчанию (через
`from_config`) включён только `rustc_explain` — он бесплатный, локальный и
не требует токенов. В smoke-тестах сеть НЕ дёргается: проверяются мок-провайдеры
и кэш.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =====================================================================
# Example — единица результата поиска
# =====================================================================
@dataclass
class Example:
    """Один внешний прецедент.

    Поля:
      source  — имя провайдера (`rustc_explain`, `github`, `stackoverflow`)
      title   — короткий заголовок (описание / тема вопроса)
      snippet — релевантный кусок кода или объяснения
      url     — ссылка на источник (если есть)
      score   — относительная полезность [0..1] для сортировки
    """

    source: str
    title: str
    snippet: str
    url: str = ""
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Example":
        return cls(
            source=str(data.get("source", "")),
            title=str(data.get("title", "")),
            snippet=str(data.get("snippet", "")),
            url=str(data.get("url", "")),
            score=float(data.get("score", 0.0) or 0.0),
        )


# =====================================================================
# ExampleSearchProvider — база
# =====================================================================
class ExampleSearchProvider:
    """База для источников внешних примеров.

    Наследники переопределяют `name`, `available()` и `search()`.
    Контракт `search` обязан быть «тихим»: при любой проблеме (нет
    инструмента, сеть упала, таймаут) — вернуть `[]`, не бросать.
    """

    name: str = "base"

    def available(self) -> bool:
        """Можно ли вообще пользоваться этим провайдером прямо сейчас.

        Например: установлен ли `rustc`, задан ли `GITHUB_TOKEN`.
        Дешёвая, синхронная, без сети проверка.
        """
        return False

    def search(self, error: Dict[str, Any], language: str,
               max_results: int = 3) -> List[Example]:
        """Вернуть до `max_results` примеров для данной ошибки.

        Базовая реализация ничего не находит.
        """
        return []


# =====================================================================
# ExampleSearchService — оркестратор
# =====================================================================
class ExampleSearchService:
    """Каскад провайдеров + кэш по error_code.

    `search(error, language)`:
      1. если выключен → [];
      2. если есть код ошибки и в кэше свежая запись → отдать её (без сети);
      3. иначе обойти доступных провайдеров, агрегировать находки,
         положить в кэш, вернуть.

    Любой провайдер изолирован try/except — падение одного не роняет
    остальных и не роняет вызывающий structured-fix.
    """

    DEFAULT_MAX_PER_PROVIDER = 3
    DEFAULT_MAX_TOTAL = 5

    def __init__(self, providers: Optional[List[ExampleSearchProvider]] = None,
                 cache=None, enabled: bool = True,
                 max_per_provider: int = DEFAULT_MAX_PER_PROVIDER,
                 max_total: int = DEFAULT_MAX_TOTAL):
        self.providers: List[ExampleSearchProvider] = list(providers or [])
        self.cache = cache
        self.enabled = bool(enabled)
        self.max_per_provider = int(max_per_provider)
        self.max_total = int(max_total)

    # -----------------------------------------------------------------
    def search(self, error: Dict[str, Any], language: str) -> List[Example]:
        if not self.enabled:
            return []

        code = (error.get("code") or "").strip()

        # 1) кэш — если есть код и свежая запись
        if code and self.cache is not None:
            try:
                cached = self.cache.get(code)
            except Exception as e:  # pragma: no cover - защита от битого кэша
                logger.debug("external-examples: cache.get упал: %s", e)
                cached = None
            if cached is not None:
                logger.info("external examples: %d (cache, code=%s)",
                            len(cached), code or "<none>")
                return cached[:self.max_total]

        # 2) обход провайдеров
        results: List[Example] = []
        per_source: Dict[str, int] = {}
        for provider in self.providers:
            try:
                if not provider.available():
                    continue
                found = provider.search(error, language,
                                        max_results=self.max_per_provider) or []
            except Exception as e:
                logger.debug("external-examples: провайдер %s упал: %s",
                             getattr(provider, "name", "?"), e)
                continue
            for ex in found:
                if not isinstance(ex, Example):
                    continue
                results.append(ex)
                per_source[ex.source] = per_source.get(ex.source, 0) + 1

        # сортировка по score (выше — раньше), стабильно
        results.sort(key=lambda e: e.score, reverse=True)
        results = results[:self.max_total]

        # 3) кэш (даже пустой результат кэшируем — не лезть в сеть повторно)
        if code and self.cache is not None:
            try:
                self.cache.put(code, results)
            except Exception as e:  # pragma: no cover
                logger.debug("external-examples: cache.put упал: %s", e)

        if results:
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(per_source.items()))
            logger.info("external examples: %d (%s)", len(results), breakdown)
        return results

    # -----------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Dict[str, Any],
                    working_path) -> "ExampleSearchService":
        """Построить сервис из `config["case_file"]["external_search"]`.

        Ключи (все опциональны):
          enabled        : bool   — мастер-выключатель (default True)
          providers      : list   — какие источники включить
                                     (default ["rustc_explain"])
          cache_ttl_days : int    — TTL кэша (default 7)

        По умолчанию активен только `rustc_explain` — бесплатно и без
        токенов. `github` / `stackoverflow` — строго opt-in.
        """
        cfg = {}
        try:
            cfg = ((config or {}).get("case_file") or {}).get("external_search") or {}
        except Exception:
            cfg = {}

        enabled = bool(cfg.get("enabled", True))
        provider_names = cfg.get("providers") or ["rustc_explain", "python_explain"]
        ttl_days = int(cfg.get("cache_ttl_days", 7) or 7)

        # Кэш переживает сессии (как memory из A.1).
        cache = None
        try:
            from analysis.external_examples.cache import ExampleCache
            root = Path(working_path) / ".webbles_fix" / "example_cache"
            cache = ExampleCache(root, ttl_days=ttl_days)
        except Exception as e:  # pragma: no cover
            logger.debug("external-examples: не удалось создать кэш: %s", e)

        providers: List[ExampleSearchProvider] = []
        for name in provider_names:
            provider = _build_provider(str(name))
            if provider is not None:
                providers.append(provider)

        return cls(providers=providers, cache=cache, enabled=enabled)


def _build_provider(name: str) -> Optional[ExampleSearchProvider]:
    """Фабрика провайдеров по имени из конфига. Неизвестное имя или
    проблема импорта → None (тихо пропускаем)."""
    key = name.strip().lower()
    try:
        if key in ("rustc_explain", "rustc", "rustc-explain"):
            from analysis.external_examples.rustc_explain import RustcExplainProvider
            return RustcExplainProvider()
        if key in ("python_explain", "py_explain", "ruff_explain", "mypy_explain"):
            from analysis.external_examples.python_explain import PythonExplainProvider
            return PythonExplainProvider()
        if key in ("github", "github_search", "gh"):
            from analysis.external_examples.github_search import GitHubCodeSearchProvider
            return GitHubCodeSearchProvider()
        if key in ("stackoverflow", "so", "stack_overflow"):
            from analysis.external_examples.stackoverflow import StackOverflowProvider
            return StackOverflowProvider()
    except Exception as e:  # pragma: no cover
        logger.debug("external-examples: провайдер %r не загрузился: %s", name, e)
        return None
    logger.debug("external-examples: неизвестный провайдер %r — пропуск", name)
    return None
