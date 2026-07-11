"""
2026-06-25 (направление "rule-based расширение", learning_cases.jsonl):
E402 — 34 попытки через LLM, 88% успеха, НО реальные кейсы в основном —
намеренный паттерн `sys.path.insert(...); import x` (Sphinx conf.py) или
`pytest.importorskip(...); import x` — перенос импорта в начало файла в
ЭТИХ случаях СЛОМАЛ БЫ логику (импорт зависит от предшествующего вызова).

Дерево решений: если ВСЁ, что предшествует импорту — docstring/другие
импорты/literal-only assignment — перенос БЕЗОПАСЕН. Если встречается
ЛЮБОЙ вызов функции/метода — добавляем # noqa: E402 вместо переноса.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.rule_based_fixer import RuleBasedFixer


def _run(content: str, line: int):
    fixer = RuleBasedFixer()
    return fixer._py_e402_module_import_not_at_top(
        {"file": "t.py", "line": line, "code": "E402", "message": "x"}, content,
    )


def test_sys_path_insert_gets_noqa_not_reorder():
    """Реальный кейс из Sphinx conf.py: sys.path.insert перед import —
    перенос наверх СЛОМАЛ БЫ резолвинг импорта, должен остаться noqa."""
    content = (
        "import os\n"
        "import sys\n"
        "\n"
        "sys.path.insert(0, os.path.abspath('..'))\n"
        "\n"
        "import webex_bot\n"
        "\n"
        "# -- General configuration --\n"
    )
    out = _run(content, 6)
    assert out is not None
    new = out.edits[0].new
    assert new == "import webex_bot  # noqa: E402\n"


def test_pytest_importorskip_gets_noqa_not_reorder():
    """Реальный кейс: pytest.importorskip() перед условным импортом —
    тоже намеренный паттерн, перенос недопустим."""
    content = (
        "from __future__ import annotations\n"
        "\n"
        "import pytest\n"
        "\n"
        'pytest.importorskip("hypothesis_crosshair_provider")\n'
        "\n"
        "from hypothesis import given\n"
    )
    out = _run(content, 7)
    assert out is not None
    new = out.edits[0].new
    assert new == "from hypothesis import given  # noqa: E402\n"


def test_literal_constant_between_imports_is_safely_reordered():
    """Реальный кейс из torch-проекта: константа-литерал (tuple чисел)
    между двумя импортами — безопасно переставить, побочных эффектов нет."""
    content = (
        "from torch import Tensor\n"
        "\n"
        "IMAGENET_DEFAULT_MEAN = (0.485, 0.456, 0.406)\n"
        "IMAGENET_DEFAULT_STD = (0.229, 0.224, 0.225)\n"
        "from torchvision import transforms\n"
        "\n"
        "\n"
        "def f():\n"
        "    pass\n"
    )
    out = _run(content, 5)
    assert out is not None
    new = out.edits[0].new
    ast.parse(new)
    lines = new.splitlines()
    assert lines[0] == "from torch import Tensor"
    assert lines[1] == "from torchvision import transforms"
    assert "IMAGENET_DEFAULT_MEAN" in new
    assert "IMAGENET_DEFAULT_STD" in new
    # данные после переноса не потерялись
    assert new.count("from torchvision import transforms") == 1


def test_call_result_assignment_gets_noqa_not_reorder():
    """x = compute_something() перед импортом — RHS не литерал (вызов),
    порядок может иметь значение → noqa, не перенос."""
    content = (
        '"""Module docstring."""\n'
        "\n"
        "x = compute_something()\n"
        "\n"
        "import os\n"
    )
    out = _run(content, 5)
    assert out is not None
    new = out.edits[0].new
    assert new == "import os  # noqa: E402\n"


def test_docstring_only_then_dunder_version_is_safely_reordered():
    """__version__ = "1.0.0" (строковый литерал) после docstring,
    перед импортом — безопасный перенос (классический pattern)."""
    content = (
        '"""Module docstring."""\n'
        "\n"
        '__version__ = "1.0.0"\n'
        "\n"
        "import os\n"
        "\n"
        "x = os.getcwd()\n"
    )
    out = _run(content, 5)
    assert out is not None
    new = out.edits[0].new
    ast.parse(new)
    lines = new.splitlines()
    assert lines[0] == '"""Module docstring."""'
    assert lines[1] == "import os"
    assert '__version__ = "1.0.0"' in new


def test_already_has_noqa_returns_none():
    """Если noqa уже стоит — не трогаем (нет смысла, не дублируем)."""
    content = (
        "x = setup_call()\n"
        "\n"
        "import os  # noqa: E402\n"
    )
    out = _run(content, 3)
    assert out is None


def test_unparseable_file_returns_none():
    content = "def f(:\n    import os\n"
    out = _run(content, 2)
    assert out is None


def test_import_not_found_at_reported_line_returns_none():
    """Если на указанной строке нет реального import-узла (несоответствие
    данных) — тихо отступаем, не выдумываем."""
    content = "x = 1\nimport os\n"
    out = _run(content, 1)  # строка 1 — не import
    assert out is None
