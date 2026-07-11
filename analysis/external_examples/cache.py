"""
Stage J.5 — Кэш внешних примеров по error_code.

Файлы: `.webbles_fix/example_cache/<safe_code>.json`.
Каждая запись: `{"saved_at": <ISO>, "examples": [<example-dict>, ...]}`.
TTL по умолчанию 7 дней — если за неделю ошибка повторилась, в сеть не лезем.
Кэш переживает сессии (как memory из A.1).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from analysis.external_examples.provider import Example

logger = logging.getLogger(__name__)

_SAFE_CODE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class ExampleCache:
    """Файловый кэш примеров по коду ошибки.

    `get(code)` — список Example или None (нет записи / протухла / битая).
    `put(code, examples)` — атомарно (через tmp + replace) сохранить.
    """

    def __init__(self, root: Path, ttl_days: int = 7):
        self.root = Path(root)
        self.ttl = timedelta(days=int(ttl_days))

    # -----------------------------------------------------------------
    def _path_for(self, code: str) -> Path:
        safe = _SAFE_CODE_RE.sub("_", (code or "").strip()) or "_unknown"
        return self.root / f"{safe}.json"

    # -----------------------------------------------------------------
    def get(self, code: str) -> Optional[List[Example]]:
        path = self._path_for(code)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("example-cache: не прочитать %s: %s", path, e)
            return None

        saved_at = raw.get("saved_at")
        if not self._is_fresh(saved_at):
            return None

        items = raw.get("examples")
        if not isinstance(items, list):
            return None
        out: List[Example] = []
        for item in items:
            if isinstance(item, dict):
                try:
                    out.append(Example.from_dict(item))
                except Exception:
                    continue
        return out

    # -----------------------------------------------------------------
    def put(self, code: str, examples: List[Example]) -> None:
        path = self._path_for(code)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug("example-cache: не создать %s: %s", path.parent, e)
            return

        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "code": (code or "").strip(),
            "examples": [
                ex.to_dict() for ex in (examples or [])
                if isinstance(ex, Example)
            ],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            # newline="" — единообразно с planning/incremental_sandbox (CRLF
            # на Windows иначе ломает побайтовое сравнение в других местах).
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8", newline="")
            tmp.replace(path)
        except Exception as e:
            logger.debug("example-cache: не записать %s: %s", path, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # -----------------------------------------------------------------
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
