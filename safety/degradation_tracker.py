"""
Трекер деградации для webles_conveyor.
Отслеживает количество ошибок и метрики, чтобы обнаружить ухудшение кодовой базы.
Все ключевые операции покрыты DEBUG-логами.
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class DegradationTracker:
    """
    Отслеживает метрики ошибок с течением времени для обнаружения деградации.
    """

    def __init__(self, history_size: int = 5):
        self.history_size = history_size
        self.error_count_history: List[int] = []
        self.metrics: Dict[str, Any] = {"error_count": 0}
        logger.debug("DegradationTracker инициализирован: history_size=%d", history_size)

    def update(self, current_errors: List[Dict[str, Any]]) -> None:
        """
        Обновляет трекер текущим набором ошибок.
        """
        current_count = len(current_errors)
        self.metrics["error_count"] = current_count
        self.error_count_history.append(current_count)

        if len(self.error_count_history) > self.history_size:
            removed = self.error_count_history.pop(0)
            logger.debug("История ошибок обрезана, удалено значение %d", removed)

        logger.debug("DegradationTracker обновлён: ошибок=%d, история=%s", current_count, self.error_count_history)

    def is_degrading(self) -> bool:
        """
        Проверяет, растёт ли количество ошибок.
        """
        if len(self.error_count_history) < 2:
            logger.debug("is_degrading: недостаточно данных (нужно >=2 точек, сейчас %d)", len(self.error_count_history))
            return False

        current = self.error_count_history[-1]
        previous_avg = sum(self.error_count_history[:-1]) / (len(self.error_count_history) - 1)
        degrading = current > previous_avg
        logger.debug("is_degrading: текущее=%d, среднее предыдущее=%.1f => %s",
                     current, previous_avg, "деградация" if degrading else "стабильно")
        return degrading

    def get_current_count(self) -> int:
        """Возвращает текущее количество ошибок."""
        return self.metrics.get("error_count", 0)

    def reset(self) -> None:
        """Очищает всю историю."""
        logger.debug("DegradationTracker сброшен")
        self.error_count_history.clear()
        self.metrics = {"error_count": 0}