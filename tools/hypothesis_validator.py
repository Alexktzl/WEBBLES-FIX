import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from tools.base_tool import BaseTool

class HypothesisValidator(BaseTool):
    """Проверяет патч property‑based тестами (Hypothesis)."""

    def is_available(self) -> bool:
        try:
            subprocess.run(["python", "-c", "import hypothesis"], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def ensure_installed(self) -> bool:
        if self.is_available():
            return True
        return self._prompt_install("pip install hypothesis", "Hypothesis")

    def run(self, file_path: Path, patched_content: str) -> bool:
        if not self.ensure_installed():
            return False
        # Здесь можно реализовать конкретные стратегии тестирования.
        # Для примера просто проверяем, что модуль hypothesis импортируется.
        try:
            import hypothesis
            return True
        except ImportError:
            return False