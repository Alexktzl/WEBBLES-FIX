# ellwise/kedro-light  [FAIL]

- Дата: 2026-06-23T02:22:31
- Ошибок: 16  →  16  (delta=0)
- ACCEPT: 0  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Циклов выполнено: 0, статус COMPLETED
- Причина: no_fixes_applied. `file_error_signatures_before пуст – переключаемся на fallback по весам` → fallback выбрал 0 кандидатов. ВАЖНО: повторяет находку из ClimateImpactLab/dodola (предыдущая серия), но здесь НЕТ PreCleanup-строк в логе вообще — значит баг не привязан к порче файлов PreCleanup, шире, чем предполагалось. Требует отдельного расследования: что именно оставляет `file_error_signatures_before` пустым на маленьких проектах (4 .py файла) с реальными ошибками.
