# BasicWolf/hexagonal-architecture-django  [PASS]

- Дата: 2026-06-14
- Ошибок: 6 → 53 (queue-based, accepted patch triggered re-scan)
- ACCEPT: 1 (hardcoded_secret в settings.py)  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Net-delta rollbacks: 2 (settings.py)
- Score: 3.0
- BUG-8: не триггерился (нет бинарных файлов)
- EDGE-5: нет (2 rollbacks < cap=3)
