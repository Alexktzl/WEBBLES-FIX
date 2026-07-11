"""
Агрегатор скоринга для webles_conveyor.
Оркестрирует компоненты скоринга и предоставляет единый интерфейс оценки.
Жёсткий гейт компиляции теперь учитывает before_validation.
"""

from typing import Any, Dict, Optional, Union

from analysis.error_intelligence.error_differ import ErrorDiff
from analysis.scoring.score_components import ScoreComponents, ScoringConfig


class ScoreAggregator:
    """
    Собирает итоговую оценку качества исправления.
    """

    def __init__(self, config: ScoringConfig):
        self.config = config
        self.components = ScoreComponents(config)

    def evaluate(
        self,
        diff: ErrorDiff,
        validation_results: Dict[str, Any],
        before_validation: Optional[Dict[str, Any]] = None,
        patch_size: int = 0,
        files_modified: int = 0,
        iteration_count: int = 0,
        debug: bool = False,
        patch_source: str = "",
    ) -> Union[float, Dict[str, Any]]:
        """
        Возвращает итоговую оценку в диапазоне [-1, 1].
        При debug=True возвращает словарь с детализацией.
        """
        # Жёсткий гейт – только если компиляция была успешной до патча и сломалась после
        if self.components.hard_fail(validation_results, before_validation):
            score = -1.0
            if debug:
                return {"score": score, "hard_fail": True}
            return score

        score = self.components.final_score(
            diff=diff,
            validation_results=validation_results,
            before_validation=before_validation,
            patch_size=patch_size,
            files_modified=files_modified,
            iteration_count=iteration_count,
            patch_source=patch_source,
        )

        if debug:
            return {
                "score": score,
                "hard_fail": False,
                "components": {
                    "net_diff": self.components.net_diff_score(diff),
                    "compile": self.components.compile_score(validation_results),
                    "security": self.components.security_score(validation_results, before_validation),
                    "lint": self.components.lint_score(validation_results, before_validation),
                    "patch_cost": self.components.patch_cost_penalty(patch_size, files_modified),
                    "iteration": self.components.iteration_penalty(iteration_count),
                },
            }
        return score