# frecar/epguides-api  [PASS]

- Дата: 2026-06-13
- Ошибок: 818 → 771 (delta=47)
- ACCEPT: 1  |  NEEDS_REVIEW: 5  |  REJECT: 0
- Score: 5.5
- Net-delta NR: 2
- Stop: runtime_budget (857s >= 90s)
- БАГ: apply_patch O.15 — патч слипает 2 строки в одну (2 случая); блокировало применение патчей
- E999 syntax errors в 3 файлах (show_service.py, cache.py, epguides.py) — маскировали ошибки
- "Не удалось объединить патчи" — 20+ повторов из-за O.15 бага
- Принят: E501 fix для app/core/observability.py
