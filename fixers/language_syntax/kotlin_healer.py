"""Минимальный синтаксический healer для Kotlin (без LLM)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class KotlinSyntaxHealer:
    """Лечит распространённые синтаксические ошибки Kotlin без LLM:
       пропущенная `}`, безопасный вызов `?.` для KT_NULLSAFE.
       Сложнее — отдаётся LLM."""

    def heal(self, error: Dict, file_path: Path,
             llm_client=None, invariant_guard=None, segmenter=None) -> Optional[str]:
        code = error.get("code", "")
        msg = (error.get("message") or "").lower()
        if not file_path.exists():
            return None
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            return None

        # KT_SYNTAX: дописать } если open > close
        if (code == "KT_SYNTAX" and "expecting '}'" in msg) or "expecting '}'" in msg:
            if text.count("{") > text.count("}"):
                tail = "\n" if not text.endswith("\n") else ""
                return text + tail + "}\n"

        # KT_NULLSAFE: попытка safe-call (?.) для простого паттерна `s.x` → `s?.x`
        if code == "KT_NULLSAFE":
            ln = int(error.get("line", 0) or 0)
            if ln >= 1:
                lines = text.splitlines(keepends=True)
                if 0 < ln <= len(lines):
                    idx = ln - 1
                    line = lines[idx]
                    # очень аккуратно: заменяем ИМЕННО `<ident>.<ident>(`
                    # на `<ident>?.<ident>(` (без жадности)
                    new_line = re.sub(
                        r"(\b[A-Za-z_]\w*)\.([A-Za-z_]\w*\()",
                        r"\1?.\2", line, count=1,
                    )
                    if new_line != line:
                        lines[idx] = new_line
                        return "".join(lines)
        return None
