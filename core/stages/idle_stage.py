"""
Стадия инициализации конвейера.
Вынесена из pipeline_stage.py для модульности.
Содержит DEBUG-логи.
"""

import logging
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

logger = logging.getLogger(__name__)


class IdleStage(PipelineStage):
    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия IDLE: инициализация конвейера")
        logger.debug("Сброс iteration_count и rollback_count")
        return context.update(iteration_count=0, rollback_count=0).add_state_to_history(State.ANALYZING)