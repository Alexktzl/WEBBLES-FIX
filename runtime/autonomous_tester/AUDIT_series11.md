# Post-Series Audit — Series 11 (2026-06-17)

## Итоги серии

| # | Проект | Initial | Accepted | NR | Remaining | Stop |
|---|--------|---------|----------|----|-----------|------|
| 1 | tiangolo/typer | 435 | 12 | 24 | 346 | max_cycles |
| 2 | realpython/python-basics-exercises | 22 | 7 | 2 | 1 | project_timeout |
| 3 | mahmoud/boltons | 675 | 5 | 6 | 642 | max_cycles |
| 4 | python-humanize/humanize | 18 | 0 | 0 | 0 | no_progress |
| 5 | jazzband/tablib | 24 | 3 | 1 | 17 | running |

**Total: 27 accepted, 33 NR, 4/5 с реальными accept'ами**

---

## Паттерны принятых патчей

| Код | Описание | Проекты |
|-----|----------|---------|
| W503 | line break before binary operator | typer (×8) |
| E203 | whitespace before ':' in slice | typer (×4) |
| E122 | continuation line missing indent | python-basics (×2) |
| E712 | comparison to True/False | python-basics (×5) |
| exec-detected | security: exec() risk comment | boltons (×2) |
| dangerous-globals | security: globals() whitelisting | boltons (×2) |
| unsafe_deserialization | security: pickle/yaml noqa | boltons (×1), tablib (×2) |
| non-literal-import | security: import_module whitelist | tablib (×1) |

---

## Найденные проблемы

### F1 — _vendor не в SKIP_DIRS [ИСПРАВЛЕНО]
- **Симптом:** tablib `_vendor/dbfpy/fields.py` — 5 × W504 не были исправлены потому что vendor code; потребовали ручного вмешательства
- **Причина:** `_vendor` отсутствовал в SKIP_DIRS → analyzer анализировал vendor зависимости
- **Фикс:** добавлен `"_vendor"` в SKIP_DIRS в `analyzers/python_analyzer.py`
- **Impact:** исключает vendor код из анализа — нет смысла "чинить" встроенные зависимости

### F2 — W391 не в rule-based fixer [ИСПРАВЛЕНО]
- **Симптом:** boltons debugutils.py:292 W391 — шёл в LLM pipeline (max_cycles — не достигнут)
- **Причина:** W391 отсутствовал в dispatch
- **Фикс:** добавлен `W391: _py_blank_line_at_eof` — delete последней пустой строки файла
- **Impact:** тривиальный детерминированный fix, 100% confidence, 0 LLM cycles

### F3 — W503 использует hardcoded _MAX_LEN=79 [ИСПРАВЛЕНО]
- **Симптом:** typer имеет ruff с line-length=88; ~8 W503 пошло в NR потому что `len(prev + op) > 79` для строк 80-85 символов
- **Причина:** `_py_w503_move_operator` использовал `_MAX_LEN = 79` независимо от конфига проекта
- **Фикс:**
  - `try_fix()` получил параметр `max_line_length: int = 79`
  - W503 handler читает `getattr(self, "_max_line_length", 79)`
  - call site в `generate_patch_stage.py` вызывает `_detect_project_line_length(work_dir)` перед `try_fix()`
- **Impact:** для ruff-проектов (88-limit) W503 fix теперь применяется там, где результат ≤88 символов

---

## Не реализованные (требуют больше данных / сложнее)

### F4 — Sandbox paths в NR queue
- **Симптом:** boltons NR queue содержит 4 записи с путями `webbles_isolated_*` — файлы из temp sandbox
- **Причина:** semgrep анализирует sandbox копию, отчёты содержат абсолютные temp пути; `webbles_isolated_XYZ` не в SKIP_DIRS (динамический суффикс)
- **Статус:** LOW risk (только загрязняет NR queue, не ломает функциональность), отложено

### F5 — E128/E125/E126/E127 continuation line indent
- **Симптом:** python-basics-exercises — 4 ошибки в `ch14/.../3-challenge-PdfFileSplitter-class.py` так и не починены; пошли в NR после 5+ LLM попыток
- **Причина:** LLM нестабильно справляется с continuation line indent без AST-понимания
- **Статус:** нужен AST-based rule. Кандидат для будущего IMP.

### F6 — Humanize empty-code errors (mypy)
- **Симптом:** humanize — 14 rejected, все с code="" (mypy ошибки без кода)
- **Причина:** mypy ошибки "Cannot find library stub", "Untyped decorator" попадают в pipeline с пустым кодом
- **Статус:** существует IMP-1 (import-untyped skip), но пустой code="" не перехватывается. Отложено.

---

## Статистика NR серии 11

| Код | Count | Причина |
|-----|-------|---------|
| W503/W504 | 11 | W504 → NR намеренно; W503 частично из-за F3 |
| E704 | 3 | multiple statements on one line (def) — спорный стиль |
| E203 | 3 | whitespace before ':' — accepted в typer для большинства |
| exec-detected | 4 | sandbox copies в NR (F4) |
| E122/E128 | 2 | continuation line — LLM не справился (F5) |
| unsafe_deserialization | 1 | test pickle (ожидаемо в NR для некоторых) |

---

## Реализованные улучшения

| ID | Изменение | Файлы |
|----|-----------|-------|
| IMP-S11-1 | `_vendor` → SKIP_DIRS | analyzers/python_analyzer.py |
| IMP-S11-2 | W391 rule-based handler | fixers/rule_based_fixer.py |
| IMP-S11-3 | W503 max_line_length из конфига | fixers/rule_based_fixer.py, core/stages/generate_patch_stage.py |

Все тесты проходят: critical_bug_fixes 20/20, duplicate_accept_loop exit=0, rule_based_fixer 5/5.
