""""
Модель адаптивных порогов для webles_conveyor.
"""

import math
from typing import List, Optional


class ThresholdModel:
    """
    Вычисляет пороги ACCEPT и RETRY на основе контекста.
    """

    def compute_thresholds(
        self,
        beam_scores: Optional[List[float]] = None,
        search_depth: int = 0,
        iteration_count: int = 0,
    ) -> tuple[float, float]:
        """
        Возвращает (accept_threshold, retry_threshold).
        """
        accept = 0.3
        retry = -0.3

        # Адаптация по перцентилям beam search
        if beam_scores and len(beam_scores) >= 5:
            sorted_scores = sorted(beam_scores)
            high = sorted_scores[int(len(sorted_scores) * 0.66)]
            low = sorted_scores[int(len(sorted_scores) * 0.33)]
            accept = max(accept * 0.8, high)
            retry = min(retry * 1.2, low)

        # Глубина поиска
        depth_factor = max(0.5, 1.0 - search_depth * 0.1)
        accept *= depth_factor
        retry *= depth_factor

        # Итерации
        iter_penalty = min(0.2, math.log1p(iteration_count) * 0.05)
        accept += iter_penalty
        retry -= iter_penalty * 0.5

        # Гарантия порядка
        if retry >= accept:
            mid = (accept + retry) / 2
            accept = mid + 0.05
            retry = mid - 0.05

        return accept, retry