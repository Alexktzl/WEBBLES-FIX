# Webbles Fix — Audit контрольной серии (control_post_improvements)

**Дата старта:** 2026-06-13T22:53  
**Дата окончания:** 2026-06-14T03:42  
**Серия:** control_post_improvements (цель: 20 проектов)  
**Статус:** ЗАВЕРШЕНА — 20/20

---

## Итоговые метрики — 20 проектов (ФИНАЛ)

| Метрика | Значение |
|---------|---------|
| Всего проектов | 20 / 20 |
| PASS | 14 (70%) |
| PARTIAL | 6 (30%) |
| FAIL | 0 (0%) |
| Effective pass rate | 0.700 |
| Ошибок найдено | 6328+ |
| Принято патчей | 15 |
| NEEDS_REVIEW | 75 |
| Отклонено | 32 |
| Net-delta rollbacks | 39 |
| LLM timeouts (calls) | 6 (P16×2, P17, P18, P19, P20×2) |

---

## Проекты P01-P20 (полный список)

| # | Проект | Class | Init→Final | A | NR | Rej | ND-rb | Стоп |
|---|--------|-------|-----------|---|-----|-----|-------|------|
| P01 | omidbimo/eds_pie | PASS | 352→130 | 1 | 1 | 0 | 1 | budget |
| P02 | centrifugal/centrifuge-python | PASS | 1047→312 | 1 | 1 | 0 | 3 | budget |
| P03 | Dirac-Robot/comet | PASS | 721→443 | 1 | 0 | 0 | 5 | budget |
| P04 | frecar/epguides-api | PASS | 818→771 | 1 | 5 | 0 | 0 | budget |
| P05 | MooshiMochi/ManhwaUpdatesBot | **PARTIAL** | ?→? | 0 | 2 | 0 | 7 | bash_timeout |
| P06 | maifeipin/lite_agent | PASS | 527→323 | 1 | 1 | 0 | 0 | budget |
| P07 | robotpy/cxxheaderparser | **PARTIAL** | ?→? | 0 | 33 | 24 | 0 | bash_timeout |
| P08 | qualiaenjoyer/polymarket-apis | PASS | 424→657 | 2 | 17 | 0 | 1 | budget |
| P09 | pyladies/pyladiescon-portal | PASS | 299→329 | 1 | 1 | 0 | 2 | budget |
| P10 | image-charts/python | PASS | 323→24 | 1 | 4 | 0 | 1 | budget |
| P11 | knifecake/steady-queue | PASS | 239→240 | 1 | 0 | 0 | 1 | budget |
| P12 | nyu-devops/sample-accounts | PASS | 14→6 | 1 | 6 | 1 | 5 | budget |
| P13 | soxoj/kronikier | PASS | 404→208 | 1 | 1 | 2 | 0 | budget |
| P14 | shablin/mtproto-proxy | PASS | 71→21 | 1 | 1 | 0 | 0 | budget |
| P15 | Infinite-Labs-AI/hermes-health-apollo | PASS | 951→917 | 1 | 2 | 0 | 1 | budget |
| P16 | dbader/schedule | PASS | 138→47 | 1 | 0 | 0 | 1 | budget |
| P17 | keleshev/schema | **PARTIAL** | ?→? | 0 | 0 | 0 | 6 | llm_timeout |
| P18 | tqdm/tqdm | **PARTIAL** | ?→? | 0 | 0 | 5 | 0 | llm_timeout |
| P19 | cookiecutter/cookiecutter | **PARTIAL** | ?→? | 0 | 0 | 0 | 5 | llm_timeout |
| P20 | dateutil/dateutil | **PARTIAL** | ?→? | 0 | 0 | 0 | 0 | llm_timeout |

---

## Найденные баги и паттерны (по приоритету)

### [BUG-1] apply_patch O.15 — LLM сливает 2 строки в одну (КРИТИЧЕСКИЙ)

**Частота:** P04, P05, P07, P08, P10, P13, P16, P17, P18 — ≥8/17 проектов (47%+)  
**Файл:** `core/apply_patch_stage.py` (детект O.15) + LLM prompt  
**Проявление:**
```
apply_patch: O.15 — патч слипает в одну строку 2 исходных строки
(['строка A', 'строка B']) в 'строка Aстрока B' — отказ
Ошибка песочницы: Merge patch application failed
  Не удалось объединить патчи – ошибка остаётся нерешённой  (× 20+)
```
**Паттерны "слипания":**
- Циклические конструкции: `for fn, fcomp in zips.items():` / `md, sd = pty.openpty()`
- Условные ветки: `elif flavor == VALIDATOR and type(s) == Regex:` / `return_schema["type"] = ...`
- Комментарии + код: `# Only for type checking purposes...` / `if TYPE_CHECKING:`
- Импорты: `from pytest import mark, raises` / `from mock import Mock`
- Строки с кавычками: `for end in [` / `']', # bar`
- Длинные строки: `"... | 231/1000 [06:32<21:44, 1.70s/it]")` / `#' * 3 + '6' + (`

**Влияние:** Блокирует применение патчей. Каждая O.15 ошибка → retry до MAX_BLOCKING_LLM_RETRIES (20).  
**Fix:** Явное требование в LLM prompt: "preserve all newlines in diff; never concatenate adjacent lines".  
**Статус:** НЕ ИСПРАВЛЕН.

---

### [BUG-2] `_o15_retries` — глобальный счётчик (КРИТИЧЕСКИЙ)

**Частота:** P10 (image-charts) подтверждён; вероятно P13, P16, P17 также затронуты  
**Файл:** `core/stages/apply_patch_stage.py`  
**Проявление:** `_o15_retries` хранится в `context.metadata` без привязки к error signature. После первого O.15 retry для любой ошибки, счётчик `_o15_retries = 1` глобально. Все последующие O.15 ошибки видят `_o15_retries >= 1` и пропускают retry.  
**Следствие P10:** ImageCharts.py получил O.15 cascade × 10. FileAntiLoop сработал при 34 попытках (порог 20), но счётчик продолжал расти.  
**Fix:** Ключевать как `_o15_retries_{error_signature}` вместо `_o15_retries`.  
**Статус:** НЕ ИСПРАВЛЕН.

---

### [BUG-3] NET_DELTA regression loop — нет per-file cap (КРИТИЧЕСКИЙ)

**Частота:** P03 (×5), P05 (×7), P12 (×5), P17 (×6) — 4/17 проектов (24%)  
**Проявление:** Один файл (напр. `schema/__init__.py`) вызывает NET_DELTA regression (+2 ошибки) при каждой попытке CLEANUP rule_based патча. Откат → следующая попытка → снова тот же откат. Цикл без выхода.  
**Паттерн P17:** 6 откатов подряд на schema/__init__.py CLEANUP +2. Пустые segment patches × 3.  
**Fix:** Добавить per-file cap для NET_DELTA regressions (аналог `_tesp_file_counts`): после N откатов одного файла → bulk-skip все pending ошибки этого файла.  
**Статус:** НЕ ИСПРАВЛЕН.

---

### [BUG-4] LLM TimeoutError — нет retry/fallback (ВЫСОКИЙ)

**Частота:** P16 (×2 timeout, 713s delay), P17 (убит через 15+ мин без ответа)  
**Файл:** `core/providers/local_llm_provider.py` (LocalLLMProvider._do_request)  
**Проявление:**
```
LocalLLMProvider._do_request TimeoutError: timed out
generate_structured_fix: ответ не парсится как EditSet, len=0
```
**Влияние:** P16 потерял 713s на 2 timeout-а перед началом основной обработки. P17 завис на 15+ минут между LLM calls (8.4 CPU seconds за всё время), процесс убит вручную.  
**Fix:** Retry с backoff (max 3, delay 10-30s); жёсткий таймаут per request (например, 60s).  
**Статус:** НЕ ИСПРАВЛЕН.

---

### [BUG-5] FileAntiLoop счётчик растёт за пределы max_attempts (СРЕДНИЙ)

**Частота:** P10 (image-charts)  
**Проявление:** FileAntiLoop блокирует файл при `count >= 20`, но счётчик продолжает инкрементироваться до 34. При каждом последующем вызове лог "FileAntiLoop" повторяется.  
**Влияние:** Лишний шум в логах; minor CPU waste.  
**Fix:** После блокировки файла — прекратить инкремент счётчика (или сбросить в 20).  
**Статус:** НЕ ИСПРАВЛЕН.

---

### [BUG-6] UnicodeDecodeError cp1251 в subprocess output (СРЕДНИЙ)

**Частота:** P06 (maifeipin/lite_agent)  
**Проявление:**
```
UnicodeDecodeError: 'cp1251' codec can't decode byte 0x98 in position N
security: ошибка - 'NoneType' object has no attribute 'splitlines'
```
**Причина:** Проект содержит non-ASCII байты в исходных файлах (CJK комментарии). Subprocess вывод декодируется системной кодировкой (cp1251 на Windows) вместо UTF-8.  
**Fix:** `encoding='utf-8', errors='replace'` при декодировании subprocess output везде.  
**Статус:** НЕ ИСПРАВЛЕН.

---

### [BUG-7] "Патч нерелевантен" — нет лимита повторов (СРЕДНИЙ)

**Частота:** P03 (comet, F821 × 20+); P06, P13, P18  
**Проявление:** `Патч не затрагивает указанную ошибку – требуем повтор` → LLM генерирует патч снова → снова нерелевантен → цикл.  
**Связь:** Уже ограничено `irrelevant_streak=5` (MAX_BLOCKING_LLM_RETRIES). Проверить — работает ли cap.  
**Специальный случай F821:** импорт-патч добавляется в начало файла, а не на строку ошибки → validator считает нерелевантным.  
**Fix:** Для F821 (import-add патчи) считать релевантным если файл совпадает.  
**Статус:** Частично исправлен (cap есть). Edge case F821 не исправлен.

---

### [FIXED] SecurityScanner TokenizeError (КРИТИЧЕСКИЙ, исправлен до P08)

**Проявление:** `module 'tokenize' has no attribute 'TokenizeError'` — Python 3.13 убрал атрибут.  
**Fix:** `_TOKENIZE_ERROR = getattr(_tokenize, 'TokenizeError', _tokenize.TokenError)` в `analysis/security_scanner.py`.  
**Статус:** **ИСПРАВЛЕН** (до P08). Не проявлялся в P08-P17.

---

### [FIXED] target_error_still_present loop — нет per-file cap (КРИТИЧЕСКИЙ, исправлен до P08)

**Проявление:** P07 cxxheaderparser — TESP rejection × 24 на parser.py (PLY-grammar), цикл без выхода.  
**Fix:** `DecideStage._cap_file_if_tesp_exceeded` с `MAX_TESP_PER_FILE = 5`.  
**Статус:** **ИСПРАВЛЕН** (до P08). В P08-P17 TESP loops ≤ 5 per file.

---

## Паттерны качества

### Паттерн A: Unmasking (E999 fix → больше ошибок, P08/P09)
- P08 polymarket-apis: 424→657 (+233). Fixing E999 syntax error revealed 233 hidden lint errors.
- P09 pyladiescon-portal: 299→329 (+30). Same unmasking effect.
- Это ожидаемое поведение — не баг.

### Паттерн B: SECURITY false-positive на test-файлах
- P02 (×3), P05 (×4), P12 (×5). NET_DELTA regression при попытке fix `hardcoded_secret` в test_*.py.
- Test-файлы намеренно содержат плохой код. NET_DELTA protection правильно откатывает такие патчи.

### Паттерн C: Windows-specific attr-defined
- P16 (schedule): `os.tzset` не существует на Windows → attr-defined NR.
- Специфика Windows окружения.

### Паттерн D: Высокий NR в P07 (cxxheaderparser, 33 NR)
- 33 E999/PLY-grammar false-positives в parser.py — синтаксически корректный PLY DSL, но ruff считает E999.
- После TESP cap fix — bulk-skip после 5 попыток. 33 ошибок → NR.

---

### [BUG-7] EditSet.no_content — LLM возвращает пустой EditSet (СРЕДНИЙ)

**Частота:** P18 (tqdm/tqdm) × 5 для tqdm_monitor.py  
**Файл:** LLM response parser  
**Проявление:**
```
EditSet.to_unified_diff: нет контента для tqdm_monitor.py — пропускаем файл
LLM вернул пустой ответ
Патч ОТКЛОНЁН: tqdm_monitor.py (причина: empty_response)
```
**Следствие:** LLM возвращает валидный EditSet-объект (структура парсится), но без файловых изменений. Каждый вызов — пустой результат, 5 раз подряд. Каждый пустой вызов ~2 мин (медленный эндпоинт).  
**Fix:** Детектировать пустой EditSet до повторного вызова; считать как empty_response и skip после N попыток.  
**Статус:** НЕ ИСПРАВЛЕН.

---

## Причины PARTIAL (6/20)

| Проект | Причина | Баги |
|--------|---------|------|
| P05 MooshiMochi | Bash timeout 300s | O.15 × 6, NET_DELTA × 7, LLM empty × 3 |
| P07 cxxheaderparser | Bash timeout 300s | TESP × 24 (FIXED), TokenizeError (FIXED) |
| P17 keleshev/schema | LLM timeout (убит 15+ мин) | O.15 × 10+, NET_DELTA × 6 |
| P18 tqdm/tqdm | LLM timeout (убит 620s) | O.15 × 5, empty_response × 5, EditSet.no_content × 5 |
| P19 cookiecutter/cookiecutter | LLM timeout (убит 740s) | NET_DELTA × 5 (разные файлы) |
| P20 dateutil/dateutil | LLM timeout (убит 280s) | TimeoutError × 2, project_tree не загружен |

**Паттерн**: P17-P20 — четыре проекта подряд с LLM stall. DeepSeek эндпоинт системно недоступен в ночное время. BUG-4 (нет retry/backoff) является корневой причиной для 4 из 6 PARTIAL.

---

## Финальный анализ провалов

### Корневые причины PARTIAL по частоте

| Причина | Кол-во PARTIAL | Проекты |
|---------|---------------|---------|
| BUG-4: LLM timeout без retry | 4 | P17, P18, P19, P20 |
| BUG-1: O.15 patch merge | 2 (вклад) | P05, P07 (до фикса TESP) |
| BUG-3: NET_DELTA per-file loop | 2 (вклад) | P17, P19 |
| Bash timeout (5 мин лимит) | 2 | P05, P07 |

### Что работает хорошо (14 PASS)

- Pipeline завершается штатно при стабильном LLM (AdaptiveCC budget срабатывает корректно)
- NET_DELTA smart classification (regression/unmasked/uncertain) верно защищает от регрессий
- TESP cap (MAX_TESP_PER_FILE=5) предотвращает бесконечные циклы (зафиксировано с P08)
- SecurityScanner TokenizeError graceful-skip (зафиксировано с P08)
- UnicodeDecodeError в P06 обработан частично (проект всё равно PASS)

---

## Итог серии

```
Серия: control_post_improvements, 20 проектов
Статус: ЗАВЕРШЕНА

PASS:    14 / 20 (70%)
PARTIAL: 6  / 20 (30%)  — все из-за LLM stall или bash timeout
FAIL:    0  / 20 (0%)

Effective pass rate: 0.700

Критические баги для исправления (по приоритету):
  BUG-4 → LLM retry/backoff (4 PARTIAL вместо PASS)
  BUG-1 → O.15 patch newline (47%+ проектов затронуты)
  BUG-2 → _o15_retries per-error-sig
  BUG-3 → NET_DELTA per-file cap
  BUG-6 → cp1251 subprocess encoding
  BUG-5 → FileAntiLoop counter overflow
  BUG-7 → EditSet.no_content detection
```

*Финальный аудит завершён: 2026-06-14T03:42*
