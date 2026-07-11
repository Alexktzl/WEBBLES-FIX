# ClimateImpactLab/dodola  [FAIL]

- Дата: 2026-06-22T22:03:07
- Ошибок: 21  →  21  (delta=0)
- ACCEPT: 0  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Циклов выполнено: 0 (статус COMPLETED, но ни одного цикла фикса не было запущено)
- Причина: no_fixes_applied. PreCleanup сломал синтаксис в 3 файлах (core.py, test_core.py, test_services.py) и пропустил запись. "file_error_signatures_before пуст – переключаемся на fallback по весам" — похоже, baseline не был зафиксирован нормально. current_total_errors=0 vs baseline_remaining=21 — расхождение, требует отдельного разбора.
