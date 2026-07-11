"""
2026-06-23 — P0 unsafe_accept (control series): фикс ОДНОЙ ошибки
import-not-found/import-untyped в malinkang/toggl2notion попутно стёр 3 из
5 имён из `from notionhub.client import ...` — независимо подтверждено,
что `ruff check --fix --select=F401` САМ убирает эти имена (RuffAutoFixStage,
не GeneratePatchStage/LLM). `RuffAutoFixStage._SAFE_CODES` включал F401,
хотя собственный докстринг модуля явно объявляет его "Deliberately excluded"
("removing imports can break re-exports / __all__ / type stubs").

Два фикса:
1. `core/stages/ruff_autofix_stage.py`: убран F401 из `_SAFE_CODES`.
2. `analysis/symbol_regression.py`: `check_symbol_regression` теперь
   отслеживает и импортированные имена (не только def/class) — любое
   исчезнувшее имя из `import`/`from...import` после патча помечает
   `ok=False` (та же безусловная политика, что уже у def/class), что в
   `ValidateStage` уводит патч в NEEDS_REVIEW вместо ACCEPT.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.symbol_regression import check_symbol_regression
from core.stages.ruff_autofix_stage import _SAFE_CODES


def test_f401_not_in_safe_codes():
    """Регрессия: F401 не должен быть в автофикс-наборе RuffAutoFixStage —
    его удаление ломает соседние имена в том же import-statement без
    проверки cross-file использования (re-export/__all__/type stubs)."""
    codes = [c for c in _SAFE_CODES.split(",") if c]
    assert "F401" not in codes


def test_missing_imported_name_flagged_as_regression():
    before = (
        "from notionhub.client import NotionHelperBase, TARGET_ICON_URL, "
        "TAG_ICON_URL, USER_ICON_URL, BOOKMARK_ICON_URL\n"
        "\n"
        "def f():\n"
        "    return NotionHelperBase()\n"
    )
    after = (
        "from notionhub.client import (  # type: ignore[import-not-found]\n"
        "    TARGET_ICON_URL,\n"
        "    NotionHelperBase,\n"
        ")\n"
        "\n"
        "def f():\n"
        "    return NotionHelperBase()\n"
    )
    reg = check_symbol_regression(before, after, "python")
    assert reg["ok"] is False
    assert set(reg["missing_imports"]) == {"TAG_ICON_URL", "USER_ICON_URL", "BOOKMARK_ICON_URL"}


def test_intact_import_list_not_flagged():
    before = "from x import a, b, c\n\ndef f():\n    return a, b, c\n"
    after = "from x import a, b, c  # type: ignore[import-not-found]\n\ndef f():\n    return a, b, c\n"
    reg = check_symbol_regression(before, after, "python")
    assert reg["ok"] is True
    assert reg["missing_imports"] == []


def test_renamed_via_asname_not_flagged_as_missing():
    """`import x as y` — должны сравниваться по `asname`, а не по
    исходному имени модуля (иначе любое добавление `as` ложно триггерило
    бы regression)."""
    before = "import numpy as np\n\ndef f():\n    return np.array([1])\n"
    after = "import numpy as np\n\ndef f():\n    return np.array([1, 2])\n"
    reg = check_symbol_regression(before, after, "python")
    assert reg["ok"] is True


def test_plain_import_missing_module_flagged():
    before = "import os\nimport sys\n\ndef f():\n    return os.getcwd()\n"
    after = "import os\n\ndef f():\n    return os.getcwd()\n"
    reg = check_symbol_regression(before, after, "python")
    assert reg["ok"] is False
    assert reg["missing_imports"] == ["sys"]


def test_non_python_language_unaffected():
    """Регресс для других языков не считает import — только defs/classes,
    как и раньше (нет общего AST-парсера import для rust/js/...)."""
    before = "fn foo() {}\n"
    after = "fn foo() {}\n"
    reg = check_symbol_regression(before, after, "rust")
    assert reg["ok"] is True
    assert reg.get("missing_imports", []) == []
