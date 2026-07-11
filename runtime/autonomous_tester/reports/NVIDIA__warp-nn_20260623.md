# NVIDIA/warp-nn  [PASS, верифицировано]

- Дата: 2026-06-23T17:00:30
- Ошибок: 317  →  1  (delta=316)
- ACCEPT (заявлено): 1  |  NEEDS_REVIEW: 1  |  REJECT: 2
- **Верификация: 1 REAL_FIX (weak — type_ignore_comment), 0 STILL_FLAGGED, 0 unsafe_accept, 0 semantic_suspicious**

`_common.py:18` import-not-found (warp) — `# type: ignore[import-not-found]` добавлен к строке импорта. Формально ошибка ушла, содержательно — типовая эрозия. Побочное изменение `from typing import Callable` → `from collections.abc import Callable` (вероятно ruff UP035) — безвредная модернизация, не задета подозрительным переименованием.
