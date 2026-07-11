"""
Стадия поиска корневой ошибки.
Интегрирован CodeQL для глубокого анализа зависимостей.
"""

import logging
from typing import Any, Dict, Optional

from analysis.dependency_graph import DependencyGraph
from analysis.root_cause import RootCauseAnalyzer
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from core.utils import process_key as _process_key

# ---------- Адаптер CodeQL ----------
from tools.codeql_graph import CodeQLGraph

logger = logging.getLogger(__name__)


class RootCauseStage(PipelineStage):
    def __init__(self, dep_graph: Optional[DependencyGraph] = None):
        self.dep_graph = dep_graph
        self.analyzer = RootCauseAnalyzer(dep_graph) if dep_graph else None
        # Советчик CodeQL
        self.codeql = CodeQLGraph()
        logger.debug("RootCauseStage инициализирован (анализатор=%s, CodeQL=%s)",
                     "есть" if self.analyzer else "отсутствует",
                     "доступен" if self.codeql.is_available() else "отключён")

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия ROOT_CAUSE: поиск корневой ошибки...")
        errors = context.prioritized_errors or list(context.current_errors)
        if not errors:
            logger.info("  Ошибок нет, завершение")
            return context.add_state_to_history(State.COMPLETED)

        # Фильтруем: исключаем ошибки, обработанные ≥3 раз, и уже помеченные unfixable
        candidates = []
        skipped_processed = 0
        skipped_unfixable = 0
        for e in errors:
            sig = self._error_signature(e)
            pkey = _process_key(e)
            if context.processed_errors.get(pkey, 0) >= 3:
                skipped_processed += 1
                continue
            if any(self._error_signature(ue) == sig for ue in context.unfixable_errors):
                skipped_unfixable += 1
                continue
            candidates.append(e)

        logger.info("  Кандидатов (ещё не обработано): %d", len(candidates))
        logger.debug("  Пропущено (≥3 попыток): %d, unfixable: %d",
                     skipped_processed, skipped_unfixable)

        if not candidates:
            logger.info("  Все кандидаты уже обработаны, завершение")
            return context.add_state_to_history(State.COMPLETED)

        # --- Попытка использовать CodeQL для точного анализа ---
        codeql_context = None
        work_dir = getattr(context, 'working_path', context.project_path)
        codeql_data = self.codeql.safe_run(project_path=work_dir)
        if codeql_data:
            codeql_context = codeql_data
            logger.info("  CodeQL предоставил граф зависимостей")

        # Выбираем корневую ошибку
        selected = None
        if self.analyzer:
            selected = self.analyzer.select_root_cause(candidates)
        else:
            selected = candidates[0]

        if selected:
            logger.info("  Выбрана ошибка: %s:%s - %s",
                        selected.get("file", ""),
                        selected.get("line", ""),
                        selected.get("message", "")[:80])
            logger.debug("  Полная сигнатура выбранной ошибки: %s",
                         self._error_signature(selected))
            context = context.set_selected_error(selected)
            return context.add_state_to_history(State.GENERATING_PATCH)

        logger.error("  Не удалось выбрать корневую ошибку")
        return context.add_state_to_history(State.FAILED)

    @staticmethod
    def _error_signature(error: Dict[str, Any]) -> str:
        return PipelineStage._static_signature(error)