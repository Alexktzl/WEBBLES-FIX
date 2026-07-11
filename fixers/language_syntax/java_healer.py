"""
Минимальный синтаксический healer для Java (без LLM).

Лечит самые типовые ошибки javac: пропущенная `;`, незакрытая `}`,
пропущенная `)`. Тривиальные эвристики; всё, что сложнее — передаётся в LLM.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class JavaSyntaxHealer:
    """Лечит распространённые синтаксические ошибки Java без LLM."""

    def heal(self, error: Dict, file_path: Path,
             llm_client=None, invariant_guard=None, segmenter=None) -> Optional[str]:
        code = error.get("code", "")
        msg = (error.get("message") or "").lower()
        line_num = error.get("line", 0)
        if not file_path.exists() or line_num < 1:
            return None
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return None
        if line_num > len(lines):
            return None

        if code == "JAVAC_SEMI" or "';' expected" in msg:
            return self._add_semicolon(lines, line_num)
        if code in ("JAVAC_BRACE", "JAVAC_EOF") or "'}' expected" in msg \
                or "reached end of file" in msg:
            return self._add_closing_brace(lines)
        if code == "JAVAC_PAREN" or "')' expected" in msg:
            return self._add_closing_paren(lines, line_num)
        return None

    @staticmethod
    def _add_semicolon(lines, line_num: int) -> Optional[str]:
        idx = line_num - 1
        line = lines[idx]
        stripped = line.rstrip("\r\n")
        if stripped.rstrip().endswith((";", "{", "}", ",")):
            return None
        nl = "\n"
        if line.endswith("\r\n"):
            nl = "\r\n"
        elif not line.endswith("\n"):
            nl = ""
        lines[idx] = stripped + ";" + nl
        return "".join(lines)

    @staticmethod
    def _add_closing_brace(lines) -> Optional[str]:
        # Простая эвристика: дописать одну `}` если открытых > закрытых.
        text = "".join(lines)
        # грубый подсчёт (без учёта строк/комментариев)
        opens = text.count("{")
        closes = text.count("}")
        if opens <= closes:
            return None
        tail = "\n" if not text.endswith("\n") else ""
        return text + tail + "}\n"

    @staticmethod
    def _add_closing_paren(lines, line_num: int) -> Optional[str]:
        idx = line_num - 1
        line = lines[idx]
        opens = line.count("(")
        closes = line.count(")")
        if opens <= closes:
            return None
        stripped = line.rstrip("\r\n")
        # вставим недостающие `)` перед открывающей `{` если есть, иначе в конец строки
        delta = opens - closes
        if "{" in stripped:
            stripped = stripped.replace("{", ")" * delta + " {", 1)
        else:
            stripped = stripped + (")" * delta)
        nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        lines[idx] = stripped + nl
        return "".join(lines)
