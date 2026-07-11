# dateutil/dateutil  [PARTIAL — LLM TIMEOUT]

- Дата: 2026-06-14
- Ошибок: неизвестно (процесс убит через 280s wall / 5.23s CPU)
- ACCEPT: 0  |  NEEDS_REVIEW: 0  |  REJECT: 0
- Score: 0.5
- Stop: llm_timeout (killed)
- LLM TimeoutError × 2 на первых же вызовах (120s timeout каждый)
- Project_tree никогда не был загружен — упал ещё в фазе первичного анализа
- ПАТТЕРН BUG-4: LLM endpoint не отвечает совсем (не только медленный)
- CPU: 5.23s / 280s wall = 1.9% — идентично P17/P18/P19 паттерну
