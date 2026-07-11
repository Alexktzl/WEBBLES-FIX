# nasgunawann/bensin-api  [PARTIAL]

- Дата: 2026-06-14
- Ошибок: 24 → 23 (delta=+1)
- ACCEPT: 0  |  NEEDS_REVIEW: 1  |  REJECT: 1
- Score: 1.5
- Причина: all_needs_review

## Детали

- **Баг с бюджетом**: движок читал `project_timeout` (умолч. 300s), а в конфиге стоял `cycle_runtime_budget_s: 600` — разные ключи. Реальный бюджет был 300s. **Исправлено**: добавлен `project_timeout: 600` в оба конфига.
- **E231** @ src/fetch_normalize.py:40 — rule_based принят, но финального ACCEPT нет (бюджет кончился)
- **E402** × 5 попыток — NET_DELTA откат × 3 → bulk-skip файла (LLM двигал import и ломал другое)
- **E501** @ tests/test_normalizer.py:53 → NEEDS_REVIEW (NET_DELTA откат × 2)

## Новые фиксы (E999 triage, parallel_workers=2, expanded SAFE_CODES)

- Проект не имел E999 ошибок → triage не активировался
- F401 не было в initial errors → SAFE_CODES расширение не повлияло
- parallel_workers=2 активирован, но только 1 файл с ошибками → параллелить нечего
