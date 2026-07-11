import subprocess
from pathlib import Path
from typing import Optional
from tools.base_tool import BaseTool

class RepomixContextProvider(BaseTool):
    """Собирает полный контекст репозитория через Repomix."""

    def is_available(self) -> bool:
        try:
            subprocess.run(["repomix", "--version"], capture_output=True, timeout=5)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def ensure_installed(self) -> bool:
        """Проверяет наличие Repomix и предлагает установить."""
        if self.is_available():
            return True
        # Repomix устанавливается через npm
        return self._prompt_install("npm install -g repomix", "Repomix")

    def run(self, project_path: Path) -> Optional[str]:
        if not self.ensure_installed():
            return None
        try:
            result = subprocess.run(
                ["repomix", "--compress", str(project_path)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return None
        except Exception:
            return None