# blackadad/paper-scraper  [PASS] — Validation V5/5

- Дата: 2026-06-14
- Серия: validation_post_bug_fixes
- Ошибок: 981 → 630 (delta=+351, ошибок стало меньше)
- ACCEPT: 1  |  NEEDS_REVIEW: 2  |  REJECT: 6
- NET_DELTA rollbacks: 0  |  NET_DELTA NR: 1
- Score: 4.0
- Стоп: runtime_budget (674s >= 90s)
- Статус пайплайна: OK (exit_code=0)

## BUG-наблюдения

- O.15 x9 (scraper.py x3 + lib.py x6): BUG-1 (prompt) не устранён. 
  BUG-2 (per-sig retries) работает — каждая сигнатура получает 1 retry, затем NEXT_ERROR.
  Множественные O.15 — это разные error signatures, не одна зацикленная.
- anchor не найден для utils.py:165,167 (regex с escape-символами): новый edge case —
  LLM генерирует anchor с backslash, который не совпадает с реальной строкой файла.
- 6x empty_response rejections: LLM периодически возвращает пустой ответ
- NET_DELTA: 0 rollbacks, 1 NR — корректно
- Патч TC006 paperscraper/utils.py — ACCEPT (успешно)
