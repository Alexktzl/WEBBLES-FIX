r"""
2026-06-25 (направление "rule-based расширение", learning_cases.jsonl):
E704 — 8 попыток через LLM, 0% успеха, при том что _py_e704_inline_def уже
покрывает 10 случаев. Расследование показало: для МНОГОСТРОЧНЫХ сигнатур
(частый паттерн @overload/Protocol-стабов — `def f(\n    args\n) -> T: ...`)
flake8 репортит E704 на ПОСЛЕДНЕЙ строке (`) -> T: ...`), которая не
начинается с `def` — старая проверка `^\s*def\s+` отбрасывала такие
случаи (return None), уходили в LLM, который тоже не справлялся.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.rule_based_fixer import RuleBasedFixer


def _run(content: str, line: int):
    fixer = RuleBasedFixer()
    return fixer._py_e704_inline_def(
        {"file": "t.py", "line": line, "code": "E704", "message": "x"}, content,
    )


def test_single_line_def_still_works_unchanged():
    """Регрессия: исходный (однострочный) случай не должен сломаться."""
    content = "def f(x): return x\n"
    out = _run(content, 1)
    assert out is not None
    new = out.edits[0].new
    assert new == "def f(x):\n    return x\n"


def test_multiline_overload_signature_inside_class(tmp_path=None):
    """Реальный кейс из click/core.py: многострочная сигнатура @t.overload
    внутри класса — E704 репортится на закрывающей строке."""
    content = (
        "class Foo:\n"
        "    @t.overload\n"
        "    def lookup_default(\n"
        "        self, name: str, call: t.Literal[True] = True\n"
        "    ) -> t.Any | None: ...\n"
    )
    out = _run(content, 5)  # строка ") -> t.Any | None: ..."
    assert out is not None, "правило должно найти многострочную сигнатуру def"
    new = out.edits[0].new
    assert new == "    ) -> t.Any | None:\n        ...\n"

    # Применяем правку и проверяем синтаксическую валидность.
    lines = content.splitlines(keepends=True)
    lines[4] = new
    patched = "".join(lines)
    ast.parse(patched)


def test_multiline_module_level_def_with_string_default():
    """Реальный кейс из click/decorators.py: многострочная сигнатура на
    модульном уровне, с строковым default-значением внутри списка
    параметров (проверяет string-aware depth-tracking при переносе)."""
    content = (
        "@t.overload\n"
        "def group(\n"
        '    name: str | None = "x", cls: None = None, **attrs: t.Any\n'
        ") -> t.Callable[[_AnyCallable], Group]: ...\n"
    )
    out = _run(content, 4)
    assert out is not None
    new = out.edits[0].new
    assert new == ") -> t.Callable[[_AnyCallable], Group]:\n    ...\n"

    lines = content.splitlines(keepends=True)
    lines[3] = new
    patched = "".join(lines)
    ast.parse(patched)


def test_multiline_async_def_signature():
    """async def тоже должен распознаваться при обратном сканировании."""
    content = (
        "class Foo:\n"
        "    @t.overload\n"
        "    async def lookup(\n"
        "        self, name: str\n"
        "    ) -> t.Any: ...\n"
    )
    out = _run(content, 5)
    assert out is not None
    new = out.edits[0].new
    assert new == "    ) -> t.Any:\n        ...\n"


def test_no_def_anywhere_returns_none():
    """Если в пределах 30 строк назад вообще нет def — должно тихо
    отступить (None), не выдумывать ложноположительный фикс."""
    content = "x = (\n    1 + 2\n)\n"
    out = _run(content, 3)
    assert out is None


def test_real_tenacity_protocol_stub_two_methods():
    """Полная репродукция реального файла из learning_cases.jsonl
    (tenacity/__init__.py) — два метода Protocol подряд, фикс ОДНОГО не
    должен зацепить другой (точечность правки)."""
    content = (
        "class _RetryDecorated(t.Protocol):\n"
        '    retry: "BaseRetrying"\n'
        "    statistics: dict[str, t.Any]\n"
        "\n"
        '    def retry_with(self, *args: t.Any, **kwargs: t.Any) -> "_RetryDecorated[P, R]": ...\n'
        "\n"
        "    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...\n"
    )
    out = _run(content, 5)
    assert out is not None
    new = out.edits[0].new
    assert "retry_with" in new
    assert "__call__" not in new
    lines = content.splitlines(keepends=True)
    lines[4] = new
    patched = "".join(lines)
    ast.parse(patched)
