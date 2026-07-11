"""
Оценка здоровья системы webles_conveyor.
Вычисляет метрики на основе скользящего окна состояний конвейера.
Теперь включает расстояние до эталона (baseline_distance).
"""

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.pipeline_context import PipelineContext
from core.state_machine import State
from analysis.health_baseline import HealthBaseline
from analysis.error_classifier import ErrorClassifier


@dataclass
class SystemHealth:
    """Снимок здоровья системы, вычисленный на основе недавней истории."""
    stability: float
    error_trend_slope: float
    rollback_rate: float
    patch_success_rate: float
    memory_hit_rate: float
    entropy: float
    baseline_distance: float = 0.0   # расстояние до эталона (0 = идеал)

    def overall(self) -> float:
        return (
            0.20 * self.stability +
            0.20 * self.patch_success_rate +
            0.15 * (1.0 - self.rollback_rate) +
            0.15 * self.memory_hit_rate +
            0.10 * (1.0 - abs(self.error_trend_slope)) +
            0.20 * (1.0 - self.baseline_distance)
        )

    def to_dict(self) -> Dict[str, float]:
        return {
            "stability": self.stability,
            "error_trend_slope": self.error_trend_slope,
            "rollback_rate": self.rollback_rate,
            "patch_success_rate": self.patch_success_rate,
            "memory_hit_rate": self.memory_hit_rate,
            "entropy": self.entropy,
            "baseline_distance": self.baseline_distance,
            "overall": self.overall(),
        }


class SystemHealthEvaluator:
    """Вычисляет SystemHealth по скользящему окну состояний PipelineContext."""

    def __init__(self, window_size: int = 10, classifier: Optional[ErrorClassifier] = None):
        self.window_size = window_size
        self.history: deque = deque(maxlen=window_size)
        self.error_counts: deque = deque(maxlen=window_size)
        self.decisions: deque = deque(maxlen=window_size)
        self.baseline: Optional[HealthBaseline] = None
        self.classifier = classifier

    def set_baseline(self, baseline: HealthBaseline) -> None:
        """Устанавливает эталон здоровья."""
        self.baseline = baseline

    def update(self, context: PipelineContext) -> None:
        self.history.append(context)
        self.error_counts.append(len(context.current_errors))
        decision = self._infer_decision(context)
        self.decisions.append(decision)

    def _infer_decision(self, context: PipelineContext) -> str:
        history = context.state_history
        if len(history) < 2:
            return "UNKNOWN"
        prev_state = history[-2]
        curr_state = history[-1]
        if prev_state == State.DECIDING:
            if curr_state == State.COMPLETED:
                return "ACCEPT"
            elif curr_state == State.NEXT_ERROR:
                return "RETRY"
            elif curr_state == State.ROLLING_BACK:
                return "REJECT"
            elif curr_state == State.FAILED:
                return "ESCALATE"
        # Tech debt audit 2026-06-21, #2: патчи, обработанные ParallelExecutor
        # (core/engine/parallel.py), никогда не проходят через DecideStage —
        # last_decision для них не выставляется, поэтому без этой проверки они
        # молча тонули в общем "UNKNOWN". metadata["_parallel_touched_files"]
        # (введён в b94924b) — надёжный маркер именно такого контекста.
        if context.metadata.get("_parallel_touched_files"):
            return "PARALLEL_BATCH"
        return context.metadata.get("last_decision", "UNKNOWN")

    def evaluate(self) -> SystemHealth:
        if len(self.history) < 2:
            return SystemHealth(
                stability=1.0, error_trend_slope=0.0, rollback_rate=0.0,
                patch_success_rate=1.0, memory_hit_rate=0.0, entropy=0.0,
                baseline_distance=0.0
            )
        baseline_dist = self._compute_baseline_distance()
        return SystemHealth(
            stability=self._compute_stability(),
            error_trend_slope=self._compute_error_trend(),
            rollback_rate=self._compute_rollback_rate(),
            patch_success_rate=self._compute_patch_success_rate(),
            memory_hit_rate=self._compute_memory_hit_rate(),
            entropy=self._compute_entropy(),
            baseline_distance=baseline_dist,
        )

    def _compute_baseline_distance(self) -> float:
        """Вычисляет расстояние до эталона на основе последнего состояния."""
        if self.baseline is None or not self.history:
            return 0.0
        ctx = self.history[-1]
        # Подсчёт взвешенной суммы ошибок через классификатор
        current_error_weight = 0.0
        if self.classifier is not None:
            for err in ctx.current_errors:
                current_error_weight += self.classifier.get_weight(err)
        else:
            current_error_weight = len(ctx.current_errors) * 50.0  # fallback

        compile_success = ctx.validation_results.get("compile", {}).get("success", True)
        security_ok = ctx.validation_results.get("security", {}).get("success", True)
        lint_count = len(ctx.validation_results.get("lint", {}).get("output", []))
        return self.baseline.distance(
            current_error_weight, compile_success, security_ok, lint_count
        )

    def _compute_stability(self) -> float:
        rollbacks = sum(1 for ctx in self.history if ctx.rollback_count > 0)
        failures = sum(1 for ctx in self.history if ctx.current_state in (State.FAILED, State.CIRCUIT_OPEN))
        instability_events = rollbacks + failures
        return 1.0 - (instability_events / len(self.history)) if self.history else 1.0

    def _compute_error_trend(self) -> float:
        if len(self.error_counts) < 2:
            return 0.0
        n = len(self.error_counts)
        x = list(range(n))
        y = list(self.error_counts)
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        denominator = sum((x[i] - mean_x) ** 2 for i in range(n))
        if denominator == 0:
            return 0.0
        slope = numerator / denominator
        # Нормализация: отрицательный slope = улучшение → положительный trend
        return max(-1.0, min(1.0, -slope / 5.0))

    def _compute_rollback_rate(self) -> float:
        rollbacks = sum(1 for ctx in self.history if ctx.rollback_count > 0)
        return rollbacks / len(self.history) if self.history else 0.0

    def _compute_patch_success_rate(self) -> float:
        accepted = sum(len(ctx.accepted_patches) for ctx in self.history)
        rejected = sum(len(ctx.rejected_patches) for ctx in self.history)
        total = accepted + rejected
        return accepted / total if total > 0 else 1.0

    def _compute_memory_hit_rate(self) -> float:
        hits = sum(1 for ctx in self.history if ctx.metadata.get("patch_source") == "memory")
        total = sum(1 for ctx in self.history if ctx.generated_patch)
        return hits / total if total > 0 else 0.0

    def _compute_entropy(self) -> float:
        if not self.decisions:
            return 0.0
        counts: Dict[str, int] = {}
        for d in self.decisions:
            counts[d] = counts.get(d, 0) + 1
        total = len(self.decisions)
        entropy = 0.0
        for cnt in counts.values():
            p = cnt / total
            entropy -= p * math.log2(p) if p > 0 else 0
        max_entropy = math.log2(4)
        return entropy / max_entropy if max_entropy > 0 else 0.0

    def reset(self) -> None:
        self.history.clear()
        self.error_counts.clear()
        self.decisions.clear()