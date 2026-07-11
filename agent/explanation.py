"""
Stage L.4 — сборка объяснения «Что / Почему / Как».

Объяснение НЕ генерируется LLM-ом заново — оно **собирается из данных, которые
уже есть** в пайплайне: текст/класс ошибки, DO/DON'T-ограничения (Stage C.2),
вердикт и причины ревьюера (Stage D.3), и `EditSet.intent/rationale/risks`
(Stage B). Это дёшево, детерминированно и не галлюцинирует. При желании поверх
можно «причесать» формулировку LLM-ом — но это опционально и не здесь.

Результат — dict `{what, why, how}` + рендер в markdown-блок для журнала L.1.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _editset_field(edit_set: Any, attr: str) -> Any:
    """Достаёт поле из EditSet-датакласса ИЛИ из dict-формы."""
    if edit_set is None:
        return None
    if isinstance(edit_set, dict):
        return edit_set.get(attr)
    return getattr(edit_set, attr, None)


def _first_rationale(edit_set: Any) -> str:
    edits = _editset_field(edit_set, "edits") or []
    for e in edits:
        rat = e.get("rationale") if isinstance(e, dict) else getattr(e, "rationale", "")
        if rat:
            return str(rat)
    return ""


def build_explanation(
    error: Dict[str, Any],
    edit_set: Any = None,
    review: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Собирает {what, why, how} из имеющихся данных. Без LLM."""
    error = error or {}
    code = error.get("code", "") or ""
    message = (error.get("message", "") or "").strip()
    error_class = error.get("error_class", "") or ""

    # --- Что: суть ошибки -------------------------------------------------
    what = message or "Обнаружена проблема в коде."
    if code:
        what = f"[{code}] {what}"

    # --- Почему: причина / контекст --------------------------------------
    why_parts: List[str] = []
    if error_class:
        why_parts.append(f"класс: {error_class}")
    # причины ревьюера, если он высказался
    reasons = (review or {}).get("reasons") if review else None
    if reasons:
        if isinstance(reasons, str):
            reasons = [reasons]
        why_parts.extend(str(r) for r in reasons[:3] if r)
    # иначе — опираемся на DO-подсказку из таблицы ограничений
    if not reasons:
        do, _dont = _get_constraints(code)
        if do:
            why_parts.append(do[0])
    why = "; ".join(why_parts) if why_parts else "Причина зафиксирована в анализе."

    # --- Как: что сделано / предлагается ---------------------------------
    intent = _editset_field(edit_set, "intent") or ""
    rationale = _first_rationale(edit_set)
    how_parts: List[str] = []
    if intent:
        how_parts.append(str(intent))
    if rationale and rationale not in how_parts:
        how_parts.append(rationale)
    risks = _editset_field(edit_set, "risks") or []
    how = ". ".join(how_parts) if how_parts else "Готовое детерминированное исправление."
    if risks:
        how += " (риски: " + "; ".join(str(r) for r in risks[:2]) + ")"

    return {"what": what, "why": why, "how": how}


def render_markdown(explanation: Dict[str, str]) -> str:
    """Markdown-блок «Объяснение» для журнала состояния (L.1)."""
    e = explanation or {}
    return (
        "### Объяснение\n"
        f"- **Что:** {e.get('what', '—')}\n"
        f"- **Почему:** {e.get('why', '—')}\n"
        f"- **Как:** {e.get('how', '—')}\n"
    )


def _get_constraints(code: str):
    """Мягкий доступ к таблице DO/DON'T (Stage C.2). При сбое импорта — пусто."""
    try:
        from analysis.constraints.error_constraints import get_constraints
        return get_constraints(code)
    except Exception:
        return [], []
