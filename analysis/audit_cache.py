"""
Stage H.4 — кэш семантических аудитов по хешу (docstring, code).

Аудит docstring↔код — это LLM-вызов, дорогой и детерминированный относительно
пары (doc, code). Пока пара не изменилась, переспрашивать LLM незачем. Кэш
живёт между сессиями (как memory из A.1 и example_cache из J.5).

Файлы: `.webbles_fix/audit_cache/<sha1>.json`, TTL по умолчанию 30 дней.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AuditCache:
    """Файловый кэш результатов аудита по хешу (doc, code)."""

    def __init__(self, root: Path, ttl_days: int = 30):
        self.root = Path(root)
        self.ttl = timedelta(days=int(ttl_days))

    @staticmethod
    def key(doc: str, code: str) -> str:
        h = hashlib.sha1()
        h.update((doc or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((code or "").encode("utf-8"))
        return h.hexdigest()

    def _path_for(self, doc: str, code: str) -> Path:
        return self.root / f"{self.key(doc, code)}.json"

    def get(self, doc: str, code: str) -> Optional[Dict[str, Any]]:
        path = self._path_for(doc, code)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("audit-cache: не прочитать %s: %s", path, e)
            return None
        if not self._is_fresh(raw.get("saved_at")):
            return None
        result = raw.get("result")
        return result if isinstance(result, dict) else None

    def put(self, doc: str, code: str, result: Dict[str, Any]) -> None:
        path = self._path_for(doc, code)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug("audit-cache: не создать %s: %s", path.parent, e)
            return
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8", newline="")
            tmp.replace(path)
        except Exception as e:
            logger.debug("audit-cache: не записать %s: %s", path, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def _is_fresh(self, saved_at: Optional[str]) -> bool:
        if not saved_at:
            return False
        try:
            ts = datetime.fromisoformat(saved_at)
        except Exception:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts) <= self.ttl
