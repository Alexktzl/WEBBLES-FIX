"""
#8 + #9 — действия над очередью `<project>/.webbles_fix/needs_review/`.

Каждая запись — JSON-файл с полями (см. NeedsReviewStage._save_review_item):
    {
        "saved_at": ISO-timestamp,
        "error_sig": "...",
        "error": {file, line, code, message, error_class, ...},
        "patch_source": "...",
        "patch": "...unified diff...",
        "intent": "...",
        "confidence": float,
        "risks": [],
        "structured_edit": {...} | None,
    }

Этот модуль:
  * `list_pending(project_path)` — массив всех ожидающих ревью записей;
  * `apply_pending(project_path, sig)` — применить патч к проекту, удалить;
  * `reject_pending(project_path, sig)` — просто удалить (без apply);
  * `read_pending(project_path, sig)` — детали одной записи (для предпросмотра / LLM-ревью).

Всё детерминированное, без LLM. LLM-ревью отдельно (см. `agent/chat_tools.py::tool_review_pending_patch`).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.\-]+")


def _queue_dir(project_path) -> Path:
    """P0.6: очередь ручного просмотра живёт в системной папке webbles_fix
    (`<webbles_fix>/runtime/<project_name>/needs_review/`), НЕ в проекте.
    Делегируем в `PipelineEngine` чтобы был один источник истины.
    """
    try:
        from core.pipeline_engine import PipelineEngine
        return PipelineEngine._project_needs_review_dir(project_path)
    except Exception:
        # Fallback: на случай circular-import во время тестов.
        sys_root = Path(__file__).resolve().parents[1] / "runtime"
        return sys_root / Path(project_path).name / "needs_review"


def _safe_sig(sig: str) -> str:
    return _SAFE_NAME_RE.sub("_", sig or "")[:120] or "unknown"


def list_pending(project_path) -> List[Dict[str, Any]]:
    """Список ожидающих ревью патчей (метаданные без полного diff'а)."""
    root = _queue_dir(project_path)
    if not root.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for p in sorted(root.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("review_actions: не удалось прочитать %s: %s", p, e)
            continue
        err = data.get("error") or {}
        items.append({
            "sig":           data.get("error_sig") or p.stem,
            "safe_sig":      p.stem,
            "saved_at":      data.get("saved_at") or "",
            "file":          err.get("file", ""),
            "line":          int(err.get("line", 0) or 0),
            "code":          str(err.get("code", "") or ""),
            "error_class":   str(err.get("error_class", "") or ""),
            "message":       str(err.get("message", "") or "")[:300],
            "patch_source":  data.get("patch_source", ""),
            "confidence":    data.get("confidence"),
            "intent":        data.get("intent", ""),
            "risks":         list(data.get("risks", []) or []),
            "has_patch":     bool(data.get("patch")),
            "patch_lines":   len((data.get("patch") or "").splitlines()),
            "category":      data.get("category", "unknown"),
            "priority":      int(data.get("priority") or 7),
        })
    # Сортировка: по приоритету (security=1 первым), внутри — по дате (новые сверху).
    items.sort(key=lambda x: (x.get("priority", 7), x.get("saved_at", "")))
    return items


def read_pending(project_path, sig: str) -> Optional[Dict[str, Any]]:
    """Полная запись (включая `patch` и `structured_edit`) для предпросмотра."""
    safe = _safe_sig(sig)
    path = _queue_dir(project_path) / f"{safe}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("review_actions: чтение %s упало: %s", path, e)
        return None


def reject_pending(project_path, sig: str) -> Dict[str, Any]:
    """Удаляет запись из очереди без применения. Возвращает результат."""
    safe = _safe_sig(sig)
    path = _queue_dir(project_path) / f"{safe}.json"
    if not path.exists():
        return {"ok": False, "reason": "not_found", "sig": sig}
    try:
        path.unlink()
    except Exception as e:
        return {"ok": False, "reason": f"unlink_failed: {e}", "sig": sig}
    return {"ok": True, "action": "rejected", "sig": sig}


def apply_pending(project_path, sig: str) -> Dict[str, Any]:
    """Применяет патч к проекту через PatchEngine. При успехе удаляет запись.

    Защитные слои PatchEngine (O.8 fuzzy, O.15 line-merge, O.16 pure-insert)
    остаются активными, так что некорректный патч НЕ будет применён.

    Возвращает:
        {ok, action, sig, file, applied_to (str path)} при успехе
        {ok=False, reason, sig} при отказе.
    """
    data = read_pending(project_path, sig)
    if data is None:
        return {"ok": False, "reason": "not_found", "sig": sig}
    err = data.get("error") or {}
    file_rel = err.get("file") or ""
    patch_text = data.get("patch") or ""
    if not file_rel:
        return {"ok": False, "reason": "no_file_in_record", "sig": sig}
    if not patch_text:
        return {"ok": False, "reason": "empty_patch", "sig": sig}

    target = Path(project_path) / file_rel
    if not target.exists():
        return {"ok": False, "reason": f"target_missing: {file_rel}", "sig": sig}

    # Бэкап до применения (как делает движок). Bypass'ит .webbles_backups/.webbles_backups
    # (O.13): пишем только в плоскую структуру.
    backup_dir = Path(project_path) / ".webbles_backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_name = file_rel.replace("/", "_").replace("\\", "_")
        shutil.copy2(target, backup_dir / f"{safe_name}.{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}")
    except Exception as e:
        logger.debug("review_actions: backup не создан: %s", e)

    # Применяем через PatchEngine (вся защита O.8/O.15/O.16 включена).
    try:
        from fixers.patch_engine import PatchEngine
        eng = PatchEngine()
        ok = eng.apply_patch(target, patch_text)
    except Exception as e:
        return {"ok": False, "reason": f"apply_failed: {e}", "sig": sig}

    if not ok:
        return {"ok": False, "reason": "patch_engine_refused", "sig": sig,
                "file": file_rel,
                "hint": "Защита PatchEngine отклонила патч (slipped line / pure-insert / "
                        "fuzzy не нашёл контекст). Запись осталась в очереди."}

    # Успех — удаляем запись.
    queue_path = _queue_dir(project_path) / f"{_safe_sig(sig)}.json"
    try:
        queue_path.unlink()
    except Exception as e:
        logger.debug("review_actions: не удалось удалить %s: %s", queue_path, e)

    return {"ok": True, "action": "applied", "sig": sig, "file": file_rel,
            "applied_to": str(target)}
