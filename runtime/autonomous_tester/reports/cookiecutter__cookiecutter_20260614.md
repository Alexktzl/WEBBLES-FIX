# cookiecutter/cookiecutter  [PARTIAL — LLM TIMEOUT]

- Дата: 2026-06-14
- Ошибок: неизвестно (процесс убит через 740s wall / 12s CPU)
- ACCEPT: 0  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Score: 0.5
- Stop: llm_timeout (killed)
- Net-delta rollbacks: 5 (разные файлы)
  - tests/zipfile/test_unzip.py: NET_DELTA +3 (SECURITY, structured_llm) × 2
  - cookiecutter/find.py: NET_DELTA +7 (UNKNOWN, structured_llm) × 1
  - tests/test_cookiecutter_local_no_input.py: NET_DELTA +3+2+3 (UNKNOWN) × 3
- LLM empty_response × 2 для tests/zipfile/test_unzip.py (первые попытки)
- ПАТТЕРН: LLM зависает между patch attempts — 12s CPU в 740s wall (1.6%)
- ПАТТЕРН: NET_DELTA regression cascade — несколько файлов подряд
- EDGE CASE: NET_DELTA per-file cap отсутствует — BUG-3 подтверждён
