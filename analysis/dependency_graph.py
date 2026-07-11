"""
Граф зависимостей файлов для webles_conveyor.
Строит граф импортов/модулей для Python, Rust и JS/TS.
"""

import ast
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional


class DependencyGraph:
    """Строит ориентированный граф зависимостей файлов в проекте."""

    def __init__(self, project_path: Path, language: str):
        self.project_path = project_path
        self.language = language.lower()
        self.graph: Dict[str, List[str]] = defaultdict(list)
        self.reverse: Dict[str, List[str]] = defaultdict(list)
        self._build()

    def rebuild(self, project_path: Optional[Path] = None) -> None:
        """2026-06-25: граф раньше строился ОДИН раз при создании движка
        (от project_path — снимок ДО первого патча) и никогда не
        обновлялся, хотя реальные правки идут в working_path (отдельная
        песочница) — RootCauseStage.cascade_score со временем считался по
        всё более устаревшей структуре импортов. Пересобирает граф с нуля
        (опционально — от нового пути, если передан working_path вместо
        исходного project_path); вызывающий код сам решает периодичность
        (раз в макро-цикл, не на каждый отдельный выбор ошибки — это было
        бы расточительно)."""
        if project_path is not None:
            self.project_path = project_path
        self.graph = defaultdict(list)
        self.reverse = defaultdict(list)
        self._build()

    def _build(self) -> None:
        if self.language == "python":
            self._build_python()
        elif self.language == "rust":
            self._build_rust()
        elif self.language in ("javascript", "typescript"):
            self._build_js()

    def _add_dep(self, source: str, target: str) -> None:
        self.graph[source].append(target)
        self.reverse[target].append(source)

    def _build_python(self) -> None:
        for py_file in self.project_path.rglob("*.py"):
            rel_path = str(py_file.relative_to(self.project_path))
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = alias.name.split('.')[0]
                        target = self._module_to_file(mod)
                        if target:
                            self._add_dep(rel_path, target)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        mod = node.module.split('.')[0]
                        target = self._module_to_file(mod)
                        if target:
                            self._add_dep(rel_path, target)

    def _module_to_file(self, module: str) -> Optional[str]:
        candidates = [f"{module}.py", f"{module}/__init__.py"]
        for cand in candidates:
            if (self.project_path / cand).exists():
                return cand
        return None

    def _build_rust(self) -> None:
        for rs_file in self.project_path.rglob("*.rs"):
            rel_path = str(rs_file.relative_to(self.project_path))
            try:
                content = rs_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for m in re.finditer(r"^\s*mod\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*;", content, re.MULTILINE):
                mod_name = m.group(1)
                parent_dir = rs_file.parent
                for cand in [f"{mod_name}.rs", f"{mod_name}/mod.rs"]:
                    cand_path = parent_dir / cand
                    if cand_path.exists():
                        cand_rel = str(cand_path.relative_to(self.project_path))
                        self._add_dep(rel_path, cand_rel)
                        break

    def _build_js(self) -> None:
        pattern = re.compile(r'(?:import\s+.*?\s+from\s+["\']([^"\']+)["\']|require\(["\']([^"\']+)["\']\))')
        for ext in ("*.js", "*.ts"):
            for js_file in self.project_path.rglob(ext):
                rel_path = str(js_file.relative_to(self.project_path))
                try:
                    content = js_file.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for m in pattern.finditer(content):
                    dep = m.group(1) or m.group(2)
                    if dep.startswith('.'):
                        resolved = self._resolve_relative_js(rel_path, dep)
                        if resolved:
                            self._add_dep(rel_path, resolved)

    def _resolve_relative_js(self, from_file: str, import_path: str) -> Optional[str]:
        from_dir = Path(from_file).parent
        clean_path = import_path.split('?')[0].split('#')[0]
        target_base = from_dir / clean_path
        for ext in ['', '.js', '.ts', '/index.js', '/index.ts']:
            candidate = Path(str(target_base) + ext)
            try:
                candidate = candidate.resolve().relative_to(self.project_path)
            except (ValueError, FileNotFoundError):
                continue
            if (self.project_path / candidate).exists():
                return str(candidate)
        return None

    def get_dependents(self, file_path: str) -> List[str]:
        return self.reverse.get(file_path, [])

    def get_dependency_chain_score(self, file_path: str) -> int:
        visited = set()
        stack = [file_path]
        while stack:
            f = stack.pop()
            if f in visited:
                continue
            visited.add(f)
            stack.extend(self.reverse.get(f, []))
        return len(visited)