"""
Анализ корневых причин ошибок для webles_conveyor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from pathlib import Path

from analysis.dependency_graph import DependencyGraph


@dataclass
class ErrorCluster:
    file: str
    errors: List[Dict[str, Any]] = field(default_factory=list)
    total_priority: float = 0.0
    cascade_score: float = 0.0
    max_priority: float = 0.0

    def add(self, error: Dict[str, Any], priority: float) -> None:
        self.errors.append(error)
        self.total_priority += priority
        if priority > self.max_priority:
            self.max_priority = priority


class RootCauseAnalyzer:
    PROTECTED_PATTERNS = [
        "Cargo.toml", "package.json", "requirements.txt",
        "migrations/", "auth/", "config/", ".env",
        "manage.py", "main.rs", "lib.rs"
    ]

    def __init__(self, dep_graph: Optional[DependencyGraph] = None):
        self.dep_graph = dep_graph

    def _normalize(self, path: str) -> str:
        return str(Path(path)).replace("\\", "/")

    def _is_protected(self, file_path: str) -> bool:
        norm_path = self._normalize(file_path)
        parts = norm_path.split('/')
        for pat in self.PROTECTED_PATTERNS:
            if pat.endswith('/'):
                # точное совпадение компонента пути
                if pat[:-1] in parts:
                    return True
            else:
                # точное совпадение имени файла или полного пути с границами
                if norm_path.endswith('/' + pat) or norm_path == pat:
                    return True
                if pat in parts:
                    # дополнительная проверка, что это целый компонент, а не подстрока
                    if any(p == pat for p in parts):
                        return True
        return False

    def select_root_cause(self, errors: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not errors:
            return None

        candidates = [
            e for e in errors
            if not self._is_protected(e.get("file", ""))
        ] or errors

        clusters: Dict[str, ErrorCluster] = {}

        for err in candidates:
            raw_file = err.get("file", "")
            if not raw_file:
                raw_file = "<unknown>"
            file = self._normalize(raw_file)
            priority = float(err.get("priority") or 1.0)

            if file not in clusters:
                clusters[file] = ErrorCluster(file=file)

            clusters[file].add(err, priority)

        if not clusters:
            return None

        if self.dep_graph is not None and hasattr(self.dep_graph, 'get_dependents'):
            deps_cache: Dict[str, List[str]] = {}
            for file in clusters:
                visited = set()
                stack = [file]
                affected_files = set()

                while stack:
                    current = self._normalize(stack.pop())

                    if current in visited:
                        continue
                    visited.add(current)

                    if current not in deps_cache:
                        deps = self.dep_graph.get_dependents(current)
                        deps_cache[current] = [self._normalize(d) for d in deps]
                    norm_deps = deps_cache[current]

                    for norm_dep in norm_deps:
                        if norm_dep in clusters and norm_dep != file:
                            affected_files.add(norm_dep)
                        if norm_dep not in visited:
                            stack.append(norm_dep)

                cascade = sum(clusters[f].total_priority for f in affected_files)
                clusters[file].cascade_score = cascade

        def cluster_key(c: ErrorCluster) -> tuple:
            # max_priority первичный: файл с E999(200) > файла с 10×E302(30 каждый)
            return (c.max_priority, c.total_priority + c.cascade_score, c.file)

        best_cluster = max(clusters.values(), key=cluster_key)

        return max(
            best_cluster.errors,
            key=lambda e: (float(e.get("priority") or 0), e.get("line", 0))
        )


__all__ = ["RootCauseAnalyzer", "ErrorCluster"]