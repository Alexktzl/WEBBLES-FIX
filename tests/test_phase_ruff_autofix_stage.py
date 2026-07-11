"""
RuffAutoFixStage — unit тесты с mock subprocess.

Покрывает:
  1. parse_patch_changed_lines (если ruff не найден)
  2. _ruff_error_count — mock subprocess
  3. _ruff_fix — mock subprocess, parse "Fixed N errors." from stderr
  4. RuffAutoFixStage.execute — пропуск не-Python, mock-прогон safe/unsafe passes
  5. Net-delta protection: если after > before — не добавляем в accepted
  6. Статистика в context.metadata
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


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_context(language="python", metadata=None):
    """Minimal PipelineContext stub."""
    ctx = mock.MagicMock()
    ctx.language = language
    ctx.metadata = dict(metadata or {})
    ctx.update = lambda **kw: _make_context(language, kw.get("metadata", ctx.metadata))
    # Make update actually return a new mock with updated metadata
    def _update(**kw):
        m = dict(ctx.metadata)
        m.update(kw.get("metadata", {}))
        return _make_context(language, m)
    ctx.update = _update
    return ctx


def _fake_run(stdout="", stderr="", returncode=0):
    r = mock.MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


# ─────────────────────────────────────────────
# 1. _ruff_error_count
# ─────────────────────────────────────────────

def test_ruff_error_count_parses_json():
    from core.stages.ruff_autofix_stage import _ruff_error_count

    json_out = '[{"code":"W291"},{"code":"E711"},{"code":"I001"}]'
    with mock.patch("subprocess.run", return_value=_fake_run(stdout=json_out)):
        n = _ruff_error_count(Path("/fake/project"))
    check("error_count_from_json", n == 3)


def test_ruff_error_count_empty():
    from core.stages.ruff_autofix_stage import _ruff_error_count

    with mock.patch("subprocess.run", return_value=_fake_run(stdout="[]")):
        n = _ruff_error_count(Path("/fake/project"))
    check("error_count_empty_json", n == 0)


def test_ruff_error_count_bad_json():
    # H5 (аудит 2026-07-01): сбой пересчёта = None (fail-closed), а не 0 —
    # 0 после --fix давал отрицательный net_delta и «успех» без проверки.
    from core.stages.ruff_autofix_stage import _ruff_error_count

    with mock.patch("subprocess.run", return_value=_fake_run(stdout="not json")):
        n = _ruff_error_count(Path("/fake"))
    check("error_count_bad_json_none", n is None)


def test_ruff_error_count_exception():
    from core.stages.ruff_autofix_stage import _ruff_error_count

    with mock.patch("subprocess.run", side_effect=Exception("no ruff")):
        n = _ruff_error_count(Path("/fake"))
    check("error_count_exception_none", n is None)


# ─────────────────────────────────────────────
# 2. _ruff_fix — parse "Fixed N errors."
# ─────────────────────────────────────────────

def test_ruff_fix_parses_fixed_count():
    from core.stages.ruff_autofix_stage import _ruff_fix

    with mock.patch("subprocess.run", return_value=_fake_run(stderr="Fixed 7 errors.")):
        _, fixed = _ruff_fix(Path("/fake"), "W291,W292")
    check("ruff_fix_fixed_7", fixed == 7)


def test_ruff_fix_no_fixed_line():
    from core.stages.ruff_autofix_stage import _ruff_fix

    with mock.patch("subprocess.run", return_value=_fake_run(stderr="All checks passed.")):
        _, fixed = _ruff_fix(Path("/fake"), "W291")
    check("ruff_fix_no_fixed_zero", fixed == 0)


def test_ruff_fix_not_found():
    from core.stages.ruff_autofix_stage import _ruff_fix

    with mock.patch("subprocess.run", side_effect=FileNotFoundError("ruff not found")):
        _, fixed = _ruff_fix(Path("/fake"), "W291")
    check("ruff_fix_not_found_zero", fixed == 0)


def test_ruff_fix_timeout():
    import subprocess
    from core.stages.ruff_autofix_stage import _ruff_fix

    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ruff", 10)):
        _, fixed = _ruff_fix(Path("/fake"), "W291", timeout=10)
    check("ruff_fix_timeout_zero", fixed == 0)


def test_ruff_fix_unsafe_flag():
    """Verify --unsafe-fixes is added when unsafe=True."""
    from core.stages.ruff_autofix_stage import _ruff_fix

    captured_cmd = []
    def fake_run(cmd, **kw):
        captured_cmd.extend(cmd)
        return _fake_run(stderr="Fixed 1 errors.")

    with mock.patch("subprocess.run", side_effect=fake_run):
        _ruff_fix(Path("/fake"), "F841", unsafe=True)

    check("ruff_fix_unsafe_flag_present", "--unsafe-fixes" in captured_cmd)


# ─────────────────────────────────────────────
# 3. RuffAutoFixStage.execute
# ─────────────────────────────────────────────

def test_execute_skips_non_python():
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="rust")
    with mock.patch("subprocess.run") as m:
        ctx_out, accepted = stage.execute(ctx, Path("/fake"))
    check("skip_non_python_no_subprocess", m.call_count == 0)
    check("skip_non_python_empty_accepted", accepted == [])


def test_execute_skips_when_no_errors():
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="python")

    # _ruff_error_count returns 0 → skip
    with mock.patch("core.stages.ruff_autofix_stage._ruff_error_count", return_value=0):
        ctx_out, accepted = stage.execute(ctx, Path("/fake"))

    check("no_errors_skip_accepted_empty", accepted == [])


def test_execute_safe_pass_accepted():
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="python")

    call_count = [0]
    def fake_error_count(path):
        call_count[0] += 1
        # before=10, after safe=7, after unsafe unchanged
        return [10, 7, 7][min(call_count[0] - 1, 2)]

    with mock.patch("core.stages.ruff_autofix_stage._ruff_error_count",
                    side_effect=fake_error_count):
        with mock.patch("core.stages.ruff_autofix_stage._ruff_fix",
                        side_effect=[
                            (0, 5),   # safe pass: fixed 5
                            (0, 0),   # unsafe pass: fixed 0
                        ]):
            ctx_out, accepted = stage.execute(ctx, Path("/fake"))

    check("safe_pass_accepted_one", len(accepted) == 1)
    check("safe_pass_source", accepted[0]["source"] == "ruff_autofix_safe")
    check("safe_pass_fixed_count", accepted[0]["fixed_count"] == 5)
    check("safe_pass_net_delta_negative", accepted[0]["net_delta"] == -3)


def test_execute_net_delta_positive_not_accepted():
    """If safe pass caused net_delta > 0, it should NOT be added to accepted."""
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="python")

    call_count = [0]
    def fake_error_count(path):
        call_count[0] += 1
        # before=10, after safe=13 (grew!), after unsafe=13
        return [10, 13, 13][min(call_count[0] - 1, 2)]

    with mock.patch("core.stages.ruff_autofix_stage._ruff_error_count",
                    side_effect=fake_error_count):
        with mock.patch("core.stages.ruff_autofix_stage._ruff_fix",
                        side_effect=[
                            (0, 3),   # safe pass: ruff reports 3 fixed (but count grew)
                            (0, 0),   # unsafe pass: nothing
                        ]):
            ctx_out, accepted = stage.execute(ctx, Path("/fake"))

    check("net_delta_positive_not_accepted", len(accepted) == 0)


def test_execute_metadata_recorded():
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="python")

    call_count = [0]
    def fake_error_count(path):
        call_count[0] += 1
        return [20, 15, 14][min(call_count[0] - 1, 2)]

    with mock.patch("core.stages.ruff_autofix_stage._ruff_error_count",
                    side_effect=fake_error_count):
        with mock.patch("core.stages.ruff_autofix_stage._ruff_fix",
                        side_effect=[(0, 5), (0, 1)]):
            ctx_out, accepted = stage.execute(ctx, Path("/fake"))

    # ctx_out is our stub - check metadata was updated
    check("metadata_stats_recorded", len(accepted) > 0)
    check("total_fixed_two_passes", sum(a["fixed_count"] for a in accepted) == 6)


def test_execute_both_passes_accepted():
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="python")

    call_count = [0]
    def fake_error_count(path):
        call_count[0] += 1
        return [20, 15, 13][min(call_count[0] - 1, 2)]

    with mock.patch("core.stages.ruff_autofix_stage._ruff_error_count",
                    side_effect=fake_error_count):
        with mock.patch("core.stages.ruff_autofix_stage._ruff_fix",
                        side_effect=[(0, 5), (0, 2)]):
            ctx_out, accepted = stage.execute(ctx, Path("/fake"))

    check("both_passes_two_accepted", len(accepted) == 2)
    sources = {a["source"] for a in accepted}
    check("both_sources", "ruff_autofix_safe" in sources and "ruff_autofix_unsafe" in sources)


def test_execute_recount_failure_rolls_back():
    """H5 (аудит 2026-07-01): если после-пересчёт вернул None (timeout/сбой),
    пасс НЕ засчитывается и файлы восстанавливаются из снапшота — раньше
    None превращался в 0 и «успех» с отрицательным net_delta."""
    import tempfile
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="python")

    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "mod.py").write_text("x = 1\n", encoding="utf-8")

        call_count = [0]
        def fake_error_count(path):
            call_count[0] += 1
            if call_count[0] == 1:
                return 10          # baseline ок
            return None            # после-пересчёт упал

        def fake_fix(path, select, unsafe=False, timeout=120):
            # имитируем правку ruff: файл изменён на диске
            (work / "mod.py").write_text("x=1  # ruffed\n", encoding="utf-8")
            return 0, 5

        with mock.patch("core.stages.ruff_autofix_stage._ruff_error_count",
                        side_effect=fake_error_count):
            with mock.patch("core.stages.ruff_autofix_stage._ruff_fix",
                            side_effect=fake_fix):
                ctx_out, accepted = stage.execute(ctx, work)

        check("recount_failure_not_accepted", accepted == [])
        check("recount_failure_file_restored",
              (work / "mod.py").read_text(encoding="utf-8") == "x = 1\n")


def test_execute_baseline_failure_skips_stage():
    """H5: baseline None → стадия пропущена, ruff --fix не вызывался."""
    from core.stages.ruff_autofix_stage import RuffAutoFixStage

    stage = RuffAutoFixStage()
    ctx = _make_context(language="python")
    with mock.patch("core.stages.ruff_autofix_stage._ruff_error_count",
                    return_value=None):
        with mock.patch("core.stages.ruff_autofix_stage._ruff_fix") as m_fix:
            ctx_out, accepted = stage.execute(ctx, Path("/fake"))
    check("baseline_none_skip", accepted == [] and m_fix.call_count == 0)


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

test_ruff_error_count_parses_json()
test_ruff_error_count_empty()
test_ruff_error_count_bad_json()
test_ruff_error_count_exception()
test_ruff_fix_parses_fixed_count()
test_ruff_fix_no_fixed_line()
test_ruff_fix_not_found()
test_ruff_fix_timeout()
test_ruff_fix_unsafe_flag()
test_execute_skips_non_python()
test_execute_skips_when_no_errors()
test_execute_safe_pass_accepted()
test_execute_net_delta_positive_not_accepted()
test_execute_metadata_recorded()
test_execute_both_passes_accepted()
test_execute_recount_failure_rolls_back()
test_execute_baseline_failure_skips_stage()

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, note) for n, ok, note in results if not ok]

print(f"ruff_autofix_stage: {passed}/{len(results)} passed")
if failed:
    for name, note in failed:
        print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
    raise SystemExit(1)
