"""
Скоринг патчей для webles_conveyor.
Оценивает качество патча до полной симуляции в песочнице.
Использует сходство с памятью, совпадение структуры AST, совместимость типа ошибки и исторический успех.
"""

import logging
from typing import Any, Dict, Optional

from memory.learning import MemoryLearning

logger = logging.getLogger(__name__)


class PatchScorer:
    """
    Вероятностный скорер для патчей.
    Комбинирует несколько эвристик для предсказания эффективности патча.
    """

    DEFAULT_WEIGHTS = {
        "memory_similarity": 0.35,
        "ast_match": 0.25,
        "error_type_match": 0.20,
        "historical_success": 0.20,
    }

    def __init__(
        self,
        memory: Optional[MemoryLearning] = None,
        weights: Optional[Dict[str, float]] = None,
    ):
        self.memory = memory
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        self._normalize_weights()

    def _normalize_weights(self) -> None:
        total = sum(self.weights.values())
        if total > 0:
            for k in self.weights:
                self.weights[k] /= total

    def score_patch(
        self,
        error: Dict[str, Any],
        patch: str,
        file_context: str,
        language: str,
    ) -> float:
        """
        Вычисляет уверенность для патча перед симуляцией.
        Возвращает значение в диапазоне [0, 1], где выше — лучше.
        """
        components = {}
        components["memory_similarity"] = self._memory_similarity(error, patch)
        components["ast_match"] = self._ast_match_score(error, patch, file_context, language)
        components["error_type_match"] = self._error_type_compatibility(error, patch)
        components["historical_success"] = self._historical_success_rate(error)

        total = sum(components[k] * self.weights[k] for k in components)
        return min(1.0, max(0.0, total))

    def _memory_similarity(self, error: Dict[str, Any], patch: str) -> float:
        """Сравнивает патч с ранее успешными исправлениями для похожих ошибок."""
        if not self.memory:
            return 0.5

        error_sig = self._error_signature(error)
        known_patch = self.memory.get_known_fix(error_sig)
        if known_patch is None:
            return 0.5

        if known_patch.strip() == patch.strip():
            return 1.0
        return 0.3

    def _ast_match_score(
        self, error: Dict[str, Any], patch: str, context: str, language: str
    ) -> float:
        """
        Эвристика: нацелен ли патч на ту же синтаксическую область, что и ошибка.
        """
        msg = error.get("message", "").lower()
        patch_lower = patch.lower()
        import re
        identifiers = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', msg))
        if not identifiers:
            return 0.5
        matches = sum(1 for ident in identifiers if ident in patch_lower)
        if matches > 0:
            return 0.7 + 0.2 * min(matches / len(identifiers), 1.0)
        return 0.3

    def _error_type_compatibility(self, error: Dict[str, Any], patch: str) -> float:
        """
        Проверяет, соответствует ли тип патча типу ошибки.
        """
        msg = error.get("message", "").lower()
        patch_lower = patch.lower()

        if "import" in msg or "unresolved" in msg:
            if "import" in patch_lower or "require" in patch_lower:
                return 1.0
            return 0.2
        if "undefined" in msg or "cannot find" in msg:
            if "def " in patch_lower or "fn " in patch_lower or "function" in patch_lower:
                return 0.9
        if "type" in msg and ("mismatch" in msg or "expected" in msg):
            if any(kw in patch_lower for kw in [":", "as ", "cast", "typeof"]):
                return 0.8

        return 0.5

    def _historical_success_rate(self, error: Dict[str, Any]) -> float:
        """Базовая частота успешных исправлений для данного типа ошибки (из памяти)."""
        if not self.memory:
            return 0.5
        error_sig = self._error_signature(error)
        if error_sig in self.memory.successful_fixes:
            entries = self.memory.successful_fixes[error_sig]
            if entries:
                avg_score = sum(e.get("score", 0.5) for e in entries) / len(entries)
                return min(1.0, avg_score)
        return 0.5

    @staticmethod
    def _error_signature(error: Dict[str, Any]) -> str:
        import re
        file = error.get("file", "")
        code = error.get("code") or error.get("error_code")
        if code:
            return f"{file}::{code}"
        msg = error.get("message", "")
        normalized = re.sub(r'\d+', '#', msg)
        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = ' '.join(normalized.split())
        return f"{file}::{normalized[:100]}"