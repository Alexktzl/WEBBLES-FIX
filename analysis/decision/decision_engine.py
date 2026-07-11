"""
Модуль принятия решений для webles_conveyor.
На основе нормализованного скора и адаптивных порогов определяет действие.
"""

from analysis.decision.threshold_model import ThresholdModel


class DecisionEngine:
    """
    Принимает решение (ACCEPT, RETRY, REJECT) на основе скора и контекста.
    """

    def __init__(self, threshold_model: ThresholdModel):
        self.threshold_model = threshold_model

    def decide(
        self,
        score: float,
        beam_scores: list[float] | None = None,
        search_depth: int = 0,
        iteration_count: int = 0,
    ) -> str:
        """
        Возвращает одно из: "ACCEPT", "RETRY", "REJECT".
        """
        accept_threshold, retry_threshold = self.threshold_model.compute_thresholds(
            beam_scores=beam_scores,
            search_depth=search_depth,
            iteration_count=iteration_count,
        )

        if score >= accept_threshold:
            return "ACCEPT"
        elif score >= retry_threshold:
            return "RETRY"
        else:
            return "REJECT"