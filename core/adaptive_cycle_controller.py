"""
AdaptiveCycleController — replaces fixed max_global_cycles with a metric-driven
controller that decides after each cycle whether to continue or stop.

stop_reason values reported in the pipeline result:
  completed        — no errors remain
  global_invariant — error count exceeded initial by more than ERROR_GROWTH_RATIO
  runtime_budget   — elapsed time >= cycle_runtime_budget_s config value
  max_cycles       — cycle cap reached with no progress to justify extension
  plateau          — errors unchanged and 0 accepted for PLATEAU_CYCLES consecutive cycles
  no_progress      — 0 accepted, no error reduction for NO_PROGRESS_CYCLES consecutive cycles
  error_growth     — errors increased two consecutive cycles in a row
  reject_spike     — high reject count with 0 accepts for 2 consecutive cycles

Config keys (under pipeline.*):
  max_global_cycles          — starting cycle cap (default 4)
  max_global_cycles_cap      — hard ceiling for dynamic extension (default 12)
  cycle_runtime_budget_s     — per-project wall-clock budget in seconds (default 240)
"""

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AdaptiveCycleController:
    DEFAULT_MAX_CYCLES = 4
    HARD_CAP = 30
    RUNTIME_BUDGET_S = 240.0
    ERROR_GROWTH_RATIO = 1.20       # stop if errors > 120% of initial
    PLATEAU_CYCLES = 2
    NO_PROGRESS_CYCLES = 2
    REJECT_SPIKE_THRESHOLD = 4      # total rejects in 2 cycles with 0 accepts
    EXTEND_STEP = 2                 # extra cycles granted per extension event

    def __init__(self, initial_error_count: int, config: Dict[str, Any]) -> None:
        pc = ((config or {}).get("pipeline") or {})
        self.initial_error_count = max(initial_error_count, 0)
        self.max_cycles = int(pc.get("max_global_cycles", self.DEFAULT_MAX_CYCLES))
        self.hard_cap = int(pc.get("max_global_cycles_cap", self.HARD_CAP))
        self.runtime_budget_s = float(pc.get("cycle_runtime_budget_s", self.RUNTIME_BUDGET_S))

        self._cycle = 0
        self._history: List[Dict[str, Any]] = []
        self._start_time = time.time()
        self._prev_error_count = self.initial_error_count
        self._prev_total_accepted = 0
        self._prev_total_rejected = 0
        self._prev_total_nr = 0
        self.stop_reason: Optional[str] = None

    @property
    def current_cycle(self) -> int:
        return self._cycle

    def should_continue(
        self,
        current_error_count: int,
        total_accepted: int,
        total_rejected: int,
        total_nr: int,
    ) -> bool:
        """Record one cycle's snapshot and return True to run another cycle.

        Must be called once after each completed cycle (including the first).
        Sets self.stop_reason when returning False.
        """
        self._cycle += 1
        elapsed = time.time() - self._start_time

        cycle_accepted = total_accepted - self._prev_total_accepted
        cycle_rejected = total_rejected - self._prev_total_rejected
        cycle_nr       = total_nr       - self._prev_total_nr
        delta_errors   = self._prev_error_count - current_error_count  # positive = improvement

        snap: Dict[str, Any] = {
            "cycle":          self._cycle,
            "error_count":    current_error_count,
            "delta_errors":   delta_errors,
            "cycle_accepted": cycle_accepted,
            "cycle_rejected": cycle_rejected,
            "cycle_nr":       cycle_nr,
            "elapsed_s":      round(elapsed, 1),
        }
        self._history.append(snap)
        logger.debug("AdaptiveCC cycle %d snap: %s", self._cycle, snap)

        self._prev_error_count    = current_error_count
        self._prev_total_accepted = total_accepted
        self._prev_total_rejected = total_rejected
        self._prev_total_nr       = total_nr

        # ── stop conditions, checked in priority order ──────────────────────

        # 1. completed
        if current_error_count == 0:
            self.stop_reason = "completed"
            logger.info("AdaptiveCC: completed — 0 errors left")
            return False

        # 2. global_invariant: errors grew > 20% above initial
        if self.initial_error_count > 0:
            if current_error_count > self.initial_error_count * self.ERROR_GROWTH_RATIO:
                self.stop_reason = "global_invariant"
                logger.warning(
                    "AdaptiveCC: global_invariant — %d errors > %.0f%% of initial %d",
                    current_error_count,
                    self.ERROR_GROWTH_RATIO * 100,
                    self.initial_error_count,
                )
                return False

        # 3. runtime_budget exceeded
        if elapsed >= self.runtime_budget_s:
            self.stop_reason = "runtime_budget"
            logger.warning(
                "AdaptiveCC: runtime_budget — %.0fs elapsed >= budget %.0fs",
                elapsed,
                self.runtime_budget_s,
            )
            return False

        # 4. max_cycles reached — try extending if progress warrants it
        if self._cycle >= self.max_cycles:
            if self._should_extend(elapsed):
                new_cap = min(self.max_cycles + self.EXTEND_STEP, self.hard_cap)
                logger.info(
                    "AdaptiveCC: extending max_cycles %d → %d (sustained progress)",
                    self.max_cycles,
                    new_cap,
                )
                self.max_cycles = new_cap
                return True
            self.stop_reason = "max_cycles"
            logger.info("AdaptiveCC: max_cycles=%d reached, no extension justified", self._cycle)
            return False

        # 5. error_growth: errors increased two cycles in a row — НО только если
        #    мы реально стали хуже старта (current > initial). Иначе это фантом
        #    (2026-07-10, spdlog): advisory-инструменты (semgrep) с throttle
        #    возвращают свои же находки постепенно по циклам (0→1→3), их
        #    вытеснил/вернул пере-скан ValidateStage — счётчик растёт (16→17→19)
        #    БЕЗ реального ухудшения (19 ≤ initial 21, откатов 0), а прогон
        #    обрывался рано, теряя ремонтную ёмкость. Реальный runaway ловит
        #    global_invariant (>ratio×initial) выше; плохой патч — per-patch
        #    net-delta. Здесь оставляем брейк на СТАБИЛЬНЫЙ рост ВЫШЕ старта.
        if len(self._history) >= 2:
            if (all(s["delta_errors"] < 0 for s in self._history[-2:])
                    and current_error_count > self.initial_error_count):
                self.stop_reason = "error_growth"
                logger.warning(
                    "AdaptiveCC: error_growth — errors rose two cycles И выше старта "
                    "(%d > initial %d)", current_error_count, self.initial_error_count)
                return False

        # 6. plateau: 0 accepted AND 0 delta for PLATEAU_CYCLES consecutive cycles
        if len(self._history) >= self.PLATEAU_CYCLES:
            recent = self._history[-self.PLATEAU_CYCLES:]
            if all(s["cycle_accepted"] == 0 and s["delta_errors"] == 0 for s in recent):
                self.stop_reason = "plateau"
                logger.info(
                    "AdaptiveCC: plateau — no change for %d consecutive cycles",
                    self.PLATEAU_CYCLES,
                )
                return False

        # 7. no_progress: 0 accepted, no error reduction for NO_PROGRESS_CYCLES
        if len(self._history) >= self.NO_PROGRESS_CYCLES:
            recent = self._history[-self.NO_PROGRESS_CYCLES:]
            if all(s["cycle_accepted"] == 0 and s["delta_errors"] <= 0 for s in recent):
                self.stop_reason = "no_progress"
                logger.info(
                    "AdaptiveCC: no_progress — 0 accepts, delta_errors <= 0 for %d cycles",
                    self.NO_PROGRESS_CYCLES,
                )
                return False

        # 8. reject_spike: many rejects with 0 accepts across 2 cycles
        if len(self._history) >= 2:
            recent = self._history[-2:]
            r_acc = sum(s["cycle_accepted"] for s in recent)
            r_rej = sum(s["cycle_rejected"] for s in recent)
            if r_acc == 0 and r_rej >= self.REJECT_SPIKE_THRESHOLD:
                self.stop_reason = "reject_spike"
                logger.info(
                    "AdaptiveCC: reject_spike — 0 accepts, %d rejects in last 2 cycles",
                    r_rej,
                )
                return False

        return True

    def _should_extend(self, elapsed: float) -> bool:
        """Return True if the last cycle justifies raising the cycle cap."""
        if self._cycle >= self.hard_cap:
            return False
        if elapsed >= self.runtime_budget_s * 0.80:
            return False
        if not self._history:
            return False
        last = self._history[-1]
        return last["cycle_accepted"] > 0 or last["delta_errors"] > 0

    def summary(self) -> Dict[str, Any]:
        return {
            "cycles_run":       self._cycle,
            "max_cycles_final": self.max_cycles,
            "stop_reason":      self.stop_reason or "running",
            "history":          self._history,
            "total_elapsed_s":  round(time.time() - self._start_time, 1),
        }
