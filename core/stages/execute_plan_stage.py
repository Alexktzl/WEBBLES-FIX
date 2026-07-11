"""
Стадия выполнения плана (запасная).
Вынесена из pipeline_stage.py для модульности.
Содержит DEBUG-логи.
"""

import logging
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

logger = logging.getLogger(__name__)


class ExecutePlanStage(PipelineStage):
    def execute(self, context: PipelineContext) -> PipelineContext:
        plan = context.metadata.get("planned_actions", [])
        if not plan:
            logger.info("Стадия EXECUTE_PLAN: план пуст, переход к генерации патча")
            return context.add_state_to_history(State.GENERATING_PATCH)
        logger.info("Стадия EXECUTE_PLAN: выполнение плана из %d действий", len(plan))
        logger.debug("Действия плана: %s", plan)
        return context.add_state_to_history(State.APPLYING_PATCH)