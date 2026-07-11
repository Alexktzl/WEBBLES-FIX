# regebro/pyroma  [PASS]

- Дата: 2026-06-14T22:40:55
- Ошибок: 15  →  69  (delta=-54, рост из-за ruff/mypy ошибок от патчей)
- ACCEPT: 12  |  NEEDS_REVIEW: 5  |  REJECT: 1
- Score: 27.5
- Runtime: 268s (в рамках project_timeout=600s)

## Заметка

**project_timeout сработал корректно** — первый прогон с исправленным кодом (deadline внутри _single_run).

**Loop-баг: 12 ACCEPT = одна ошибка.**
`dangerous_eval` в `pyroma/tests.py:56` принята 12 раз (MAX_PATCHES_PER_FILE лимит отработал и остановил серию).
Реально уникальных фиксов: 1.

**final > initial:** 15 → 69 ошибок. Причина: патч на dangerous_eval изменил код так, что ruff/mypy обнаружили 69 новых ошибок в последующих сканах.
