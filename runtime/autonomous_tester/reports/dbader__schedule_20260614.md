# dbader/schedule  [PASS]

- Дата: 2026-06-14
- Ошибок: 138 → 47 (delta=+91)
- ACCEPT: 1  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Score: 3.0
- Net-delta rollbacks: 1
- Stop: runtime_budget
- НОВЫЙ ПАТТЕРН: LLM TimeoutError × 2 (LocalLLMProvider._do_request) → 713s задержка
- O.15 × 5 (test assertions merged)
- attr-defined tzset: Windows-specific (os.tzset не существует на Windows)
- Принят: E501 в docs/conf.py
