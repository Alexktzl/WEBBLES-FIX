"""
Stage L.5+ — пер-ошибочный live-стриминг событий движка.

Чистая функция-хелпер: переводит «состояние после стейджа» в поток событий
(error_dequeued / patch_proposed / verdict / applied / needs_review / failed).
Вынесена отдельно от `PipelineEngine`, чтобы можно было тестировать без
подъёма всего движка (он тянет аналайзеры, LLM-клиент, dep-граф и т.п.).

PipelineEngine просто хранит `event_emitter` и `_emitted_error_sigs`, а после
каждого `stage.execute(...)` зовёт `emit_after_stage(...)`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set


# Имена ключей метадаты — дублируем литералами, чтобы хелпер был автономным
# и не тянул contract.py (там нет циклов, но всё же).
_META_LAST_DECISION = "last_decision"
_META_STRUCTURED_EDIT = "structured_edit"
_META_PATCH_SOURCE = "patch_source"
_META_REVIEW = "review"
_META_CONFIDENCE = "confidence"


def _base_for(error: Dict[str, Any]) -> Dict[str, Any]:
    """Стандартные поля для всех событий, привязанных к одной ошибке."""
    return {
        "file": error.get("file", "?"),
        "line": error.get("line", 0),
        "code": error.get("code") or error.get("error_code") or "?",
        "message": (error.get("message") or "")[:200],
    }


def emit_after_stage(
    emitter: Optional[Callable[[str, Dict[str, Any]], None]],
    stage_state_name: str,
    context: Any,
    *,
    error_signature_fn: Callable[[Dict[str, Any]], str],
    emitted_sigs: Set[str],
) -> None:
    """Эмитит пер-ошибочные события после стадии `stage_state_name`.

    Параметры:
      emitter            — callable(type:str, data:dict)→None или None (no-op);
      stage_state_name   — имя State (например, "GENERATING_PATCH", "DECIDING");
      context            — `PipelineContext` (нужен `.selected_error`,
                           `.metadata`, `.generated_patch`);
      error_signature_fn — функция-сигнатура (sig используется для дедупа
                           `error_dequeued`);
      emitted_sigs       — изменяемый set уже-отправленных sig-ов.

    Все исключения в emitter глушатся. Если `selected_error` отсутствует —
    не делаем ничего.
    """
    if emitter is None:
        return
    error = getattr(context, "selected_error", None)
    if not error:
        return

    base = _base_for(error)
    try:
        sig = error_signature_fn(error)
    except Exception:
        sig = f"{base['file']}:{base['line']}:{base['code']}"

    def _send(etype: str, data: Dict[str, Any]) -> None:
        try:
            emitter(etype, dict(data or {}))
        except Exception:
            pass

    if sig not in emitted_sigs:
        emitted_sigs.add(sig)
        _send("error_dequeued", base)

    meta = getattr(context, "metadata", None) or {}

    if stage_state_name == "GENERATING_PATCH":
        if getattr(context, "generated_patch", None):
            structured = meta.get(_META_STRUCTURED_EDIT) or {}
            intent = ""
            if isinstance(structured, dict):
                intent = str(structured.get("intent") or "")
            data = dict(base)
            data["intent"] = intent[:200]
            data["patch_source"] = str(meta.get(_META_PATCH_SOURCE, "") or "")
            _send("patch_proposed", data)
        return

    if stage_state_name == "DECIDING":
        decision = meta.get(_META_LAST_DECISION)
        if not decision:
            return
        review = meta.get(_META_REVIEW) or {}
        verdict_data = dict(
            base,
            verdict=str(decision),
            confidence=float(meta.get(_META_CONFIDENCE, 0.0) or 0.0),
            reviewer=str(review.get("verdict") or ""),
        )
        _send("verdict", verdict_data)
        if decision == "ACCEPT":
            _send("applied", base)
        elif decision == "NEEDS_REVIEW":
            _send("needs_review", base)
        elif decision == "REJECT":
            _send("failed", base)
