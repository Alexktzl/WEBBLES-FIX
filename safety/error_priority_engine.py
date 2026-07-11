"""
Движок приоритизации ошибок для webles_conveyor.
Ранжирует ошибки по критичности для определения порядка исправления.
"""

from typing import Any, Dict, List


class ErrorPriorityEngine:
    """
    Ранжирует список ошибок на основе предопределённых критериев.
    """

    SEVERITY_WEIGHTS = {
        "critical": 100,
        "error": 10,
        "warning": 1,
        "info": 0,
    }

    def __init__(self, severity_weights: Dict[str, int] = None):
        self.severity_weights = severity_weights or self.SEVERITY_WEIGHTS.copy()

    def rank(self, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Сортирует ошибки по вычисленному приоритету (по убыванию).
        """
        if not errors:
            return []

        for err in errors:
            if "priority" not in err:
                severity = err.get("severity", "warning").lower()
                err["priority"] = self.severity_weights.get(severity, 1)

        return sorted(errors, key=lambda e: e.get("priority", 0), reverse=True)