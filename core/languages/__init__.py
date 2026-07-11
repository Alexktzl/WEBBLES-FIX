"""
Пакет языковых провайдеров для Webbles Fix.
При импорте автоматически сканирует папку и регистрирует все поддерживаемые языки.
"""

import importlib
import importlib.util
import logging
from pathlib import Path

from core.language_support import register_language, LanguageSupport

logger = logging.getLogger(__name__)

# Папка, в которой лежат провайдеры
_LANGUAGES_DIR = Path(__file__).parent


def _discover_and_register():
    """Сканирует текущую папку и регистрирует все найденные провайдеры."""
    registered = 0
    for file_path in _LANGUAGES_DIR.glob("*_support.py"):
        module_name = file_path.stem  # например, rust_support
        try:
            # Загружаем модуль
            spec = importlib.util.spec_from_file_location(
                f"core.languages.{module_name}", str(file_path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Ищем класс, наследующий LanguageSupport
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

            # Регистрируем
            register_language(language, provider_class)
            registered += 1
            logger.info(f"Автоматически зарегистрирован провайдер: {language} ({provider_class.__name__})")

        except Exception as e:
            logger.error(f"Ошибка загрузки провайдера из {file_path}: {e}")

    logger.info(f"Всего зарегистрировано языков: {registered}")


# Запускаем сканирование при импорте пакета
_discover_and_register()