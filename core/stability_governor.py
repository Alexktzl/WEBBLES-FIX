"""
Губернатор стабильности для webles_conveyor.
Адаптивно регулирует параметры конвейера на основе здоровья системы.
"""

import logging
from typing import Any, Dict, Optional

from core.system_health import SystemHealth, SystemHealthEvaluator

logger = logging.getLogger(__name__)


class StabilityGovernor:
    """
    Подстраивает управляющие параметры конвейера на основе здоровья системы.
    """

    DEFAULT_PARAMS = {
        "accept_threshold": 0.3,
        "retry_threshold": -0.3,
        "exploration_rate": 0.5,
        "max_patch_size": 100,
        "max_iterations": 10,
        "rollback_limit": 3,
    }

    def __init__(
        self,
        health_evaluator: SystemHealthEvaluator,
        base_params: Optional[Dict[str, float]] = None,
        adjustment_rate: float = 0.1,
        hysteresis: float = 0.05
    ):
        self.health_evaluator = health_evaluator
        self.base_params = base_params or self.DEFAULT_PARAMS.copy()
        self.adjustment_rate = adjustment_rate
        self.hysteresis = hysteresis
        self.current_params = self.base_params.copy()
        self.last_health: Optional[SystemHealth] = None

    def step(self) -> Dict[str, float]:
        health = self.health_evaluator.evaluate()
        overall = health.overall()

        if self.last_health is None:
            self.last_health = health
            return self.current_params

        prev_overall = self.last_health.overall()
        delta = overall - prev_overall

        if abs(delta) > self.hysteresis:
            self._adjust_accept_threshold(health, delta)
            self._adjust_exploration_rate(health, delta)
            self._adjust_max_patch_size(health, delta)
            self._adjust_iteration_limit(health, delta)
            self._adjust_rollback_limit(health, delta)

        self.last_health = health
        logger.debug(f"System health: {overall:.3f}, params: {self.current_params}")
        return self.current_params

    def _adjust_accept_threshold(self, health: SystemHealth, delta: float) -> None:
        base = self.base_params["accept_threshold"]
        current = self.current_params["accept_threshold"]
        target_shift = (1.0 - health.stability) * 0.2 + (1.0 - health.patch_success_rate) * 0.15
        target = base + target_shift
        target = max(0.1, min(0.7, target))
        self.current_params["accept_threshold"] = current + self.adjustment_rate * (target - current)

    def _adjust_exploration_rate(self, health: SystemHealth, delta: float) -> None:
        base = self.base_params["exploration_rate"]
        current = self.current_params["exploration_rate"]
        target_shift = health.entropy * 0.3 + (1.0 - health.memory_hit_rate) * 0.2
        target = base + target_shift
        target = max(0.2, min(0.8, target))
        self.current_params["exploration_rate"] = current + self.adjustment_rate * (target - current)

    def _adjust_max_patch_size(self, health: SystemHealth, delta: float) -> None:
        base = self.base_params["max_patch_size"]
        current = self.current_params["max_patch_size"]
        risk_factor = health.rollback_rate * 0.8 + (1.0 - health.stability) * 0.5
        target = base * (1.0 - risk_factor * 0.7)
        target = max(20, min(200, target))
        self.current_params["max_patch_size"] = current + self.adjustment_rate * (target - current)

    def _adjust_iteration_limit(self, health: SystemHealth, delta: float) -> None:
        base = self.base_params["max_iterations"]
        current = self.current_params["max_iterations"]
        target_shift = health.error_trend_slope * 5.0
        target = base + target_shift
        target = max(5, min(30, target))
        self.current_params["max_iterations"] = int(round(current + self.adjustment_rate * (target - current)))

    def _adjust_rollback_limit(self, health: SystemHealth, delta: float) -> None:
        base = self.base_params["rollback_limit"]
        current = self.current_params["rollback_limit"]
        target_shift = - (1.0 - health.stability) * 2.0
        target = base + target_shift
        target = max(1, min(5, target))
        self.current_params["rollback_limit"] = int(round(current + self.adjustment_rate * (target - current)))

    def get_params(self) -> Dict[str, float]:
        return self.current_params.copy()

    def reset(self) -> None:
        self.current_params = self.base_params.copy()
        self.last_health = None