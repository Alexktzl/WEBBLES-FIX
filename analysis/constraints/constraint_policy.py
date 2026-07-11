"""
Политика применения ограничений для webles_conveyor.
На основе нарушений от HardConstraints и текущего уровня строгости принимает решение:
REJECT, ESCALATE или ALLOW (смягчение).
"""

from analysis.constraints.constraint_context import ConstraintContext


class ConstraintPolicy:
    """
    Принимает решение о действии на основе найденных нарушений.
    """

    def __init__(self, base_strictness: float = 1.0):
        self.base_strictness = base_strictness

    def apply(self, violations: dict, context: ConstraintContext) -> dict:
        """
        Возвращает решение по жёстким ограничениям.
        violations — результат HardConstraints.check_simple():
        {'action': 'ESCALATE'/'REJECT', 'reason': ...} или None.
        """
        if not violations:
            return {"action": "ALLOW", "reason": "no_violations"}

        action = violations.get("action", "REJECT")
        reason = violations.get("reason", "unknown")

        # Высокая строгость: любое нарушение → действие без смягчения
        if self.base_strictness >= 1.0:
            return {"action": action, "reason": reason}

        # Средняя строгость (0.5–1.0): эскалацию оставляем, REJECT смягчаем до ALLOW
        if 0.5 <= self.base_strictness < 1.0:
            if action == "ESCALATE":
                return {"action": "ESCALATE", "reason": reason}
            elif action == "REJECT":
                return {"action": "ALLOW", "reason": f"softened: {reason}"}

        # Низкая строгость (<0.5): даже эскалацию смягчаем до ALLOW
        return {"action": "ALLOW", "reason": f"softened: {reason}"}