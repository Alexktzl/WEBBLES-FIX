# fireantology/django-logtailer  [PASS]

- Дата: 2026-06-14
- Ошибок: 54 → 26
- ACCEPT: 1 (E128 в admin.py)  |  NEEDS_REVIEW: 2  |  REJECT: 0
- Net-delta rollbacks: 0
- Score: 3.0
- BUG-8: не триггерился (есть .mo бинарные файлы — copytree прошёл без ошибок)
- EDGE-5: нет (0 rollbacks)
