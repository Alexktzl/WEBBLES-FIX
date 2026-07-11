"""
Regress H6 (PROJECT_AUDIT_REPORT 2026-07-01): save_state использовал
json.dump(default=str) — set объектов ErrorSignature в
file_error_signatures_before превращался в ОДНУ строку
"{ErrorSignature(...)}". На resume audit_run получал строку вместо set:
baseline терялся, все ошибки считались «новыми» (или аудит уходил в
fallback без символьных проверок). invariant_guard (живой объект)
попадал в persist строкой.

Фикс: явная сериализация в списки кортежей + восстановление в set-ы на
load; invariant_guard исключён из persist (движок переинжектит).

Запуск: python tests/test_phase_state_sigs_roundtrip.py
"""

import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from analysis.error_intelligence.error_signature import ErrorSignature  # noqa: E402
from core.engine.state import StateManager  # noqa: E402
from core.pipeline_context import PipelineContext  # noqa: E402


def _mk_manager(runtime_dir: Path) -> StateManager:
    return StateManager(
        project_path=runtime_dir / "proj", memory=None,
        state_lock=threading.Lock(), runtime_dir=runtime_dir,
    )


def _mk_context(project: Path, metadata: dict) -> PipelineContext:
    return PipelineContext(
        project_path=project, language="python", config={"pipeline": {}},
        metadata=metadata,
    )


def _save_and_load(metadata: dict):
    with tempfile.TemporaryDirectory() as d:
        runtime = Path(d)
        (runtime / "proj").mkdir()
        mgr = _mk_manager(runtime)
        ctx = _mk_context(runtime / "proj", metadata)
        anti_loop = MagicMock()
        anti_loop.to_dict = MagicMock(return_value={})
        cb = MagicMock()
        cb.failure_count = 0
        mgr.save_state(ctx, anti_loop, cb, {}, {}, 0.0)
        raw = json.loads(mgr.state_file.read_text(encoding="utf-8"))
        loaded = mgr.load_state()
        return raw, loaded


# --- 1. Сигнатуры выживают в roundtrip как set кортежей ---
def test_sigs_roundtrip():
    err = {"file": "src/app.py", "line": 3, "code": "E501",
           "message": "line too long (120 > 79 characters)"}
    sig = ErrorSignature.from_error(err)
    metadata = {"file_error_signatures_before": {"src/app.py": {sig}}}

    raw, loaded = _save_and_load(metadata)

    # На диске — JSON-списки, не строковый дамп set-а.
    disk_sigs = raw["context"]["metadata"]["file_error_signatures_before"]["src/app.py"]
    assert isinstance(disk_sigs, list) and isinstance(disk_sigs[0], list), disk_sigs

    restored = loaded.metadata["file_error_signatures_before"]["src/app.py"]
    assert isinstance(restored, set), f"ждали set, получили {type(restored)}"
    assert sig.to_tuple() in restored, (
        f"кортеж сигнатуры обязан выжить в roundtrip: {restored}"
    )


# --- 2. invariant_guard не попадает в persist ---
def test_invariant_guard_not_persisted():
    metadata = {
        "file_error_signatures_before": {},
        "invariant_guard": object(),  # живой объект
    }
    raw, loaded = _save_and_load(metadata)
    assert "invariant_guard" not in raw["context"]["metadata"], (
        "живой invariant_guard не должен сериализоваться в state.json"
    )


# --- 3. Восстановленные кортежи матчятся audit-ом (интеграция с C1) ---
def test_restored_sigs_match_audit_normalization():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "audit_for_test_roundtrip", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    err = {"file": "src/app.py", "line": 3, "code": "E501",
           "message": "line too long (120 > 79 characters)"}
    metadata = {"file_error_signatures_before": {
        "src/app.py": {ErrorSignature.from_error(err)},
    }}
    _, loaded = _save_and_load(metadata)
    restored = loaded.metadata["file_error_signatures_before"]["src/app.py"]

    before = mod.AuditManager._normalize_before_sigs(restored)
    after = mod.AuditManager._extract_error_signatures([err])
    assert after - before == set(), (
        f"restored-сигнатура обязана вычитать ту же ошибку: after={after}, before={before}"
    )


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
    print(f"\nState sigs roundtrip: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
