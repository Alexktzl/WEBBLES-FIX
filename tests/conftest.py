"""
Часть `tests/` написана в стиле «самостоятельный скрипт»: модуль на верхнем
уровне делает `sys.exit(0 if passed == total else 1)` после серии ручных
`check(...)`, рассчитывая на запуск `python tests/test_x.py`, а не на pytest.

При коллекции через `pytest tests/` это убивает её целиком: импорт такого
модуля вызывает `sys.exit()` прямо во время сбора тестов pytest'ом и
получается `INTERNALERROR`, маскирующий результаты ВСЕХ остальных файлов
(см. project_tech_debt_backlog в памяти, пункт 1, зафиксировано 2026-06-19).

Это НЕ миграция этих 56 файлов на pytest-assertions (отдельная крупная
задача, делать её мимоходом — риск). Это safety-net: pytest пропускает при
коллекции файлы с module-level `sys.exit(`, остальные тесты продолжают
собираться и запускаться нормально. Сами self-running файлы по-прежнему
работают как раньше через `python tests/test_x.py`.
"""

import re
from pathlib import Path

_SELF_RUN_SYS_EXIT = re.compile(r"^sys\.exit\(", re.MULTILINE)


def pytest_ignore_collect(collection_path, config):
    path = Path(str(collection_path))
    if path.suffix != ".py" or not path.name.startswith("test_"):
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if _SELF_RUN_SYS_EXIT.search(text):
        return True
    return None
