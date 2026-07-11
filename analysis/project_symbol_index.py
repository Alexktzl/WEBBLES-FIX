"""
Stage I.1 — Project-wide symbol index (cross-file reasoning).

Строит индекс по ВСЕМ исходникам проекта за один проход: где определён каждый
символ (struct/enum/trait/fn/class/def/function/...) и где он используется. Это
основа для cross-file рассуждений (I.3): найти все определения и ссылки символа,
оценить «радиус влияния» правки, обогатить case-file определениями из всего
проекта (а не одного файла, как C.1).

tree-sitter в среде недоступен, поэтому индекс — regex-овый (тот же стиль, что
`analysis/symbol_graph.py`). Это honest-ограничение: ловим объявления верхнего
уровня и обычные ссылки по имени; не строим полноценный AST/scope-резолвинг.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Служебные каталоги, которые не индексируем.
_SKIP_DIRS = {"target", "node_modules", ".git", "venv", ".venv",
              "__pycache__", ".webbles_fix", "dist", "build", ".mypy_cache"}

_EXTS = {
    "rust": (".rs",), "rs": (".rs",),
    "python": (".py",), "py": (".py",),
    "javascript": (".js", ".jsx"), "js": (".js", ".jsx"),
    "typescript": (".ts", ".tsx"), "ts": (".ts", ".tsx"),
}

# Лимиты, чтобы индексация большого репо оставалась дешёвой.
_MAX_FILES = 2000
_MAX_FILE_BYTES = 1_000_000

# Идентификатор для подсчёта ссылок.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


@dataclass
class SymbolDef:
    """Одно определение символа."""

    name: str
    kind: str          # fn/struct/enum/trait/class/function/const/...
    file: str          # путь относительно корня проекта
    line: int          # 1-based
    signature: str     # строка объявления (обрезанная)

    def to_dict(self):
        return {"name": self.name, "kind": self.kind, "file": self.file,
                "line": self.line, "signature": self.signature}


def _def_patterns(language: str):
    """(compiled_regex, kind_group_or_const) для языка."""
    lang = (language or "").lower()
    if lang in ("rust", "rs"):
        return [(re.compile(
            r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?"
            r"(?P<kind>fn|struct|enum|trait|union|type|const|static|mod)\s+"
            r"(?P<name>[A-Za-z_]\w*)"), None)]
    if lang in ("python", "py"):
        return [
            (re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)"), "function"),
            (re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)"), "class"),
        ]
    if lang in ("javascript", "typescript", "js", "ts"):
        return [
            (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)"), "function"),
            (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"), "class"),
            (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*="), "binding"),
            (re.compile(r"^\s*(?:export\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)"), "interface"),
            (re.compile(r"^\s*(?:export\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)"), "type"),
        ]
    return []


class ProjectSymbolIndex:
    """Индекс определений и ссылок по всему проекту."""

    def __init__(self, language: str):
        self.language = (language or "").lower()
        self._defs: Dict[str, List[SymbolDef]] = {}
        self._files: Dict[str, str] = {}      # rel-path → текст (кэш для ссылок)
        self._built = False

    # -----------------------------------------------------------------
    @classmethod
    def build(cls, root, language: str) -> "ProjectSymbolIndex":
        idx = cls(language)
        idx._build(Path(root))
        return idx

    def _build(self, root: Path) -> None:
        patterns = _def_patterns(self.language)
        exts = _EXTS.get(self.language)
        if not exts or not patterns:
            self._built = True
            return
        count = 0
        for path in sorted(root.rglob("*")):
            if count >= _MAX_FILES:
                logger.info("ProjectSymbolIndex: лимит %d файлов", _MAX_FILES)
                break
            if not path.is_file() or path.suffix not in exts:
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            count += 1
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            self._files[rel] = text
            self._index_defs(rel, text, patterns)
        self._built = True
        logger.debug("ProjectSymbolIndex: %d файлов, %d уникальных символов",
                     count, len(self._defs))

    def _index_defs(self, rel: str, text: str, patterns) -> None:
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rx, kind_const in patterns:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group("name")
                kind = kind_const or (m.groupdict().get("kind") or "def")
                self._defs.setdefault(name, []).append(SymbolDef(
                    name=name, kind=kind, file=rel, line=lineno,
                    signature=line.strip()[:200],
                ))

    # -----------------------------------------------------------------
    # Запросы (I.3 cross-file reasoning)
    # -----------------------------------------------------------------
    def definitions(self, name: str) -> List[SymbolDef]:
        """Все определения символа по проекту."""
        return list(self._defs.get(name, []))

    def is_defined(self, name: str) -> bool:
        return name in self._defs

    def references(self, name: str,
                   exclude_definitions: bool = True) -> List[Tuple[str, int]]:
        """(file, line) всех вхождений `name` как отдельного слова по проекту.

        `exclude_definitions=True` убирает строки, которые сами являются
        объявлением этого символа (чтобы отделить «использование» от «места
        определения»)."""
        if not name:
            return []
        word = re.compile(r"\b" + re.escape(name) + r"\b")
        def_lines = {(d.file, d.line) for d in self._defs.get(name, [])}
        out: List[Tuple[str, int]] = []
        for rel, text in self._files.items():
            for lineno, line in enumerate(text.splitlines(), start=1):
                if word.search(line):
                    if exclude_definitions and (rel, lineno) in def_lines:
                        continue
                    out.append((rel, lineno))
        return out

    def impact_files(self, name: str) -> List[str]:
        """Файлы, которые ссылаются на символ (кроме файлов его определения).

        «Если поменять `name`, эти файлы под ударом» — радиус влияния правки.
        """
        def_files = {d.file for d in self._defs.get(name, [])}
        files = {rel for (rel, _ln) in self.references(name)}
        return sorted(files - def_files)

    def undefined_symbols(self, names: List[str]) -> List[str]:
        """Из переданных имён — те, что нигде в проекте не определены
        (кандидаты на cross-file «неразрешённую ссылку»)."""
        return [n for n in names if n and n not in self._defs]

    def cross_file_context(self, name: str, max_refs: int = 10) -> str:
        """Человекочитаемый блок для case-file: где символ определён и где
        используется по всему проекту. Пусто, если символ не индексирован.
        """
        defs = self._defs.get(name)
        if not defs:
            return ""
        parts: List[str] = [f"### `{name}` — project-wide"]
        for d in defs[:5]:
            parts.append(f"- def [{d.kind}] {d.file}:{d.line}  `{d.signature}`")
        refs = self.references(name)
        if refs:
            parts.append(f"used in {len(refs)} place(s):")
            for rel, ln in refs[:max_refs]:
                parts.append(f"  - {rel}:{ln}")
            if len(refs) > max_refs:
                parts.append(f"  - ... (+{len(refs) - max_refs} more)")
        return "\n".join(parts)
