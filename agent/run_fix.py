"""
Stage L.2 — точка сборки «живого» Fix-агента.

Архитектурный вывод: переизобретать `run_single` не нужно — существующий
`PipelineEngine` уже сам является поэтапным циклом починки. Агент здесь
оркестрирует существующий движок и проецирует его результат в состояние и
событийный поток, а не строит второй параллельный цикл.

Stage L.5+ — пер-ошибочный live-стриминг. Если `Controller.run_pipeline`
принимает `event_emitter`, передаём адаптер, который оборачивает события
движка в `agent.events.Event` и шлёт в общий поток. На вердиктах
applied/needs_review/failed дополнительно пишем запись в `chat/changes.jsonl`
и обновляем by-file дневник `file_snapshots` (для UI-окон «Было / Стало»).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from agent import modes
from agent.events import Event, EventType, as_emitter
from agent.state_projection import StateProjection


def _project_result(
    projection: StateProjection,
    result: Dict[str, Any],
    emit,
    *,
    skip_per_item_events: bool = False,
) -> None:
    """Проецирует result-dict движка в проекцию и события."""
    em = as_emitter(emit)
    result = result or {}
    accepted = int(result.get("accepted_patches", 0) or 0)
    rejected = int(result.get("rejected_patches", 0) or 0)
    nr_items = list(result.get("needs_review_items", []) or [])
    nr_count = int(result.get("needs_review_count", len(nr_items)) or 0)
    remaining = list(result.get("remaining_errors", []) or [])
    status = result.get("status", "UNKNOWN")

    if not skip_per_item_events:
        for it in nr_items:
            file = it.get("file", "?")
            data = {"file": file, "line": it.get("line", 0),
                    "code": it.get("error_code") or it.get("code") or "?"}
            em(Event(EventType.ERROR_DEQUEUED, dict(data)))
            em(Event(EventType.VERDICT, dict(data, verdict="NEEDS_REVIEW")))
            em(Event(EventType.NEEDS_REVIEW, dict(data)))

    head = projection.load_head()
    counters = head.get("counters") or {"accepted": 0, "needs_review": 0, "failed": 0}
    counters["accepted"] = counters.get("accepted", 0) + accepted
    counters["needs_review"] = counters.get("needs_review", 0) + nr_count
    counters["failed"] = counters.get("failed", 0) + len(remaining)
    head["counters"] = counters
    head["last_verdict"] = status
    head["next_action"] = None
    head["queue"] = []
    projection.save_head(head)

    lines = [
        f"## Прогон движка — статус {status}",
        f"- Принято патчей: {accepted}",
        f"- Отклонено: {rejected}",
        f"- На ручной просмотр: {nr_count}",
        f"- Осталось ошибок: {len(remaining)}",
    ]
    for it in nr_items[:10]:
        lines.append(f"  - review: {it.get('file', '?')}:{it.get('line', 0)} "
                     f"[{it.get('error_code') or it.get('code') or '?'}]")
    for e in remaining[:10]:
        lines.append(f"  - осталось: {e.get('file', '?')}:{e.get('line', 0)} "
                     f"[{e.get('code', '?')}] {e.get('message', '')}")
    projection.append_journal("\n".join(lines))

    em(Event(EventType.RUN_FINISHED, {
        "accepted": accepted, "needs_review": nr_count,
        "failed": len(remaining), "status": status,
        "initial": int(result.get("initial_error_count", 0) or 0),
        "final": int(result.get("final_error_count", 0) or 0),
    }))


def run_fix_agent(
    project_path,
    language: str,
    *,
    mode: modes.Mode = modes.Mode.FIX,
    emit=None,
    controller: Any = None,
    config: Optional[Dict[str, Any]] = None,
    state_name: str = "fix",
    resume: bool = False,
    changes_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Запускает живой Fix-агент.

    `changes_root` — корень для `chat/changes.jsonl` + `file_snapshots`
    (по умолчанию реальный корень `webbles_fix`, это общая для всех проектов
    история чата — намеренно). Параметр существует в первую очередь для
    тестов: позволяет изолировать побочные записи во временную директорию
    вместо настоящего рабочего дерева репозитория.
    """
    em = as_emitter(emit)
    if not modes.can(mode, modes.Capability.RUN_PIPELINE):
        return {"refused": True, "message": modes.refusal(mode, modes.Capability.RUN_PIPELINE)}

    # P0.6: StateProjection пишет в runtime-папку системы webbles_fix,
    # а не в папку проекта — чтобы в проекте осталось только .webbles_backups/.
    try:
        from core.pipeline_engine import PipelineEngine
        _proj_runtime = PipelineEngine._project_runtime_dir(project_path)
        _proj_runtime.mkdir(parents=True, exist_ok=True)
    except Exception:
        _proj_runtime = Path(project_path)
    projection = StateProjection(_proj_runtime, name=state_name)
    projection.init_if_absent(mode.value)

    if controller is None:
        from core.controller import Controller
        controller = Controller()
    if config is None:
        config = controller.load_config()

    em(Event(EventType.RUN_STARTED, {"mode": mode.value, "project": str(project_path),
                                     "language": language}))

    # Снимок дерева проекта для левой панели UI.
    try:
        from agent.project_tree import build_tree_event_payload
        em(Event(EventType.PROJECT_TREE, build_tree_event_payload(project_path)))
    except Exception:
        pass

    # changes_log + file_snapshots — корень общий (webbles_fix) по умолчанию,
    # но переопределяем через changes_root (см. docstring) если передан.
    try:
        from agent.changes_log import append_change, event_to_change
        from agent import file_snapshots as _fs
        _changes_root = Path(changes_root) if changes_root is not None else Path(__file__).resolve().parent.parent
    except Exception:
        append_change = None  # type: ignore
        event_to_change = None  # type: ignore
        _fs = None  # type: ignore
        _changes_root = None

    # Новый прогон — старые before/after устарели.
    if _fs and _changes_root is not None:
        try:
            _fs.clear(_changes_root)
        except Exception:
            pass

    def _engine_emit(etype: str, data: Dict[str, Any]) -> None:
        try:
            event_type = EventType(etype)
        except ValueError:
            return
        try:
            em(Event(event_type, dict(data or {})))
        except Exception:
            pass

        # 1) changes.jsonl — только финальные вердикты.
        if append_change and event_to_change and _changes_root is not None:
            try:
                change = event_to_change(etype, data or {})
                if change is not None:
                    append_change(_changes_root, change)
            except Exception:
                pass

        # 2) file_snapshots — by-file дневник.
        if _fs is None or _changes_root is None:
            return
        file_rel = (data or {}).get("file") or ""
        if not file_rel:
            return
        try:
            if etype == "error_dequeued":
                _fs.record_before(_changes_root, project_path, file_rel)
                _fs.add_error_line(_changes_root, file_rel,
                                   int((data or {}).get("line") or 0),
                                   str((data or {}).get("code") or "?"),
                                   str((data or {}).get("message") or ""))
            elif etype in ("applied", "needs_review", "failed"):
                verdict_map = {"applied": "ACCEPT",
                               "needs_review": "NEEDS_REVIEW",
                               "failed": "FAILED"}
                _fs.add_change(_changes_root, file_rel, {
                    "verdict": verdict_map.get(etype, "?"),
                    "code": (data or {}).get("code") or "?",
                    "intent": (data or {}).get("intent") or "",
                    "patch_source": (data or {}).get("patch_source") or "",
                    "confidence": float((data or {}).get("confidence") or 0.0),
                    "line": int((data or {}).get("line") or 0),
                })
        except Exception:
            pass

    try:
        result = controller.run_pipeline(
            Path(project_path), language, config, event_emitter=_engine_emit,
            resume=resume,
        )
        streamed_live = True
    except TypeError:
        result = controller.run_pipeline(Path(project_path), language, config)
        streamed_live = False

    # После прогона — снимаем `after` по всем зафиксированным файлам.
    if _fs and _changes_root is not None:
        try:
            _fs.record_after_all(_changes_root, project_path)
        except Exception:
            pass

    _project_result(projection, result, em, skip_per_item_events=streamed_live)
    return result
