"""
Модуль сравнения наборов ошибок до и после применения патча.
Использует стабильные сигнатуры и модель взвешивания ошибок.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from analysis.error_intelligence.error_signature import ErrorSignature
from analysis.scoring.weighted_error_model import WeightedErrorModel

WEIGHT_EPSILON = 1e-6
MAX_CACHE_SIZE = 10000


@dataclass
class ErrorDiff:
    resolved: List[Tuple[ErrorSignature, int]] = field(default_factory=list)
    new: List[Tuple[ErrorSignature, int]] = field(default_factory=list)
    regressed: List[Tuple[ErrorSignature, float, float, int, int]] = field(default_factory=list)
    improved: List[Tuple[ErrorSignature, float, float, int, int]] = field(default_factory=list)

    total_before_weight: float = 0.0
    total_after_weight: float = 0.0
    resolved_weight: float = 0.0
    new_weight: float = 0.0
    regression_weight: float = 0.0
    improved_weight: float = 0.0

    resolved_count: int = 0
    new_count: int = 0
    regressed_count: int = 0
    improved_count: int = 0

    def net_weight_delta(self) -> float:
        return self.resolved_weight + self.improved_weight - self.new_weight - self.regression_weight


class ErrorDiffer:
    def __init__(
        self,
        scale: float = 100.0,
        type_multipliers: Optional[Dict[str, float]] = None,
        confidence_decay: Optional[Dict[str, float]] = None,
        use_cache: bool = True,
    ):
        self.scale = scale
        self.type_multipliers = type_multipliers
        self.confidence_decay = confidence_decay
        self.use_cache = use_cache
        self._weight_cache: Dict[Tuple[float, str, float, float], float] = {}

    def _get_weight(self, error: Dict[str, Any]) -> float:
        """Вычисляет вес ошибки с кэшированием.

        Передаёт `type_multipliers` и `confidence_decay`, которые ранее
        принимались в __init__, но не пробрасывались дальше — настройки
        молча игнорировались.
        """
        if not self.use_cache:
            return WeightedErrorModel.get_weight(
                error,
                scale=self.scale,
                type_multipliers=self.type_multipliers,
                confidence_decay=self.confidence_decay,
            )

        base = error.get("base_weight", 0.0)
        etype = error.get("error_type", "unknown")
        conf = error.get("confidence", 0.0)
        # Кэш привязан к экземпляру ErrorDiffer, а type_multipliers/confidence_decay
        # фиксированы на экземпляре, поэтому ключ кэша их не учитывает.
        key = (round(base, 4), etype, round(conf, 4), round(self.scale, 2))
        if key in self._weight_cache:
            return self._weight_cache[key]
        w = WeightedErrorModel.get_weight(
            error,
            scale=self.scale,
            type_multipliers=self.type_multipliers,
            confidence_decay=self.confidence_decay,
        )
        if len(self._weight_cache) >= MAX_CACHE_SIZE:
            self._weight_cache.clear()
        self._weight_cache[key] = w
        return w

    def diff(
        self,
        before_errors: List[Dict[str, Any]],
        after_errors: List[Dict[str, Any]],
    ) -> ErrorDiff:
        before_agg: Dict[str, Tuple[ErrorSignature, float, int]] = {}
        for err in before_errors:
            sig = ErrorSignature.from_error_dict(err)
            sig.error_type = err.get("error_type", "unknown")
            weight = self._get_weight(err)
            h = sig.to_hash()
            if h in before_agg:
                prev_sig, prev_weight, prev_count = before_agg[h]
                before_agg[h] = (prev_sig, prev_weight + weight, prev_count + 1)
            else:
                before_agg[h] = (sig, weight, 1)

        after_agg: Dict[str, Tuple[ErrorSignature, float, int]] = {}
        for err in after_errors:
            sig = ErrorSignature.from_error_dict(err)
            sig.error_type = err.get("error_type", "unknown")
            weight = self._get_weight(err)
            h = sig.to_hash()
            if h in after_agg:
                prev_sig, prev_weight, prev_count = after_agg[h]
                after_agg[h] = (prev_sig, prev_weight + weight, prev_count + 1)
            else:
                after_agg[h] = (sig, weight, 1)

        diff = ErrorDiff()
        for h, (sig, w_before, count_before) in before_agg.items():
            diff.total_before_weight += w_before
            if h not in after_agg:
                diff.resolved.append((sig, count_before))
                diff.resolved_weight += w_before
                diff.resolved_count += count_before
            else:
                w_after, count_after = after_agg[h][1], after_agg[h][2]
                if w_after > w_before + WEIGHT_EPSILON:
                    diff.regressed.append((sig, w_before, w_after, count_before, count_after))
                    diff.regression_weight += w_after - w_before
                    diff.regressed_count += max(0, count_after - count_before)
                elif w_after < w_before - WEIGHT_EPSILON:
                    diff.improved.append((sig, w_before, w_after, count_before, count_after))
                    diff.improved_weight += w_before - w_after
                    diff.improved_count += max(0, count_before - count_after)

        for h, (sig, w_after, count_after) in after_agg.items():
            diff.total_after_weight += w_after
            if h not in before_agg:
                diff.new.append((sig, count_after))
                diff.new_weight += w_after
                diff.new_count += count_after

        return diff

    def clear_cache(self) -> None:
        self._weight_cache.clear()