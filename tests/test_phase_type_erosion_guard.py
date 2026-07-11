"""
O.18 — анти-эрозия типов (2026-06-23, control series Skyscanner/pycfmodel):
LLM «решала» mypy-ошибки union-attr/operator/assignment/return-value через
Any/cast(Any, ...) вместо содержательной правки. Не относится к
import-not-found/import-untyped/untyped-decorator — там `# type: ignore`
единственно доступный детерминированный фикс (см. GeneratePatchStage.
_UNFIXABLE_MYPY_CODES), это не эрозия.

Также покрывает попутно найденный gap: DecideStage проверял только
missing_defs/missing_classes из symbol_regression, не missing_imports
(добавленный отдельным более ранним фиксом той же сессии) — патч мог
терять импорты, попадать в metadata, но НЕ уходить в NEEDS_REVIEW.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from analysis.type_erosion_guard import check_type_erosion, ALLOWED_EROSION_CODES


def test_any_annotation_flagged_for_fixable_code():
    before = "def f(x):\n    result = {}\n    return result\n"
    after = "def f(x):\n    result: Dict[Any, Any] = {}\n    return result\n"
    out = check_type_erosion(before, after, "assignment")
    assert out["ok"] is False
    assert "any_type" in out["markers"]


def test_cast_any_flagged_for_fixable_code():
    before = "value = compute()\n"
    after = "value = cast(Any, compute())\n"
    out = check_type_erosion(before, after, "operator")
    assert out["ok"] is False
    assert "cast_any" in out["markers"]


def test_return_any_flagged_for_return_value_code():
    before = "def f() -> IPv4Network:\n    return cls(x)\n"
    after = "def f() -> Any:\n    return cls(x)\n"
    out = check_type_erosion(before, after, "return-value")
    assert out["ok"] is False
    assert "any_type" in out["markers"]


def test_import_not_found_with_type_ignore_is_allowed():
    """Детерминированный _make_type_ignore_patch — не эрозия, штатный фикс."""
    before = "import numpy as np\n"
    after = "import numpy as np  # type: ignore[import-not-found]\n"
    out = check_type_erosion(before, after, "import-not-found")
    assert out["ok"] is True
    assert out["reason"] == "allowed_unfixable_code"


def test_import_untyped_with_type_ignore_is_allowed():
    before = "import pandas\n"
    after = "import pandas  # type: ignore[import-untyped]\n"
    out = check_type_erosion(before, after, "import-untyped")
    assert out["ok"] is True


def test_allowed_codes_set_matches_generate_patch_stage():
    """Синхронизация с GeneratePatchStage._UNFIXABLE_MYPY_CODES — если там
    список изменится, этот тест должен напомнить обновить и здесь."""
    assert ALLOWED_EROSION_CODES == frozenset(
        {"import-untyped", "import-not-found", "untyped-decorator"}
    )


def test_genuine_fix_without_erosion_markers_is_ok():
    before = "def f(x):\n    return x.group(1)\n"
    after = "def f(x):\n    match = x\n    if match:\n        return match.group(1)\n    return None\n"
    out = check_type_erosion(before, after, "union-attr")
    assert out["ok"] is True
    assert out["markers"] == []


def test_type_ignore_for_fixable_code_still_flagged():
    """`# type: ignore` без кода-исключения (не import-*) — тоже эрозия,
    не только Any/cast."""
    before = "x = foo()\n"
    after = "x = foo()  # type: ignore\n"
    out = check_type_erosion(before, after, "arg-type")
    assert out["ok"] is False
    assert "type_ignore_comment" in out["markers"]


def test_e704_line_split_relocating_existing_marker_is_not_erosion():
    """control series 2026-07-01 (tenacity — 0 ACCEPT, все патчи в NEEDS_REVIEW):
    E704-фиксер разносит `def f(): ...  # type: ignore` на две строки —
    line-level diff видел старую строку как removed и ОБЕ новые как added,
    включая ту же подстроку `# type: ignore`, перенесённую без изменений —
    и ложно засчитывал это как новую эрозию. Маркер не НОВЫЙ, просто
    переехал на другую строку — не должно флагаться."""
    before = "def f(x: int) -> Any: return x  # type: ignore[override]\n"
    after = (
        "def f(x: int) -> Any:  # type: ignore[override]\n"
        "    return x\n"
    )
    out = check_type_erosion(before, after, "E704")
    assert out["ok"] is True, f"перенос строки не должен считаться эрозией: {out}"
    assert out["markers"] == []


def test_e704_line_split_that_also_adds_new_any_is_still_flagged():
    """Контрольный случай: если ПОПУТНО с переносом строки добавляется
    НОВЫЙ маркер (не просто перенесённый существующий) — эрозия по-прежнему
    должна ловиться."""
    before = "def f(x: int) -> int: return x  # type: ignore[override]\n"
    after = (
        "def f(x: int) -> Any:  # type: ignore[override]\n"
        "    return x\n"
    )
    out = check_type_erosion(before, after, "return-value")
    assert out["ok"] is False
    assert "any_type" in out["markers"]
