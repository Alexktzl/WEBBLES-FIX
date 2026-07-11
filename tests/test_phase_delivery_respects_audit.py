"""
Regress C3+H2 (PROJECT_AUDIT_REPORT 2026-07-01): доставка в оригинал обязана
уважать вердикт финального аудита.

До фикса:
  * `restore_failed_segments` глотала исключения без возврата статуса;
  * шаг 3 `_copy_passed_files_to_original` (ACCEPT-fallback) копировал файлы
    из failed_segments, потому что полностью откачённые файлы не попадали в
    `copied` — при провале restore повреждённый файл уезжал в оригинал ПОСЛЕ
    корректной детекции аудитом;
  * файл, исчезнувший из sandbox, молча пропускался audit_run (ложный успех
    метрик).

Запуск: python tests/test_phase_delivery_respects_audit.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_engine import PipelineEngine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_for_test_delivery", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AuditManager


def _engine(project: Path, accepted_files: list) -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    eng.context = SimpleNamespace(accepted_patches=[
        {"error": {"file": f, "message": "x", "code": "X"}} for f in accepted_files
    ])
    return eng


def _setup(d: str, files_in_proj: dict, files_in_sandbox: dict):
    proj = Path(d) / "proj"
    sandbox = Path(d) / "sandbox"
    proj.mkdir(parents=True, exist_ok=True)
    sandbox.mkdir(parents=True, exist_ok=True)
    for rel, content in files_in_proj.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel, content in files_in_sandbox.items():
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return proj, sandbox


# --- 1. ACCEPT-файл, проваливший аудит (полный откат), НЕ копируется fallback-ом ---
def test_accepted_but_audit_failed_never_copied():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"models.py": "ORIGINAL"},
            files_in_sandbox={"models.py": "DAMAGED"},
        )
        # Аудит пометил файл на полный откат; restore НЕ выполнялся вовсе
        # (симуляция провала restore — sandbox остался повреждённым).
        audit_result = {
            "passed_files": [],
            "failed_segments": {"models.py": [(1, 0, "ORIGINAL")]},
        }
        eng = _engine(proj, accepted_files=["models.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={"models.py": False})
        assert (proj / "models.py").read_text(encoding="utf-8") == "ORIGINAL", (
            "файл из failed_segments не должен копироваться в оригинал даже при ACCEPT"
        )


# --- 2. Частичный откат без подтверждения restore → не копируется ---
def test_partial_restore_unconfirmed_not_copied():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"a.py": "ORIG_A"},
            files_in_sandbox={"a.py": "PATCHED_A"},
        )
        audit_result = {
            "passed_files": [],
            "failed_segments": {"a.py": [(2, 3, "seg")]},  # частичный (end != 0)
        }
        eng = _engine(proj, accepted_files=[])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={"a.py": False})
        assert (proj / "a.py").read_text(encoding="utf-8") == "ORIG_A"


# --- 3. Подтверждённый частичный откат → копируется ---
def test_partial_restore_confirmed_copied():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"a.py": "ORIG_A"},
            files_in_sandbox={"a.py": "PARTIALLY_ROLLED"},
        )
        audit_result = {
            "passed_files": [],
            "failed_segments": {"a.py": [(2, 3, "seg")]},
        }
        eng = _engine(proj, accepted_files=[])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={"a.py": True})
        assert (proj / "a.py").read_text(encoding="utf-8") == "PARTIALLY_ROLLED"


# --- 4. restore_failed_segments возвращает per-file статус и верифицирует байты ---
def test_restore_returns_status():
    AuditManager = _load_audit()
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"ok.py": "ORIG_OK", "gone.py": "ORIG_GONE"},
            files_in_sandbox={"ok.py": "DAMAGED"},  # gone.py отсутствует в sandbox
        )
        am = AuditManager(
            project_path=proj, language="python", analyzer=MagicMock(),
            classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
            context_getter=lambda: None,
        )
        status = am.restore_failed_segments(sandbox, {
            "ok.py": [(1, 0, "ORIG_OK")],
            "gone.py": [(1, 0, "ORIG_GONE")],   # H2: пересоздаётся из оригинала
            "no_orig.py": [(1, 0, "")],          # оригинала нет → False
        })
        assert status["ok.py"] is True, status
        assert (sandbox / "ok.py").read_text(encoding="utf-8") == "ORIG_OK"
        assert status["gone.py"] is True, status
        assert (sandbox / "gone.py").read_text(encoding="utf-8") == "ORIG_GONE"
        assert status["no_orig.py"] is False, status


# --- 5. Сегмент вне диапазона строк → статус False (раньше — тихий пропуск) ---
def test_out_of_range_segment_is_failure():
    AuditManager = _load_audit()
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"a.py": "line1\n"},
            files_in_sandbox={"a.py": "line1\n"},
        )
        am = AuditManager(
            project_path=proj, language="python", analyzer=MagicMock(),
            classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
            context_getter=lambda: None,
        )
        status = am.restore_failed_segments(sandbox, {"a.py": [(99, 100, "seg")]})
        assert status["a.py"] is False, status


# --- 6. H2: исчезнувший из sandbox файл → failed_segments, не молчаливый skip ---
def test_missing_in_sandbox_marked_failed():
    AuditManager = _load_audit()
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        (proj / "src").mkdir(parents=True)
        sandbox.mkdir(parents=True)
        (proj / "src" / "app.py").write_text("def f():\n    pass\n", encoding="utf-8")
        # sandbox НЕ содержит src/app.py — файл «исчез»

        analyzer = MagicMock()
        analyzer.analyze = MagicMock(return_value=[])
        holder = {"ctx": None}
        am = AuditManager(
            project_path=proj, language="python", analyzer=analyzer,
            classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
            context_getter=lambda: holder["ctx"],
        )
        ctx = MagicMock()
        ctx.metadata = {"file_error_signatures_before": {"src/app.py": set()},
                        "patch_snapshots": []}
        ctx.working_path = sandbox
        holder["ctx"] = ctx

        res = am.audit_run(ctx)
        assert res["audit_ok"] is False, f"исчезнувший файл не может быть success: {res}"
        assert "src/app.py" in res["failed_segments"], res
        assert "src/app.py" in res.get("missing_in_sandbox", []), res


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
    print(f"\nDelivery respects audit: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
