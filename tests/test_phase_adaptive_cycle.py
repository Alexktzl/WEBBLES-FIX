"""Tests for AdaptiveCycleController."""

import time
import pytest
from unittest.mock import patch

from core.adaptive_cycle_controller import AdaptiveCycleController


def _make(initial=100, max_cycles=4, hard_cap=12, budget_s=240.0):
    config = {"pipeline": {
        "max_global_cycles": max_cycles,
        "max_global_cycles_cap": hard_cap,
        "cycle_runtime_budget_s": budget_s,
    }}
    return AdaptiveCycleController(initial, config)


# ── basic continuation ──────────────────────────────────────────────────────

def test_continues_when_making_progress():
    ctrl = _make(initial=100, max_cycles=4)
    assert ctrl.should_continue(80, 3, 1, 0) is True
    assert ctrl.stop_reason is None


def test_completed_on_zero_errors():
    ctrl = _make(initial=50)
    result = ctrl.should_continue(0, 5, 0, 0)
    assert result is False
    assert ctrl.stop_reason == "completed"


# ── global_invariant ────────────────────────────────────────────────────────

def test_global_invariant_triggers_above_ratio():
    ctrl = _make(initial=100)
    # 121 > 100 * 1.20
    result = ctrl.should_continue(121, 0, 0, 0)
    assert result is False
    assert ctrl.stop_reason == "global_invariant"


def test_global_invariant_exactly_at_threshold_allows():
    ctrl = _make(initial=100)
    # 120 == 100 * 1.20 — not strictly greater, so allowed
    result = ctrl.should_continue(120, 0, 0, 0)
    assert result is True


def test_global_invariant_skipped_when_initial_zero():
    ctrl = _make(initial=0)
    # No initial errors → no invariant check possible
    result = ctrl.should_continue(999, 0, 0, 0)
    # Should not stop on global_invariant (may stop on other reasons)
    assert ctrl.stop_reason != "global_invariant"


# ── runtime_budget ───────────────────────────────────────────────────────────

def test_runtime_budget_exceeded():
    ctrl = _make(initial=100, budget_s=1.0)
    time.sleep(1.1)
    result = ctrl.should_continue(90, 1, 0, 0)
    assert result is False
    assert ctrl.stop_reason == "runtime_budget"


# ── max_cycles ───────────────────────────────────────────────────────────────

def test_stops_at_max_cycles_with_no_progress():
    ctrl = _make(initial=100, max_cycles=2)
    ctrl.should_continue(90, 0, 0, 0)   # cycle 1 — no accept, delta=10
    result = ctrl.should_continue(90, 0, 2, 0)  # cycle 2 — no accept, no delta
    assert result is False
    assert ctrl.stop_reason == "max_cycles"


def test_extends_max_cycles_on_good_progress():
    ctrl = _make(initial=100, max_cycles=2, hard_cap=8)
    ctrl.should_continue(80, 3, 0, 0)   # cycle 1 — good: delta=20, acc=3
    result = ctrl.should_continue(60, 2, 0, 0)  # cycle 2 — still good
    # At max_cycles=2 with good last cycle → extend to 4
    assert result is True
    assert ctrl.max_cycles == 4


def test_hard_cap_not_exceeded():
    ctrl = _make(initial=100, max_cycles=4, hard_cap=4)
    for i in range(4):
        ctrl.should_continue(100 - (i + 1) * 5, i + 1, 0, 0)
    # At hard cap=4, no further extension
    assert ctrl.stop_reason == "max_cycles"


# ── error_growth ─────────────────────────────────────────────────────────────

def test_error_growth_two_consecutive_rising_cycles():
    ctrl = _make(initial=50, max_cycles=10)
    ctrl.should_continue(55, 0, 0, 0)   # errors went up: delta=-5
    result = ctrl.should_continue(60, 0, 0, 0)  # errors went up again: delta=-5
    assert result is False
    assert ctrl.stop_reason == "error_growth"


def test_error_growth_not_triggered_on_single_bad_cycle():
    ctrl = _make(initial=50, max_cycles=10)
    ctrl.should_continue(55, 0, 0, 0)   # up
    result = ctrl.should_continue(50, 1, 0, 0)  # recovered
    assert result is True
    assert ctrl.stop_reason != "error_growth"


def test_error_growth_not_triggered_below_initial_phantom():
    """Регресс (2026-07-10, spdlog): счётчик растёт 2 цикла подряд, но остаётся
    НИЖЕ старта — это фантом (advisory-инструменты типа semgrep с throttle
    возвращают свои же находки постепенно: 16→17→19 при initial 21), а не
    реальное ухудшение. error_growth НЕ должен срабатывать и обрывать прогон."""
    ctrl = _make(initial=21, max_cycles=10)
    ctrl.should_continue(16, 2, 0, 0)   # semgrep вытеснен: только static
    ctrl.should_continue(17, 3, 0, 0)   # semgrep возвращается: рост (delta<0)
    result = ctrl.should_continue(19, 3, 0, 0)  # ещё рост, но 19 < initial 21
    assert result is True, "фантомный рост ниже старта не должен обрывать прогон"
    assert ctrl.stop_reason != "error_growth"


def test_error_growth_still_fires_above_initial():
    """Настоящий рост ВЫШЕ старта (2 цикла) — error_growth срабатывает."""
    ctrl = _make(initial=21, max_cycles=10)
    ctrl.should_continue(22, 0, 0, 0)   # выше старта
    result = ctrl.should_continue(24, 0, 0, 0)  # ещё выше — реальная дивергенция
    assert result is False
    assert ctrl.stop_reason == "error_growth"


# ── plateau ──────────────────────────────────────────────────────────────────

def test_plateau_detected_after_two_unchanged_cycles():
    ctrl = _make(initial=100, max_cycles=10)
    ctrl.should_continue(80, 0, 0, 0)   # cycle 1: delta=20, no accept
    ctrl.should_continue(80, 0, 0, 0)   # cycle 2: delta=0, no accept — potential plateau
    result = ctrl.should_continue(80, 0, 0, 0)  # cycle 3: still stuck
    assert result is False
    assert ctrl.stop_reason == "plateau"


def test_plateau_not_triggered_if_accepts_present():
    ctrl = _make(initial=100, max_cycles=10)
    ctrl.should_continue(80, 1, 0, 0)   # cycle 1: accept=1
    result = ctrl.should_continue(80, 1, 0, 0)  # cycle 2: same errors but acc=0 this cycle
    # The delta_errors for cycle 2 is 0, but cycle_accepted for cycle 1 was 1 (total=1→1=0 this cycle)
    # plateau requires BOTH: cycle_accepted==0 AND delta_errors==0 for 2 cycles
    # cycle 1 had delta=20 (not 0), so plateau only looks at last 2 which are cycles 2&3
    assert ctrl.stop_reason != "plateau"


# ── no_progress ──────────────────────────────────────────────────────────────

def test_no_progress_two_cycles_zero_accept_no_delta():
    # Scenario that separates no_progress from plateau and error_growth:
    #   cycle 1: delta=5 (progress, so no early stop)
    #   cycle 2: delta=-1 (slight regression, single bad — no error_growth yet)
    #   cycle 3: delta=0 (stall — no plateau since cycle 2 had delta=-1, but
    #             last 2 cycles are delta≤0 with 0 accepts → no_progress)
    ctrl = _make(initial=100, max_cycles=10)
    ctrl.should_continue(95, 0, 2, 0)   # cycle 1: errors 100→95, delta=+5
    ctrl.should_continue(96, 0, 4, 0)   # cycle 2: errors 95→96, delta=-1
    result = ctrl.should_continue(96, 0, 6, 0)  # cycle 3: errors stuck at 96, delta=0
    assert result is False
    assert ctrl.stop_reason == "no_progress"


# ── reject_spike ─────────────────────────────────────────────────────────────

def test_reject_spike_triggers_on_many_rejects_no_accept():
    # total_rejected is cumulative — must always increase.
    ctrl = _make(initial=100, max_cycles=10)
    ctrl.should_continue(98, 0, 3, 0)   # cycle 1: cycle_rej=3, acc=0
    result = ctrl.should_continue(96, 0, 5, 0)  # cycle 2: cycle_rej=2, acc=0 → sum=5 ≥ 4
    assert result is False
    assert ctrl.stop_reason == "reject_spike"


def test_reject_spike_not_triggered_with_one_accept():
    ctrl = _make(initial=100, max_cycles=10)
    ctrl.should_continue(98, 0, 3, 0)   # cycle 1: 3 rejects
    result = ctrl.should_continue(95, 1, 2, 0)  # cycle 2: 2 rejects but 1 accept
    assert ctrl.stop_reason != "reject_spike"


# ── summary ───────────────────────────────────────────────────────────────────

def test_summary_contains_expected_keys():
    ctrl = _make(initial=100, max_cycles=3)
    ctrl.should_continue(80, 2, 1, 0)
    s = ctrl.summary()
    assert s["cycles_run"] == 1
    assert "stop_reason" in s
    assert "history" in s
    assert len(s["history"]) == 1
    assert s["history"][0]["cycle"] == 1
    assert s["history"][0]["delta_errors"] == 20
    assert s["history"][0]["cycle_accepted"] == 2


def test_summary_stop_reason_running_while_active():
    ctrl = _make(initial=100, max_cycles=5)
    ctrl.should_continue(90, 1, 0, 0)
    assert ctrl.summary()["stop_reason"] == "running"


# ── default config fallback ──────────────────────────────────────────────────

def test_default_config_used_when_empty():
    ctrl = AdaptiveCycleController(100, {})
    assert ctrl.max_cycles == AdaptiveCycleController.DEFAULT_MAX_CYCLES
    assert ctrl.hard_cap == AdaptiveCycleController.HARD_CAP
    assert ctrl.runtime_budget_s == AdaptiveCycleController.RUNTIME_BUDGET_S


def test_initial_error_zero_no_crash():
    ctrl = AdaptiveCycleController(0, {})
    result = ctrl.should_continue(0, 0, 0, 0)
    assert result is False
    assert ctrl.stop_reason == "completed"


# ── priority ordering ────────────────────────────────────────────────────────

def test_completed_beats_global_invariant():
    """completed (0 errors) is checked before global_invariant."""
    ctrl = _make(initial=10)
    # 0 errors — completed takes priority even though initial was 10
    result = ctrl.should_continue(0, 0, 0, 0)
    assert ctrl.stop_reason == "completed"


def test_global_invariant_beats_max_cycles():
    ctrl = _make(initial=10, max_cycles=1)
    # errors = 15 > 10*1.2=12, and cycle 1 = max_cycles
    # global_invariant fires before max_cycles check
    result = ctrl.should_continue(15, 0, 0, 0)
    assert ctrl.stop_reason == "global_invariant"
