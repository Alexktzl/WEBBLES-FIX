"""
Stage L.2 — тонкий Fix-оркестратор.

Это **драйвер**, а не «мозг»: он гоняет цикл починки по ОДНОЙ ошибке за раз,
читает вердикт и пишет проекцию состояния (L.1) + объяснение (L.4). Реальную
работу (генерация/валидация/применение/откат) делает пайплайн — он передаётся
сюда как callable `fixer(error) -> FixResult`. Благодаря этому оркестратор
тестируется офлайн (со стаб-fixer'ом), а к `pipeline_engine` подключается тонким
адаптером отдельно (`agent/pipeline_fixer.py`).

Протокол шага (жёсткий, агент не может «сделать заодно» другое):
  1. взять следующую ошибку из очереди (пропустив уже завершённые — resume);
  2. выставить `next_action` в проекции;
  3. выполнить ОДНО действие через `fixer`;
  4. ACCEPT → принять; REJECT → повторить до `max_attempts`, иначе «не смог»;
     NEEDS_REVIEW → записать и идти дальше;
  5. записать результат + объяснение «Что/Почему/Как» в журнал;
  6. обновить счётчики/`next_action` и перейти к следующей.

Anti-loop: повторы по одной ошибке ограничены `max_attempts`, общий прогон —
`max_steps`. Режим: запускается только если режим разрешает RUN_PIPELINE
(app-level gating, L.3) — иначе возвращает отказ и НИЧЕГО не трогает.

L.5: при наличии `emit` (callable или EventBus) оркестратор эмитит единый поток
событий (run_started/error_dequeued/patch_proposed/verdict/applied|needs_review|
failed/explanation/run_finished) для UI. Без `emit` поведение прежнее.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent import modes
from agent.events import Event, EventType, as_emitter
from agent.explanation import build_explanation, render_markdown
from agent.state_projection import StateProjection

# Вердикты — те же, что у DecideStage (трихотомия D.4).
ACCEPT = "ACCEPT"
NEEDS_REVIEW = "NEEDS_REVIEW"
REJECT = "REJECT"
FAILED = "FAILED"  # REJECT, исчерпавший повторы → «не смог»


@dataclass
class FixResult:
    """Результат одной попытки починки от пайплайна (или стаба в тестах)."""
    verdict: str
    edit_set: Any = None
    review: Optional[Dict[str, Any]] = None
    detail: str = ""


Fixer = Callable[[Dict[str, Any]], FixResult]


def error_signature(error: Dict[str, Any]) -> str:
    """Стабильная сигнатура ошибки. Переиспользует core.utils, если доступна."""
    try:
        from core.utils import error_signature as _sig  # type: ignore
        return _sig(error)
    except Exception:
        code = error.get("code", "?")
        file = error.get("file", "?")
        line = error.get("line", 0)
        return f"{code}:{file}:{line}"


@dataclass
class RunSummary:
    accepted: int = 0
    needs_review: int = 0
    failed: int = 0
    steps: int = 0
    refused: bool = False
    message: str = ""
    done: List[Dict[str, str]] = field(default_factory=list)


class FixOrchestrator:
    """Тонкий драйвер цикла починки поверх произвольного `fixer`."""

    def __init__(
        self,
        projection: StateProjection,
        fixer: Fixer,
        mode: modes.Mode = modes.Mode.FIX,
        max_attempts: int = 2,
        max_steps: int = 1000,
        emit=None,
    ):
        self.projection = projection
        self.fixer = fixer
        self.mode = mode
        self.max_attempts = max(1, int(max_attempts))
        self.max_steps = max(1, int(max_steps))
        # Единый событийный поток (L.5). None → noop; принимает callable или EventBus.
        self._emit = as_emitter(emit)

    def _fire(self, event_type: EventType, **data) -> None:
        self._emit(Event(event_type, data))

    # ------------------------------------------------------------------
    def run(self, errors: List[Dict[str, Any]]) -> RunSummary:
        # Gating: чинить можно только в режиме с RUN_PIPELINE.
        if not modes.can(self.mode, modes.Capability.RUN_PIPELINE):
            return RunSummary(
                refused=True,
                message=modes.refusal(self.mode, modes.Capability.RUN_PIPELINE),
            )

        head = self.projection.init_if_absent(self.mode.value)
        done_sigs = {d.get("sig") for d in head.get("done", [])}

        # Очередь: пропускаем уже завершённое (resume).
        queue = [e for e in (errors or []) if error_signature(e) not in done_sigs]
        head["queue"] = [self._descriptor(e) for e in queue]
        self.projection.save_head(head)

        total = len(queue)
        self._fire(EventType.RUN_STARTED, mode=self.mode.value, queued=total)

        summary = RunSummary()
        # учитываем уже посчитанное в прошлой сессии (для счётчиков проекции)
        prev = head.get("counters", {})
        for idx, e in enumerate(queue):
            if summary.steps >= self.max_steps:
                break
            sig = error_signature(e)
            self._fire(EventType.ERROR_DEQUEUED, index=idx, total=total, **self._descriptor(e))
            verdict, result = self._process_one(e)
            edit_set = getattr(result, "edit_set", None) if result else None
            if edit_set:
                intent = edit_set.get("intent") if isinstance(edit_set, dict) else getattr(edit_set, "intent", "")
                self._fire(EventType.PATCH_PROPOSED, sig=sig, intent=intent or "",
                           file=e.get("file", "?"))
            self._fire(EventType.VERDICT, sig=sig, verdict=verdict, file=e.get("file", "?"))
            if verdict == ACCEPT:
                self._fire(EventType.APPLIED, sig=sig, file=e.get("file", "?"))
            elif verdict == NEEDS_REVIEW:
                self._fire(EventType.NEEDS_REVIEW, sig=sig, file=e.get("file", "?"))
            elif verdict == FAILED:
                self._fire(EventType.FAILED, sig=sig, file=e.get("file", "?"))
            self._record(e, verdict, result, head, prev)
            summary.steps += 1
            summary.done.append({"sig": sig, "verdict": verdict})
            if verdict == ACCEPT:
                summary.accepted += 1
            elif verdict == NEEDS_REVIEW:
                summary.needs_review += 1
            elif verdict == FAILED:
                summary.failed += 1

        # Очередь пройдена — простаиваем.
        head = self.projection.load_head()
        head["next_action"] = None
        head["queue"] = []
        self.projection.save_head(head)
        self._fire(EventType.RUN_FINISHED, accepted=summary.accepted,
                   needs_review=summary.needs_review, failed=summary.failed,
                   steps=summary.steps)
        return summary

    # ------------------------------------------------------------------
    def _process_one(self, error: Dict[str, Any]) -> "tuple[str, Optional[FixResult]]":
        """Одна ошибка: до max_attempts попыток. REJECT повторяем, иначе FAILED."""
        sig = error_signature(error)
        # отметить «сейчас работаем над этим»
        head = self.projection.load_head()
        head["next_action"] = sig
        self.projection.save_head(head)

        last: Optional[FixResult] = None
        for attempt in range(self.max_attempts):
            last = self.fixer(error)
            v = (last.verdict or "").upper()
            if v == ACCEPT:
                return ACCEPT, last
            if v == NEEDS_REVIEW:
                return NEEDS_REVIEW, last
            # REJECT → ещё попытка, если остались
            if attempt < self.max_attempts - 1:
                continue
        return FAILED, last

    def _record(self, error, verdict, result, head, prev) -> None:
        """Пишет шаг в проекцию: счётчики/done в голову + блок в журнал."""
        head = self.projection.load_head()
        head["step_id"] = int(head.get("step_id", 0)) + 1
        step_id = head["step_id"]
        head["last_verdict"] = verdict
        counters = head.get("counters") or {"accepted": 0, "needs_review": 0, "failed": 0}
        if verdict == ACCEPT:
            counters["accepted"] = counters.get("accepted", 0) + 1
        elif verdict == NEEDS_REVIEW:
            counters["needs_review"] = counters.get("needs_review", 0) + 1
        elif verdict == FAILED:
            counters["failed"] = counters.get("failed", 0) + 1
        head["counters"] = counters
        sig = error_signature(error)
        head.setdefault("done", []).append({"sig": sig, "verdict": verdict})
        # удалить из очереди
        head["queue"] = [d for d in head.get("queue", []) if d.get("sig") != sig]
        head["next_action"] = None
        self.projection.save_head(head)

        edit_set = getattr(result, "edit_set", None) if result else None
        review = getattr(result, "review", None) if result else None
        expl = build_explanation(error, edit_set, review)
        header = (f"## [шаг {step_id}] {error.get('code', '?')} @ "
                  f"{error.get('file', '?')}:{error.get('line', 0)} — {verdict}")
        self.projection.append_journal(header + "\n" + render_markdown(expl))
        self._fire(EventType.EXPLANATION, sig=sig, file=error.get("file", "?"),
                   step=step_id, verdict=verdict, **expl)

    @staticmethod
    def _descriptor(error: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "sig": error_signature(error),
            "code": error.get("code", "?"),
            "file": error.get("file", "?"),
            "line": error.get("line", 0),
        }
