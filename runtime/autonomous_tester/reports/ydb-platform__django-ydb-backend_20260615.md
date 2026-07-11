# ydb-platform/django-ydb-backend  [FAIL]

- Дата: 2026-06-15T01:08:06
- Ошибок: 504  →  504  (delta=0)
- ACCEPT: 0  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Score: 1.0
- Причина: LLM hard timeout ×7 — проект слишком большой (504 ошибок), LLM не успевала отвечать в рамках project_timeout=600s

## Заметки

- 504 ошибки — очень большой проект для 600s бюджета
- LLM timeout происходил на каждом вызове (7 раз) — Gemma перегружена
- PROJECT_TIMEOUT сработал корректно, pipeline завершился чисто (exit_code=0)
