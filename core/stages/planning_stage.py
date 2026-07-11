"""
Стадия планирования – полноценная версия.
Использует RepairPlanner с beam search, симуляцией и кэшированием состояний.
"""

import logging
from typing import Any, Dict, List, Optional

from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from core.utils import process_key as _process_key
from fixers.syntax_repair import SyntaxRepair

logger = logging.getLogger(__name__)


class PlanningStage(PipelineStage):
    def __init__(self, simulator=None, planner=None, memory=None, llm_client=None,
                 sandbox=None, state_cache=None, state_hasher=None, patch_scorer=None):
        self.simulator = simulator
        self.planner = planner
        self.memory = memory
        self.llm_client = llm_client
        self.sandbox = sandbox
        self.state_cache = state_cache
        self.state_hasher = state_hasher
        self.patch_scorer = patch_scorer
        logger.debug("PlanningStage инициализирован")

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия PLANNING: планирование действий...")

        # Проверяем доступность всех компонентов
        if not all([self.simulator, self.planner, self.sandbox, self.state_cache]):
            logger.warning("  Компоненты планирования не инициализированы – fallback")
            return self._fallback_select(context)

        errors = list(context.current_errors)
        if not errors:
            logger.info("  Нет ошибок для планирования")
            return context.add_state_to_history(State.COMPLETED)

        # Собираем кандидатов-патчей (эвристика, память, опционально LLM)
        available_patches = self._gather_available_patches(errors, context)

        # Вычисляем хеш начального состояния
        try:
            initial_hash = self.state_hasher.compute_hash(context)
        except Exception as e:
            logger.error(f"  Ошибка вычисления хеша состояния: {e}")
            return self._fallback_select(context)

        # Запускаем планировщик
        logger.info("  Запуск планировщика...")
        plan = self.planner.plan(
            initial_state_hash=initial_hash,
            current_errors=errors,
            available_patches=available_patches,
            language=context.language,
        )

        if not plan:
            logger.info("  План не найден – fallback")
            return self._fallback_select(context)

        logger.info(f"  План содержит {len(plan)} действий")
        # Сохраняем план в метаданных для последовательного выполнения
        new_metadata = dict(context.metadata)
        new_metadata["planned_actions"] = [
            {"error": act.error, "patch": act.patch, "source": act.source}
            for act in plan
        ]
        new_metadata["plan_score"] = plan[0].confidence  # для логов
        context = context.update(metadata=new_metadata)

        # Выбираем первое действие плана и переходим к его выполнению
        first_action = plan[0]
        context = context.set_selected_error(first_action.error)
        context = context.set_patch(first_action.patch)
        logger.info("  Переход к выполнению первого действия плана")
        return context.add_state_to_history(State.APPLYING_PATCH)

    def _gather_available_patches(
        self, errors: List[Dict[str, Any]], context: PipelineContext
    ) -> Dict[str, List[str]]:
        """Собирает доступные патчи для каждой ошибки из памяти и эвристик."""
        available: Dict[str, List[str]] = {}
        for error in errors:
            sig = self._error_signature(error)
            patches = []

            # 1. Эвристика для критического синтаксиса
            if error.get("error_class") == "CRITICAL_SYNTAX":
                file_path = context.working_path / error.get("file", "")
                if file_path.exists():
                    heuristic_patch = SyntaxRepair.heal_critical_syntax(error, file_path)
                    if heuristic_patch:
                        patches.append(heuristic_patch)
                        logger.debug(f"  Эвристический патч для {sig}")

            # 2. Память (MemoryLearning)
            if self.memory:
                known = self.memory.get_known_fix(sig)
                if known and known not in patches:
                    patches.append(known)
                    logger.debug(f"  Патч из памяти для {sig}")

            # 3. Опционально – быстрый запрос к LLM (можно добавить при необходимости)
            # if self.llm_client and not patches:
            #     ...

            if patches:
                available[sig] = patches
        logger.info(f"  Найдено патчей для {len(available)} сигнатур ошибок")
        return available

    _ESCALATION_MAX_ATTEMPTS = 2

    def _fallback_select(self, context: PipelineContext) -> PipelineContext:
        errors = context.prioritized_errors or list(context.current_errors)
        logger.info("  Выбор следующей ошибки из %d кандидатов", len(errors))
        unfixable_sigs = {self._error_signature(e) for e in (context.unfixable_errors or [])}
        for e in errors:
            sig = self._error_signature(e)
            if sig in unfixable_sigs:
                continue
            if context.processed_errors.get(_process_key(e), 0) < 3:
                logger.info("  Выбрана ошибка: %s:%s", e.get('file'), e.get('line'))
                context = context.set_selected_error(e)
                new_meta = dict(context.metadata)
                new_meta.pop("_escalation", None)
                context = context.update(metadata=new_meta)
                return context.add_state_to_history(State.GENERATING_PATCH)

        # Все fixable ошибки исчерпаны — пробуем escalation для unfixable
        unfixable = list(context.unfixable_errors or [])
        if unfixable:
            escalation_counts = context.metadata.get("_escalation_counts", {})
            for e in unfixable:
                sig = self._error_signature(e)
                if escalation_counts.get(sig, 0) < self._ESCALATION_MAX_ATTEMPTS:
                    logger.info(
                        "  ESCALATION: повторная попытка unfixable %s:%s (attempt %d)",
                        e.get('file'), e.get('line'),
                        escalation_counts.get(sig, 0) + 1,
                    )
                    new_counts = dict(escalation_counts)
                    new_counts[sig] = new_counts.get(sig, 0) + 1
                    new_meta = dict(context.metadata)
                    new_meta["_escalation"] = True
                    new_meta["_escalation_counts"] = new_counts
                    # Убираем из unfixable чтобы generate_patch_stage принял ошибку
                    remaining_unfixable = [u for u in unfixable if self._error_signature(u) != sig]
                    context = context.update(
                        metadata=new_meta,
                        unfixable_errors=remaining_unfixable,
                    )
                    context = context.set_selected_error(e)
                    return context.add_state_to_history(State.GENERATING_PATCH)

        logger.info("  Все ошибки обработаны, завершение")
        return context.add_state_to_history(State.COMPLETED)

    @staticmethod
    def _error_signature(error: Dict[str, Any]) -> str:
        return PipelineStage._static_signature(error)