"""
Компоненты скоринга для webles_conveyor.
Рассчитывают отдельные метрики качества патча.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from analysis.error_intelligence.error_differ import ErrorDiff


@dataclass
class ScoringConfig:
    """Конфигурация весов для компонентов скоринга."""
    net_diff_weight: float = 1.0
    compile_weight: float = 1.5
    security_weight: float = 2.0
    lint_weight: float = 0.5
    patch_cost_weight: float = 0.3
    iteration_penalty_weight: float = 0.2


class ScoreComponents:
    """Вычисляет отдельные компоненты итоговой оценки."""

    def __init__(self, config: ScoringConfig):
        self.config = config

    def hard_fail(
        self,
        validation_results: Dict[str, Any],
        before_validation: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Возвращает True, если компиляция была успешной до патча и сломалась после.
        Если состояние до патча неизвестно, не блокирует.
        """
        compile_after = validation_results.get("compile", {})
        if not compile_after.get("success", False):
            if before_validation:
                compile_before = before_validation.get("compile", {})
                if compile_before.get("success", False):
                    return True  # Была успешной, стала неудачной – жёсткий провал
        return False

    def final_score(
        self,
        diff: ErrorDiff,
        validation_results: Dict[str, Any],
        before_validation: Optional[Dict[str, Any]] = None,
        patch_size: int = 0,
        files_modified: int = 0,
        iteration_count: int = 0,
        patch_source: str = "",
    ) -> float:
        """
        Вычисляет итоговый скор на основе взвешенных компонентов.
        """
        net = self.net_diff_score(diff)
        compile_ok = self.compile_score(validation_results, before_validation)
        security_ok = self.security_score(validation_results, before_validation)
        lint_ok = self.lint_score(validation_results, before_validation)
        patch_penalty = self.patch_cost_penalty(patch_size, files_modified)
        iter_penalty = self.iteration_penalty(iteration_count)

        score = (
            self.config.net_diff_weight * net +
            self.config.compile_weight * compile_ok +
            self.config.security_weight * security_ok +
            self.config.lint_weight * lint_ok +
            self.config.patch_cost_weight * patch_penalty +
            self.config.iteration_penalty_weight * iter_penalty
        )
        # Нормализация в [-1, 1]
        return max(-1.0, min(1.0, score))

    def net_diff_score(self, diff: ErrorDiff) -> float:
        """
        Основной сигнал: насколько изменился взвешенный вес ошибок.
        """
        if diff.total_before_weight == 0:
            return 0.0
        delta = diff.resolved_weight - diff.new_weight
        return delta / diff.total_before_weight

    def compile_score(
        self,
        validation_results: Dict[str, Any],
        before_validation: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Возвращает 1.0 если компиляция успешна, -1.0 если была успешной и сломалась,
        0.0 если состояние до патча неизвестно.
        """
        compile_after = validation_results.get("compile", {})
        if compile_after.get("success", False):
            return 1.0
        # Компиляция не успешна
        if before_validation:
            compile_before = before_validation.get("compile", {})
            if compile_before.get("success", False):
                return -1.0  # Сломалась
        return 0.0  # Не знаем, было ли до

    def security_score(
        self,
        validation_results: Dict[str, Any],
        before_validation: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Возвращает 1.0 если проверка безопасности пройдена, -1.0 если появились новые уязвимости, 0.0 если неизвестно."""
        sec_after = validation_results.get("security", {})
        if sec_after.get("success", False):
            return 1.0
        if before_validation and before_validation.get("security", {}).get("success", False):
            return -1.0
        return 0.0

    def lint_score(
        self,
        validation_results: Dict[str, Any],
        before_validation: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Возвращает 1.0 если линтер не нашёл новых предупреждений, -1.0 если нашёл, 0.0 если неизвестно."""
        lint_after = validation_results.get("lint", {})
        if lint_after.get("success", False):
            return 1.0
        if before_validation and before_validation.get("lint", {}).get("success", False):
            return -1.0
        return 0.0

    def patch_cost_penalty(self, patch_size: int, files_modified: int) -> float:
        """Штраф за размер патча и количество изменённых файлов."""
        penalty = 0.0
        if patch_size > 10:
            penalty -= 0.1
        if patch_size > 50:
            penalty -= 0.2
        if files_modified > 1:
            penalty -= 0.3
        return penalty

    def iteration_penalty(self, iteration_count: int) -> float:
        """Штраф за количество итераций."""
        return -0.1 * min(iteration_count, 5)