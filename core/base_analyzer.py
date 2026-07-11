"""
Базовый анализатор для webles_conveyor.
Гарантирует, что все ошибки, выходящие из анализатора, содержат ключи:
    - error_type   (str)
    - confidence   (float)
    - base_weight  (float)
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List

from analysis.error_classifier import ErrorClassifier


class BaseAnalyzer(ABC):
    """Абстрактный анализатор, нормализующий выдачу."""

    def __init__(self, language: str):
        self.language = language
        self.classifier = ErrorClassifier()

    @abstractmethod
    def _raw_analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        """
        Сырой метод анализа.
        Возвращает список ошибок, каждая из которых МОЖЕТ уже содержать
        'error_type', 'confidence', 'base_weight', но не обязана.
        """
        ...

    def analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        """
        Нормализованный публичный интерфейс.
        Гарантирует наличие 'error_type', 'confidence' и 'base_weight'.
        """
        errors = self._raw_analyze(project_path)
        for err in errors:
            self._ensure_fields(err)
        return errors

    def _ensure_fields(self, error: Dict[str, Any]) -> None:
        """Заполняет обязательные поля, если их нет."""
        # Классификация
        if "error_type" not in error or "confidence" not in error:
            classification = self.classifier.classify(error)
            if "error_type" not in error:
                error["error_type"] = classification.get("class", "UNKNOWN")
            if "confidence" not in error:
                error["confidence"] = classification.get("confidence", 0.5)

        # Вес на основе severity
        if "base_weight" not in error:
            sev = error.get("severity", "warning").lower()
            weight_map = {
                "critical": 100.0,
                "error": 10.0,
                "warning": 1.0,
                "info": 0.0,
            }
            error["base_weight"] = weight_map.get(sev, 1.0)