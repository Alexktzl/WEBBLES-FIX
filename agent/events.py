"""
Stage L.5 — единый событийный протокол агента.

Оркестратор (L.2) эмитит ОДИН поток событий, на который подписаны все
представления UI (чат, дерево файлов, панель задач, лог активности). Один
источник → несколько представлений, без рассинхрона.

События сериализуются в JSON (`Event.to_dict`) и могут уходить по WebSocket/SSE
во фронтенд (`webbles_ui.html` → `applyAgentEvent`). В тестах подписываемся
`CollectingSink` и проверяем последовательность — без сети/UI.

Поток одного Fix-прогона:
    run_started → project_tree → ( error_dequeued → [patch_proposed] →
                    verdict → {applied|needs_review|failed} → explanation )*
                  → run_finished
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    PROJECT_TREE = "project_tree"       # снимок структуры проекта (для UI-дерева)
    ERROR_DEQUEUED = "error_dequeued"   # агент взял следующую ошибку
    PATCH_PROPOSED = "patch_proposed"   # есть структурный патч (EditSet)
    VERDICT = "verdict"                 # ACCEPT / NEEDS_REVIEW / REJECT / FAILED
    APPLIED = "applied"                 # патч принят и применён
    NEEDS_REVIEW = "needs_review"       # ушло в ручную очередь
    FAILED = "failed"                   # не смогли починить
    EXPLANATION = "explanation"         # «Что/Почему/Как»
    RUN_FINISHED = "run_finished"


@dataclass
class Event:
    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        t = self.type.value if isinstance(self.type, EventType) else str(self.type)
        return {"type": t, "data": dict(self.data)}


Listener = Callable[[Event], None]


class EventBus:
    """Простой синхронный pub/sub. Падение одного слушателя не валит остальных."""

    def __init__(self) -> None:
        self._listeners: List[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        if callable(listener):
            self._listeners.append(listener)

    def emit(self, event: Event) -> None:
        for ln in list(self._listeners):
            try:
                ln(event)
            except Exception:
                continue


class CollectingSink:
    """Слушатель-накопитель для тестов и отладки."""

    def __init__(self) -> None:
        self.events: List[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def types(self) -> List[str]:
        return [e.type.value if isinstance(e.type, EventType) else str(e.type)
                for e in self.events]

    def of(self, event_type: EventType) -> List[Event]:
        return [e for e in self.events if e.type == event_type]


def as_emitter(emit: Any) -> Callable[[Event], None]:
    """Приводит аргумент к callable(Event): None→noop, EventBus→.emit, callable→он сам."""
    if emit is None:
        return lambda _e: None
    if hasattr(emit, "emit") and callable(emit.emit):
        return emit.emit
    if callable(emit):
        return emit
    return lambda _e: None
