"""
Контекст для проверки жёстких ограничений в webles_conveyor.
Хранит информацию о предыдущем состоянии компиляции и параметры уверенности.
Больше не импортирует сам себя и не содержит циклических зависимостей.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ConstraintContext:
    """
    Неизменяемый контекст для оценки жёстких ограничений.
    """

    # Информация о компиляции до патча
    compile_known: bool = False
    compile_was_passing: bool = False

    # Пороги уверенности
    security_confidence_threshold: float = 0.7

    # Дополнительные настройки
    intent: Optional[str] = None
    search_depth: int = 0
    iteration_count: int = 0

    def is_compile_stable_pass(self) -> bool:
        """Возвращает True, если достоверно известно, что компиляция проходила."""
        return self.compile_known and self.compile_was_passing

    @classmethod
    def from_validation(
        cls,
        before_validation: Optional[Dict[str, Any]] = None,
        intent: Optional[str] = None,
        search_depth: int = 0,
        iteration_count: int = 0,
    ) -> "ConstraintContext":
        """
        Создаёт контекст на основе результатов валидации до патча.
        """
        compile_known = False
        compile_was_passing = False

        if before_validation:
            compile_before = before_validation.get("compile", {})
            if "success" in compile_before:
                compile_known = True
                compile_was_passing = compile_before.get("success", False)

        return cls(
            compile_known=compile_known,
            compile_was_passing=compile_was_passing,
            intent=intent,
            search_depth=search_depth,
            iteration_count=iteration_count,
        )