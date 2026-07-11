"""
Regression-тест: DecideStage._emit_patch_record() должен логировать WARNING
(а не DEBUG) с exc_info=True, когда recorder.record() выбрасывает исключение.

До фикса: except-ветка вызывала logger.debug("_emit_patch_record failed: %s", _e) —
ошибка молча проглатывалась на уровне DEBUG, который в проде не виден.
Это приводило к тому, что patch_events.jsonl не писался, а в логах не было
ни одного видимого предупреждения.

После фикса: logger.warning(..., exc_info=True) — ошибка видна на уровне WARNING
с полным traceback.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


# ---------------------------------------------------------------------------
# Test 1: ошибка recorder.record() логируется как WARNING с exc_info
# ---------------------------------------------------------------------------

def test_recorder_failure_logged_as_warning_not_debug():
    """recorder.record() бросает RuntimeError → должен появиться WARNING, не DEBUG."""
    import core.stages.decide_stage as ds_mod
    from core.stages.decide_stage import DecideStage

    broken_recorder = MagicMock()
    broken_recorder.record.side_effect = RuntimeError("disk full")
    stage = DecideStage(quality_evaluator=MagicMock(), recorder=broken_recorder)

    error = {"file": "x.py", "line": 5, "code": "E501", "message": "line too long"}

    # Минимальный mock контекста (только поля, которые читает _emit_patch_record)
    fake_ctx = MagicMock()
    fake_ctx.validation_results = {}
    fake_ctx.metadata = {}
    fake_ctx.current_errors = []
    fake_ctx.processed_errors = {}
    fake_ctx.generated_patch = None
    fake_ctx.language = "python"

    warnings_captured = []
    debug_captured = []

    original_warning = ds_mod.logger.warning
    original_debug = ds_mod.logger.debug

    def capture_warning(msg, *args, **kwargs):
        warnings_captured.append({"msg": msg, "args": args, "kwargs": kwargs})

    def capture_debug(msg, *args, **kwargs):
        debug_captured.append({"msg": msg, "args": args, "kwargs": kwargs})

    ds_mod.logger.warning = capture_warning
    ds_mod.logger.debug = capture_debug
    try:
        stage._emit_patch_record(fake_ctx, error, "REJECT", "test reason", False)
    finally:
        ds_mod.logger.warning = original_warning
        ds_mod.logger.debug = original_debug

    # После фикса: должно быть хотя бы одно WARNING, содержащее "_emit_patch_record"
    failure_warnings = [
        w for w in warnings_captured
        if "_emit_patch_record" in str(w["msg"]) or "failed" in str(w["msg"]).lower()
    ]
    check(
        "failure_logged_as_warning",
        len(failure_warnings) > 0,
        f"warnings captured: {warnings_captured}",
    )

    # exc_info=True должен быть в kwargs
    check(
        "exc_info_true_in_warning",
        any(w["kwargs"].get("exc_info") for w in failure_warnings),
        f"kwargs of failure warnings: {[w['kwargs'] for w in failure_warnings]}",
    )

    # НЕ должен уйти в DEBUG как "_emit_patch_record failed"
    failure_debugs = [
        d for d in debug_captured
        if "_emit_patch_record" in str(d["msg"]) or "failed" in str(d["msg"]).lower()
    ]
    check(
        "failure_not_in_debug",
        len(failure_debugs) == 0,
        f"unexpected debug entries: {failure_debugs}",
    )


# ---------------------------------------------------------------------------
# Test 2: при отсутствии recorder _emit_patch_record завершается тихо (без логов)
# ---------------------------------------------------------------------------

def test_no_recorder_exits_silently():
    """Если recorder=None, метод просто выходит без предупреждений."""
    import core.stages.decide_stage as ds_mod
    from core.stages.decide_stage import DecideStage

    stage = DecideStage(quality_evaluator=MagicMock(), recorder=None)
    error = {"file": "x.py", "line": 1, "code": "E501", "message": "msg"}
    fake_ctx = MagicMock()
    fake_ctx.validation_results = {}
    fake_ctx.metadata = {}
    fake_ctx.current_errors = []

    warnings_captured = []
    original_warning = ds_mod.logger.warning

    def capture_warning(msg, *args, **kwargs):
        warnings_captured.append(msg)

    ds_mod.logger.warning = capture_warning
    try:
        stage._emit_patch_record(fake_ctx, error, "REJECT", "no recorder", False)
    finally:
        ds_mod.logger.warning = original_warning

    # Нет recorder → нет warnings от _emit_patch_record
    check("no_recorder_no_warning", len(warnings_captured) == 0,
          f"unexpected warnings: {warnings_captured}")


# ---------------------------------------------------------------------------
# Test 3: когда recorder работает без ошибок — WARNING не вызывается
# ---------------------------------------------------------------------------

def test_successful_record_no_warning():
    """Если recorder.record() успешен, никакого WARNING не должно быть."""
    import core.stages.decide_stage as ds_mod
    from core.stages.decide_stage import DecideStage

    good_recorder = MagicMock()
    good_recorder.record.return_value = None  # success
    stage = DecideStage(quality_evaluator=MagicMock(), recorder=good_recorder)

    error = {"file": "x.py", "line": 1, "code": "E501", "message": "long"}
    fake_ctx = MagicMock()
    fake_ctx.validation_results = {"error_count_before": 5, "error_count_after": 4}
    fake_ctx.metadata = {"_project_id": "proj", "_current_global_cycle": 1}
    fake_ctx.current_errors = []
    fake_ctx.processed_errors = {}
    fake_ctx.generated_patch = "--- a/x.py\n+++ b/x.py\n..."
    fake_ctx.iteration_count = 0
    fake_ctx.language = "python"

    warnings_captured = []
    original_warning = ds_mod.logger.warning

    def capture_warning(msg, *args, **kwargs):
        warnings_captured.append(msg)

    ds_mod.logger.warning = capture_warning
    try:
        stage._emit_patch_record(fake_ctx, error, "ACCEPT", "net_delta ok", False)
    finally:
        ds_mod.logger.warning = original_warning

    # При успехе — никаких WARNING о сбое
    failure_warns = [
        w for w in warnings_captured
        if "_emit_patch_record" in str(w) or "failed" in str(w).lower()
    ]
    check("no_warning_on_success", len(failure_warns) == 0,
          f"unexpected warnings: {failure_warns}")
    check("recorder_was_called", good_recorder.record.called)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

test_recorder_failure_logged_as_warning_not_debug()
test_no_recorder_exits_silently()
test_successful_record_no_warning()

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, note) for n, ok, note in results if not ok]

print(f"decide_emit_patch_record_warning: {passed}/{len(results)} passed")
if failed:
    for name, note in failed:
        print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
    raise SystemExit(1)
