"""
2026-07-07 (перемер httpx, encode/httpx): `_py_remove_duplicate_lines`
(fixers/rule_based_fixer.py, PreCleanup pass 2 — несмежные дубли def/class)
стёр property-ГЕТТЕР `def request` в httpx/_exceptions.py и httpx/_models.py:
геттер (@property) и сеттер (@request.setter) — оба FunctionDef с именем
"request" в одном классе, группировка `{kind}::{name}` посчитала их дублями,
keep-last удалил геттер. Дальше каскад: фантомный F821 "undefined name
'request'" (порождён самим движком) -> мусорный "фикс" из memory
(@request.setter -> @Request.setter) -> ACCEPT по count-гейту -> финальный
аудит верно откатил файл (класс UNCHANGED_FILE, сожжённый бюджет
FileAntiLoop на фантомных ошибках).

Тот же класс бага, что @overload-инцидент agronholm/anyio 2026-06-25
(см. tests/test_phase_precleanup_duplicate_def_keep_last.py) — легитимное
разделение одного имени несколькими def, только через дескрипторный
протокол property вместо typing.overload.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from fixers.rule_based_fixer import RuleBasedFixer


def _run(content: str):
    fixer = RuleBasedFixer()
    return fixer._py_remove_duplicate_lines(
        {"file": "t.py", "line": 0, "code": "DUPLICATE_LINES", "message": ""}, content,
    )


def test_property_getter_setter_deleter_all_survive():
    """@property / @x.setter / @x.deleter одного имени в одном классе —
    легитимный дескрипторный протокол, не дубли. Все три def должны
    остаться."""
    src = (
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return self._x\n"
        "\n"
        "    @x.setter\n"
        "    def x(self, value):\n"
        "        self._x = value\n"
        "\n"
        "    @x.deleter\n"
        "    def x(self):\n"
        "        del self._x\n"
    )
    out = _run(src)
    assert out is None, (
        "правило не должно трогать файл — геттер/сеттер/делитер "
        "легитимно делят одно имя через property"
    )


def test_cached_property_single_survives():
    """Одиночный @functools.cached_property не должен затрагиваться
    (нет второго вхождения имени — контроль, что декоратор распознаётся
    и не ломает обычный однократный дедуп-путь)."""
    src = (
        "import functools\n"
        "\n"
        "class C:\n"
        "    @functools.cached_property\n"
        "    def x(self):\n"
        "        return 42\n"
    )
    out = _run(src)
    assert out is None


def test_real_duplicate_without_decorators_still_deduped():
    """Контроль: настоящие дубли БЕЗ декораторов в том же классе
    по-прежнему дедупятся keep-last (существующее поведение не
    сломано новой проверкой на property-декораторы)."""
    src = (
        "class C:\n"
        "    def helper(self):\n"
        "        return 'old'\n"
        "\n"
        "    def helper(self):\n"
        "        return 'new'\n"
    )
    out = _run(src)
    assert out is not None
    new_content = out.edits[0].new
    ast.parse(new_content)
    assert new_content.count("def helper(") == 1
    assert "'new'" in new_content
    assert "'old'" not in new_content


def test_httpx_request_property_regression():
    """Регресс-сценарий инцидента: фрагмент по мотивам
    httpx/_exceptions.py — @property def request + @request.setter
    def request. Оба должны выжить, F821-предпосылка (пропавший геттер)
    не должна создаваться."""
    src = (
        "class HTTPError(Exception):\n"
        "    def __init__(self, message):\n"
        "        super().__init__(message)\n"
        "        self._request = None\n"
        "\n"
        "    @property\n"
        "    def request(self):\n"
        "        if self._request is None:\n"
        "            raise RuntimeError('The .request property has not been set.')\n"
        "        return self._request\n"
        "\n"
        "    @request.setter\n"
        "    def request(self, request):\n"
        "        self._request = request\n"
    )
    out = _run(src)
    assert out is None, (
        "геттер и сеттер 'request' — легитимная пара, файл не должен "
        "меняться (геттер не должен пропадать)"
    )
