"""
Стадия классификации ошибок.
Вынесена из pipeline_stage.py для модульности.
Содержит подробные DEBUG-логи.
"""

import logging
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

logger = logging.getLogger(__name__)


class ClassifyStage(PipelineStage):
    """Классифицирует ошибки и определяет стратегию исправления."""

    def __init__(self, classifier):
        self.classifier = classifier
        logger.debug("ClassifyStage инициализирован")

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия CLASSIFY: классификация ошибок...")
        errors = list(context.current_errors)
        logger.info("  Всего ошибок для классификации: %d", len(errors))
        logger.debug("  Первые 3 ошибки до классификации: %s", errors[:3])
        
        classified_errors = []
        class_counts = {}
        for err in errors:
            classification = self.classifier.classify(err)
            new_class = classification.get("class", "UNKNOWN")
            # Не затираем error_class, уже выставленный анализатором (SecurityScanner,
            # bandit), если он содержательнее того, что вернул классификатор.
            existing = err.get("error_class")
            _PRESERVE = {"SECURITY", "BLOCKING", "CRITICAL_SYNTAX", "BUILD_SCRIPT", "MANIFEST"}
            if existing in _PRESERVE and new_class not in _PRESERVE:
                logger.debug("  Сохраняем error_class=%s (классификатор предложил %s) для %s:%s",
                             existing, new_class, err.get("file", ""), err.get("line", ""))
            else:
                err["error_class"] = new_class
            if not err.get("allowed_actions"):
                err["allowed_actions"] = classification.get("allowed_actions", [])
            class_counts[err["error_class"]] = class_counts.get(err["error_class"], 0) + 1
            classified_errors.append(err)
        
        logger.info("  Результаты классификации: %s", class_counts)
        logger.debug("  Первые 3 ошибки после классификации: %s", classified_errors[:3])
        
        context = context.set_errors(classified_errors)
        return context.add_state_to_history(State.PRE_CLEANUP)