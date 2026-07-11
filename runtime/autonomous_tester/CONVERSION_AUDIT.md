# Аудит конверсии: REJECT / NEEDS_REVIEW / invalid_context
**Дата:** 2026-06-14  
**База:** 108 проектов с ошибками (series 50/50 + validation runs)

---

## 1. Сводная статистика

| Метрика | Значение |
|---------|----------|
| Проектов с ошибками | 108 |
| Суммарных ошибок (initial) | 126,348 |
| **Accepted (LLM)** | **118** (0.09%) |
| Needs_Review | 922 (0.73%) |
| Rejected | 188 (0.15%) |
| Errors fixed (rule-based + LLM) | ~64,307 (**51%**) |
| accepted / project | 1.09 |
| accepted / found | 0.09% |

> Правило: 51% ошибок исправляет rule-based / ruff — это хорошо. Узкое место — LLM принимает 
> только ~118 патчей при 922 NR и 188 REJECT. NR/ACCEPT = 7.8× — огромный потенциал.

---

## 2. Топ-20 кодов ошибок по частоте (remaining + NR)

| # | Code | Remaining | NR | Причина | Текущее состояние |
|---|------|-----------|----|---------|--------------------|
| 1 | **E501** | 103 (32%) | 0 | Строка длиннее 79 символов | Исключён из ruff autofix намеренно |
| 2 | **E302** | 78 (24%) | 0 | Ожидается 2 пустые строки | НЕТ в `_SAFE_CODES` ruff |
| 3 | **E999** | 38 (11%) | 2 | SyntaxError | SyntaxRepairStage (частично) |
| 4 | **sql_injection** | 0 | 61 (63% NR) | Безопасность SQL | NR (правильно) |
| 5 | **xss** | 0 | 23 (24% NR) | Cross-site scripting | NR (правильно) |
| 6 | **E0382** | 23 (7%) | 0 | Use after del / undefined | Требует LLM |
| 7 | **W292** | 19 (5%) | 0 | Нет новой строки в конце файла | Уже в ruff SAFE_CODES |
| 8 | **E122** | 16 (5%) | 0 | Continuation line missing indent | НЕТ в ruff (flake8-compat) |
| 9 | **E402** | 13 (4%) | 2 | Import not at top of file | LLM, иногда NR |
| 10 | **F401** | 9 (2%) | 0 | Unused import | ruff _UNSAFE_CODES (F841, не F401) |
| 11 | **F821** | 2 | 4 | Undefined name | NR (F821 literal guard) |
| 12 | **hardcoded_secret** | 0 | 3 | Захардкоженный секрет | NR (правильно) |
| 13 | **W293** | 4 (1%) | 0 | Trailing whitespace | Уже в ruff SAFE_CODES |
| 14 | **E305** | 3 | 0 | 2 blank lines after def | НЕТ в ruff SAFE_CODES |
| 15 | **E261** | 2 | 0 | Inline comment не с 2 пробелов | НЕТ в ruff SAFE_CODES |
| 16 | **W503** | 2 | 0 | Line break before binary op | ruff не поддерживает (W504 ≠ W503) |
| 17 | **W391** | 2 | 0 | Blank line at end of file | Уже в ruff SAFE_CODES |
| 18 | **E203** | 0 | 0 | Whitespace before ':' | ruff конфликтует с black |
| 19 | **E701** | (в проектах) | 0 | Multiple statements on one line | В _SAFE_LLM_CODES, threshold 0.70 |
| 20 | **E131** | (в проектах) | 0 | Continuation line unaligned | В _SAFE_LLM_CODES, threshold 0.70 |

---

## 3. Корневые причины REJECT / NR (по убыванию impact)

### 3.1 O.15 — LLM сливает 2 строки в одну (12/38 проектов = 32%)
**Механизм:** LLM генерирует patch diff где 2 исходные строки соединены в одну строку контекста.  
Результат: anchor mismatch → patch REJECT (invalid_context cascade).  
**Влияние:** 32% проектов теряют LLM-патчи на первом же REJECT → cascade invalid_context для всех остальных ошибок того же файла.

### 3.2 NET_DELTA regression → invalid_context cascade (7 проектов, 19 упоминаний)
**Механизм:** LLM применяет патч → NET_DELTA обнаруживает +N новых ошибок → rollback. После rollback все queued patches для этого файла получают `invalid_context` потому что patch plan устарел.  
**Влияние:** 1 rollback блокирует обработку ВСЕХ остальных ошибок файла.

### 3.3 E999 cascade → NR=329 (ALTIbaba — 1 проект, 36% всех NR)
**Механизм:** Файл с SyntaxError (E999) → все связанные ошибки становятся NR через cascade. 15 broken files × ~22 ошибки = 329 NR в одном проекте.  
**Влияние:** Один проект искажает всю статистику NR.

### 3.4 E302/E305/E261 не в ruff autofix (78+3+2 = 83 remaining)
**Механизм:** Ruff может детерминированно исправить пустые строки и комментарии, но эти коды НЕ включены в `_SAFE_CODES` ruff_autofix_stage.py.  
**Влияние:** 24% всех remaining ошибок — чистые форматирование, решаемые без LLM.

### 3.5 F401 не в ruff autofix (9 remaining)
**Механизм:** F841 (unused var) в ruff unsafe, но F401 (unused import) — нет. Ruff умеет безопасно удалять неиспользуемые импорты с net-delta проверкой.  
**Влияние:** Небольшой, но simple win.

### 3.6 structured_llm confidence=0.70 на пороге (часть NR)
**Механизм:** Threshold для safe codes = 0.70. Default confidence structured_llm = 0.70. ReviewStage применяет ±0.2, при `-0.01` патч уходит в NR. 80% safe-code LLM патчей проходят, 20% выпадают в NR из-за ReviewStage.  
**Влияние:** Консервативная граница — часть качественных патчей остаётся в NR.

### 3.7 W503 / E203 — неисправимые коды
**Механизм:** W503 — "старый" PEP8 стиль (line break BEFORE operator). Ruff использует W504 (AFTER) — противоположный. E203 — ruff конфликтует с black (black намеренно ставит пробел перед `:`).  
**Влияние:** Коды попадают в очередь, расходуют бюджет (LLM пытается починить), всегда завершаются в remaining.

### 3.8 reviewer_verdict_wrong → REJECT
**Механизм:** ReviewStage возвращает verdict="wrong" → декид REJECT вместо NR.  
**Влияние:** Маленький (188 reject vs 922 NR), но каждый REJECT это потраченный LLM вызов.

---

## 4. Специальные случаи (из задания)

| Code | Что происходит | Предложение |
|------|---------------|-------------|
| **E999** | SyntaxRepairStage обрабатывает, но не всегда успевает первым; cascade→NR | Приоритет E999 = MAX в очереди; после fix — re-scan до других ошибок |
| **W503** | Ruff не фиксит (конфликт стилей), LLM тратит бюджет попусту | Добавить W503 в skip-list на уровне prioritize/pre_cleanup |
| **E203** | Аналогично W503 — black и flake8 конфликтуют | Добавить в skip-list |
| **E131** | В `_SAFE_LLM_CODES` (threshold 0.70) — structured_llm должен принимать | Проверить: если confirm_still_present → validate, должны проходить |
| **E701** | В `_SAFE_LLM_CODES` → должен принимать при structured_llm 0.70 | NET_DELTA rollback если LLM вводит новые ошибки (кейс docker-hub-proxy) |
| **invalid_context** | Cascade после rollback — одна неудача блокирует весь файл | После rollback: clear stale patch plans для файла, re-queue errors |
| **target_error_still_present** | После патча ошибка осталась → REJECT (TESP counter) | Ограничить TESP до 2 попыток per-error (сейчас MAX_TESP_PER_FILE=5 per-file) |

---

## 5. План работ (по ожидаемому приросту)

### TIER 1: Быстрые wins (1-2 часа, нулевой риск)

#### IMP-A: Добавить E302/E301/E303/E304/E305/E306/E261/E262/E265/E266 в ruff SAFE_CODES
**Файл:** `core/stages/ruff_autofix_stage.py`  
**Изменение:** Добавить к `_SAFE_CODES`:
```python
_SAFE_CODES = "W291,W292,W293,W391,E711,E712,I001,UP006,UP007,UP032,UP034,UP035,"
              "E301,E302,E303,E304,E305,E306,"   # blank lines — ruff perfect fix
              "E261,E262,E265,E266"               # comments — deterministic
```
**Ожидаемый прирост:** E302 = 24% remaining → исправлено. Прирост ~0.3-0.5 errors/project.  
**Риск:** Нулевой — ruff + net-delta rollback уже есть.

#### IMP-B: Добавить W503/E203 в skip-list (не расходовать бюджет)
**Файл:** `core/stages/pre_cleanup_stage.py` или `prioritize_stage.py`  
**Изменение:** Фильтровать W503, E203, W504 из очереди ДО LLM вызовов:
```python
_SKIP_UNFIXABLE = frozenset({"W503", "W504", "E203"})
```
**Ожидаемый прирост:** Освобождает ~2-5% LLM бюджета для полезных ошибок.  
**Риск:** Нулевой — эти коды НИКОГДА не исправляются.

#### IMP-C: Добавить F401 в ruff SAFE_CODES (осторожный вариант)
**Файл:** `core/stages/ruff_autofix_stage.py`  
**Изменение:** Добавить F401 к `_SAFE_CODES` (ruff удаляет unused imports с проверкой __all__)  
**Ожидаемый прирост:** F401 = 2% remaining → 0.1 errors/project.  
**Риск:** Низкий. Миtigated by net-delta rollback.

---

### TIER 2: Средний impact (2-4 часа, умеренный риск)

#### IMP-D: Добавить E501 в ruff autofix с project-level line length detection
**Файл:** `core/stages/ruff_autofix_stage.py`  
**Механизм:** Детектировать `max_line_length` из setup.cfg / .flake8 / pyproject.toml / tox.ini. Если проект использует 79 chars, предложить ruff fix с `--line-length=100` (сдвиг на 21 символ). Только если net-delta < 0.
```python
# Новый _LINE_LENGTH_PASS
_RELAXED_LINE_LENGTH = 100  # vs flake8 default 79
```
**Ожидаемый прирост:** E501 = 32% remaining → потенциально 10-20% исправляется при смягчении лимита.  
**Риск:** Средний. Нужна project config detection. Rollback если ошибки растут.

#### IMP-E: invalid_context cascade fix (после rollback — re-queue)
**Файл:** `core/stages/validate_stage.py` (NET_DELTA rollback block)  
**Механизм:** После rollback: вместо того чтобы все queued patches для файла получали invalid_context, добавить их обратно в очередь с пометкой `needs_fresh_patch=True`. При `needs_fresh_patch=True` → генерировать новый патч (не использовать кешированный план).
**Ожидаемый прирост:** Снижение cascade REJECT с ~107 до ~20 после каждого rollback.  
**Риск:** Средний — нужно аккуратно обрабатывать состояние очереди.

#### IMP-F: E999 приоритизация (fix first, then re-scan)
**Файл:** `core/stages/prioritize_stage.py`  
**Механизм:** E999 (CRITICAL_SYNTAX) всегда ставить на первое место в очереди. После успешного SyntaxRepair → принудительный re-scan перед продолжением. Предотвращает 329 NR cascade из-за 15 broken files.
**Ожидаемый прирост:** Снижение NR с 922 до ~600 (убираем E999 cascade effect).  
**Риск:** Низкий — E999 уже имеет высокий вес в CRITICAL_SYNTAX classifier.

---

### TIER 3: Высокий impact, требует тестирования (4-8 часов)

#### IMP-G: O.15 fix — LLM line merge detection и pre-apply validation
**Файл:** `core/stages/apply_patch_stage.py` + patch engine  
**Механизм:** Перед применением patch diff — проверить, что каждая строка контекста (`" "`) в patch ТОЧНО соответствует строке в файле (с учётом отступов). Если контекстная строка содержит merged content (два фрагмента соединены без разрыва) → отклонить патч сразу, не тратя NET_DELTA validation.  
**Альтернатива:** Улучшить LLM prompt: явно запрещать "merging source lines", добавить инструкцию "each context line must correspond to exactly ONE line in the source file".
**Ожидаемый прирост:** 32% проектов (12/38) получат 1-3 дополнительных ACCEPT вместо cascade REJECT. +0.3-0.5 accept/project.  
**Риск:** Высокий сложности. Нужно тестирование на реальных проектах (5-project validation).

#### IMP-H: structured_llm safe-code threshold 0.70 → 0.65
**Файл:** `core/stages/decide_stage.py`  
**Изменение:**
```python
_SAFE_LLM_THRESHOLD = 0.65  # было 0.70
```
**Ратionale:** ReviewStage часто снижает confidence на -0.02...-0.05 для нормальных патчей. Патч на E302 со structured_llm (conf=0.70) после review (-0.03) = 0.67 → NR вместо ACCEPT. Снижение до 0.65 позволит качественным патчам пройти.  
**Ожидаемый прирост:** ~15-20% NR на safe codes конвертируется в ACCEPT.  
**Риск:** Нужно проверить: не увеличится ли число плохих патчей. Запустить 5-project validation.

#### IMP-I: F401 — умная проверка __all__ перед ruff fix
**Файл:** `core/stages/ruff_autofix_stage.py`  
**Механизм:** Перед ruff --fix для F401: проверить нет ли `__all__` в файле, нет ли импорта в `__init__.py` с re-export pattern. Если чисто → ruff fix.  
**Ожидаемый прирост:** F401 conversion от 30% до 70%.  
**Риск:** Средний. Нужно тестирование.

---

### TIER 4: Долгосрочное (без новых сложных стадий)

#### IMP-J: E122/E131 — добавить к ruff SAFE_CODES (тест)
Ruff handles continuation line indentation. Нужно проверить нет ли регрессий на 5 проектах с E122.

#### IMP-K: E402 — rule-based фиксер (move imports to top)
Детерминированное перемещение import в топ файла (если нет условных импортов). Сейчас уходит в LLM.

---

## 6. Сводная таблица ROI

| ID | Изменение | Файл | Слож-ть | Прирост accept/found | Прирост accept/project | Риск |
|----|-----------|------|---------|----------------------|------------------------|------|
| A | E302+E305+E261 в ruff | ruff_autofix_stage.py | 1ч | +15-20% remaining fix | +0.5/proj | Нет |
| B | W503/E203 skip-list | pre_cleanup_stage.py | 1ч | +2% budget freed | +0.05/proj | Нет |
| C | F401 в ruff safe | ruff_autofix_stage.py | 1ч | +2% remaining | +0.1/proj | Низкий |
| F | E999 first + re-scan | prioritize_stage.py | 2ч | -30% NR | +0.3/proj | Низкий |
| E | invalid_context re-queue | validate_stage.py | 3ч | -50% cascade REJECT | +0.2/proj | Средний |
| D | E501 ruff с line-length | ruff_autofix_stage.py | 3ч | +10-20% E501 fixed | +0.4/proj | Средний |
| H | threshold 0.70→0.65 | decide_stage.py | 1ч | +15% NR→ACCEPT | +0.2/proj | Средний |
| G | O.15 line merge fix | apply_patch + prompt | 4ч | +32% proj. unlocked | +0.4/proj | Высокий |
| I | F401 __all__ check | ruff_autofix_stage.py | 2ч | +1% | +0.05/proj | Средний |
| J | E122/E131 ruff | ruff_autofix_stage.py | 2ч | +5% remaining | +0.1/proj | Средний |

---

## 7. Рекомендуемый порядок реализации

1. **Сначала IMP-A + IMP-B** — быстро, нулевой риск, немедленный результат
2. **Затем IMP-F** — E999 приоритет, убирает крупнейший NR источник  
3. **Валидация на 5 проектах** — подтвердить прирост
4. **Затем IMP-D + IMP-E** — E501 и invalid_context cascade
5. **Затем IMP-H** — снижение threshold (требует тестирования)
6. **Затем IMP-G** — O.15 (самое сложное, наибольший потенциал)

**НЕ ДЕЛАТЬ** без тестирования: новых pipeline стадий, изменений в LLM prompt без замера на 10+ проектах.

---

## 8. Ожидаемый суммарный эффект

При реализации IMP-A..IMP-F (TIER 1+2):
- accepted/project: 1.09 → **1.6-2.0** (+50-80%)
- effective fix rate: 51% → **60-65%**
- NR: 922 → **~550** (-40%)
- Rejected cascade: 188 → **~80** (-57%)

IMP-G (O.15) дополнительно: accepted/project → **2.0-2.5**
