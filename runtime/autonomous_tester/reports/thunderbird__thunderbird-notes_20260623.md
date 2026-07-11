# thunderbird/thunderbird-notes  [PARTIAL]

- Дата: 2026-06-23T02:07:41
- Ошибок: 38  →  7  (delta=31)
- ACCEPT: 0  |  NEEDS_REVIEW: 4  |  REJECT: 1
- Причина: all_needs_review — project_timeout до применения (preview.py/Flask XSS-находки semgrep, Jinja2 autoescape)
- total_current_errors=40 (flake8=7, semgrep=17, bandit=0) — flake8 точно совпадает с baseline_remaining=7, разница объясняется видимым semgrep-источником, не загадкой.
