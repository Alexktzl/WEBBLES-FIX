"""
Regress H3 (PROJECT_AUDIT_REPORT 2026-07-01): детерминированные guard-ы
финального аудита обязаны фейлиться «закрыто». Раньше исключение внутри
символьной проверки audit_run (например, сбой чтения файла) только
логировалось — файл продолжал путь и при непроросших lint-сигнатурах уходил
в passed_files, т.е. любой сбой ЧТЕНИЯ выключал последний рубеж защиты
(класс «silent exception» из P0 unsafe_accept).

Запуск: python tests/test_phase_audit_fail_closed.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_audit_module():
    spec = importlib.util.spec_from_file_location(
        "audit_for_test_failclosed", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(d: Path, fname: str, content: str) -> Path:
    (d / fname).parent.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(content, encoding="utf-8")
    return d


def _mk(mod, project_dir, after_errors):
    analyzer = MagicMock()
    analyzer.analyze = MagicMock(return_value=after_errors)
    holder = {"ctx": None}
    am = mod.AuditManager(
        project_path=project_dir, language="python", analyzer=analyzer,
        classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
        context_getter=lambda: holder["ctx"],
    )
    return am, holder


def _ctx(file_sigs_before, working_path):
    ctx = MagicMock()
    ctx.metadata = {"file_error_signatures_before": file_sigs_before,
                    "patch_snapshots": []}
    ctx.working_path = working_path
    return ctx


# --- 1. Исключение в символьной проверке → файл failed, не passed ---
def test_symbol_check_exception_fails_file():
    mod = _load_audit_module()
    fname = "src/app.py"
    content = "def f():\n    pass\n"
    project_dir = _write(Path(tempfile.mkdtemp()), fname, content)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, content + "# patched\n")

    def _boom(*a, **k):
        raise RuntimeError("simulated read/parse failure")

    mod.check_symbol_regression = _boom  # символьная проверка «падает»

    am, holder = _mk(mod, project_dir, after_errors=[])
    holder["ctx"] = _ctx({fname: set()}, working_dir)

    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is False, (
        f"H3: непроверенный файл не может пройти аудит (fail-closed): {res}"
    )
    assert fname in res["failed_segments"], res
    assert fname not in res["passed_files"], res


# --- 2. Не-UTF8 байты в файле не роняют проверку (errors='replace') ---
def test_non_utf8_file_still_checked():
    mod = _load_audit_module()
    fname = "src/app.py"
    project_dir = Path(tempfile.mkdtemp())
    working_dir = Path(tempfile.mkdtemp())
    original = "def keep(x):\n    return x\n\ndef lost(y):\n    return y\n# копейка\n"
    damaged = "def keep(x):\n    return x\n# копейка\n"
    for d, text in ((project_dir, original), (working_dir, damaged)):
        p = d / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("cp1251"))  # не-UTF8 кириллица

    am, holder = _mk(mod, project_dir, after_errors=[])
    holder["ctx"] = _ctx({fname: set()}, working_dir)

    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is False, (
        f"потеря def lost в cp1251-файле обязана детектироваться: {res}"
    )
    assert fname in res["failed_segments"], res


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
    print(f"\nAudit fail-closed: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
