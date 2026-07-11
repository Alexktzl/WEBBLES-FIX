"""
Кластеризация ошибок для webles_conveyor.
Группирует ошибки по типу, файлу и каскадным связям.
"""

from collections import defaultdict
from typing import Any, Dict, List, Set

from analysis.error_classifier import ErrorClassifier
from analysis.error_intelligence.error_signature import ErrorSignature


class ErrorClusterer:
    """
    Группирует ошибки в кластеры на основе типа, файла и цепочек зависимостей.
    """

    def __init__(self, classifier: ErrorClassifier):
        self.classifier = classifier

    def cluster_by_type(self, errors: List[Dict[str, Any]]) -> Dict[str, List[ErrorSignature]]:
        """Группирует ошибки по классифицированному типу."""
        clusters: Dict[str, List[ErrorSignature]] = defaultdict(list)
        for err in errors:
            sig = ErrorSignature.from_error_dict(err)
            classification = self.classifier.classify(err)
            t = classification.get("class", "UNKNOWN")
            sig.error_type = t
            clusters[t].append(sig)
        return dict(clusters)

    def cluster_by_file(self, errors: List[Dict[str, Any]]) -> Dict[str, List[ErrorSignature]]:
        """Группирует ошибки по файлу, в котором они возникли."""
        clusters: Dict[str, List[ErrorSignature]] = defaultdict(list)
        for err in errors:
            sig = ErrorSignature.from_error_dict(err)
            clusters[sig.file].append(sig)
        return dict(clusters)

    def find_cascade_clusters(
        self,
        errors: List[Dict[str, Any]],
        dep_graph: Any = None
    ) -> List[List[ErrorSignature]]:
        """
        Определяет потенциальные каскадные кластеры, в которых одна корневая ошибка вызывает другие.
        Использует близость файлов и (опционально) граф зависимостей.
        """
        by_file: Dict[str, List[ErrorSignature]] = defaultdict(list)
        for err in errors:
            sig = ErrorSignature.from_error_dict(err)
            by_file[sig.file].append(sig)

        clusters: List[List[ErrorSignature]] = []
        visited: Set[str] = set()

        # Простой каскад: ошибки в одном файле, вероятно, связаны
        for file, sigs in by_file.items():
            if len(sigs) > 1:
                clusters.append(sigs)
                visited.update(s.to_key() for s in sigs)

        # Если доступен граф зависимостей, связываем ошибки между зависимыми файлами
        if dep_graph:
            for file, sigs in by_file.items():
                dependents = dep_graph.get_dependents(file)
                for dep in dependents:
                    if dep in by_file:
                        cluster = sigs + by_file[dep]
                        clusters.append(cluster)
                        visited.update(s.to_key() for s in cluster)

        # Оставшиеся некластеризованные ошибки добавляем как синглтоны
        for err in errors:
            sig = ErrorSignature.from_error_dict(err)
            if sig.to_key() not in visited:
                clusters.append([sig])

        return clusters

    def cluster_by_priority(
        self,
        errors: List[Dict[str, Any]],
        top_n: int = 5
    ) -> List[List[ErrorSignature]]:
        """
        Возвращает кластеры, отсортированные по суммарному весу, от наиболее приоритетных.
        """
        by_file = self.cluster_by_file(errors)
        file_weights = {}
        for file, sigs in by_file.items():
            total = 0
            for sig in sigs:
                for err in errors:
                    if ErrorSignature.from_error_dict(err).to_key() == sig.to_key():
                        total += self.classifier.get_weight(err)
                        break
            file_weights[file] = total

        sorted_files = sorted(file_weights.keys(), key=lambda f: file_weights[f], reverse=True)
        return [by_file[f] for f in sorted_files[:top_n]]