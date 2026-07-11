"""
Стадия перехода к следующей ошибке.
Вынесена из pipeline_stage.py для модульности.
Содержит DEBUG-логи.
"""

import logging
from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

logger = logging.getLogger(__name__)


class NextErrorStage(PipelineStage):
    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия NEXT_ERROR: переход к следующей ошибке...")
        logger.debug("  Сброс selected_error и generated_patch")
        context = context.set_selected_error(None)
        context = context.set_patch(None)
        new_metadata = dict(context.metadata)
        new_metadata.pop("patch_attempt", None)
        new_metadata.pop("use_full_file", None)
        # Очищаем флаг broken_file_mode, чтобы он не влиял на другие файлы
        new_metadata.pop("broken_file_mode", None)
        # Очищаем per-patch поля: structured_edit и intent от предыдущей итерации
        # не должны попадать в review-prompt следующей ошибки (P1: review leakage).
        new_metadata.pop("structured_edit", None)
        new_metadata.pop("intent", None)
        # 2026-06-24 (pytils, needs_review-аудит): risks/confidence/patch_source
        # выставляются ТОЛЬКО при успешном structured_llm-ответе (см.
        # generate_patch_stage.py) и никогда не сбрасывались — если СЛЕДУЮЩАЯ
        # ошибка не дошла до успешной генерации (например, F821 "неисправима
        # в этой фазе" → сразу needs_review), в JSON записи попадали risks/
        # confidence/patch_source от СОВЕРШЕННО ДРУГОЙ, давно обработанной
        # ошибки — нашлось 17 из 19 needs_review-записей в одном прогоне
        # с пустым patch/intent, но с чужим, не относящимся к делу risks-текстом.
        new_metadata.pop("risks", None)
        new_metadata.pop("confidence", None)
        new_metadata.pop(MetadataKeys.PATCH_SOURCE, None)
        new_metadata.pop("file_content_hash", None)
        new_metadata.pop("_stale_retries", None)
        # Сбрасываем attempt buffer при переходе к новой ошибке
        new_metadata.pop("_attempt_history", None)
        new_metadata.pop("last_patch_intent", None)
        # Сбрасываем escalation флаг — он устанавливается заново planning_stage при необходимости
        new_metadata.pop("_escalation", None)
        # control series 12 fix verification (2026-06-20, cantools/textparser):
        # segmented_attempts — счётчик per-error (сколько раз GeneratePatchStage
        # уходил в _segmented_strategy для ОДНОЙ ошибки), но никогда не сбрасывался
        # при переходе к новой. После добавления deadline-проверки внутри
        # segmented-петли (см. GeneratePatchStage._deadline_exceeded) ошибки
        # стали обрабатываться намного быстрее, и этот счётчик быстро копился
        # через РАЗНЫЕ ошибки: 4-я ошибка проекта уже стартовала с
        # segmented_attempts=4 > MAX_SEGMENTED_STRATEGY_ATTEMPTS=3 и
        # немедленно помечалась unfixable без единой реальной попытки.
        new_metadata.pop(MetadataKeys.SEGMENTED_ATTEMPTS, None)
        # C6/C4 (аудит 2026-07-01): per-patch артефакты предыдущей ошибки.
        # _pre_patch_content — страховочная очистка (основные точки: ACCEPT в
        # DecideStage, rollback-пути, net-delta в ValidateStage): оставленная
        # карта могла быть повторно записана на диск net-delta-откатом
        # следующей ошибки, стирая принятую работу. snapshot_failed относится
        # только к конкретному патчу — не должен блокировать ACCEPT следующего.
        new_metadata.pop("_pre_patch_content", None)
        new_metadata.pop("snapshot_failed", None)
        logger.debug("  Очищены patch_attempt, use_full_file, broken_file_mode, structured_edit, intent, file_content_hash, attempt_history, _escalation, segmented_attempts из метаданных")
        return context.update(metadata=new_metadata).add_state_to_history(
            State.PLANNING if context.config.get("pipeline", {}).get("use_planning") else State.ROOT_CAUSE
        )