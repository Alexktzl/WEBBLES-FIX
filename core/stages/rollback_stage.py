"""
Стадия отката изменений.
Вынесена из pipeline_stage.py для модульности.
Содержит DEBUG-логи.
При откате удаляет снимки инвариантов для файла из контекста.
"""

import logging
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

logger = logging.getLogger(__name__)


class RollbackStage(PipelineStage):
    def __init__(self, patch_engine, analyzer):
        self.patch_engine = patch_engine
        self.analyzer = analyzer
        logger.debug("RollbackStage инициализирован")

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия ROLLBACK: откат изменений...")

        # Удаляем снимки для файла, который откатывается
        if context.selected_error:
            file_name = context.selected_error.get("file", "")
            if file_name:
                context = context.remove_patch_snapshot(file_name)
                logger.debug("  Удалены снимки инвариантов для файла %s", file_name)

        logger.debug("  Попытка восстановления из backup")
        context = context.restore_backup()
        try:
            self.patch_engine.revert_last_patch()
            logger.info("  Откат успешен")
        except Exception as e:
            logger.warning("  Откат не потребовался или не удался: %s", e)
        return context.add_state_to_history(State.NEXT_ERROR)