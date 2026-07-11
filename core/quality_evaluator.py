"""
Публичный интерфейс оценки качества исправлений в webles_conveyor.
Использует модульный EvaluationPipeline для всех проверок и расчётов.
Теперь включает пороги уверенности по типам ошибок, двухфазный ACCEPT и shadow-оценку.
ConstraintPolicy полноценно применяется к результатам HardConstraints.
Усиленный ACCEPT: если целевая ошибка исчезла, патч принимается независимо от скора.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from analysis.constraints.constraint_context import ConstraintContext
from analysis.constraints.constraint_policy import ConstraintPolicy
from analysis.constraints.hard_constraints import HardConstraints
from analysis.decision.decision_engine import DecisionEngine
from analysis.decision.threshold_model import ThresholdModel
from analysis.error_classifier import ErrorClassifier
from analysis.error_intelligence.error_differ import ErrorDiffer
from analysis.quality.evaluation_context import EvaluationContext
from analysis.quality.evaluation_pipeline import EvaluationPipeline
from analysis.scoring.score_aggregator import ScoreAggregator
from analysis.scoring.score_components import ScoringConfig

if TYPE_CHECKING:
    from core.pipeline_engine import PipelineEngine


class QualityEvaluator:
    """Основной интерфейс для оценки качества патча."""

    ACCEPT_THRESHOLDS = {
        "security": 0.8,
        "compile": 0.7,
        "syntax": 0.6,
        "dependency": 0.6,
        "runtime": 0.5,
        "lint": 0.3,
        "warning": 0.3,
        "info": 0.2,
        "unknown": 0.5,
    }

    def __init__(
        self,
        classifier: ErrorClassifier,
        config: Optional[ScoringConfig] = None,
        base_strictness: float = 1.0,
    ):
        self.classifier = classifier
        self.config = config or ScoringConfig()
        self.base_strictness = base_strictness

        self.error_differ = ErrorDiffer()
        self.hard_constraints = HardConstraints(classifier)
        self.constraint_policy = ConstraintPolicy(base_strictness)
        self.score_aggregator = ScoreAggregator(self.config)
        self.threshold_model = ThresholdModel()
        self.decision_engine = DecisionEngine(self.threshold_model)

        self.pipeline = EvaluationPipeline(
            hard_constraints=self.hard_constraints,
            constraint_policy=self.constraint_policy,
            score_aggregator=self.score_aggregator,
            decision_engine=self.decision_engine,
            error_differ=self.error_differ,
        )

    def evaluate(
        self,
        before_errors: List[Dict[str, Any]],
        after_errors: List[Dict[str, Any]],
        validation_results: Dict[str, Any],
        before_validation: Optional[Dict[str, Any]] = None,
        patch_size: int = 0,
        files_modified: int = 0,
        iteration_count: int = 0,
        search_depth: int = 0,
        beam_scores: Optional[List[float]] = None,
        intent: Optional[str] = None,
        patch_source: str = "",
        target_error_sig: Optional[str] = None,
        target_error_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        context = EvaluationContext(
            before_errors=before_errors,
            after_errors=after_errors,
            validation_results=validation_results,
            before_validation=before_validation,
            patch_size=patch_size,
            files_modified=files_modified,
            iteration_count=iteration_count,
            search_depth=search_depth,
            beam_scores=beam_scores,
            intent=intent,
            patch_source=patch_source,
        )
        result = self.pipeline.evaluate(context)

        # Принудительный ACCEPT, если целевая ошибка исчезла
        if target_error_sig:
            was_present = any(self._signature(e) == target_error_sig for e in before_errors)
            is_still_present = any(self._signature(e) == target_error_sig for e in after_errors)

            if was_present and not is_still_present:
                result["decision"] = "ACCEPT"
                result["reason"] = "target_error_fixed"
                result["confidence_ok"] = True
                result["score"] = max(result.get("score", 0.0), 0.5)
                return result
            elif was_present and is_still_present:
                result["decision"] = "REJECT"
                result["reason"] = "target_error_still_present"
                return result

        # Стандартная проверка (если target_error_sig не задана)
        violations = self.hard_constraints.check_simple(
            before_errors, after_errors, validation_results
        )
        constraint_ctx = ConstraintContext()
        policy_decision = self.constraint_policy.apply(violations, constraint_ctx)

        if policy_decision.get("action") in ("REJECT", "ESCALATE"):
            result["decision"] = policy_decision["action"]
            result["reason"] = policy_decision.get("reason", "policy_violation")
            return result

        threshold = self.ACCEPT_THRESHOLDS.get(target_error_type, 0.5)
        score = result.get("score", 0.0)
        if score >= threshold:
            result["decision"] = "ACCEPT"
            result["reason"] = "score_above_threshold"
            result["confidence_ok"] = True
        elif score >= -threshold:
            result["decision"] = "RETRY"
            result["reason"] = "score_in_retry_range"
        else:
            result["decision"] = "REJECT"
            result["reason"] = "score_below_threshold"

        return result

    def evaluate_hard_accept(
        self,
        soft_result: Dict[str, Any],
        before_errors: List[Dict[str, Any]],
        after_errors: List[Dict[str, Any]],
        target_error_sig: str,
        target_error_type: str,
    ) -> Dict[str, Any]:
        threshold = self.ACCEPT_THRESHOLDS.get(target_error_type, 0.5)

        was_present = any(self._signature(e) == target_error_sig for e in before_errors)
        is_still_present = any(self._signature(e) == target_error_sig for e in after_errors)

        if was_present and not is_still_present:
            score = soft_result.get("score", 0.0)
            if score >= threshold:
                return {"decision": "ACCEPT", "phase": "HARD", "score": score}
            else:
                return {"decision": "REJECT", "phase": "HARD", "reason": "below_threshold"}
        else:
            return {"decision": "REJECT", "phase": "HARD", "reason": "target_error_reappeared"}

    @staticmethod
    def _signature(error: Dict[str, Any]) -> str:
        # Единый источник — core.utils.error_signature.
        from core.utils import error_signature
        return error_signature(error)

    def check_shadow_false_positives(self, shadow_log: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        false_positives = []
        for entry in shadow_log:
            if entry.get("decision") == "ACCEPT":
                before = entry.get("before_errors", [])
                after = entry.get("after_errors", [])
                target_sig = entry.get("error_sig", "")
                was_present = any(self._signature(e) == target_sig for e in before)
                still_present = any(self._signature(e) == target_sig for e in after)
                if was_present and still_present:
                    false_positives.append(entry)
        return false_positives