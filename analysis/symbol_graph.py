"""
Полный символьный анализ проекта (Symbol Graph).
Собирает определения, использования, достижимость символов.
Классифицирует неиспользуемые элементы для CleanupStage.
Поддерживает Rust (cargo check), Python (AST), JS/TS (ESLint).
"""

import ast
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class SymbolGraph:
    """Построитель графа символов и анализатор unused."""

    def __init__(self, project_path: Path, language: str):
        self.project_path = project_path
        self.language = language.lower()
        self.definitions: Dict[str, Dict[str, Any]] = {}
        self.usages: Dict[str, List[Dict[str, Any]]] = {}
        self.symbols: List[Dict[str, Any]] = []

    def analyze(self) -> List[Dict[str, Any]]:
        if self.language in ("rust", "rs"):
            return self._analyze_rust()
        elif self.language in ("python", "py"):
            return self._analyze_python()
        elif self.language in ("javascript", "typescript", "js", "ts"):
            return self._analyze_javascript()
        else:
            logger.warning(f"Symbol Graph не поддерживает язык {self.language}")
            return []

    def classify_unused(self, symbols: List[Dict[str, Any]], cleanup_mode: str = "safe") -> Dict[str, List[Dict[str, Any]]]:
        result = {"safe": [], "suppress": [], "unsure": []}
        for sym in symbols:
            if sym.get("used", True):
                continue
            kind = sym.get("kind", "")
            reasons = sym.get("reasons", [])
            if any("public" in r.lower() or "export" in r.lower() for r in reasons):
                category = "unsure"
            elif kind in ("import", "module"):
                category = "safe"
            elif "side_effect" in reasons or "unsafe_remove" in reasons:
                category = "suppress" if cleanup_mode != "aggressive" else "safe"
            else:
                category = "safe" if cleanup_mode in ("smart", "aggressive") else "suppress"
            result[category].append(sym)
        return result

    def _analyze_rust(self) -> List[Dict[str, Any]]:
        symbols = []
        try:
            result = subprocess.run(
                ["cargo", "check", "--message-format=json"],
                cwd=str(self.project_path), capture_output=True, text=True, timeout=300,
            )
            for line in result.stdout.splitlines():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("reason") != "compiler-message":
                    continue
                msg_data = msg.get("message", {})
                code = msg_data.get("code")
                if isinstance(code, dict):
                    code = code.get("code", "")
                if code in ("unused_variable", "unused_import", "unused_mut",
                           "unused_function", "dead_code", "unused_assignments"):
                    spans = msg_data.get("spans", [])
                    if spans:
                        span = spans[0]
                        name = self._extract_symbol_name(msg_data.get("message", ""), code)
                        if name:
                            symbols.append({
                                "name": name, "kind": self._map_rust_code_to_kind(code),
                                "defined_at": {"file": span.get("file_name", ""),
                                               "line": span.get("line_start", 0),
                                               "column": span.get("column_start", 0)},
                                "used": False, "usage_count": 0,
                                "safe_to_remove": code != "dead_code",
                                "reasons": [f"rustc_warning: {code}"],
                                "reachable_from_pub": code == "dead_code",
                            })
        except Exception as e:
            logger.warning(f"Rust symbol analysis failed: {e}")
        return symbols

    @staticmethod
    def _extract_symbol_name(message: str, code: str) -> str:
        match = re.search(r"`([^`]+)`", message)
        if match:
            return match.group(1)
        if code == "dead_code":
            m = re.search(r"function\s+`([^`]+)`", message)
            if m:
                return m.group(1)
        return "unknown"

    @staticmethod
    def _map_rust_code_to_kind(code: str) -> str:
        return {"unused_variable": "variable", "unused_mut": "variable",
                "unused_import": "import", "unused_function": "function",
                "dead_code": "function", "unused_assignments": "variable"}.get(code, "unknown")

    def _analyze_python(self) -> List[Dict[str, Any]]:
        symbols = []
        for py_file in self.project_path.rglob("*.py"):
            try:
                with open(py_file, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=str(py_file))
            except Exception:
                continue
            defined: Set[str] = set()
            used: Set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    defined.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    defined.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    defined.add(node.id)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    used.add(node.id)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    used.add(node.func.id)
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    used.add(node.value.id)
            for name in defined:
                is_used = name in used
                kind = "variable"
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == name:
                        kind = "function"; break
                    elif isinstance(node, ast.ClassDef) and node.name == name:
                        kind = "class"; break
                symbols.append({
                    "name": name, "kind": kind,
                    "defined_at": {"file": str(py_file), "line": 0, "column": 0},
                    "used": is_used, "usage_count": 1 if is_used else 0,
                    "safe_to_remove": not is_used and kind == "variable",
                    "reasons": [], "reachable_from_pub": False,
                })
        return symbols

    def _analyze_javascript(self) -> List[Dict[str, Any]]:
        symbols = []
        try:
            result = subprocess.run(
                ["npx", "eslint", ".", "--ext", ".js,.ts", "--format=json"],
                cwd=str(self.project_path), capture_output=True, text=True, timeout=120,
            )
            data = json.loads(result.stdout) if result.stdout else []
            for file_info in data:
                file_path = file_info.get("filePath", "")
                for msg in file_info.get("messages", []):
                    rule_id = msg.get("ruleId", "")
                    if rule_id in ("no-unused-vars", "@typescript-eslint/no-unused-vars"):
                        name = self._extract_js_symbol_name(msg.get("message", ""))
                        symbols.append({
                            "name": name or "unknown", "kind": "variable",
                            "defined_at": {"file": file_path,
                                           "line": msg.get("line", 0),
                                           "column": msg.get("column", 0)},
                            "used": False, "usage_count": 0, "safe_to_remove": True,
                            "reasons": ["eslint: no-unused-vars"],
                            "reachable_from_pub": False,
                        })
        except FileNotFoundError:
            logger.warning("ESLint не найден – пропускаем JS/TS анализ")
        except Exception as e:
            logger.warning(f"JS/TS symbol analysis failed: {e}")
        return symbols

    @staticmethod
    def _extract_js_symbol_name(message: str) -> str:
        match = re.search(r"'(.*?)'", message)
        return match.group(1) if match else "unknown"

    # =================================================================
    # Case-file поддержка: ищем определения символов из ошибки.
    # =================================================================

    _BUILTIN_NAMES = frozenset({
        "let", "mut", "fn", "use", "pub", "struct", "enum", "trait", "impl",
        "match", "if", "else", "loop", "while", "for", "return", "break",
        "continue", "as", "in", "ref", "Self", "self", "true", "false",
        "Some", "None", "Ok", "Err", "String", "Vec", "Box", "Result",
        "Option", "i32", "u32", "i64", "u64", "f32", "f64", "bool", "str",
        "def", "class", "import", "from", "True", "False", "None",
        "lambda", "yield", "raise", "try", "except", "finally", "with",
        "function", "const", "let", "var", "extends", "implements", "interface",
        "type", "export", "default", "new", "this", "super",
    })

    _SYMBOL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

    def find_related_definitions(self, error: Dict[str, Any],
                                 working_path: Optional[Path] = None,
                                 max_symbols: int = 5) -> str:
        root = Path(working_path) if working_path else self.project_path
        symbols = self._extract_mentioned_symbols(error)
        if not symbols:
            return ""
        target_file = (error or {}).get("file") or ""
        parts: List[str] = []
        seen: Set[str] = set()
        for sym in symbols:
            if sym in seen or sym in self._BUILTIN_NAMES:
                continue
            seen.add(sym)
            if len(parts) >= max_symbols:
                break
            block = self._find_definition_block(sym, root, exclude_file=target_file)
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    @classmethod
    def _extract_mentioned_symbols(cls, error: Dict[str, Any]) -> List[str]:
        message = (error or {}).get("message") or ""
        if not message:
            return []
        names: List[str] = []
        for m in re.finditer(r"[`'\"]([A-Za-z_][A-Za-z_0-9]*)[`'\"]", message):
            names.append(m.group(1))
        for kw in ("struct", "enum", "trait", "fn", "type", "field", "method", "class", "module"):
            for m in re.finditer(rf"\b{kw}\s+`?([A-Za-z_][A-Za-z_0-9]*)`?", message):
                names.append(m.group(1))
        for m in cls._SYMBOL_NAME_RE.finditer(message):
            tok = m.group(0)
            if len(tok) >= 3 and (tok[0].isupper() or "_" in tok or len(tok) >= 5):
                names.append(tok)
        out: List[str] = []
        seen: Set[str] = set()
        for n in names:
            if n in seen or n in cls._BUILTIN_NAMES:
                continue
            seen.add(n)
            out.append(n)
        return out

    def _find_definition_block(self, symbol: str, root: Path,
                               exclude_file: str = "", max_lines: int = 25) -> str:
        """Ищет определение символа. Сначала смотрит в файлах ОТЛИЧНЫХ от
        target_file (соседи); если нигде не нашлось — fallback на target_file
        (`same file` маркер)."""
        if self.language in ("rust", "rs"):
            patterns = [rf"^(?:pub(?:\([^)]+\))?\s+)?(?:struct|enum|trait|union|type|fn|const|static)\s+{re.escape(symbol)}\b"]
            exts = (".rs",)
        elif self.language in ("python", "py"):
            patterns = [rf"^(?:async\s+)?def\s+{re.escape(symbol)}\b",
                        rf"^class\s+{re.escape(symbol)}\b"]
            exts = (".py",)
        elif self.language in ("javascript", "typescript", "js", "ts"):
            patterns = [
                rf"^(?:export\s+)?(?:async\s+)?function\s+{re.escape(symbol)}\b",
                rf"^(?:export\s+)?class\s+{re.escape(symbol)}\b",
                rf"^(?:export\s+)?(?:const|let|var)\s+{re.escape(symbol)}\s*=",
                rf"^(?:export\s+)?interface\s+{re.escape(symbol)}\b",
                rf"^(?:export\s+)?type\s+{re.escape(symbol)}\b",
            ]
            exts = (".js", ".ts", ".jsx", ".tsx")
        else:
            return ""

        compiled = [re.compile(p, re.MULTILINE) for p in patterns]
        exclude_resolved = ""
        if exclude_file:
            try:
                exclude_resolved = str((root / exclude_file).resolve())
            except Exception:
                exclude_resolved = exclude_file

        fallback_match = None  # (path, match, text) — найден в target_file

        for path in self._iter_source_files(root, exts):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for rx in compiled:
                m = rx.search(text)
                if not m:
                    continue
                same_file = bool(exclude_resolved and
                                 str(path.resolve()) == exclude_resolved)
                if same_file:
                    if fallback_match is None:
                        fallback_match = (path, m, text)
                    break
                start = m.start()
                line_no = text.count("\n", 0, start) + 1
                snippet = self._extract_snippet(text, start, max_lines=max_lines)
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    rel = path
                return f"### {symbol} ({rel}:{line_no})\n{snippet}"

        if fallback_match is not None:
            path, m, text = fallback_match
            start = m.start()
            line_no = text.count("\n", 0, start) + 1
            snippet = self._extract_snippet(text, start, max_lines=max_lines)
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path
            return f"### {symbol} ({rel}:{line_no}, same file)\n{snippet}"
        return ""

    @staticmethod
    def _iter_source_files(root: Path, exts: Tuple[str, ...]):
        skip = {"target", "node_modules", ".git", ".webles_sandbox",
                ".webbles_fix", "venv", ".venv", "__pycache__", "dist", "build"}
        if not root.exists():
            return
        for entry_root, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in files:
                if name.endswith(exts):
                    yield Path(entry_root) / name

    @staticmethod
    def _extract_snippet(text: str, start: int, max_lines: int = 25) -> str:
        lines = text[start:].splitlines()
        if not lines:
            return ""
        out: List[str] = []
        depth = 0
        opened = False
        for ln in lines[:max_lines]:
            out.append(ln)
            depth += ln.count("{") + ln.count("(")
            depth -= ln.count("}") + ln.count(")")
            if "{" in ln:
                opened = True
            if opened and depth <= 0:
                break
        return "\n".join(out)
