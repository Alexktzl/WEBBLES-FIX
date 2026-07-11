"""
Состояния и конечный автомат конвейера webles_conveyor.
"""

from enum import Enum, auto


class State(Enum):
    """Состояния конвейера исправления."""
    IDLE = auto()
    ANALYZING = auto()
    CLASSIFY = auto()
    PRE_CLEANUP = auto()            # предварительная очистка от артефактов
    PRIORITIZING = auto()
    PLANNING = auto()
    EXECUTE_PLAN = auto()
    ROOT_CAUSE = auto()
    GENERATING_PATCH = auto()
    APPLYING_PATCH = auto()
    VALIDATING = auto()
    REVIEWING = auto()              # D.3: strict reviewer LLM, перед DECIDING
    DECIDING = auto()
    ROLLING_BACK = auto()
    CLEANUP_ANALYSIS = auto()       # очистка на основе символьного графа
    NEEDS_REVIEW = auto()           # патч на ручной просмотр (см. D.1 / D.4)
    FINAL_RESOLVE = auto()          # финальное разрешение неисправимых ошибок
    COMPLETED = auto()
    FAILED = auto()
    CIRCUIT_OPEN = auto()
    NEXT_ERROR = auto()


class StateMachine:
    """Простой конечный автомат для отслеживания текущего состояния."""

    def __init__(self, initial: State = State.IDLE):
        self.current = initial

    def transition(self, new_state: State) -> None:
        self.current = new_state

    @property
    def current_state(self) -> State:
        return self.current

    def is_terminal(self) -> bool:
        return self.current in {State.COMPLETED, State.FAILED, State.CIRCUIT_OPEN}

    def __repr__(self) -> str:
        return f"StateMachine(current={self.current.name})"
