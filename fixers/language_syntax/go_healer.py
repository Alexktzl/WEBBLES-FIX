"""Минимальный синтаксический healer для Go (без LLM)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class GoSyntaxHealer:
    """Лечит распространённые синтаксические ошибки Go без LLM.
    Go очень строг к стилю — большинство правок делает `gofmt`. Этот healer
    закрывает только базовые: пропущенная `}` и удаление неиспользуемого
    `import`-а (две частые цели go vet/build)."""

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

        # GO_UNUSED_IMP: imported and not used "X"
        if code == "GO_UNUSED_IMP" or "imported and not used" in msg:
            return self._strip_unused_import(text, error)
        # GO_SYNTAX: реактивная дописка } если опен > клоуз
        if code == "GO_SYNTAX" and text.count("{") > text.count("}"):
            tail = "\n" if not text.endswith("\n") else ""
            return text + tail + "}\n"
        return None

    @staticmethod
    def _strip_unused_import(text: str, error: Dict) -> Optional[str]:
        # Имя в кавычках после "imported and not used"
        import re
        msg = error.get("message", "") or ""
        m = re.search(r'imported and not used:\s*"([^"]+)"', msg)
        if not m:
            return None
        path = m.group(1)
        lines = text.splitlines(keepends=True)
        kept = []
        removed = False
        for line in lines:
            # одиночный import "X"
            if not removed and line.strip() == f'import "{path}"':
                removed = True
                continue
            # внутри () блока: строка вида `"path"` или `alias "path"`
            if not removed and (
                line.strip() == f'"{path}"'
                or line.strip().endswith(f' "{path}"')
            ):
                removed = True
                continue
            kept.append(line)
        if not removed:
            return None
        return "".join(kept)
