"""
Regress M2 (PROJECT_AUDIT_REPORT 2026-07-01): ApplyPatchStage._same_rel
использовал голый endswith — `utils.py` отождествлялся с `tests/utils.py` и
`src/utils.py`: патч на чужой одноимённый файл проходил проверку «затрагивает
целевой файл», блокировка .md/.json снималась не для того файла.

Фикс: суффиксное совпадение только по границе сегмента пути.

Запуск: python tests/test_phase_same_rel_segment_boundary.py
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

from core.stages.apply_patch_stage import ApplyPatchStage  # noqa: E402

_same = ApplyPatchStage._same_rel


def test_exact_match():
    assert _same("src/utils.py", "src/utils.py")


def test_separator_and_dot_normalization():
    assert _same("src\\utils.py", "src/utils.py")
    assert _same("./src/utils.py", "src/utils.py")


def test_git_prefix_suffix_on_boundary():
    # a/-префикс диффа: полный путь как суффикс по границе сегмента
    assert _same("a/src/utils.py", "src/utils.py")
    assert _same("repo/src/utils.py", "src/utils.py")


def test_basename_does_not_match_other_dirs():
    # M2: одноимённый файл в другой директории — НЕ тот же файл
    assert not _same("tests/utils.py", "utils.py"), (
        "basename не должен матчить файл из другой директории"
    )
    assert not _same("utils.py", "tests/utils.py")
    assert not _same("src/utils.py", "tests/utils.py")


def test_partial_segment_does_not_match():
    # "b_utils.py".endswith("utils.py") — истина для голого endswith,
    # но это РАЗНЫЕ файлы.
    assert not _same("src/b_utils.py", "utils.py")
    assert not _same("src/b_utils.py", "src/utils.py")


def test_empty_inputs():
    assert not _same("", "utils.py")
    assert not _same("utils.py", "")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n_same_rel segment boundary: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
