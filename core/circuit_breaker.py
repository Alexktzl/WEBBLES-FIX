"""
Паттерн Circuit Breaker для webles_conveyor.
Предотвращает повторные попытки операций, которые с высокой вероятностью завершатся неудачей.
"""

from typing import Optional


class CircuitBreaker:
    """
    Простой предохранитель, размыкающий цепь после порога последовательных сбоев.
    """

    def __init__(self, failure_threshold: int = 3, reset_timeout: Optional[float] = None):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0

    def record_failure(self) -> None:
        """Зафиксировать сбой, увеличить счётчик."""
        self.failure_count += 1

    def record_success(self) -> None:
        """Зафиксировать успех, сбросить счётчик."""
        self.failure_count = 0

    def is_open(self) -> bool:
        """Возвращает True, если цепь разомкнута (слишком много сбоев)."""
        return self.failure_count >= self.failure_threshold

    def reset(self) -> None:
        """Ручной сброс предохранителя."""
        self.failure_count = 0

    def __repr__(self) -> str:
        status = "OPEN" if self.is_open() else "CLOSED"
        return f"CircuitBreaker({status}, failures={self.failure_count}/{self.failure_threshold})"