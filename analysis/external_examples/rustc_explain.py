"""
Stage J.2 — RustcExplainProvider.

`rustc --explain <code>` бесплатно, локально и мгновенно выдаёт официальное
объяснение ошибки rustc вместе с каноническим примером кода. Это самый дешёвый
источник в каскаде и единственный, включённый по умолчанию.

Только Rust и только коды вида `E0382`. Без установленного `rustc` —
graceful `[]` (провайдер считается недоступным).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any, Dict, List

from analysis.external_examples.provider import Example, ExampleSearchProvider

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^E\d{4}$")
# Блок ```rust ... ``` (или просто ``` ... ```) внутри вывода --explain.
_FENCE_RE = re.compile(r"```(?:rust)?\s*\n(.*?)```", re.DOTALL)


class RustcExplainProvider(ExampleSearchProvider):
    name = "rustc_explain"

    # Жёсткий timeout — rustc --explain локален, должен отвечать мгновенно.
    TIMEOUT_SEC = 10

    def available(self) -> bool:
        return shutil.which("rustc") is not None

    def search(self, error: Dict[str, Any], language: str,
               max_results: int = 3) -> List[Example]:
        if (language or "").lower() not in ("rust", "rs"):
            return []
        code = (error.get("code") or "").strip()
        if not _CODE_RE.match(code):
            return []

        text = self._run_explain(code)
        if not text:
            return []

        snippet, title = self._parse(text, code)
        if not snippet and not title:
            return []

        return [Example(
            source=self.name,
            title=title or f"rustc --explain {code}",
            snippet=snippet,
            url=f"https://doc.rust-lang.org/error_codes/{code}.html",
            # Официальный источник — высокий приоритет в каскаде.
            score=0.9,
        )]

    # -----------------------------------------------------------------
    def _run_explain(self, code: str) -> str:
        try:
            proc = subprocess.run(
                ["rustc", "--explain", code],
                capture_output=True, text=True,
                timeout=self.TIMEOUT_SEC,
            )
        except Exception as e:
            logger.debug("rustc_explain: subprocess упал для %s: %s", code, e)
            return ""
        if proc.returncode != 0:
            logger.debug("rustc_explain: rc=%s для %s", proc.returncode, code)
            return ""
        return proc.stdout or ""

    # -----------------------------------------------------------------
    @staticmethod
    def _parse(text: str, code: str):
        """Первый ```rust```-блок → snippet; первая содержательная строка
        текста → title. Если кодовых блоков нет — весь текст в snippet
        (обрезанный)."""
        snippet = ""
        m = _FENCE_RE.search(text)
        if m:
            snippet = m.group(1).strip()

        # title — первая непустая строка вне fence-блоков.
        title = ""
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("```"):
                continue
            title = s
            break
        if len(title) > 200:
            title = title[:197] + "..."

        if not snippet:
            # Нет примера кода — отдаём усечённое объяснение как snippet.
            snippet = text.strip()
        # Не раздуваем окно LLM огромным объяснением.
        if len(snippet) > 1500:
            snippet = snippet[:1500].rstrip() + "\n... [truncated]"
        return snippet, title
