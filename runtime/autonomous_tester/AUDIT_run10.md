# Аудит: Run-10 Series (10 проектов)

**Дата:** 2026-06-14  
**Цель:** После валидации BUG-2…BUG-7 — 10 реальных проектов для поиска новых багов и edge cases  
**Результат: 9/10 PASS, 1/10 FAIL — effective_pass_rate = 0.900**

---

## Сводка

| # | Проект | Статус | ACCEPT | NR | REJ | NET_Δ | Примечания |
|---|--------|--------|--------|----|-----|-------|------------|
| R1 | allenporter/pyrainbird | PASS | 1 | 4 | 0 | 3 cap✓ | BUG-3 cap сработал корректно |
| R2 | fsistemas/sql2json | PASS | 1 | 0 | 1 | 1 | COMPLETED: 20→0 ошибок |
| R3 | zksync-sdk/zksync-python | **FAIL** | 0 | 0 | 0 | 0 | **EDGE-4**: copytree crash woff2 |
| R4 | themonomers/tesla | PASS | 2 | 16 | 4 | 13 | **EDGE-5**: BUG-3 cap fires 3+4 раза |
| R5 | Shaffer-Softworks/hyperhdr-ha | PASS | 1 | 0 | 0 | 1 | irrelevant_streak=5 x3 (OK) |
| R6 | swileran/v2ray-config-collector | PASS | 1 | 1 | 1 | 0 | 1x empty_response |
| R7 | MartinHjelmare/leicacam | PASS | 2 | 1 | 0 | 0 | 4→1 ошибки, чистый прогон |
| R8 | Dirac-Robot/comet | PASS | 1 | 1 | 0 | 5 | **EDGE-5**: BUG-3 cap 3+4 (повтор) |
| R9 | Cvolton/GDHistory | PASS | 1 | 1 | 0 | 0 | 6026 ошибок, стабильно |
| R10 | AliAkhtari78/SpotifyScraper | PASS | 1 | 6 | 0 | 2 | hardcoded_secret NR x3 файла |

Итого: errors_found=10553, accepted=11, needs_review=30, rejected=6, net_delta_rollbacks=27

---

## Новые баги и edge cases

### BUG-8 / EDGE-4: shutil.copytree [Errno 22] на бинарных файлах (Windows) [НОВЫЙ — КРИТИЧЕСКИЙ]

**Проект:** zksync-sdk/zksync-python  
**Файл pipeline:** `core/pipeline_engine.py` — `_prepare_environment()` → `shutil.copytree()`  
**Симптом:** `shutil.Error: [Errno 22] Invalid argument` при копировании `fa-solid-400.woff2`  
**Причина:** На Windows `shutil.copytree` падает с errno 22 при определённых бинарных файлах (FontAwesome woff2). Вероятнее всего — проблема с метаданными/атрибутами файла при copy2.  
**Эффект:** Pipeline crash, exit_code=1, проект полностью пропущен.  
**Частота:** 1/10 (10%), но будет стабильно на любом проекте с Font Awesome или подобными binary assets.  
**Приоритет:** HIGH (вызывает FAIL)  
**Фикс:** В `_prepare_environment()` добавить `ignore=shutil.ignore_patterns(...)` для бинарных расширений, ИЛИ обернуть `shutil.copytree` в try/except и при ошибке использовать `copy_function=shutil.copy` вместо `shutil.copy2` (который копирует метаданные и может вызвать Errno 22).

### EDGE-5: BUG-3 per-file cap срабатывает, но не блокирует все последующие rollbacks

**Проекты:** R4 (themonomers/tesla configutil.py), R8 (Dirac-Robot/comet run_benchmark_hard.py)  
**Симптом:** BUG-3 cap логирует `3 rollbacks — bulk-skip` затем снова `4 rollbacks — bulk-skip` для того же файла.  
**Причина:** `bulk-skip` устанавливает `processed_errors[sig] = 3` только для текущих `context.current_errors`. Если pipeline после rollback обнаруживает новые/другие ошибки в том же файле (другие сигнатуры, не бывшие в очереди в момент cap), они не попадают под bulk-skip.  
**Эффект:** 1-2 дополнительных rollback сверх лимита. Не критично (pipeline не падает), но менее эффективно.  
**Приоритет:** MEDIUM (частичная эффективность BUG-3)  
**Фикс:** После cap — добавить файл в `_skipped_files` множество (глобально для всей сессии), и в начале NET_DELTA блока проверять это множество.

---

## Подтверждённые фиксы (все работают)

- **BUG-2** (O.15 per-sig): каждая сигнатура получает max 1 retry → NEXT_ERROR. R3-R10: O.15 встречается но не зацикливается.
- **BUG-3** (NET_DELTA per-file cap): cap срабатывает и уменьшает rollback-и. EDGE-5 — уточнение, не провал.
- **BUG-4** (LLM timeout retry): 0 LLM stalls в 10 проектах (vs 4/6 PARTIAL в control series от stall).
- **BUG-5** (FileAntiLoop counter): нет переполнений.
- **BUG-6** (cp1251 encoding): нет UnicodeDecodeError.
- **BUG-7** (EditSet basename): нет crash от path mismatch. Anchor mismatch — отдельный edge case.
- **SecurityScanner graceful-skip**: R4 tesla — `SecurityScanner failed: unindent...` логируется, pipeline продолжает (не падает).
- **max_irrelevant_retries=5**: R5 hyperhdr-ha — streak=5 x3, корректно.

---

## Наблюдения без новых фиксов (known / expected)

- **BUG-1 (O.15 LLM prompt)**: 16 случаев в validation_5, встречается в run10. Деферред.
- **EDGE-1 (anchor mismatch special chars)**: utils.py regex backslash — встречается в V3 и V5, но редко. LOW priority.
- **EDGE-2 (empty_response)**: R6 x1 — pipeline корректно отклоняет и продолжает.
- **file_error_signatures_before пуст**: R2 — fallback по весам, пайплайн справляется.

---

## Вывод

Найден 1 новый критический баг (EDGE-4/BUG-8 — copytree crash) и 1 refinement (EDGE-5 — BUG-3 partial effectiveness).

**Следующий шаг:** Исправить BUG-8, проверить на 5 проектах, затем 10 проектов. Если новых проблем нет — финальный отчёт.
