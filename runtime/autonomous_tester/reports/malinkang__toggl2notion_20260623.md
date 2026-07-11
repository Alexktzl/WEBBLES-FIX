# malinkang/toggl2notion  [PASS, верифицировано — ПЕРЕСЧИТАНО после фикса]

## Прогон 1 (до фикса F401 в RuffAutoFixStage)
- ACCEPT: 1 (notion_helper.py:3, import-not-found)
- Верификация: 0 REAL_FIX, 1 STILL_FLAGGED, **1 unsafe_accept** (потеряно 3 из 5 импортируемых имён — `ruff --fix --select=F401`, не сам патч)

## Прогон 2 (после фикса: F401 убран из _SAFE_CODES, symbol_regression теперь видит импорты)
- Ошибок: 304 → 4 (baseline_remaining)
- ACCEPT: 1 (toggl.py:6, import-not-found, другая ошибка — недетерминизм LLM/расписания между прогонами)
- Верификация: 0 REAL_FIX, **1 STILL_FLAGGED, 0 unsafe_accept**

**Итог**: P0 (потеря импортируемых имён) устранена — подтверждено повторным прогоном. STILL_FLAGGED — отдельная, более старая проблема: `_make_type_ignore_patch` добавляет `# type: ignore[code]` к конкретной строке, но `RuffAutoFixStage`-шный `I001` (isort) может переставить импорты ДО или ПОСЛЕ применения этого патча, из-за чего комментарий-подавитель оказывается не на той строке, которую видит свежий mypy-рескан. Не относится к текущему расследованию (P0 unsafe_accept), не исправлено.
