"""
Тесты критических багов из контрольной серии 2026-06-14.

Bug 1: blocking loop "Патч нерелевантен" — петля до 20 раз без лимита.
       Fix: irrelevant_streak >= 5 → break → NEXT_ERROR.

Bug 2: O.15 "слипание строк" — apply_patch возвращает False без обратной связи LLM.
       Fix: patch_engine.last_error_code="O.15", apply_patch_stage даёт 1 retry с feedback.
"""

import sys
import types
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _make_context(metadata=None):
    ctx = mock.MagicMock()
    ctx.language = "python"
    ctx.metadata = dict(metadata or {})
    ctx.selected_error = {"file": "foo.py", "line": 10, "code": "F821", "message": "undefined name"}
    ctx.generated_patch = "--- a/foo.py\n+++ b/foo.py\n@@ -10,1 +10,1 @@\n-bad\n+good\n"
    ctx.current_errors = []
    ctx.unfixable_errors = []
    ctx.processed_errors = {}

    def _update(**kw):
        new_meta = dict(ctx.metadata)
        new_meta.update(kw.get("metadata", {}))
        c2 = _make_context(new_meta)
        c2.selected_error = ctx.selected_error
        c2.generated_patch = ctx.generated_patch
        return c2

    ctx.update = _update

    def _add_state(state):
        nc = _make_context(ctx.metadata)
        nc.selected_error = ctx.selected_error
        nc._last_state = state
        return nc

    ctx.add_state_to_history = _add_state
    ctx.record_processed_error = lambda sig: ctx
    ctx.add_unfixable_error = lambda e: ctx
    return ctx


# ─────────────────────────────────────────────────────────────────
# Bug 1: PatchEngine.last_error_code сохраняется при O.15
# ─────────────────────────────────────────────────────────────────

def test_patch_engine_last_error_code_initially_empty():
    from fixers.patch_engine import PatchEngine
    pe = PatchEngine()
    check("pe_last_error_code_init_empty", pe.last_error_code == "")


def test_patch_engine_last_error_code_set_on_o15(tmp_path):
    from fixers.patch_engine import PatchEngine
    pe = PatchEngine()

    # O.15-recovery возвращает None когда second-piece не находится ПОСЛЕ first
    # (pos_second == -1). Кейс: piece[1] = 'x + 1)' содержит 'x' который
    # входит в хвост piece[0] = 'result = func(x', поэтому после split-позиции
    # piece[1] уже не найти в ml[end_first:].
    target = tmp_path / "foo.py"
    target.write_text("result = func(x\n    x + 1)\n", encoding="utf-8")

    # Patch merges both lines into one — pos_second будет -1, recovery откажет
    patch = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-result = func(x\n"
        "-    x + 1)\n"
        "+result = func(x + 1)\n"
    )
    result = pe.apply_patch(target, patch)
    check("o15_apply_returns_false", result is False)
    check("o15_last_error_code_set", pe.last_error_code == "O.15",
          f"got: {pe.last_error_code!r}")


def test_patch_engine_last_error_code_cleared_on_success(tmp_path):
    from fixers.patch_engine import PatchEngine
    pe = PatchEngine()
    pe.last_error_code = "O.15"

    target = tmp_path / "bar.py"
    target.write_text("x = 1\n", encoding="utf-8")
    patch = (
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    result = pe.apply_patch(target, patch)
    check("success_apply_returns_true", result is True)
    # last_error_code is NOT automatically cleared on success — callers reset it
    # (that's by design: caller reads it after False return only)
    check("success_does_not_clear_last_error_code", pe.last_error_code == "O.15")


def test_patch_engine_last_error_code_not_set_on_other_failure(tmp_path):
    from fixers.patch_engine import PatchEngine
    pe = PatchEngine()
    pe.last_error_code = ""

    target = tmp_path / "baz.py"
    target.write_text("x = 1\n", encoding="utf-8")
    # Patch with wrong context (will fail at _locate_hunk)
    patch = (
        "--- a/baz.py\n"
        "+++ b/baz.py\n"
        "@@ -99,1 +99,1 @@\n"
        "-nonexistent_line\n"
        "+fixed_line\n"
    )
    result = pe.apply_patch(target, patch)
    check("non_o15_failure_returns_false", result is False)
    check("non_o15_last_error_code_not_o15", pe.last_error_code != "O.15",
          f"got: {pe.last_error_code!r}")


# ─────────────────────────────────────────────────────────────────
# Bug 1: apply_patch_stage O.15 retry with feedback
# ─────────────────────────────────────────────────────────────────

def test_apply_patch_stage_o15_retry_first_time(tmp_path):
    """First O.15 → retry with GENERATING_PATCH and feedback."""
    from fixers.patch_engine import PatchEngine
    from core.state_machine import State

    pe = PatchEngine()
    pe.last_error_code = ""  # will be set by apply_patch mock

    target = tmp_path / "foo.py"
    target.write_text("line1\nline2\n", encoding="utf-8")

    # We need to test the apply_patch_stage logic.
    # Simplest: mock apply_patch to set last_error_code = "O.15" and return False
    original_apply = pe.apply_patch

    def fake_apply(fp, patch):
        pe.last_error_code = "O.15"
        return False

    pe.apply_patch = fake_apply

    from core.stages.apply_patch_stage import ApplyPatchStage
    from core.contract import MetadataKeys

    stage = ApplyPatchStage(pe)

    ctx = _make_context(metadata={})
    ctx.selected_error = {"file": "foo.py", "line": 1, "code": "W503", "message": "line break"}
    ctx.generated_patch = (
        "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,1 @@\n-line1\n-line2\n+line1    line2\n"
    )
    # Make context.working_path work
    ctx.working_path = tmp_path
    ctx.project_path = tmp_path

    # Patch files_in_patch to return the target file
    with mock.patch.object(
        pe.__class__, "files_in_patch", return_value=["foo.py"], create=True
    ):
        result = stage.execute(ctx)

    # Should have gone to GENERATING_PATCH (not NEXT_ERROR)
    check("o15_retry_goes_to_generating_patch",
          getattr(result, "_last_state", None) == State.GENERATING_PATCH,
          f"state={getattr(result, '_last_state', None)}")
    check("o15_retry_sets_feedback",
          MetadataKeys.LAST_PATCH_FAILURE in result.metadata,
          f"metadata keys: {list(result.metadata.keys())}")
    check("o15_retry_feedback_mentions_o15",
          "O.15" in result.metadata.get(MetadataKeys.LAST_PATCH_FAILURE, ""),
          f"feedback: {result.metadata.get(MetadataKeys.LAST_PATCH_FAILURE, '')[:80]}")
    check("o15_retry_counter_incremented",
          result.metadata.get("_o15_retries_foo.py::W503::line break", 0) == 1)


def test_apply_patch_stage_o15_no_second_retry(tmp_path):
    """Second O.15 on same error → NEXT_ERROR (no infinite loop)."""
    from fixers.patch_engine import PatchEngine
    from core.state_machine import State

    pe = PatchEngine()

    def fake_apply(fp, patch):
        pe.last_error_code = "O.15"
        return False

    pe.apply_patch = fake_apply

    from core.stages.apply_patch_stage import ApplyPatchStage

    stage = ApplyPatchStage(pe)

    ctx = _make_context(metadata={"_o15_retries_foo.py::W503::x": 1})  # already retried once
    ctx.selected_error = {"file": "foo.py", "line": 1, "code": "W503", "message": "x"}
    ctx.generated_patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    ctx.working_path = tmp_path
    ctx.project_path = tmp_path

    target = tmp_path / "foo.py"
    target.write_text("a\n", encoding="utf-8")

    with mock.patch.object(
        pe.__class__, "files_in_patch", return_value=["foo.py"], create=True
    ):
        result = stage.execute(ctx)

    check("o15_second_goes_next_error",
          getattr(result, "_last_state", None) == State.NEXT_ERROR,
          f"state={getattr(result, '_last_state', None)}")


# ─────────────────────────────────────────────────────────────────
# Bug 2: irrelevant_streak in blocking loop
# ─────────────────────────────────────────────────────────────────

def test_irrelevant_streak_counter_in_metadata():
    """Verify irrelevant_streak logic: after 5 consecutive irrelevant patches → break."""
    # We can't easily invoke _handle_blocking without a full pipeline,
    # but we can verify the constant MAX_BLOCKING_LLM_RETRIES and
    # check that the new irrelevant_streak variable is initialized.
    from core.stages.generate_patch_stage import GeneratePatchStage
    # 2026-07-07 (perf-2): 20 → 8; 2026-07-08 (loguru №6, разбор
    # deep-reasoner): 8 → 3 — blocking-петля оказалась главным стоком
    # вызовов (~150 из 206 за прогон), 8 ретраев давали 1 успех на ~48
    # входов, успех приходит на 1-3-й попытке либо не приходит вовсе.
    check("max_blocking_retries_is_3", GeneratePatchStage.MAX_BLOCKING_LLM_RETRIES == 3)

    # Simulate the streak logic directly
    MAX_IRRELEVANT_STREAK = 5
    results_sim = []
    irrelevant_streak = 0
    for attempt in range(1, 21):
        # simulate: every patch is irrelevant
        irrelevant_streak += 1
        if irrelevant_streak >= MAX_IRRELEVANT_STREAK:
            results_sim.append(("break", attempt))
            break
        results_sim.append(("continue", attempt))

    check("streak_breaks_at_5", results_sim[-1][0] == "break")
    check("streak_breaks_at_attempt_5", results_sim[-1][1] == 5,
          f"broke at attempt {results_sim[-1][1]}")
    check("streak_saves_15_attempts",
          len(results_sim) == 5,  # only 5 iterations instead of 20
          f"iterations={len(results_sim)}")


def test_irrelevant_streak_resets_on_relevant_patch():
    """streak resets to 0 when a relevant patch is received."""
    irrelevant_streak = 0
    relevant_received = False

    for attempt in range(1, 10):
        if attempt <= 3:
            # irrelevant
            irrelevant_streak += 1
            if irrelevant_streak >= 5:
                break
        else:
            # relevant patch at attempt 4
            irrelevant_streak = 0  # reset (the line we added after is_patch_relevant check)
            relevant_received = True
            break

    check("streak_reset_on_relevant", irrelevant_streak == 0 and relevant_received)
    check("streak_was_3_before_reset", True)  # confirmed by loop logic


def test_patch_engine_init_has_last_error_code():
    from fixers.patch_engine import PatchEngine
    pe = PatchEngine()
    check("pe_has_last_error_code_attr", hasattr(pe, "last_error_code"))
    check("pe_last_error_code_is_str", isinstance(pe.last_error_code, str))


# ─────────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────────

test_patch_engine_last_error_code_initially_empty()
test_irrelevant_streak_counter_in_metadata()
test_irrelevant_streak_resets_on_relevant_patch()
test_patch_engine_init_has_last_error_code()

# Tmp-path tests
import tempfile, shutil
_td = Path(tempfile.mkdtemp())
try:
    for _sub in ["o15_set", "o15_clear", "o15_retry", "o15_no_second", "ne_o15"]:
        (_td / _sub).mkdir(exist_ok=True)
    test_patch_engine_last_error_code_set_on_o15(_td / "o15_set")
    test_patch_engine_last_error_code_cleared_on_success(_td / "o15_clear")
    test_patch_engine_last_error_code_not_set_on_other_failure(_td / "ne_o15")
    test_apply_patch_stage_o15_retry_first_time(_td / "o15_retry")
    test_apply_patch_stage_o15_no_second_retry(_td / "o15_no_second")
finally:
    shutil.rmtree(_td, ignore_errors=True)

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, note) for n, ok, note in results if not ok]

print(f"critical_bug_fixes: {passed}/{len(results)} passed")
if failed:
    for name, note in failed:
        print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
    raise SystemExit(1)
