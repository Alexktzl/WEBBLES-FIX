"""
Модуль поддержки языков для Webbles Fix.
Предоставляет абстрактный базовый класс LanguageSupport и реестр языков,
чтобы добавление нового языка сводилось к реализации одного класса.
Теперь с автоматическим сканированием папки languages.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
import importlib.util

logger = logging.getLogger(__name__)


class LanguageSupport(ABC):
    """
    Абстрактный интерфейс для языковой поддержки.
    Каждый конкретный язык (Rust, Python, JS и т.д.) реализует этот класс,
    инкапсулируя все языко-зависимые компоненты.
    """

    def __init__(self, language: str):
        self.language = language

    # ------------------------------------------------------------------
    # Фабрики компонентов
    # ------------------------------------------------------------------

    @abstractmethod
    def create_analyzer(self):
        """Возвращает экземпляр анализатора ошибок для данного языка."""

    @abstractmethod
    def create_syntax_healer(self):
        """
        Возвращает экземпляр CriticalSyntaxHealer (или его аналог),
        способный исправлять критические синтаксические ошибки языка.
        """

    @abstractmethod
    def create_segmenter(self):
        """
        Возвращает экземпляр FileSegmenter, настроенный на язык.
        """

    # ------------------------------------------------------------------
    # Данные для LLM
    # ------------------------------------------------------------------

    @abstractmethod
    def get_error_examples(self) -> Dict[str, str]:
        """
        Возвращает словарь: код_ошибки -> пример исправления (строка с unified diff).
        Примеры добавляются в промпт LLM для повышения качества патчей.
        """

    def get_prompt_enhancements(self, error: Dict[str, Any]) -> str:
        """
        Возвращает дополнительный текст, который будет добавлен в промпт
        для специфичных ситуаций (например, хинт про E0765 в Rust).
        По умолчанию пустая строка.
        """
        return ""

    # ------------------------------------------------------------------
    # Классификация ошибок и веса (мультиязычное расширение)
    # ------------------------------------------------------------------

    def get_classification_rules(self) -> Dict[str, Dict[str, Any]]:
        """
        Правила классификации ошибок: код -> {"class": "...", "allowed_actions": [...]}.
        Если возвращает пустой словарь, ErrorClassifier использует универсальные эвристики.
        """
        return {}

    def get_class_weight(self, error_class: str) -> Optional[float]:
        """
        Возвращает числовой вес класса ошибки для расчёта здоровья проекта.
        Если возвращает None, используется стандартный вес из ErrorClassifier.
        """
        return None

    # ------------------------------------------------------------------
    # Сигнатуры и аудит
    # ------------------------------------------------------------------

    def get_initial_signatures_provider(self):
        """
        Возвращает callable (project_path, context) -> Dict[str, set],
        который собирает исходные сигнатуры ошибок до патчей.
        Если не переопределён, возвращает None (аудит по весам).
        """
        return None

    # ------------------------------------------------------------------
    # Возможности языка (для условной логики в ядре)
    # ------------------------------------------------------------------

    def supports_feature(self, feature: str) -> bool:
        """
        Проверяет, поддерживает ли язык определённую возможность.
        Возможные ключи: 'tree_sitter', 'cargo_clean', 'audit_crate', ...
        """
        return False

    # ------------------------------------------------------------------
    # Конфигурация по умолчанию
    # ------------------------------------------------------------------

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        """Возвращает словарь с рекомендуемыми параметрами конвейера для языка."""
        return {}


# =============================================================================
# Глобальный реестр языков (с автоматической регистрацией)
# =============================================================================

LANGUAGE_REGISTRY: Dict[str, Type[LanguageSupport]] = {}

def register_language(lang_id: str, provider_class: Type[LanguageSupport]) -> None:
    """Регистрирует новый языковой провайдер."""
    LANGUAGE_REGISTRY[lang_id.lower()] = provider_class
    logger.info(f"Языковой провайдер зарегистрирован: {lang_id}")


def get_language_provider(lang_id: str) -> Optional[LanguageSupport]:
    """
    Возвращает экземпляр провайдера для заданного языка.
    Если язык не зарегистрирован, возвращает None.
    """
    lang_id = lang_id.lower()
    provider_class = LANGUAGE_REGISTRY.get(lang_id)
    if provider_class is None:
        # Попытка нечёткого сопоставления
        for key in LANGUAGE_REGISTRY:
            if key in lang_id or lang_id in key:
                provider_class = LANGUAGE_REGISTRY[key]
                break
    if provider_class:
        return provider_class(lang_id)
    return None


def _discover_and_register():
    """Сканирует папку core/languages и регистрирует все найденные провайдеры."""
    _LANGUAGES_DIR = Path(__file__).parent / "languages"
    if not _LANGUAGES_DIR.exists():
        logger.warning(f"Папка {_LANGUAGES_DIR} не найдена")
        return
    
    registered = 0
    for file_path in _LANGUAGES_DIR.glob("*_support.py"):
        module_name = file_path.stem
        try:
            spec = importlib.util.spec_from_file_location(
                f"core.languages.{module_name}", str(file_path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            provider_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, LanguageSupport) and attr is not LanguageSupport:
                    provider_class = attr
                    break

            if provider_class is None:
                logger.debug(f"В модуле {file_path} не найден класс LanguageSupport, пропускаем")
                continue

            # Создаём экземпляр, чтобы узнать язык
            instance = provider_class()
            language = instance.language

            register_language(language, provider_class)
            registered += 1
            logger.info(f"Автоматически зарегистрирован провайдер: {language} ({provider_class.__name__})")

        except Exception as e:
            logger.error(f"Ошибка загрузки провайдера из {file_path}: {e}")
    
    logger.info(f"Всего зарегистрировано языков: {registered}")


# Запускаем автоматическую регистрацию при импорте модуля
_discover_and_register()