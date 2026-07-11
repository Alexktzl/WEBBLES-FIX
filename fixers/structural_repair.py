"""
Структурный восстановитель кода для webles_conveyor.
Обнаруживает и исправляет незакрытые блоки (отсутствующие '}') в файлах.
Поддерживает Rust, Python, JavaScript/TypeScript.
Все ключевые операции покрыты DEBUG-логами.
"""

import logging
from pathlib import Path
from typing import List, Tuple

from fixers.brace_utils import count_braces_safe

logger = logging.getLogger(__name__)


class StructuralRepair:
    """
    Находит и добавляет недостающие закрывающие скобки в исходных файлах.
    """

    @staticmethod
    def repair_rust(content: str) -> str:
        """Восстанавливает баланс фигурных скобок в коде Rust."""
        logger.debug("repair_rust: запуск, длина контента=%d", len(content))
        return StructuralRepair._repair_braces(content, '{', '}')

    @staticmethod
    def repair_python(content: str) -> str:
        """В Python скобки используются реже, но метод оставлен для совместимости."""
        logger.debug("repair_python: заглушка, возврат без изменений")
        return content

    @staticmethod
    def repair_javascript(content: str) -> str:
        """Восстанавливает баланс фигурных скобок в JavaScript/TypeScript."""
        logger.debug("repair_javascript: запуск, длина контента=%d", len(content))
        return StructuralRepair._repair_braces(content, '{', '}')

    @staticmethod
    def _repair_braces(content: str, open_brace: str, close_brace: str) -> str:
        """
        Универсальный метод для языков с парными скобками.
        Если количество открывающих скобок больше закрывающих,
        добавляет недостающие закрывающие скобки в конец файла.
        """
        logger.debug("_repair_braces: open=%r, close=%r", open_brace, close_brace)
        
        # Используем безопасный подсчёт скобок, игнорирующий строки и комментарии
        open_count, close_count = count_braces_safe(content)
        logger.debug("_repair_braces: открыто=%d, закрыто=%d", open_count, close_count)

        missing = open_count - close_count
        if missing <= 0:
            logger.debug("_repair_braces: скобки сбалансированы, исправлений нет")
            return content

        lines = content.splitlines(keepends=True)
        last_line = lines[-1] if lines else ""
        base_indent = StructuralRepair._get_indent(last_line)
        logger.debug("_repair_braces: не хватает %d скобок, отступ=%r", missing, base_indent)

        for _ in range(missing):
            lines.append(f"{base_indent}{close_brace}\n")

        logger.info("Добавлено %d закрывающих скобок '%s'", missing, close_brace)
        return ''.join(lines)

    @staticmethod
    def _get_indent(line: str) -> str:
        """Возвращает последовательность пробелов/табов в начале строки."""
        return line[:len(line) - len(line.lstrip())]

    @staticmethod
    def repair_file(file_path: Path, language: str) -> bool:
        """
        Исправляет структурные ошибки в файле (незакрытые блоки).
        Возвращает True, если файл был изменён.
        """
        logger.debug("repair_file: файл=%s, язык=%s", file_path, language)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                original = f.read()
            logger.debug("repair_file: файл прочитан, длина=%d", len(original))
        except Exception as e:
            logger.warning("Не удалось прочитать %s: %s", file_path, e)
            return False

        if language in ('rust', 'rs'):
            repaired = StructuralRepair.repair_rust(original)
        elif language == 'python':
            repaired = StructuralRepair.repair_python(original)
        elif language in ('javascript', 'typescript', 'js', 'ts'):
            repaired = StructuralRepair.repair_javascript(original)
        else:
            logger.debug("Структурный ремонт не поддерживается для языка %s", language)
            return False

        if repaired != original:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(repaired)
                logger.info("Структурные ошибки исправлены в %s", file_path.name)
                return True
            except Exception as e:
                logger.error("Не удалось записать исправленный %s: %s", file_path, e)
                return False
        logger.debug("repair_file: изменения не потребовались")
        return False