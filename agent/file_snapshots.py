"""
Снимки `before`/`after` по файлам прогона и накопление ошибок по строкам.

Хранение: `chat/snapshots/<safe_key>.json` (один файл проекта = один снимок).
Структура:
    {
      "path":         "src/main.rs",
      "project_root": "C:/.../broken",
      "before":       "<содержимое до правок>",
      "after":        "<содержимое после правок>",
      "error_lines":  [{"line": 12, "code": "E0382", "message": "..."}, ...],
      "changes":      [{"verdict": "ACCEPT", "code": "E0382", "intent": "...",
                        "patch_source": "structured_llm", "ts": "...", "confidence": 0.87}],
      "ts":           "ISO-8601"
    }

Используется UI (клик по файлу) и чатом (для пояснений «было/стало»):
  * `record_before(snapshots_root, project_path, rel_path)` — однократно фиксирует
    оригинальное содержимое (если ещё не записано);
  * `add_error_line(...)` — пополняет error_lines при `error_dequeued`;
  * `add_change(...)` — пополняет список финальных вердиктов;
  * `record_after_all(snapshots_root, project_path)` — после прогона снимает
    текущее содержимое всех зафиксированных файлов как `after`;
  * `get_file_diff(snapshots_root, rel_path)` — возвращает payload для UI.

Никогда не валит вызывающего: I/O-ошибки идут в DEBUG-лог.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CHAT_DIR_NAME = "chat"
SNAPSHOTS_DIR_NAME = "snapshots"
# Жёсткий лимит размера файла, который мы вообще снимаем (защита от мегабайтов).
MAX_FILE_BYTES = 256 * 1024


# ---------------------------------------------------------------------- IO
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_key(rel_path: str) -> str:
    """Безопасный ключ файла для имени snapshot.json — sha1 от нормализованного пути."""
    rel = (rel_path or "").replace("\\", "/").strip("/")
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()


def _snap_dir(root) -> Path:
    p = Path(root) / CHAT_DIR_NAME / SNAPSHOTS_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snap_path(root, rel_path: str) -> Path:
    return _snap_dir(root) / (_safe_key(rel_path) + ".json")


def _load(root, rel_path: str) -> Dict[str, Any]:
    p = _snap_path(root, rel_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("snapshot не прочитан %s: %s", p, e)
        return {}


def _save(root, rel_path: str, snap: Dict[str, Any]) -> bool:
    p = _snap_path(root, rel_path)
    try:
        snap = dict(snap or {})
        snap.setdefault("path", rel_path)
        snap.setdefault("ts", _now_iso())
        p.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception as e:
        logger.debug("snapshot не сохранён %s: %s", p, e)
        return False


def _read_file_text(project_path, rel_path: str, working_path=None) -> Optional[str]:
    """Читает rel_path из working_path (если задан) или project_path."""
    if not project_path and not working_path:
        return None
    if not rel_path:
        return None
    # Сначала пробуем working_path (изолированную копию)
    if working_path:
        try:
            full = (Path(working_path) / rel_path).resolve()
            full.relative_to(Path(working_path).resolve())
            if full.is_file():
                raw = full.read_bytes()
                if b"\x00" not in raw[:4096]:
                    if len(raw) > MAX_FILE_BYTES:
                        raw = raw[:MAX_FILE_BYTES]
                    return raw.decode("utf-8", errors="replace")
        except Exception:
            pass
    # Fallback: project_path
    if project_path:
        try:
            full = (Path(project_path) / rel_path).resolve()
            full.relative_to(Path(project_path).resolve())
            if full.is_file():
                raw = full.read_bytes()
                if b"\x00" not in raw[:4096]:
                    if len(raw) > MAX_FILE_BYTES:
                        raw = raw[:MAX_FILE_BYTES]
                    return raw.decode("utf-8", errors="replace")
        except Exception as e:
            logger.debug("read_file_text упал %s: %s", rel_path, e)
    return None


# ---------------------------------------------------------------------- API
def list_snapshots(root) -> List[str]:
    """Список rel_path всех зафиксированных файлов прогона."""
    d = _snap_dir(root)
    out: List[str] = []
    for jf in d.glob("*.json"):
        try:
            snap = json.loads(jf.read_text(encoding="utf-8"))
            p = snap.get("path")
            if p:
                out.append(p)
        except Exception:
            continue
    return sorted(set(out))


def record_before(root, project_path, rel_path: str, working_path=None) -> bool:
    """Однократно фиксирует «before» — содержимое файла до правок.

    Если before уже есть в снимке — не перезаписываем (значит, это не первое
    касание; первое было раньше и держим именно его). Возвращает True, если
    содержимое записано (или уже было).
    """
    if not rel_path:
        return False
    snap = _load(root, rel_path)
    if "before" in snap and snap["before"] is not None:
        # Освежим только project_root, если поменялся (новый прогон того же
        # файла поверх старых снимков допускается? — нет, мы держим первичный).
        return True
    content = _read_file_text(project_path, rel_path, working_path=working_path)
    if content is None:
        return False
    snap["path"] = rel_path
    snap["project_root"] = str(project_path) if project_path else ""
    snap["before"] = content
    snap.setdefault("error_lines", [])
    snap.setdefault("changes", [])
    return _save(root, rel_path, snap)


def add_error_line(root, rel_path: str, line: int, code: str,
                   message: str = "") -> bool:
    """Добавляет запись в error_lines (дедуп по (line, code))."""
    if not rel_path:
        return False
    snap = _load(root, rel_path)
    snap.setdefault("path", rel_path)
    errs = list(snap.get("error_lines") or [])
    key = (int(line or 0), str(code or "?"))
    for e in errs:
        if (int(e.get("line") or 0), str(e.get("code") or "?")) == key:
            return True  # уже есть
    errs.append({"line": int(line or 0), "code": str(code or "?"),
                 "message": (message or "")[:300]})
    snap["error_lines"] = errs
    return _save(root, rel_path, snap)


def add_change(root, rel_path: str, change: Dict[str, Any]) -> bool:
    """Пополняет список финальных вердиктов по файлу (ACCEPT/REVIEW/FAILED)."""
    if not rel_path:
        return False
    snap = _load(root, rel_path)
    snap.setdefault("path", rel_path)
    ch = dict(change or {})
    ch.setdefault("ts", _now_iso())
    arr = list(snap.get("changes") or [])
    arr.append(ch)
    snap["changes"] = arr
    return _save(root, rel_path, snap)


def record_after_all(root, project_path, working_path=None) -> int:
    """После прогона: снимает текущее содержимое всех файлов как `after`.

    Возвращает число обновлённых снимков. Перезаписывает существующий after.
    """
    updated = 0
    for rel in list_snapshots(root):
        snap = _load(root, rel)
        content = _read_file_text(project_path, rel, working_path=working_path)
        if content is None:
            continue
        snap["after"] = content
        # Если before не был зафиксирован (файл появился во время прогона) —
        # хотя бы зафиксируем сам факт сравнения: before = "" (новый файл).
        snap.setdefault("before", "")
        snap["path"] = rel
        snap["project_root"] = str(project_path) if project_path else snap.get("project_root", "")
        if _save(root, rel, snap):
            updated += 1
    return updated


def get_file_diff(root, rel_path: str) -> Dict[str, Any]:
    """Полный payload для UI: before/after/error_lines/changes/path."""
    snap = _load(root, rel_path)
    if not snap:
        return {"path": rel_path, "before": "", "after": "",
                "error_lines": [], "changes": [], "missing": True}
    return {
        "path": snap.get("path") or rel_path,
        "before": snap.get("before") or "",
        "after": snap.get("after") or "",
        "error_lines": list(snap.get("error_lines") or []),
        "changes": list(snap.get("changes") or []),
        "ts": snap.get("ts") or "",
        "project_root": snap.get("project_root") or "",
        "missing": False,
    }


def clear(root) -> int:
    """Удаляет все снимки. Используется в начале нового прогона."""
    d = _snap_dir(root)
    n = 0
    for jf in d.glob("*.json"):
        try:
            jf.unlink()
            n += 1
        except Exception:
            continue
    return n
