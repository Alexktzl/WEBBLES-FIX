# Аудит: Валидация BUG-8 и EDGE-5 (5 проектов)

**Дата:** 2026-06-14  
**Цель:** Проверить, что фиксы BUG-8 (shutil.copytree Errno 22) и EDGE-5 (per-file cap persistence) не ломают pipeline и работают корректно  
**Результат: 4/5 PASS, 1/5 PARTIAL — effective_pass_rate = 0.800**

---

## Сводка

| # | Проект | Статус | ACCEPT | NR | REJ | ND_rollbacks | Примечания |
|---|--------|--------|--------|----|-----|--------------|------------|
| V1 | hatamiarash7/CheckFiltering | PARTIAL | 0 | 1 | 0 | 2 | W503 — только NR |
| V2 | BasicWolf/hexagonal-architecture-django | PASS | 1 | 0 | 0 | 2 | settings.py hardcoded_secret |
| V3 | SynoCommunity/spkrepo | PASS | 1 | 1 | 0 | 0 | config.py hardcoded_secret |
| V4 | bropines/chrome-lens-py | PASS | 1 | 2 | 0 | 1 | F821 protobuf |
| V5 | fireantology/django-logtailer | PASS | 1 | 2 | 0 | 0 | E128 admin.py |

Итого: errors_found=916, accepted=4, needs_review=6, rejected=0, net_delta_rollbacks=5

---

## Анализ фиксов

### BUG-8: shutil.copytree [Errno 22] — ПОДТВЕРЖДЕНО

Ни в одном из 5 проектов pipeline не упал с copytree ошибкой. Проекты содержали:
- PNG изображения (V3 spkrepo статика, V5 django-logtailer)
- .mo бинарные locale файлы (V5)
- Сгенерированные protobuf .py файлы (V4)

Вывод: try/except в `_prepare_environment()` не ломает нормальный copy2 путь. Фикс прозрачный.

Примечание: woff2-файлы (исходный триггер в zksync-python) специально не тестировались — ни один из 5 проектов их не содержит. Fallback на `shutil.copy` протестирован только теоретически.

### EDGE-5: per-file NET_DELTA cap persistence — НЕ АКТИВИРОВАЛСЯ

Во всех 5 проектах количество NET_DELTA rollbacks на один файл не превысило 2 (cap = 3). Следовательно:
- `_net_delta_capped_files` ни разу не обновлялся
- Новый early-return код не выполнялся
- Но и не мешал

Фикс EDGE-5 не регрессировал ни одного из 5 прогонов. Функциональная верификация cap persistence отложена до прогонов с большим количеством ошибок на один файл (аналог R4/R8 из run10).

### Все предыдущие фиксы (BUG-2..BUG-7)

По результатам 5 прогонов:
- NET_DELTA cap логика работает нормально (2 rollbacks → нет spurious кэпа)
- O.15 per-sig ретри работает (нет зацикливания)
- LLM timeout стабилен (0 stalls)
- BUG-6 (cp1251): нет UnicodeDecodeError
- Остальные — без изменений

---

## Вывод

Валидация прошла успешно: 4/5 PASS, 1/5 PARTIAL (причина partial — только NR патчи, не баг).

**BUG-8 фикс:** Не ломает нормальный copytree. Прямая верификация woff2-fallback — только в следующем прогоне с Font Awesome или аналогичными проектами.

**EDGE-5 фикс:** Не регрессирует. Прямая верификация cap persistence — при следующем прогоне с ≥3 rollbacks на один файл.

**Следующий шаг:** Прогон на 10 проектах.
