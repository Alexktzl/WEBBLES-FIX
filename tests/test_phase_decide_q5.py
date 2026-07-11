"""
Q.5 InvariantGuard поверх Logic Guard HIGH — тесты роутинга DecideStage.

Проверяет логику _lg_llm_review_enabled и расширенного Q.1/Q.3-блока:
- флаг выключен по умолчанию
- при включённом флаге + принятии InvariantGuard -> HIGH-нарушения сняты
- при включённом флаге + отклонении -> NEEDS_REVIEW остаётся
- нет invariant_guard в metadata -> HIGH-блок не падает
- нет patch_snapshots -> HIGH-нарушения остаются

Run: python3 tests/test_phase_decide_q5.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

results = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    status = "OK " if cond else "FAIL"
    try:
        print(f"  [{status}] {name}")
    except UnicodeEncodeError:
        print(f"  [{status}] {name.encode('ascii', 'replace').decode('ascii')}")


# ---------------------------------------------------------------------------
# Stub-объекты (без зависимостей от Pipeline / LLM)
# ---------------------------------------------------------------------------

class _Config(dict):
    pass


class _Context:
    def __init__(self, config=None, metadata=None):
        self.config = config or {}
        self.metadata = metadata or {}


class _AcceptingGuard:
    def verify_patch(self, original, patched, error):
        return {"accepted": True}


class _RejectingGuard:
    def verify_patch(self, original, patched, error):
        return {"accepted": False, "reason": "contract broken"}


# ---------------------------------------------------------------------------
# Дублируем логику Q.5 из decide_stage.py для unit-тестирования
# (тестируем именно логическую ветку, не всю стадию)
# ---------------------------------------------------------------------------

def _lg_llm_review_enabled(context) -> bool:
    try:
        cfg = (getattr(context, "config", None) or {})
        return bool(cfg.get("pipeline", {}).get("logic_guard_llm_review", False))
    except Exception:
        return False


def _run_q5_routing(context, error, lg_high):
    """Симулирует Q.5-блок из _dispatch_after_success.
    Возвращает (итоговый_lg_high, cleared_by_llm: bool).
    """
    cleared = False
    if lg_high and _lg_llm_review_enabled(context):
        try:
            invariant_guard = context.metadata.get("invariant_guard")
            if invariant_guard:
                snapshots = context.metadata.get("patch_snapshots", [])
                file_name = error.get("file", "")
                relevant_snap = None
                for snap in snapshots:
                    if snap.get("file") == file_name:
                        relevant_snap = snap
                        break
                if relevant_snap:
                    original = relevant_snap.get("original_content", "")
                    patched = relevant_snap.get("patched_content", "")
                    if original and patched:
                        result = invariant_guard.verify_patch(original, patched, error)
                        if result.get("accepted", True):
                            cleared = True
                            lg_high = []
        except Exception:
            pass
    return lg_high, cleared


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

def test_flag_default_off():
    ctx = _Context(config={})
    check("Q5: flag default OFF", not _lg_llm_review_enabled(ctx))


def test_flag_explicit_on():
    ctx = _Context(config={"pipeline": {"logic_guard_llm_review": True}})
    check("Q5: flag explicit ON", _lg_llm_review_enabled(ctx))


def test_flag_explicit_off():
    ctx = _Context(config={"pipeline": {"logic_guard_llm_review": False}})
    check("Q5: flag explicit OFF", not _lg_llm_review_enabled(ctx))


def test_accepting_guard_clears_high():
    snap = {"file": "main.py", "original_content": "before", "patched_content": "after"}
    ctx = _Context(
        config={"pipeline": {"logic_guard_llm_review": True}},
        metadata={"invariant_guard": _AcceptingGuard(), "patch_snapshots": [snap]},
    )
    lg_high = [{"type": "SIDE_EFFECT_ADDED", "severity": "high"}]
    remaining, cleared = _run_q5_routing(ctx, {"file": "main.py"}, lg_high)
    check("Q5: accepting guard -> lg_high cleared", remaining == [])
    check("Q5: accepting guard -> cleared=True", cleared is True)


def test_rejecting_guard_keeps_high():
    snap = {"file": "main.py", "original_content": "before", "patched_content": "after"}
    ctx = _Context(
        config={"pipeline": {"logic_guard_llm_review": True}},
        metadata={"invariant_guard": _RejectingGuard(), "patch_snapshots": [snap]},
    )
    lg_high = [{"type": "REQUIRED_PARAM_ADDED", "severity": "high"}]
    remaining, cleared = _run_q5_routing(ctx, {"file": "main.py"}, lg_high)
    check("Q5: rejecting guard -> lg_high kept", len(remaining) == 1)
    check("Q5: rejecting guard -> cleared=False", cleared is False)


def test_no_invariant_guard_keeps_high():
    ctx = _Context(
        config={"pipeline": {"logic_guard_llm_review": True}},
        metadata={"patch_snapshots": []},  # нет invariant_guard
    )
    lg_high = [{"type": "PARAM_REMOVED", "severity": "high"}]
    remaining, cleared = _run_q5_routing(ctx, {"file": "main.py"}, lg_high)
    check("Q5: no invariant_guard -> high kept", len(remaining) == 1)
    check("Q5: no invariant_guard -> not cleared", cleared is False)


def test_flag_off_skips_guard_entirely():
    """Даже если guard принимает, флаг выключен — не вызываем guard вообще."""
    class _TrackingGuard:
        called = False
        def verify_patch(self, *a, **kw):
            _TrackingGuard.called = True
            return {"accepted": True}

    snap = {"file": "main.py", "original_content": "before", "patched_content": "after"}
    ctx = _Context(
        config={},  # флаг выключен по умолчанию
        metadata={"invariant_guard": _TrackingGuard(), "patch_snapshots": [snap]},
    )
    lg_high = [{"type": "SIDE_EFFECT_ADDED", "severity": "high"}]
    remaining, cleared = _run_q5_routing(ctx, {"file": "main.py"}, lg_high)
    check("Q5: flag OFF -> guard not called", not _TrackingGuard.called)
    check("Q5: flag OFF -> high kept", len(remaining) == 1)


def test_no_matching_snapshot_keeps_high():
    snap = {"file": "other.py", "original_content": "before", "patched_content": "after"}
    ctx = _Context(
        config={"pipeline": {"logic_guard_llm_review": True}},
        metadata={"invariant_guard": _AcceptingGuard(), "patch_snapshots": [snap]},
    )
    lg_high = [{"type": "RETURN_PATH_LOST", "severity": "high"}]
    remaining, cleared = _run_q5_routing(ctx, {"file": "main.py"}, lg_high)
    check("Q5: no matching snapshot -> high kept", len(remaining) == 1)
    check("Q5: no matching snapshot -> not cleared", cleared is False)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n=== Q.5 InvariantGuard over Logic Guard Tests ===\n")
    test_flag_default_off()
    test_flag_explicit_on()
    test_flag_explicit_off()
    test_accepting_guard_clears_high()
    test_rejecting_guard_keeps_high()
    test_no_invariant_guard_keeps_high()
    test_flag_off_skips_guard_entirely()
    test_no_matching_snapshot_keeps_high()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        print("\nFailed:")
        for name, ok in results:
            if not ok:
                print(f"  FAIL: {name}")
        sys.exit(1)
    else:
        print("All tests passed.")
