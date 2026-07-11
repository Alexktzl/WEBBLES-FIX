# hatamiarash7/CheckFiltering  [PARTIAL]

- Дата: 2026-06-14
- Ошибок: 101 → 13 (delta=88)
- ACCEPT: 0  |  NEEDS_REVIEW: 1  |  REJECT: 0
- Net-delta rollbacks: 2 (test_check.py)
- Score: 1.5
- Причина: all_needs_review (W503 — только NR, нет ACCEPT)
- BUG-8: не триггерился (нет бинарных шрифтов)
- EDGE-5: нет (2 rollbacks < cap=3)
