# ol-iver/denonavr  [PASS]

- Дата: 2026-06-15T00:10:09
- Ошибок: 176  →  206  (delta=-30, рост из-за новых ошибок после патчей)
- ACCEPT: 13  |  NEEDS_REVIEW: 17  |  REJECT: 0
- Score: 35.5
- Runtime: ~600s (project_timeout)

## Заметки

- 13 уникальных ACCEPT — хороший результат (E713 not-in, structured_llm)
- Bug B fix подтверждён: дублей нет (outer loop остановился правильно по dup-check)
- Рост ошибок 176→206: NET_DELTA regression при попытке fix ssdp.py откатился, но patched файлы вероятно добавили новые ruff-ошибки
- 17 NEEDS_REVIEW — потенциал для ручного анализа
