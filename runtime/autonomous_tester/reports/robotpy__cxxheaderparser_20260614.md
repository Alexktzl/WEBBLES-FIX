# robotpy/cxxheaderparser  [PARTIAL]

- Дата: 2026-06-14
- Ошибок: unknown → unknown (Bash timeout, ИТОГ не получен)
- ACCEPT: 0  |  NEEDS_REVIEW: 33  |  REJECT: 24
- Score: 17.5 (NR-heavy)
- Stop: bash_tool_timeout_300s
- EDGE CASE: target_error_still_present × 24 на parser.py (нет cap — петля)
  → parser.py содержит PLY-грамматику с C++-like конструкциями → ruff E999 false-positive
  → патч применяется, но ruff продолжает репортить ту же ошибку → цикл без выхода
- EDGE CASE: SecurityScanner failed: module 'tokenize' has no attribute 'TokenizeError'
  → Python 3.11+ убрал tokenize.TokenizeError → нужно заменить на tokenize.TokenError
- NR: 33 (все E999/invalid-syntax — false positives PLY-грамматики)
- FIX NEEDED: max_target_error_retries = 5 (cap loop on same error signature)
- FIX NEEDED: SecurityScanner graceful-skip на syntactically invalid файлах
