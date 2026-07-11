# image-charts/python  [PASS]

- Дата: 2026-06-14
- Ошибок: 323 → 24 (delta=+299; 1 accepted patch removed chained errors)
- ACCEPT: 1  |  NEEDS_REVIEW: 4  |  REJECT: 0
- Score: 5.0
- Net-delta rollbacks: 1  |  Net-delta NR: 2
- Stop: runtime_budget
- EDGE CASE: O.15 cascade ×10 на ImageCharts.py (_o15_retries счётчик глобальный, не per-error)
- EDGE CASE: FileAntiLoop 34/20 — счётчик растёт за лимит, блокирует повторно при каждом вызове
