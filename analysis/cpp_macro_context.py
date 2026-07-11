"""
Препроцессор-context (мини): подкладка определений макросов в case-file.

ЗАЧЕМ. В C/C++ макросы развёртываются ДО компиляции — ошибка приходит на
строке, где использован макрос, но смысл ошибки кроется в его теле.
Например, `FOO(x)` на строке 14 может развернуться в 20 строк кода, и
g++ скажет «cannot convert 'int*' to 'string' at line 14», не показывая
саму магию `FOO`. LLM, видя только исходник, не понимает причину.

Этот модуль решает 80% случаев макро-путаницы простым способом — БЕЗ
полного `g++ -E` маппинга (он сложнее и фрагильнее с `#ifdef`/include-цепочкой):

  1. сканируем все `.cpp/.cc/.cxx/.c/.hpp/.h/.hxx/.inl` за один проход;
  2. извлекаем `#define NAME ...` (object-like) и `#define NAME(args) ...`
     (function-like), с поддержкой многострочных через `\\` continuation;
  3. на запрос «дай макросы, упомянутые в этой ошибке» — ищем uppercase-
     идентификаторы (соглашение C-макросов) в строке исходника и в
     сообщении компилятора;
  4. возвращаем текстовый блок «## MACRO CONTEXT» для case-file.

Honest-ограничения:
  * Регекс-уровень, не парсер. `#ifdef`-зависимые ветки не разруливаются.
  * Comment-stripping простой: `//`-комментарии до конца строки; блочные
    `/* */` пропускаем построчно.
  * Не подгружаем системные/`/usr/include` пути.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Файлы C/C++.
_CXX_EXTS = {".cpp", ".cc", ".cxx", ".c", ".hpp", ".h", ".hxx", ".inl", ".h++", ".cpp++"}

# Каталоги, которые скипаем (артефакты сборки/чужие либы).
_SKIP_DIRS = {"build", "cmake-build-debug", "cmake-build-release", "out",
              ".git", "node_modules", ".vs", ".vscode", "vcpkg_installed",
              "third_party", ".webbles_fix"}

_MAX_FILES = 2000
_MAX_FILE_BYTES = 1_000_000

# `#define NAME` — object-like; `#define NAME(...)` — function-like.
_DEFINE_HEAD = re.compile(
    r"^\s*#\s*define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<params>\([^)]*\))?"
    r"(?:\s+(?P<body>.*))?$"
)
# Uppercase-идентификатор: соглашение макросов. Минимум 2 символа,
# чтобы не ловить случайные `X` / `Y`.
_UPPER_IDENT = re.compile(r"\b([A-Z][A-Z0-9_]{1,})\b")
# Любой идентификатор (для исходной строки — там макросы тоже бывают
# и в нижнем регистре, но мы ограничим по таблице макросов).
_ANY_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]+)\b")


@dataclass
class MacroDef:
    """Одно определение макроса из `#define`."""
    name: str
    file: str          # путь относительно корня проекта
    line: int          # 1-based строка `#define`
    body: str          # тело макроса (может быть пустым / многострочным)
    is_function_like: bool = False
    params: List[str] = field(default_factory=list)

    def render(self) -> str:
        """Однострочное отображение определения для case-file."""
        if self.is_function_like:
            p = ", ".join(self.params)
            head = f"#define {self.name}({p})"
        else:
            head = f"#define {self.name}"
        body = self.body.strip()
        if not body:
            return head
        # Если тело многострочное — подрежем до первой строки + индикатор.
        first = body.splitlines()[0].rstrip("\\").rstrip()
        if "\n" in body:
            return f"{head} {first} \\ ..."
        return f"{head} {first}"


@dataclass
class MacroLookupResult:
    """Результат `find_relevant_macros`. Для тестов и debug."""
    matched: List[MacroDef] = field(default_factory=list)
    queried: Set[str] = field(default_factory=set)  # имена, которые искали


# ---------------------------------------------------------------------------
# Сканер
# ---------------------------------------------------------------------------
class MacroScanner:
    """Проектный сканер `#define`-макросов. Один проход по `.cpp/.h/...`."""

    def __init__(self):
        self._macros: Dict[str, MacroDef] = {}
        self._built = False

    @classmethod
    def build(cls, root) -> "MacroScanner":
        """Сканирует все .cpp/.h файлы под `root` и возвращает готовый scanner."""
        s = cls()
        s._build(Path(root))
        return s

    def _iter_files(self, root: Path) -> Iterable[Tuple[Path, str]]:
        count = 0
        for path in sorted(root.rglob("*")):
            if count >= _MAX_FILES:
                logger.info("MacroScanner: лимит %d файлов", _MAX_FILES)
                break
            if not path.is_file() or path.suffix.lower() not in _CXX_EXTS:
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
            yield path, text

    def _build(self, root: Path) -> None:
        for path, text in self._iter_files(root):
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            self._extract_defines(text, rel)
        self._built = True
        logger.debug("MacroScanner: %d уникальных макросов", len(self._macros))

    def _extract_defines(self, text: str, rel_file: str) -> None:
        """Ловит #define с поддержкой backslash-continuation и игнором
        однострочных `//`-комментариев."""
        raw_lines = text.splitlines()
        in_block_comment = False
        i = 0
        while i < len(raw_lines):
            ln = raw_lines[i]
            # очень грубый comment-strip: блочные `/* */` пропускаем построчно.
            if in_block_comment:
                if "*/" in ln:
                    in_block_comment = False
                    ln = ln[ln.index("*/") + 2:]
                else:
                    i += 1
                    continue
            if "/*" in ln and "*/" not in ln:
                in_block_comment = True
                ln = ln[:ln.index("/*")]
            # //-коммент
            sl = ln.find("//")
            if sl >= 0:
                ln = ln[:sl]
            m = _DEFINE_HEAD.match(ln)
            if not m:
                i += 1
                continue
            name = m.group("name")
            params_raw = m.group("params") or ""
            body_first = (m.group("body") or "").rstrip()
            # backslash continuation
            body_parts: List[str] = []
            current = body_first
            while current.endswith("\\"):
                body_parts.append(current[:-1].rstrip())
                i += 1
                if i >= len(raw_lines):
                    break
                next_ln = raw_lines[i]
                # снова обрежем //-коммент в продолжении
                sl2 = next_ln.find("//")
                if sl2 >= 0:
                    next_ln = next_ln[:sl2]
                current = next_ln.rstrip()
            body_parts.append(current)
            body = "\n".join(p for p in body_parts if p).strip()

            is_func = bool(params_raw)
            params: List[str] = []
            if is_func:
                inner = params_raw[1:-1].strip()
                if inner:
                    params = [p.strip() for p in inner.split(",") if p.strip()]
            line_no = i + 1 - (len(body_parts) - 1)  # начало `#define`
            # Сохраняем первое встреченное определение под именем (как ведёт
            # себя препроцессор: повторный `#define` — warning, тело новое).
            # Мы тоже перезаписываем — последнее определение имеет приоритет.
            self._macros[name] = MacroDef(
                name=name, file=rel_file, line=max(1, line_no),
                body=body, is_function_like=is_func, params=params,
            )
            i += 1

    # ---- запросы ---------------------------------------------------------
    def get(self, name: str) -> Optional[MacroDef]:
        return self._macros.get(name)

    def all(self) -> Dict[str, MacroDef]:
        return dict(self._macros)

    def find_relevant_macros(self,
                             error_line_text: str = "",
                             error_message: str = "",
                             extra_idents: Optional[Iterable[str]] = None,
                             limit: int = 5) -> MacroLookupResult:
        """Возвращает макросы, упомянутые на строке исходника ошибки И/ИЛИ
        в тексте сообщения компилятора.

        Стратегия имен-кандидатов:
          * uppercase-идентификаторы (≥2 символа) из обоих текстов;
          * любые идентификаторы из `error_line_text` (но матчим только
            если присутствуют в таблице макросов — этим ограничиваем шум);
          * имена из `extra_idents` (если переданы — например, из case_file
            symbol-graph).
        """
        cands: List[str] = []
        seen: Set[str] = set()

        def push(name: str):
            if not name or name in seen:
                return
            seen.add(name)
            cands.append(name)

        # uppercase в сообщении и строке исходника — самые надёжные кандидаты
        for src in (error_line_text or "", error_message or ""):
            for m in _UPPER_IDENT.finditer(src):
                push(m.group(1))

        # затем — любые идентификаторы из исходной строки (но только
        # те, что есть в нашей таблице макросов — это срежет шум).
        if error_line_text:
            for m in _ANY_IDENT.finditer(error_line_text):
                nm = m.group(1)
                if nm in self._macros:
                    push(nm)

        if extra_idents:
            for nm in extra_idents:
                push(nm)

        matched: List[MacroDef] = []
        used: Set[str] = set()
        for nm in cands:
            mdef = self._macros.get(nm)
            if mdef is None or nm in used:
                continue
            used.add(nm)
            matched.append(mdef)
            if len(matched) >= limit:
                break

        return MacroLookupResult(matched=matched, queried=set(cands))


# ---------------------------------------------------------------------------
# Рендер для case-file
# ---------------------------------------------------------------------------
def render_macro_context(matches: List[MacroDef], *, max_body_lines: int = 6) -> str:
    """Формирует текстовый блок для case-file.

    Пустой список → пустая строка (секция в case-file не появится).
    """
    if not matches:
        return ""
    parts: List[str] = ["## MACRO CONTEXT"]
    for m in matches:
        parts.append(f"### `{m.name}` (defined at {m.file}:{m.line})")
        body_lines = (m.body or "").splitlines()
        if not body_lines:
            parts.append(f"```c\n{m.render()}\n```")
            continue
        head_line = (
            f"#define {m.name}({', '.join(m.params)})"
            if m.is_function_like else f"#define {m.name}"
        )
        # ограничиваем тело
        shown = body_lines[:max_body_lines]
        ellipsis = "    // ... (truncated)" if len(body_lines) > max_body_lines else ""
        block = "\n".join([head_line + (" " + shown[0].lstrip() if shown else "")]
                          + ["    " + ln for ln in shown[1:]]
                          + ([ellipsis] if ellipsis else []))
        parts.append(f"```c\n{block}\n```")
    return "\n".join(parts)
