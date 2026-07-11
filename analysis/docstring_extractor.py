"""
Stage H.1 — DocstringExtractor.

Достаёт из исходника пары «задокументированное намерение ↔ код, который его
реализует». Это вход для семантического аудита (H.2/H.3): сравнить, делает ли
код то, что обещает его docstring/doc-комментарий.

Поддержка:
  * Python  — docstring у `def`/`class` (через ast, надёжно).
  * Rust    — `///` / `//!` doc-комментарии над `fn`/`struct`/`enum`/`trait`.
  * JS/TS   — JSDoc-блок `/** ... */` над `function`/методом/`class`.

Чистый модуль без LLM и без сети — полностью детерминирован и тестируем.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class DocItem:
    """Один задокументированный элемент."""

    name: str
    kind: str          # function / class / struct / enum / trait / method
    doc: str           # текст документации (очищенный)
    code: str          # тело элемента (сигнатура + содержимое)
    line: int          # 1-based строка объявления

    def to_dict(self):
        return {"name": self.name, "kind": self.kind, "doc": self.doc,
                "code": self.code, "line": self.line}


class DocstringExtractor:
    """Извлекает `DocItem` из текста файла по языку."""

    # сколько строк тела максимум кладём в `code` (чтобы аудит-промпт не пух)
    MAX_CODE_LINES = 60

    def extract(self, source: str, language: str) -> List[DocItem]:
        lang = (language or "").lower()
        if not source or not source.strip():
            return []
        try:
            if lang in ("python", "py"):
                return self._extract_python(source)
            if lang in ("rust", "rs"):
                return self._extract_rust(source)
            if lang in ("javascript", "js", "typescript", "ts"):
                return self._extract_js(source)
        except Exception as e:  # pragma: no cover - защита от кривого ввода
            logger.debug("DocstringExtractor: %s упал: %s", lang, e)
        return []

    # ---- Python (ast) ----------------------------------------------
    def _extract_python(self, source: str) -> List[DocItem]:
        out: List[DocItem] = []
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            doc = ast.get_docstring(node)
            if not doc or not doc.strip():
                continue
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            code = "\n".join(lines[start - 1:end])
            out.append(DocItem(
                name=node.name, kind=kind, doc=doc.strip(),
                code=self._trim(code), line=start,
            ))
        return out

    # ---- Rust ( /// doc comments ) ---------------------------------
    _RUST_ITEM_RE = re.compile(
        r"^\s*(?:pub\s+)?(?:async\s+)?(?P<kind>fn|struct|enum|trait)\s+(?P<name>[A-Za-z_]\w*)"
    )

    def _extract_rust(self, source: str) -> List[DocItem]:
        out: List[DocItem] = []
        lines = source.splitlines()
        i = 0
        n = len(lines)
        while i < n:
            stripped = lines[i].strip()
            # копим подряд идущие doc-комментарии
            if stripped.startswith("///") or stripped.startswith("//!"):
                doc_lines = []
                start_doc = i
                while i < n and (lines[i].strip().startswith("///")
                                 or lines[i].strip().startswith("//!")):
                    doc_lines.append(re.sub(r"^\s*//[/!]\s?", "", lines[i]))
                    i += 1
                # пропускаем атрибуты (#[...]) между doc и объявлением
                while i < n and lines[i].strip().startswith("#["):
                    i += 1
                if i < n:
                    m = self._RUST_ITEM_RE.match(lines[i])
                    if m:
                        code = self._rust_item_body(lines, i)
                        out.append(DocItem(
                            name=m.group("name"), kind=m.group("kind"),
                            doc="\n".join(doc_lines).strip(),
                            code=self._trim(code), line=i + 1,
                        ))
                continue
            i += 1
        return out

    @staticmethod
    def _rust_item_body(lines: List[str], decl_idx: int) -> str:
        """Тело rust-элемента: от объявления до сбалансированной `}` или `;`."""
        text_from = lines[decl_idx:]
        depth = 0
        seen_brace = False
        collected: List[str] = []
        for ln in text_from:
            collected.append(ln)
            depth += ln.count("{")
            if "{" in ln:
                seen_brace = True
            depth -= ln.count("}")
            if seen_brace and depth <= 0:
                break
            if not seen_brace and ln.rstrip().endswith(";"):
                break  # struct Foo; / fn без тела
            if len(collected) > 80:
                break
        return "\n".join(collected)

    # ---- JS / TS ( /** JSDoc */ ) ----------------------------------
    _JS_DECL_RE = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        r"(?:(?P<fkind>function)\s+(?P<fname>[A-Za-z_$][\w$]*)"
        r"|(?P<ckind>class)\s+(?P<cname>[A-Za-z_$][\w$]*)"
        r"|(?:const|let|var)\s+(?P<vname>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>)"
        r"|(?P<mname>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{)"
    )

    def _extract_js(self, source: str) -> List[DocItem]:
        out: List[DocItem] = []
        lines = source.splitlines()
        n = len(lines)
        i = 0
        while i < n:
            if "/**" in lines[i]:
                doc_lines = []
                # собрать JSDoc-блок до */
                j = i
                while j < n:
                    doc_lines.append(lines[j])
                    if "*/" in lines[j]:
                        break
                    j += 1
                # следующая непустая строка — объявление
                k = j + 1
                while k < n and not lines[k].strip():
                    k += 1
                if k < n:
                    m = self._JS_DECL_RE.match(lines[k])
                    if m:
                        name = (m.group("fname") or m.group("cname")
                                or m.group("vname") or m.group("mname") or "")
                        kind = ("class" if m.group("ckind")
                                else "function")
                        code = self._js_item_body(lines, k)
                        out.append(DocItem(
                            name=name, kind=kind,
                            doc=self._clean_jsdoc(doc_lines),
                            code=self._trim(code), line=k + 1,
                        ))
                i = j + 1
                continue
            i += 1
        return out

    @staticmethod
    def _clean_jsdoc(doc_lines: List[str]) -> str:
        out = []
        for ln in doc_lines:
            s = ln.strip()
            s = re.sub(r"^/\*\*?", "", s)
            s = re.sub(r"\*/$", "", s)
            s = re.sub(r"^\*\s?", "", s)
            if s.strip():
                out.append(s.strip())
        return "\n".join(out).strip()

    @staticmethod
    def _js_item_body(lines: List[str], decl_idx: int) -> str:
        depth = 0
        seen_brace = False
        collected: List[str] = []
        for ln in lines[decl_idx:]:
            collected.append(ln)
            depth += ln.count("{")
            if "{" in ln:
                seen_brace = True
            depth -= ln.count("}")
            if seen_brace and depth <= 0:
                break
            if len(collected) > 80:
                break
        return "\n".join(collected)

    # ---- util -------------------------------------------------------
    @classmethod
    def _trim(cls, code: str) -> str:
        lines = code.splitlines()
        if len(lines) <= cls.MAX_CODE_LINES:
            return code
        kept = lines[:cls.MAX_CODE_LINES]
        kept.append(f"// ... [{len(lines) - cls.MAX_CODE_LINES} more lines truncated]")
        return "\n".join(kept)
