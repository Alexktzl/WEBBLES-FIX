"""
Жёсткие ограничения для webles_conveyor.
Определяет критические условия, при которых патч должен быть отвергнут или эскалирован.
"""

from typing import Any, Dict, List, Optional

from analysis.constraints.constraint_context import ConstraintContext
from analysis.error_classifier import ErrorClassifier


class HardConstraints:
    """
    Проверяет жёсткие условия, не зависящие от скоринга.
    Возвращает словарь с действием и причиной или None, если ограничений нет.
    """

    def __init__(self, classifier: ErrorClassifier):
        self.classifier = classifier

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def check(
        self,
        validation_results: Dict[str, Any],
        context: ConstraintContext,
    ) -> Optional[Dict[str, str]]:
        """
        Основной метод, требующий ConstraintContext.
        Возвращает {'action': 'ESCALATE'/'REJECT', 'reason': ...} или None.
        """
        return self._run_checks(validation_results, context)

    def check_simple(
        self,
        before_errors: List[Dict[str, Any]],
        after_errors: List[Dict[str, Any]],
        validation_results: Dict[str, Any],
    ) -> Optional[Dict[str, str]]:
        """
        Упрощённая версия для QualityEvaluator.
        Использует ConstraintContext с параметрами по умолчанию.
        Возвращает {'action': 'ESCALATE'/'REJECT', 'reason': ...} или None.
        """
        # Создаём контекст с безопасными умолчаниями
        context = ConstraintContext()
        return self._run_checks(validation_results, context)

    # ------------------------------------------------------------------
    # Приватная логика проверок
    # ------------------------------------------------------------------
    def _run_checks(
        self,
        validation_results: Dict[str, Any],
        context: ConstraintContext,
    ) -> Optional[Dict[str, str]]:
        """Общая логика для обоих методов."""
        if not validation_results:
            return None

        # 1. Критические уязвимости безопасности
        security_escalation = self._check_security_escalation(validation_results, context)
        if security_escalation:
            return {"action": "ESCALATE", "reason": security_escalation}

        # 2. Сломанная компиляция (только если до этого собиралось)
        compile_rejection = self._check_compile_rejection(validation_results, context)
        if compile_rejection:
            return {"action": "REJECT", "reason": compile_rejection}

        return None

    def _check_security_escalation(
        self,
        validation_results: Dict[str, Any],
        context: ConstraintContext,
    ) -> Optional[str]:
        """Возвращает причину эскалации или None."""
        sec_result = validation_results.get("security", {})
        if sec_result.get("success", False):
            return None

        sec_errors = sec_result.get("output", [])
        if not isinstance(sec_errors, list):
            return None

        for err in sec_errors:
            if isinstance(err, dict):
                classification = self.classifier.classify(err)
                label = classification.get("class", "UNKNOWN")
                conf = classification.get("confidence", 0.0)
                severity = err.get("severity", "low")
                if conf > context.security_confidence_threshold and severity in ("critical", "high"):
                    return f"critical security issue: {label} (confidence={conf:.2f})"
        return None

    def _check_compile_rejection(
        self,
        validation_results: Dict[str, Any],
        context: ConstraintContext,
    ) -> Optional[str]:
        """Возвращает причину отказа или None."""
        compile_result = validation_results.get("compile", {})
        compile_success = compile_result.get("success", False)

        if context.is_compile_stable_pass() and not compile_success:
            return "compile was passing before, now failing"

        return None