"""
Адаптер для Mogglo — мультиязычная проверка синтаксиса.
Работает в паре с бэкапом: если синтаксис нарушен, система откатывает файл.
"""

import subprocess
from pathlib import Path
from typing import Optional

from tools.base_tool import BaseTool


class MoggloRecovery(BaseTool):
    """Проверяет синтаксис через mogglo (Tree‑sitter) для 5+ языков."""

    # Соответствие language → бинарник mogglo
    LANGUAGE_MAP = {
        "rust": "mogglo-rust",
        "python": "mogglo-python",
        "javascript": "mogglo-javascript",
        "typescript": "mogglo-typescript",
        "cpp": "mogglo-cpp",
        "csharp": "mogglo-cpp",   # C# использует тот же парсер, что и C/C++
    }

    def is_available(self) -> bool:
        """Проверяет наличие основного бинарника mogglo‑rust."""
        try:
            subprocess.run(
                ["mogglo-rust", "--version"],
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
        # Устанавливаем все нужные пакеты одной командой
        return self._prompt_install(
            "cargo install mogglo-rust mogglo-python mogglo-javascript mogglo-typescript mogglo-cpp --locked",
            "mogglo",
        )

    def run(self, file_path: Path, language: str) -> bool:
        """
        Проверяет синтаксис файла.
        Возвращает True, если файл синтаксически корректен.
        """
        if not self.ensure_installed():
            return False  # не можем проверить → считаем, что есть ошибки

        binary = self.LANGUAGE_MAP.get(language, "mogglo-rust")
        try:
            result = subprocess.run(
                [binary, "check", str(file_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception:
            return False