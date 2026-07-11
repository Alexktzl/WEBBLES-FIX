"""
Статистика ошибок для webles_conveyor.
Предоставляет агрегированные метрики по наборам ошибок.
"""

from collections import Counter
from typing import Any, Dict, List

from analysis.error_classifier import ErrorClassifier
from analysis.error_intelligence.error_signature import ErrorSignature


class ErrorStats:
    """
    Агрегирует статистику по набору ошибок.
    """

    def __init__(self, classifier: ErrorClassifier):
        self.classifier = classifier

    def compute_stats(self, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Возвращает подробный словарь со статистикой.
        """
        if not errors:
            return {
                "total_count": 0,
                "total_weight": 0,
                "type_distribution": {},
                "severity_distribution": {},
                "file_distribution": {},
                "avg_weight": 0.0,
                "max_weight": 0,
                "min_weight": 0,
            }

        total_weight = 0
        type_counts: Dict[str, int] = Counter()
        severity_counts: Dict[str, int] = Counter()
        file_counts: Dict[str, int] = Counter()
        weights = []

        for err in errors:
            w = self.classifier.get_weight(err)
            weights.append(w)
            total_weight += w

            classification = self.classifier.classify(err)
            t = classification.get("class", "UNKNOWN")
            type_counts[t] += 1

            sev = err.get("severity", "unknown")
            severity_counts[sev] += 1

            sig = ErrorSignature.from_error_dict(err)
            file_counts[sig.file] += 1

        return {
            "total_count": len(errors),
            "total_weight": total_weight,
            "type_distribution": dict(type_counts),
            "severity_distribution": dict(severity_counts),
            "file_distribution": dict(file_counts),
            "avg_weight": total_weight / len(errors) if errors else 0.0,
            "max_weight": max(weights) if weights else 0,
            "min_weight": min(weights) if weights else 0,
        }

    def compare_stats(self, before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Сравнивает статистику двух наборов ошибок.
        """
        before_stats = self.compute_stats(before)
        after_stats = self.compute_stats(after)

        return {
            "count_delta": after_stats["total_count"] - before_stats["total_count"],
            "weight_delta": after_stats["total_weight"] - before_stats["total_weight"],
            "type_delta": self._dict_delta(
                before_stats["type_distribution"], after_stats["type_distribution"]
            ),
            "severity_delta": self._dict_delta(
                before_stats["severity_distribution"], after_stats["severity_distribution"]
            ),
            "file_delta": self._dict_delta(
                before_stats["file_distribution"], after_stats["file_distribution"]
            ),
        }

    @staticmethod
    def _dict_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
        delta = {}
        all_keys = set(before.keys()) | set(after.keys())
        for k in all_keys:
            delta[k] = after.get(k, 0) - before.get(k, 0)
        return delta