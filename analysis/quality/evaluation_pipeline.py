"""
Конвейер оценки качества патча в webles_conveyor.
"""

from typing import Any, Dict, List, Optional

from analysis.constraints.constraint_policy import ConstraintPolicy
from analysis.constraints.hard_constraints import HardConstraints
from analysis.decision.decision_engine import DecisionEngine
from analysis.error_intelligence.error_differ import ErrorDiffer
from analysis.quality.evaluation_context import EvaluationContext
from analysis.scoring.score_aggregator import ScoreAggregator


class EvaluationPipeline:
    def __init__(
        self,
        hard_constraints: HardConstraints,
        constraint_policy: ConstraintPolicy,
        score_aggregator: ScoreAggregator,
        decision_engine: DecisionEngine,
        error_differ: ErrorDiffer,
    ):
        self.hard_constraints = hard_constraints
        self.constraint_policy = constraint_policy
        self.score_aggregator = score_aggregator
        self.decision_engine = decision_engine
        self.error_differ = error_differ

    def evaluate(self, context: EvaluationContext) -> Dict[str, Any]:
        diff = self.error_differ.diff(
            before_errors=context.before_errors,
            after_errors=context.after_errors,
        )
        score = self.score_aggregator.evaluate(
            diff=diff,
            validation_results=context.validation_results,
            before_validation=context.before_validation,
            patch_size=context.patch_size,
            files_modified=context.files_modified,
            iteration_count=context.iteration_count,
            patch_source=context.patch_source,
        )
        decision = self.decision_engine.decide(
            score,
            beam_scores=context.beam_scores,
            search_depth=context.search_depth,
            iteration_count=context.iteration_count,
        )
        return {
            "decision": decision,
            "score": score,
            "diff": diff,
        }