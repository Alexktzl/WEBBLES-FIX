"""
Regress M1+M6 (PROJECT_AUDIT_REPORT 2026-07-01).

M1: аудит проверял только файлы с ошибками в baseline — побочный файл
многофайлового патча (или accepted-файл вне sigs-карты) с потерянными
символами проходил мимо последнего рубежа.

M6: снапшоты без segment_start/segment_end хранят полный файл —
_find_problem_segments теперь возвращает полный откат вместо вставки
полного текста в диапазон строк.

Запуск: python tests/test_phase_audit_union_coverage.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_for_test_union", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AuditManager


def _write(d: Path, fname: str, content: str) -> Path:
    (d / fname).parent.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(content, encoding="utf-8")
    return d


def _mk_audit(project_dir, after_errors):
    AuditManager = _load_audit()
    analyzer = MagicMock()
    analyzer.analyze = MagicMock(return_value=after_errors)
    holder = {"ctx": None}
    am = AuditManager(
        project_path=project_dir, language="python", analyzer=analyzer,
        classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
        context_getter=lambda: holder["ctx"],
    )
    return am, holder


def _mk_context(file_sigs_before, working_path, snapshots=None):
    ctx = MagicMock()
    ctx.metadata = {"file_error_signatures_before": file_sigs_before,
                    "patch_snapshots": snapshots or []}
    ctx.working_path = working_path
    return ctx


# --- 1. M1: побочный файл (в снапшотах, но не в sigs-карте) с потерей def → failed ---
def test_side_file_with_symbol_loss_caught():
    fmain = "src/main.py"
    fside = "src/side.py"
    side_original = "def keep(x):\n    return x\n\ndef lost(y):\n    return y\n"
    side_damaged = "def keep(x):\n    return x\n"

    project_dir = _write(Path(tempfile.mkdtemp()), fmain, "def m():\n    pass\n")
    _write(project_dir, fside, side_original)
    working_dir = _write(Path(tempfile.mkdtemp()), fmain, "def m():\n    pass\n")
    _write(working_dir, fside, side_damaged)

    am, holder = _mk_audit(project_dir, after_errors=[])
    # sigs-карта знает только про main; side попал в patch_snapshots
    holder["ctx"] = _mk_context(
        {fmain: set()}, working_dir,
        snapshots=[{"file": fside, "error_line": 1,
                    "original_content": side_original,
                    "patched_content": side_damaged}],
    )

    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is False, (
        f"M1: потеря def lost в побочном файле обязана детектироваться: {res}"
    )
    assert fside in res["failed_segments"], res
    assert fmain in res["passed_files"], res


# --- 2. M1: чистый побочный файл без потерь → passed, ничего не ломаем ---
def test_side_file_clean_passes():
    fside = "src/side.py"
    content = "def keep(x):\n    return x\n"
    project_dir = _write(Path(tempfile.mkdtemp()), fside, content)
    working_dir = _write(Path(tempfile.mkdtemp()), fside, content + "# formatted\n")

    am, holder = _mk_audit(project_dir, after_errors=[])
    holder["ctx"] = _mk_context(
        {}, working_dir,
        snapshots=[{"file": fside, "error_line": 1,
                    "original_content": content}],
    )
    # ПУСТАЯ sigs-карта уводит в fallback-аудит — поэтому даём карту с
    # другим файлом, чтобы остаться на основном пути.
    fmain = "src/main.py"
    _write(project_dir, fmain, "def m():\n    pass\n")
    _write(working_dir, fmain, "def m():\n    pass\n")
    holder["ctx"] = _mk_context(
        {fmain: set()}, working_dir,
        snapshots=[{"file": fside, "error_line": 1, "original_content": content}],
    )

    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is True, res
    assert fside in res["passed_files"], res


# --- 3. M6: снапшот без segment-границ → полный откат, не вставка в диапазон ---
def test_boundless_snapshot_full_rollback():
    AuditManager = _load_audit()
    am, _ = _mk_audit(Path(tempfile.mkdtemp()), after_errors=[])
    full_file = "line1\nline2\nline3\n"
    segments = am._find_problem_segments(
        "src/app.py",
        new_sigs={("src/app.py", "E999", "boom")},
        file_errors=[{"file": "src/app.py", "code": "E999", "message": "boom", "line": 2}],
        snapshots=[{"file": "src/app.py", "error_line": 2,
                    "original_content": full_file}],
    )
    assert segments == [(1, 0, "")], (
        f"M6: снапшот без границ обязан давать полный откат, получили {segments}"
    )


# --- 4. M6: снапшот С явными границами по-прежнему даёт сегментный откат ---
def test_bounded_snapshot_keeps_segment():
    am, _ = _mk_audit(Path(tempfile.mkdtemp()), after_errors=[])
    segments = am._find_problem_segments(
        "src/app.py",
        new_sigs={("src/app.py", "E999", "boom")},
        file_errors=[{"file": "src/app.py", "code": "E999", "message": "boom", "line": 5}],
        snapshots=[{"file": "src/app.py", "error_line": 5,
                    "segment_start": 4, "segment_end": 6,
                    "original_content": "seg\n"}],
    )
    assert segments == [(4, 6, "seg\n")], segments


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
    print(f"\nAudit union coverage: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
