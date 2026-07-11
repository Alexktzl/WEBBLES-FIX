"""
Реестр чатов: каждый чат — отдельная сессия с собственным `history.jsonl`.

Структура на диске:
    chat/
      index.json                       — список чатов + активный
      rules.md                         — общие правила (по умолчанию один на всех)
      changes.jsonl                    — глобальный лог изменений движка
      sessions/
        <chat_id>/history.jsonl        — история ровно этого чата

`index.json`:
    {
      "active": "<chat_id или null>",
      "chats": [
        {"id": "<uuid>", "name": "Fix type error", "ts": "ISO-8601",
         "mode": "chat" | "fix" | "create"}
      ]
    }

Без явного создания первый `chat_send` создаст «default» чат автоматически.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CHAT_DIR_NAME = "chat"
SESSIONS_DIR_NAME = "sessions"
INDEX_FILE_NAME = "index.json"
HISTORY_FILE_NAME = "history.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chat_dir(root) -> Path:
    p = Path(root) / CHAT_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sessions_dir(root) -> Path:
    p = _chat_dir(root) / SESSIONS_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_path(root) -> Path:
    return _chat_dir(root) / INDEX_FILE_NAME


def _safe_id() -> str:
    # 12 hex-символов — устойчивый и читаемый идентификатор.
    return secrets.token_hex(6)


def _load_index(root) -> Dict[str, Any]:
    p = _index_path(root)
    if not p.exists():
        return {"active": None, "chats": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"active": None, "chats": []}
        data.setdefault("active", None)
        data.setdefault("chats", [])
        return data
    except Exception as e:
        logger.debug("index.json не прочитан: %s", e)
        return {"active": None, "chats": []}


def _save_index(root, data: Dict[str, Any]) -> bool:
    p = _index_path(root)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception as e:
        logger.debug("index.json не сохранён: %s", e)
        return False


def session_dir(root, chat_id: str) -> Path:
    """Возвращает каталог конкретного чата (создаёт при необходимости)."""
    p = _sessions_dir(root) / chat_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def history_path(root, chat_id: str) -> Path:
    return session_dir(root, chat_id) / HISTORY_FILE_NAME


def list_chats(root) -> List[Dict[str, Any]]:
    """Возвращает список чатов с пометкой `active` и счётчиком сообщений."""
    data = _load_index(root)
    active = data.get("active")
    out: List[Dict[str, Any]] = []
    for c in data.get("chats", []):
        if not isinstance(c, dict) or not c.get("id"):
            continue
        entry = {
            "id": c.get("id"),
            "name": c.get("name") or "Без названия",
            "ts": c.get("ts") or "",
            "mode": c.get("mode") or "chat",
            "active": c.get("id") == active,
        }
        # Кол-во сообщений — небольшой UX-сигнал.
        try:
            hp = history_path(root, c["id"])
            if hp.exists():
                entry["messages"] = sum(1 for _ in hp.open("r", encoding="utf-8"))
            else:
                entry["messages"] = 0
        except Exception:
            entry["messages"] = 0
        out.append(entry)
    # Сортировка по ts (новые сверху).
    out.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return out


def get_active(root) -> Optional[str]:
    return _load_index(root).get("active")


def set_active(root, chat_id: str) -> bool:
    """Делает чат активным. Если такого нет — False."""
    data = _load_index(root)
    if not any(c.get("id") == chat_id for c in data.get("chats", [])):
        return False
    data["active"] = chat_id
    return _save_index(root, data)


def create_chat(root, name: Optional[str] = None, *, mode: str = "chat",
                set_as_active: bool = True) -> str:
    """Создаёт новый чат, возвращает его id."""
    data = _load_index(root)
    chat_id = _safe_id()
    rec = {
        "id": chat_id,
        "name": (name or "Новый чат")[:120],
        "ts": _now_iso(),
        "mode": mode,
    }
    data.setdefault("chats", []).append(rec)
    if set_as_active:
        data["active"] = chat_id
    _save_index(root, data)
    # Создаём пустой каталог сессии заранее, чтобы list_chats показал 0 messages.
    session_dir(root, chat_id)
    return chat_id


def rename_chat(root, chat_id: str, name: str) -> bool:
    data = _load_index(root)
    for c in data.get("chats", []):
        if c.get("id") == chat_id:
            c["name"] = (name or "Без названия")[:120]
            return _save_index(root, data)
    return False


def set_mode(root, chat_id: str, mode: str) -> bool:
    data = _load_index(root)
    for c in data.get("chats", []):
        if c.get("id") == chat_id:
            c["mode"] = mode
            return _save_index(root, data)
    return False


def delete_chat(root, chat_id: str) -> bool:
    """Удаляет чат и его историю. Если он был активным — сбрасываем active."""
    data = _load_index(root)
    chats = data.get("chats", [])
    new_chats = [c for c in chats if c.get("id") != chat_id]
    if len(new_chats) == len(chats):
        return False
    data["chats"] = new_chats
    if data.get("active") == chat_id:
        data["active"] = new_chats[0]["id"] if new_chats else None
    # Чистим файлы.
    try:
        import shutil
        shutil.rmtree(session_dir(root, chat_id), ignore_errors=True)
    except Exception:
        pass
    return _save_index(root, data)


def ensure_active(root, *, default_name: str = "Чат 1",
                  default_mode: str = "chat") -> str:
    """Гарантирует, что есть активный чат. Если нет — создаёт.
    Возвращает id активного чата.
    """
    data = _load_index(root)
    active = data.get("active")
    if active and any(c.get("id") == active for c in data.get("chats", [])):
        return active
    # Возьмём первый существующий, если есть, иначе создадим.
    chats = [c for c in data.get("chats", []) if isinstance(c, dict) and c.get("id")]
    if chats:
        data["active"] = chats[0]["id"]
        _save_index(root, data)
        return chats[0]["id"]
    return create_chat(root, default_name, mode=default_mode)
