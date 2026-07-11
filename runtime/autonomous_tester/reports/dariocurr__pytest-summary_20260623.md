# dariocurr/pytest-summary  [PASS]

- Дата: 2026-06-23T02:25:41
- Ошибок: 2  →  0  (delta=2, полностью закрыт)
- ACCEPT: 1  |  NEEDS_REVIEW: 0  |  REJECT: 0
- total_current_errors=2 (flake8=0, semgrep=2) — точное совпадение с baseline.
- "file_error_signatures_before пуст" тоже появился здесь, но fallback по весам в этот раз успешно выбрал кандидата (в отличие от kedro-light) — подтверждает, что баг не всегда даёт 0 кандидатов, иногда работает нормально.
