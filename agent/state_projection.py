"""
Stage L.1 — слой проекции состояния агента.

Пишет/читает `.webbles/{name}_state.md` и `.webbles/{name}_context.md`.

`{name}_state.md` устроен как **frontmatter-голова + проза-журнал**:

    ---
    { "mode": "fix", "next_action": "E0609:src/main.rs:4", "queue": [...],
      "done": [...], "step_id": 3, "last_verdict": "ACCEPT",
      "counters": {"accepted": 2, "needs_review": 1, "failed": 0} }
    ---

    # Журнал агента

    ## [шаг 3] E0609 @ src/main.rs:4 — ACCEPT
    - **Что:** ...
    - **Почему:** ...
    - **Как:** ...

**Машина** читает только frontmatter (JSON между первыми `---`). **Человек/UI**
читают прозу ниже. Это сознательно: markdown-проза НЕ парсится обратно в логику,
поэтому она не может разойтись с исполнением. Авторитет исполнения остаётся за
`PipelineContext`/`memory`; этот файл — проекция и журнал.

Зависимости — только стандартная библиотека.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_FENCE = "---"


def default_head(mode: str = "fix") -> Dict[str, Any]:
    return {
        "mode": mode,
        "next_action": None,   # сигнатура текущей/следующей ошибки или None
        "queue": [],           # компактные дескрипторы ожидающих ошибок
        "done": [],            # [{sig, verdict}] завершённых
        "step_id": 0,
        "last_verdict": None,
        "counters": {"accepted": 0, "needs_review": 0, "failed": 0},
    }


class StateProjection:
    """Проекция состояния агента в человеко-читаемый .md с машинной головой."""

    def __init__(self, project_root, name: str = "fix"):
        self.root = Path(project_root)
        self.dir = self.root / ".webbles"
        self.state_path = self.dir / f"{name}_state.md"
        self.context_path = self.dir / f"{name}_context.md"
        self.name = name

    # ------------------------------------------------------------------
    # Frontmatter (машинная голова)
    # ------------------------------------------------------------------
    def load_head(self) -> Dict[str, Any]:
        """Читает JSON-frontmatter. Если файла/головы нет или она битая —
        возвращает дефолтную голову (мягкая деградация)."""
        if not self.state_path.exists():
            return default_head()
        text = self.state_path.read_text(encoding="utf-8", errors="ignore")
        head_json = self._extract_frontmatter(text)
        if head_json is None:
            return default_head()
        try:
            data = json.loads(head_json)
            if not isinstance(data, dict):
                return default_head()
            # дополняем недостающие ключи дефолтами
            base = default_head(data.get("mode", "fix"))
            base.update(data)
            return base
        except json.JSONDecodeError:
            return default_head()

    def load_journal(self) -> str:
        """Возвращает прозу-журнал (всё после второго `---`)."""
        if not self.state_path.exists():
            return ""
        text = self.state_path.read_text(encoding="utf-8", errors="ignore")
        return self._extract_body(text)

    def save_head(self, head: Dict[str, Any]) -> None:
        """Перезаписывает frontmatter, сохраняя существующую прозу."""
        body = self.load_journal()
        self._write(head, body)

    def append_journal(self, markdown: str) -> None:
        """Дописывает блок в прозу-журнал, не трогая голову."""
        head = self.load_head()
        body = self.load_journal()
        if body and not body.endswith("\n"):
            body += "\n"
        body += markdown.rstrip("\n") + "\n"
        self._write(head, body)

    def init_if_absent(self, mode: str = "fix") -> Dict[str, Any]:
        """Первый запуск: создаёт файл с дефолтной головой и заголовком журнала."""
        if self.state_path.exists():
            return self.load_head()
        head = default_head(mode)
        self._write(head, "# Журнал агента\n")
        return head

    # ------------------------------------------------------------------
    # Контекст-файл
    # ------------------------------------------------------------------
    def write_context(self, markdown: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.context_path.write_text(markdown, encoding="utf-8")

    def read_context(self) -> str:
        if not self.context_path.exists():
            return ""
        return self.context_path.read_text(encoding="utf-8", errors="ignore")

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _write(self, head: Dict[str, Any], body: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        head_json = json.dumps(head, ensure_ascii=False, indent=2)
        content = f"{_FENCE}\n{head_json}\n{_FENCE}\n\n{body}"
        if not content.endswith("\n"):
            content += "\n"
        self.state_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _extract_frontmatter(text: str) -> Optional[str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != _FENCE:
            return None
        for i in range(1, len(lines)):
            if lines[i].strip() == _FENCE:
                return "\n".join(lines[1:i])
        return None

    @staticmethod
    def _extract_body(text: str) -> str:
        lines = text.splitlines()
        if not lines or lines[0].strip() != _FENCE:
            return text  # нет frontmatter — всё тело
        for i in range(1, len(lines)):
            if lines[i].strip() == _FENCE:
                return "\n".join(lines[i + 1:]).lstrip("\n")
        return ""
