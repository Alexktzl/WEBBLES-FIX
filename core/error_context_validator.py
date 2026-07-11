"""
Валидатор соответствия ошибки реальному контексту файла.
Отсекает ошибки, для которых указанный файл не существует.
Теперь не мешает чинить обычные ошибки, пропускает все, кроме некорректных путей.
"""

import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class ErrorContextValidator:
    """
    Проверяет, соответствует ли контекст ошибки фактическому содержимому проекта.
    """

    @staticmethod
    def is_valid(error: Dict[str, Any], project_path: Path) -> bool:
        file_name = error.get("file", "")
        error_type = error.get("error_type", "")

        # Файл должен быть указан
        if not file_name:
            logger.debug("Ошибка без указания файла – пропускаем")
            return False

        file_path = project_path / file_name
        if not file_path.exists():
            logger.debug(f"Файл {file_name} не найден – пропускаем")
            return False

        # Для Cargo.toml ВСЕГДА пропускаем security/dependency
        if file_name.lower() == "cargo.toml" and error_type in ("security", "dependency"):
            logger.debug(f"Security/dependency ошибка в Cargo.toml – разрешаем генерацию патча")
            return True

        # Для всех остальных ошибок (обычный код) – просто разрешаем,
        # потому что файл существует и это не Cargo-специфика.
        return True