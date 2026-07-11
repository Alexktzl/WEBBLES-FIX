"""
Классификатор ошибок с правилами маршрутизации.
Каждая ошибка получает класс (BLOCKING / STRUCTURAL / CLEANUP / WARNING / CRITICAL_SYNTAX / BUILD_SCRIPT / MANIFEST)
и рекомендации по режиму исправления.
Полностью делегирует классификацию провайдеру языка (LanguageSupport),
используя универсальные эвристики только при отсутствии провайдера.
"""

import logging
from typing import Any, Dict, List, Optional

from core.language_support import LanguageSupport

logger = logging.getLogger(__name__)


class ErrorClassifier:
    """Классифицирует ошибки и определяет стратегию их исправления."""

    def __init__(self, language_provider: Optional[LanguageSupport] = None):
        self.provider = language_provider

    def classify(self, error: Dict[str, Any]) -> Dict[str, Any]:
        """
        Возвращает класс ошибки, допустимые действия и уверенность.
        Использует провайдер языка, если он есть, иначе универсальные эвристики.
        """
        # 0. Проверка специальных типов файлов
        special = self._classify_by_filename(error.get("file", ""))
        if special:
            return {"class": special, "allowed_actions": [], "confidence": 1.0}

        code = error.get("code", "")
        level = error.get("level", "").lower()
        message = error.get("message", "").lower()

        # ---------- 1. Явные предупреждения ----------
        if level == "warning":
            return {"class": "CLEANUP", "allowed_actions": ["suppress", "remove"], "confidence": 0.9}

        # ---------- 2. Ключевые слова, указывающие на мёртвый код ----------
        cleanup_keywords = ["unused", "dead code", "never used",
                            "unused import", "unused variable",
                            "unused function", "function is never used"]
        if any(kw in message for kw in cleanup_keywords):
            return {"class": "CLEANUP", "allowed_actions": ["suppress", "remove"], "confidence": 0.8}

        # ---------- 3. Явные clippy-линты ----------
        if code and code.startswith("clippy::"):
            return {"class": "CLEANUP", "allowed_actions": ["suppress", "remove"], "confidence": 0.9}

        # ---------- 4. Точные коды через провайдер (основной путь) ----------
        if self.provider:
            rules = self.provider.get_classification_rules()
            entry = rules.get(code)
            if entry:
                result = dict(entry)
                if "confidence" not in result:
                    result["confidence"] = 0.9
                logger.debug(f"Классификация через провайдер: {code} -> {result['class']}")
                return result

        # ---------- 5. Универсальный критический синтаксис ----------
        if "prefix" in message and "unknown" in message:
            return {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"], "confidence": 1.0}

        critical_keywords = [
            "unclosed", "unterminated", "double quote",
            "expected ';'", "expected token",
            "expected one of", "expected '('", "expected ')'", "expected '{'",
            "expected '}'", "expected expression", "could not parse",
            "unexpected token", "unexpected end of file",
            "missing `fn`", "missing `struct`",
        ]
        if any(kw in message for kw in critical_keywords):
            return {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"], "confidence": 1.0}

        # ---------- 6. Критические ошибки скобок и разделителей ----------
        bracket_keywords = [
            "unclosed delimiter",
            "unexpected closing delimiter",
        ]
        if any(kw in message for kw in bracket_keywords):
            return {"class": "CRITICAL_SYNTAX", "allowed_actions": ["fix_syntax_heuristic"], "confidence": 1.0}

        # ---------- 7. Semgrep security-правила: код содержит .security. или .audit. ----------
        code_lower = code.lower()
        if ".security." in code_lower or ".audit." in code_lower or "security.audit" in code_lower:
            return {"class": "SECURITY", "allowed_actions": ["edit_line"], "confidence": 0.8}

        # ---------- 8. Всё остальное ----------
        return {"class": "UNKNOWN", "allowed_actions": [], "confidence": 0.5}

    @staticmethod
    def _classify_by_filename(file_name: str) -> Optional[str]:
        if not isinstance(file_name, str):
            return None
        base = file_name.replace('\\', '/').split('/')[-1]
        if base == "build.rs":
            return "BUILD_SCRIPT"
        if base == "Cargo.toml":
            return "MANIFEST"
        return None

    def get_weight(self, error: Dict[str, Any]) -> float:
        """Возвращает вес ошибки, делегируя провайдеру, если возможно."""
        error_class = error.get("error_class")
        if not error_class:
            error_class = self.classify(error).get("class", "UNKNOWN")
        if self.provider:
            weight = self.provider.get_class_weight(error_class)
            if weight is not None:
                return weight
        # Стандартные веса
        weights = {
            "CRITICAL_SYNTAX": 200.0,
            "BLOCKING": 100.0,
            # SECURITY: реальная уязвимость в компилирующемся коде. Ниже BLOCKING,
            # потому что код, который не собирается, надо чинить раньше (иначе
            # патч уязвимости нечем валидировать), но выше STRUCTURAL/тестов —
            # дыра опаснее структурного шума.
            "SECURITY": 90.0,
            "STRUCTURAL": 70.0,
            "TEST_FAILURE": 60.0,
            "SEMANTIC_MISMATCH": 55.0,
            "CLEANUP": 30.0,
            "WARNING": 10.0,
            "UNKNOWN": 50.0,
            "BUILD_SCRIPT": 150.0,
            "MANIFEST": 200.0,
        }
        return weights.get(error_class, 50.0)

    def total_weight(self, errors: List[Dict[str, Any]]) -> float:
        """Суммарный вес списка ошибок для сравнения здоровья проекта."""
        total = sum(self.get_weight(e) for e in errors)
        logger.info("Суммарный вес ошибок: %.2f (всего %d ошибок)", total, len(errors))
        return total
