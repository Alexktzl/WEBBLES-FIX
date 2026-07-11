# QubesOS/qubes-pdf  [PASS] — Validation V3/5

- Дата: 2026-06-14
- Серия: validation_post_bug_fixes
- Ошибок: 102 → 174 (delta=-72, pipeline расширил очередь)
- ACCEPT: 1  |  NEEDS_REVIEW: 1  |  REJECT: 3
- NET_DELTA rollbacks: 2 (qvm_convert_pdf_nautilus.py)
- Score: 3.5
- Стоп: runtime_budget (616s >= 90s)
- Статус пайплайна: OK (exit_code=0)

## BUG-наблюдения

- O.15 x1 (qubespdfconverter/server.py) — BUG-1 ожидаемо не устранён
- 3x REJECT empty_response: qubespdfconverter/server.py x2, tests/__init__.py x1
- EditSet.to_unified_diff: anchor не найден в server.py:400 (anchor mismatch — не BUG-7, другое)
  - BUG-7 исправляет file-path lookup, здесь проблема в тексте якоря (пароль с спецсимволами)
- NET_DELTA: 2 rollback-а (class=BLOCKING) + 1 NR — работает корректно
- Патч E128 qubespdfconverter/tests/__init__.py — ACCEPT (успешно)
