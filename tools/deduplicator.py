"""
Адаптер для huniq — удаление дубликатов строк в файлах.
Используется для очистки кода перед отправкой в LLM.
"""

import subprocess
from pathlib import Path
from typing import Optional

from tools.base_tool import BaseTool


class HuniqDeduplicator(BaseTool):
    """Удаляет дубликаты строк с помощью huniq (без сортировки)."""

    def is_available(self) -> bool:
        """Проверяет, установлен ли huniq."""
        try:
            subprocess.run(
                ["huniq", "--version"],
                shell=True,
                capture_output=True,
                timeout=5,
            )
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def ensure_installed(self) -> bool:
        if self.is_available():
            return True
        # huniq ставится через cargo
        return self._prompt_install("cargo install huniq", "huniq")

    def run(self, file_path: Path) -> Optional[str]:
        """
        Удаляет дубликаты строк из файла.
        Возвращает очищенное содержимое или None при ошибке.
        """
        if not self.ensure_installed():
            return None
        try:
            # huniq удаляет дубликаты строк, сохраняя исходный порядок
            with open(file_path, 'r', encoding='utf-8') as f:
                result = subprocess.run(
                    "huniq",
                    stdin=f,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            if result.returncode == 0:
                return result.stdout
            return None
        except Exception:
            return None