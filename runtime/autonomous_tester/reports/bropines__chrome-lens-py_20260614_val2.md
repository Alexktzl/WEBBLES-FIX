# bropines/chrome-lens-py  [PASS]

- Дата: 2026-06-14
- Ошибок: 615 → 363
- ACCEPT: 1 (_DRIVEMETADATA F821)  |  NEEDS_REVIEW: 2  |  REJECT: 0
- Net-delta rollbacks: 1 (lens_overlay_content_metadata_pb2.py)
- Score: 3.0
- BUG-8: не триггерился (нет бинарных шрифтов; много py protobuf-файлов)
- EDGE-5: нет (1 rollback < cap=3)
