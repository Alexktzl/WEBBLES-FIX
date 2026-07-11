# keleshev/schema  [PARTIAL — LLM TIMEOUT]

- Дата: 2026-06-14
- Ошибок: неизвестно (процесс убит через 15+ мин ожидания LLM)
- ACCEPT: 0  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Score: 0.5
- Stop: llm_timeout (PID killed, no ИТОГ)
- Net-delta rollbacks: 6 (schema/__init__.py CLEANUP +2 × 6)
- apply_patch O.15 × 10+ (4 паттерна):
  1. `_invoke_with_optional_kwargs(...)` / `new[default.key] = (` × 3
  2. `elif flavor == VALIDATOR and type(s) == Regex:` / `return_schema[...]` × 3
  3. `# Only for type checking purposes...` / `if TYPE_CHECKING:` × 3
  4. `from pytest import mark, raises` / `from mock import Mock|schema import (` × 3+
- NET_DELTA regression: schema/__init__.py CLEANUP rule_based +2 × 6 (одна и та же ошибка)
- "Не удалось получить ни одного сегментного патча" × 3
- ПАТТЕРН: LLM зависает между patch attempts, каждый вызов занимает 2-5 мин
- EDGE CASE: NET_DELTA per-file cap отсутствует — одна ошибка schema/__init__.py крутится бесконечно
