"""
Лог изменений, применённых движком (`chat/changes.jsonl`).

Формат: одна JSON-строка на изменение. Чат-LLM читает последние N записей и
кладёт их в system-prompt, чтобы корректно отвечать на вопросы вида
«что и почему ты изменил в файле X».

Запись содержит:
    {
        "ts":        ISO-8601 UTC,
        "verdict":   "ACCEPT" | "NEEDS_REVIEW" | "REJECT" | "FAILED" | ...,
        "file":      "src/lib/foo.rs",
        "line":      42,
        "code":      "E0432" | "F401" | "sql_injection" | ...,
        "intent":    "fix import",                   # из EditSet.intent
        "patch_source": "structured_llm" | "rule_based" | ...,
        "message":   первые ~200 символов исходного сообщения,
        "confidence": 0.85
    }

Никогда не валит вызывающего: I/O-ошибки только логируются.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CHANGES_FILE_NAME = "changes.jsonl"
# Стандартная подкаталог-папка для чата.
CHAT_DIR_NAME = "chat"


def _chat_dir(root) -> Path:
    """Возвращает путь к папке chat/ под `root`, создавая её при первом обращении."""
    p = Path(root) / CHAT_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def append_change(root, change: Dict[str, Any]) -> bool:
    """Дописывает одну запись в `<root>/chat/changes.jsonl`.

    Возвращает True при успехе, False при ошибке (никогда не бросает).
    Поле `ts` проставляется автоматически, если нет.
    """
    try:
        d = dict(change or {})
        d.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        line = json.dumps(d, ensure_ascii=False)
        path = _chat_dir(root) / CHANGES_FILE_NAME
        # Атомарный append-через-открытие; для JSONL это безопасно.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        logger.debug("append_change упал: %s", e)
        return False


def read_changes(root, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Читает `<root>/chat/changes.jsonl`. `limit=N` — берём последние N записей.

    Битые строки пропускаем. Файла нет → `[]`.
    """
    path = _chat_dir(root) / CHANGES_FILE_NAME
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except Exception:
                    continue
    except Exception as e:
        logger.debug("read_changes упал: %s", e)
        return []
    if limit is not None and limit >= 0:
        return out[-limit:]
    return out


def event_to_change(event_type: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Конвертирует пер-ошибочное событие в запись changes-лога.

    Логируем только финальные вердикты по ошибке: applied / needs_review / failed.
    Прочие события (run_started/error_dequeued/patch_proposed/verdict) пропускаем.
    Если data пустая или не содержит ключевых полей — возвращаем None.
    """
    if not data:
        return None
    mapping = {
        "applied": "ACCEPT",
        "needs_review": "NEEDS_REVIEW",
        "failed": "FAILED",
    }
    verdict = mapping.get(event_type)
    if verdict is None:
        return None
    return {
        "verdict": verdict,
        "file": data.get("file", "?"),
        "line": data.get("line", 0),
        "code": data.get("code") or "?",
        "intent": data.get("intent") or "",
        "patch_source": data.get("patch_source") or "",
        "message": (data.get("message") or "")[:200],
        "confidence": float(data.get("confidence") or 0.0),
    }
