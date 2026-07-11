"""
Взвешенная модель ошибок для webles_conveyor.
Строгий API: используйте WeightedErrorModel.from_error(error, **kwargs)
Версия модели: 1.0
"""

import math
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Union

# Версия модели весов – меняется при изменении формулы или дефолтных параметров
MODEL_VERSION = "1.0"

# Иммутабельные значения по умолчанию
_DEFAULT_TYPE_MULTIPLIERS = MappingProxyType({
    "syntax": 1.2,
    "compile": 1.2,
    "type": 1.1,
    "dependency": 1.0,
    "runtime": 0.9,
    "security": 2.0,
    "lint": 0.3,
    "warning": 0.2,
    "info": 0.05,
    "unknown": 0.8,
})

_DEFAULT_CONFIDENCE_DECAY = MappingProxyType({
    "security": 3.0,
    "compile": 2.5,
    "syntax": 2.5,
    "type": 2.0,
    "dependency": 1.5,
    "runtime": 1.5,
    "lint": 1.0,
    "warning": 0.8,
    "info": 0.5,
    "unknown": 1.8,
})


class WeightedErrorModel:
    """Модель веса ошибки. Единая точка входа: from_error()."""

    VERSION = MODEL_VERSION

    @staticmethod
    def from_error(error: Dict[str, Any], **kwargs) -> float:
        """
        Основной метод. Принимает словарь ошибки.
        Поля 'error_type', 'confidence' рекомендуются, но не обязательны.
        'base_weight' – опционально (по умолчанию 1.0).
        """
        # Извлекаем значения с умолчаниями, защищая от падения
        base_weight = error.get("base_weight", 1.0)
        if base_weight is None:
            base_weight = 1.0
        error_type = error.get("error_type", "unknown")
        if error_type is None:
            error_type = "unknown"
        confidence = error.get("confidence", 0.5)
        if confidence is None:
            confidence = 0.5

        return WeightedErrorModel._get_weight_impl(
            base_weight=float(base_weight),
            error_type=str(error_type),
            confidence=float(confidence),
            **kwargs
        )

    @staticmethod
    def get_weight(*args, **kwargs) -> Union[float, Dict[str, float]]:
        """
        Низкоуровневый API. Предпочтительнее использовать from_error().
        """
        if args and isinstance(args[0], dict):
            return WeightedErrorModel.from_error(args[0], **kwargs)
        if "error" in kwargs:
            error = kwargs.pop("error")
            return WeightedErrorModel.from_error(error, **kwargs)

        required = {"base_weight", "error_type", "confidence"}
        if not required.issubset(kwargs):
            raise ValueError(f"При явном вызове необходимы параметры: {required}")
        return WeightedErrorModel._get_weight_impl(**kwargs)

    @staticmethod
    def _get_weight_impl(
        base_weight: float,
        error_type: str,
        confidence: float,
        scale: float = 100.0,
        type_multipliers: Optional[Dict[str, float]] = None,
        confidence_decay: Optional[Dict[str, float]] = None,
        debug: bool = False,
    ) -> Union[float, Dict[str, float]]:
        """Реализация вычисления веса."""
        base_weight = max(0.0, float(base_weight))
        confidence = max(0.0, min(1.0, float(confidence)))
        scale = max(1.0, float(scale))

        if type_multipliers is None:
            type_multipliers = dict(_DEFAULT_TYPE_MULTIPLIERS)
        if confidence_decay is None:
            confidence_decay = dict(_DEFAULT_CONFIDENCE_DECAY)

        k = confidence_decay.get(error_type, 2.0)
        confidence_factor = math.exp(-k * (1.0 - confidence))
        type_mult = type_multipliers.get(error_type, 1.0)

        raw = base_weight * confidence_factor * type_mult
        weight = scale * math.tanh(raw / scale)

        if debug:
            return {
                "final": weight,
                "base": base_weight,
                "confidence": confidence,
                "decay_constant": k,
                "confidence_factor": confidence_factor,
                "type": error_type,
                "type_multiplier": type_mult,
                "raw": raw,
                "scale": scale,
                "model_version": MODEL_VERSION,
            }
        return weight

    @staticmethod
    def total_weight(
        errors_data: List[Dict[str, Any]],
        scale: float = 100.0,
        type_multipliers: Optional[Dict[str, float]] = None,
        confidence_decay: Optional[Dict[str, float]] = None,
    ) -> float:
        total = 0.0
        for data in errors_data:
            total += WeightedErrorModel.from_error(data, scale=scale,
                                                   type_multipliers=type_multipliers,
                                                   confidence_decay=confidence_decay)
        return total