"""
Стадия безопасной очистки (CLEANUP) на основе символьного графа.
Вынесена из pipeline_stage.py для модульности.
Содержит DEBUG-логи.
"""

import logging
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

logger = logging.getLogger(__name__)


class CleanupStage(PipelineStage):
    """Выполняет безопасную очистку на основе символьного графа (CLEANUP)."""

    def __init__(self, symbol_graph_analyzer):
        self.symbol_graph_analyzer = symbol_graph_analyzer
        logger.debug("CleanupStage инициализирован")

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия CLEANUP: анализ неиспользуемых символов...")
        symbols = self.symbol_graph_analyzer.analyze()
        logger.debug("  Найдено символов всего: %d", len(symbols))
        classified = self.symbol_graph_analyzer.classify_unused(symbols, cleanup_mode="safe")
        logger.info("  Найдено символов для безопасного удаления: %d", len(classified.get("safe", [])))
        logger.info("  Найдено символов для подавления: %d", len(classified.get("suppress", [])))

        for sym in classified.get("safe", []):
            self._apply_suppression_or_remove(context, sym, mode="safe")
        for sym in classified.get("suppress", []):
            self._apply_suppression_or_remove(context, sym, mode="suppress")

        return context.add_state_to_history(State.COMPLETED)

    def _apply_suppression_or_remove(self, context, sym, mode):
        logger.info("  Cleanup: %s для символа %s в %s",
                     mode, sym['name'],
                     sym.get('defined_at', {}).get('file', '?'))
        logger.debug("  Полные данные символа: %s", sym)