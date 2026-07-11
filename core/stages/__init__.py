"""
Пакет стадий конвейера Webbles Fix.
Каждая стадия реализована в отдельном модуле для удобства поддержки.
"""

from core.stages.analyze_stage import AnalyzeStage
from core.stages.apply_patch_stage import ApplyPatchStage
from core.stages.classify_stage import ClassifyStage
from core.stages.cleanup_stage import CleanupStage
from core.stages.decide_stage import DecideStage
from core.stages.execute_plan_stage import ExecutePlanStage
from core.stages.final_resolve_stage import FinalResolveStage
from core.stages.generate_patch_stage import GeneratePatchStage
from core.stages.idle_stage import IdleStage
from core.stages.needs_review_stage import NeedsReviewStage
from core.stages.next_error_stage import NextErrorStage
from core.stages.planning_stage import PlanningStage
from core.stages.pre_cleanup_stage import PreCleanupStage
from core.stages.prioritize_stage import PrioritizeStage
from core.stages.review_stage import ReviewStage
from core.stages.rollback_stage import RollbackStage
from core.stages.root_cause_stage import RootCauseStage
from core.stages.validate_stage import ValidateStage

__all__ = [
    "AnalyzeStage",
    "ApplyPatchStage",
    "ClassifyStage",
    "CleanupStage",
    "DecideStage",
    "ExecutePlanStage",
    "FinalResolveStage",
    "GeneratePatchStage",
    "IdleStage",
    "NeedsReviewStage",
    "NextErrorStage",
    "PlanningStage",
    "PreCleanupStage",
    "PrioritizeStage",
    "ReviewStage",
    "RollbackStage",
    "RootCauseStage",
    "ValidateStage",
]
