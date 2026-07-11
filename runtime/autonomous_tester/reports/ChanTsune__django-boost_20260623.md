# ChanTsune/django-boost  [PASS, верифицировано]

- Дата: 2026-06-23T15:11:20
- Ошибок: 371  →  1  (delta=370)
- ACCEPT (заявлено): 1  |  NEEDS_REVIEW: 0  |  REJECT: 2
- **Верификация: 0 REAL_FIX, 1 STILL_FLAGGED, 0 unsafe_accept**

## STILL_FLAGGED причина: добавлена несовместимая аннотация типа
`example/views/__init__.py:100` mypy `assignment` — базовый класс `AllowContentTypeMixin` объявляет `allowed_content_types` как `None`. Патч добавил `allowed_content_types: list = []` (аннотацию типа list), но это всё равно несовместимо с типом `None` родителя (нарушение LSP) — mypy продолжает флагать ту же строку при свежем рескане. Отличается от import-not-found/isort паттерна (см. toggl2notion) — здесь проблема в том, что добавленная аннотация не решает реальную типовую несовместимость с базовым классом.
