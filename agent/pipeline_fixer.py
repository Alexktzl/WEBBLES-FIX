"""
Stage L.2 — адаптер `fixer` к реальному пайплайну.

`FixOrchestrator` (L.2) принимает callable `fixer(error) -> FixResult`. В тестах
это стаб; в проде — этот адаптер. Он берёт функцию `run_single(error) ->
context`, которая прогоняет ОДНУ ошибку через стадии пайплайна
(GeneratePatch → Validate → Review → Decide), и переводит итоговый
`context.metadata` в `FixResult`:

    metadata["last_decision"]  ∈ ACCEPT/NEEDS_REVIEW/REJECT  → verdict
    metadata["structured_edit"] (dict от EditSet.to_dict)    → edit_set
    metadata["review"]          {verdict, reasons, ...}       → review

Источник вердикта — `DecideStage` (D.4) и `ReviewStage` (D.3); ключи берём из
`core.contract.MetadataKeys`. Если вердикта нет (пайплайн не дошёл до DECIDE) —
консервативно считаем REJECT, и оркестратор сам решит про retry/«не смог».

Сам прогон стадий (`run_single`) вынесен наружу: адаптер от него не зависит и
тестируется детерминированно. Это сохраняет инвариант «агент оркестрирует,
пайплайн исполняет» и не тянет в `agent/` тяжёлые импорты `core.stages`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from agent.fix_orchestrator import FixResult, ACCEPT, NEEDS_REVIEW, REJECT

# Ключи метаданных — единые с пайплайном (core.contract.MetadataKeys).
_KEY_DECISION = "last_decision"
_KEY_STRUCTURED = "structured_edit"
_KEY_REVIEW = "review"

_VALID_VERDICTS = {ACCEPT, NEEDS_REVIEW, REJECT}


def _metadata_of(context: Any) -> Dict[str, Any]:
    meta = getattr(context, "metadata", None)
    if isinstance(meta, dict):
        return meta
    if isinstance(context, dict):  # на случай dict-подобного контекста
        m = context.get("metadata")
        return m if isinstance(m, dict) else {}
    return {}


def result_from_context(context: Any) -> FixResult:
    """Переводит финальный `context` прогона одной ошибки в FixResult."""
    if context is None:
        return FixResult(verdict=REJECT, detail="no context (pipeline produced nothing)")
    meta = _metadata_of(context)

    raw = str(meta.get(_KEY_DECISION) or "").upper()
    verdict = raw if raw in _VALID_VERDICTS else REJECT  # нет решения → консервативно REJECT

    edit_set = meta.get(_KEY_STRUCTURED)  # dict (EditSet.to_dict) или None
    review = meta.get(_KEY_REVIEW)
    review = review if isinstance(review, dict) else None

    detail = "" if raw in _VALID_VERDICTS else f"no decision (last_decision={meta.get(_KEY_DECISION)!r})"
    return FixResult(verdict=verdict, edit_set=edit_set, review=review, detail=detail)


class PipelineFixer:
    """Адаптер: оборачивает прогон одной ошибки через пайплайн в `fixer`-callable.

    `run_single(error) -> context` — функция, которую предоставляет точка сборки
    (она конструирует/переиспользует стадии и гоняет один цикл по `error`).
    Любой сбой прогона изолируется → REJECT (оркестратор решит про retry).
    """

    def __init__(self, run_single: Callable[[Dict[str, Any]], Any]):
        self._run_single = run_single

    def __call__(self, error: Dict[str, Any]) -> FixResult:
        try:
            context = self._run_single(error)
        except Exception as e:  # прогон упал — не роняем оркестратор
            return FixResult(verdict=REJECT, detail=f"pipeline run failed: {type(e).__name__}: {e}")
        return result_from_context(context)
