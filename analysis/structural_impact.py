"""
Структурный анализ изменений для webles_conveyor.
Оценивает архитектурные последствия патча: радиус поражения, глубину зависимостей, критичность файлов.
"""

import math
from pathlib import Path
from typing import Dict, List, Optional, Set

from analysis.dependency_graph import DependencyGraph


class StructuralImpact:
    """
    Анализирует влияние патча на структуру проекта.
    """

    # Файлы, изменение которых особенно чувствительно
    CRITICAL_FILES = {
        "Cargo.toml", "Cargo.lock",
        "package.json", "package-lock.json",
        "pyproject.toml", "requirements.txt",
        "tsconfig.json",
        "main.rs", "lib.rs", "mod.rs",
        "__init__.py", "main.py",
        "index.js", "index.ts",
    }

    def __init__(
        self,
        project_path: Path,
        dep_graph: Optional[DependencyGraph] = None,
    ):
        self.project_path = project_path
        self.dep_graph = dep_graph

    def analyze(
        self,
        modified_files: List[str],
        patch_size: int = 0,
    ) -> Dict[str, float]:
        """
        Вычисляет метрики структурного влияния для набора изменённых файлов.

        Возвращает словарь с метриками:
            - blast_radius: количество затронутых зависимых модулей
            - depth_impact: максимальная глубина цепочки зависимостей
            - criticality_score: штраф за изменение чувствительных файлов
            - structural_penalty: комбинированный штраф для скоринга
        """
        result = {
            "blast_radius": 0.0,
            "depth_impact": 0.0,
            "criticality_score": 0.0,
            "structural_penalty": 0.0,
        }

        if not modified_files:
            return result

        affected_files: Set[str] = set(modified_files)
        if self.dep_graph:
            for f in modified_files:
                dependents = self.dep_graph.get_dependents(f)
                affected_files.update(dependents)

            result["blast_radius"] = len(affected_files) - len(modified_files)

            max_depth = 0
            for f in modified_files:
                depth = self._max_dependency_depth(f, set())
                max_depth = max(max_depth, depth)
            result["depth_impact"] = max_depth

        critical_count = sum(1 for f in modified_files if self._is_critical(f))
        result["criticality_score"] = critical_count * 0.5

        blast_penalty = math.log(result["blast_radius"] + 1) * 0.3
        depth_penalty = result["depth_impact"] * 0.2
        critical_penalty = result["criticality_score"]

        if critical_count > 0 and patch_size > 50:
            critical_penalty *= 1.5

        result["structural_penalty"] = blast_penalty + depth_penalty + critical_penalty

        return result

    def _is_critical(self, file_path: str) -> bool:
        """Проверяет, является ли файл критическим."""
        path = Path(file_path)
        if path.name in self.CRITICAL_FILES:
            return True
        parts = path.parts
        return any(p in {"config", "settings", "migrations", "auth"} for p in parts)

    def _max_dependency_depth(self, file_path: str, visited: Set[str]) -> int:
        """Вычисляет максимальную глубину цепочки зависимостей от файла."""
        if file_path in visited:
            return 0
        visited.add(file_path)

        if not self.dep_graph:
            return 0

        dependents = self.dep_graph.get_dependents(file_path)
        if not dependents:
            return 0

        max_child_depth = 0
        for dep in dependents:
            child_depth = self._max_dependency_depth(dep, visited.copy())
            max_child_depth = max(max_child_depth, child_depth)

        return 1 + max_child_depth