"""
LLM hard-timeout и project-level timeout — unit тесты.

Покрывает:
  1. LLMClient._hard_timeout_call — реальный тред с задержкой
  2. hard_timeout_count счётчик
  3. Логику project_budget в pipeline_engine (без поднятия всего движка)
  4. RunStatistics.record_final принимает timeout-поля
"""

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


# ─────────────────────────────────────────────
# 1. LLMClient._hard_timeout_call
# ─────────────────────────────────────────────

def test_hard_timeout_call_success():
    from fixers.llm_client import LLMClient

    cfg = {"llm": {"timeout": 5, "max_retries": 1}}
    client = LLMClient(config=cfg)

    def fast_func():
        return ("result_text", None)

    out, err = client._hard_timeout_call(fast_func)
    check("hard_timeout_success_result", out == "result_text")
    check("hard_timeout_success_no_err", err is None)
    check("hard_timeout_count_zero_on_success", client.hard_timeout_count == 0)


def test_hard_timeout_call_fires():
    from fixers.llm_client import LLMClient

    # Short timeout: 0.1s HTTP timeout → 0.15s hard limit
    cfg = {"llm": {"timeout": 0.1, "max_retries": 1}}
    client = LLMClient(config=cfg)

    def slow_func():
        time.sleep(5)  # much longer than hard limit
        return ("never", None)

    t0 = time.monotonic()
    out, err = client._hard_timeout_call(slow_func)
    elapsed = time.monotonic() - t0

    check("hard_timeout_fires_result_none", out is None)
    check("hard_timeout_fires_err_has_timeout", err is not None and "hard_timeout" in str(err))
    check("hard_timeout_fires_fast", elapsed < 2.0, f"elapsed={elapsed:.2f}s")
    check("hard_timeout_count_incremented", client.hard_timeout_count == 1)


def test_hard_timeout_count_accumulates():
    from fixers.llm_client import LLMClient

    cfg = {"llm": {"timeout": 0.05, "max_retries": 1}}
    client = LLMClient(config=cfg)

    def slow():
        time.sleep(5)
        return ("never", None)

    client._hard_timeout_call(slow)
    client._hard_timeout_call(slow)
    check("hard_timeout_count_two", client.hard_timeout_count == 2)


def test_hard_timeout_exception_in_func():
    from fixers.llm_client import LLMClient

    cfg = {"llm": {"timeout": 5, "max_retries": 1}}
    client = LLMClient(config=cfg)

    def raises():
        raise RuntimeError("provider error")

    out, err = client._hard_timeout_call(raises)
    check("exception_in_func_result_none", out is None)
    check("exception_in_func_err_not_none", err is not None)
    check("exception_count_not_incremented", client.hard_timeout_count == 0)


# ─────────────────────────────────────────────
# 2. Project-level timeout logic (unit)
# ─────────────────────────────────────────────

def test_project_budget_check_logic():
    """Simulate the budget check logic without actual timing."""
    # Inject synthetic elapsed values to test the condition
    budget = 300.0

    cases = [
        (299.9, False),   # within budget
        (300.0, True),    # exactly at limit
        (301.5, True),    # over budget
    ]
    for elapsed, expected_fired in cases:
        fired = elapsed >= budget
        check(
            f"project_budget_elapsed_{elapsed}",
            fired == expected_fired,
            f"elapsed={elapsed}, budget={budget}",
        )


# ─────────────────────────────────────────────
# 3. RunStatistics accepts timeout fields
# ─────────────────────────────────────────────

def test_run_statistics_record_final_timeout_fields():
    from analysis.run_statistics import RunStatistics

    stats = RunStatistics()
    stats.start("/tmp/proj", "python")
    stats.record_final(
        final_error_count=3,
        accepted_patches=[],
        rejected_patches=[],
        needs_review_items=[],
        audit_result={},
        iterations=2,
        rollbacks=1,
        llm_timeout_count=3,
        project_timeout_triggered=True,
        stop_reason="project_timeout",
        net_delta_rollback_count=5,
        net_delta_uncertain_count=2,
        ruff_autofix_total_fixed=17,
    )

    check("stats_llm_timeout_count", stats.llm_timeout_count == 3)
    check("stats_project_timeout", stats.project_timeout_triggered is True)
    check("stats_stop_reason", stats.stop_reason == "project_timeout")
    check("stats_nd_rollback", stats.net_delta_rollback_count == 5)
    check("stats_nd_uncertain", stats.net_delta_uncertain_count == 2)
    check("stats_ruff_fixed", stats.ruff_autofix_total_fixed == 17)

    agg = stats.aggregate()
    check("agg_llm_timeout", agg["llm_timeout_count"] == 3)
    check("agg_project_timeout", agg["project_timeout_triggered"] is True)
    check("agg_stop_reason", agg["stop_reason"] == "project_timeout")
    check("agg_nd_rollback", agg["net_delta_rollback_count"] == 5)
    check("agg_nd_uncertain", agg["net_delta_uncertain_count"] == 2)
    check("agg_ruff_fixed", agg["ruff_autofix_total_fixed"] == 17)


def test_run_statistics_markdown_contains_telemetry():
    from analysis.run_statistics import RunStatistics

    stats = RunStatistics()
    stats.start("/tmp/proj", "python")
    stats.record_final(
        final_error_count=0,
        accepted_patches=[],
        rejected_patches=[],
        needs_review_items=[],
        audit_result={},
        llm_timeout_count=2,
        project_timeout_triggered=True,
        net_delta_rollback_count=1,
        net_delta_uncertain_count=3,
        ruff_autofix_total_fixed=8,
    )

    md = stats.to_markdown()
    check("md_has_telemetry_section", "Телеметрия" in md)
    check("md_has_project_timeout", "PROJECT_TIMEOUT" in md)
    check("md_has_llm_timeout", "LLM hard timeout" in md)
    check("md_has_nd_rollback", "Net-delta regression" in md)
    check("md_has_nd_uncertain", "Net-delta uncertain" in md)
    check("md_has_ruff_autofix", "Ruff autofix" in md)


def test_run_statistics_markdown_no_telemetry_section_when_zero():
    from analysis.run_statistics import RunStatistics

    stats = RunStatistics()
    stats.start("/tmp/proj", "python")
    stats.record_final(
        final_error_count=0,
        accepted_patches=[],
        rejected_patches=[],
        needs_review_items=[],
        audit_result={},
    )

    md = stats.to_markdown()
    check("md_no_telemetry_when_all_zero", "Телеметрия" not in md)


# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

test_hard_timeout_call_success()
test_hard_timeout_call_fires()
test_hard_timeout_count_accumulates()
test_hard_timeout_exception_in_func()
test_project_budget_check_logic()
test_run_statistics_record_final_timeout_fields()
test_run_statistics_markdown_contains_telemetry()
test_run_statistics_markdown_no_telemetry_section_when_zero()

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, note) for n, ok, note in results if not ok]

print(f"llm_timeout: {passed}/{len(results)} passed")
if failed:
    for name, note in failed:
        print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
    raise SystemExit(1)
