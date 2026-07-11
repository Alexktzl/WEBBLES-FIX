"""
Contract Hint (Q.4) — извлекает контракт функции, содержащей строку ошибки,
и форматирует его как секцию case-file для генератора патчей.

Добавляется в конец case-file после всех других секций:

    ## FUNCTION CONTRACT: `имя_функции`

    - Parameters: a, b, c  (N required)
    - Returns value: yes
    - Raises: ValueError, TypeError
    - Side effects: subprocess, file_write
    - Calls: open, shutil.copy
    - Branch count: 4

Это подсказывает LLM, какой контракт обязан соблюдать патч, и снижает
вероятность нарушений, которые Logic Guard поймает постфактум.

Только Python.  Для других языков возвращается "".
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from analysis.logic_guard import LogicGuardExtractor, LogicSnapshot


def _find_function_at_line(source: str, line: int) -> Optional[LogicSnapshot]:
    """Вернуть снапшот функции, охватывающей строку *line*, или None.

    Если несколько функций охватывают строку (вложенные), возвращается
    самая внутренняя (с наибольшим line_start).
    """
    if line <= 0:
        return None
    snaps = LogicGuardExtractor.extract_python(source)
    best: Optional[LogicSnapshot] = None
    for snap in snaps.values():
        if snap.line_start <= line <= snap.line_end:
            if best is None or snap.line_start > best.line_start:
                best = snap
    return best


def _format_contract(snap: LogicSnapshot) -> str:
    """Форматирует снапшот как секцию `## FUNCTION CONTRACT`."""
    lines = [f"## FUNCTION CONTRACT: `{snap.name}`", ""]

    # Параметры
    if snap.param_names:
        params_str = ", ".join(snap.param_names)
        lines.append(f"- Parameters: {params_str}  ({snap.required_param_count} required)")
    else:
        lines.append("- Parameters: none")

    # Возвращаемое значение
    lines.append(f"- Returns value: {'yes' if snap.has_value_return else 'no'}")

    # Исключения
    if snap.raised_exceptions:
        lines.append(f"- Raises: {', '.join(sorted(snap.raised_exceptions))}")

    # Побочные эффекты
    if snap.side_effects:
        lines.append(f"- Side effects: {', '.join(sorted(snap.side_effects))}")

    # Вызываемые функции (не более 8, чтобы не раздувать промпт)
    if snap.called_functions:
        calls_preview = sorted(snap.called_functions)[:8]
        suffix = ", ..." if len(snap.called_functions) > 8 else ""
        lines.append(f"- Calls: {', '.join(calls_preview)}{suffix}")

    # Количество ветвлений
    lines.append(f"- Branch count: {snap.branch_count}")

    return "\n".join(lines)


def build_contract_hint(
    source: str,
    error: Dict[str, Any],
    language: str = "",
) -> str:
    """Вернуть строку-секцию contract hint или "" при любом сбое / non-Python."""
    lang = (language or "").strip().lower()
    if lang not in ("python", "py"):
        return ""
    line = 0
    try:
        line = int(error.get("line") or 0)
    except (TypeError, ValueError):
        pass
    if not source or line <= 0:
        return ""
    snap = _find_function_at_line(source, line)
    if snap is None:
        return ""
    return _format_contract(snap)
