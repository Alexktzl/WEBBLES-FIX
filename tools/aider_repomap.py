import subprocess
from pathlib import Path
from typing import Optional
from tools.base_tool import BaseTool

class AiderRepoMap(BaseTool):
    """Использует Aider для построения карты репозитория."""

    def is_available(self) -> bool:
        try:
            subprocess.run(["aider", "--version"], capture_output=True, timeout=5)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def ensure_installed(self) -> bool:
        if self.is_available():
            return True
        return self._prompt_install("pip install aider-chat", "Aider")

    def run(self, project_path: Path) -> Optional[str]:
        if not self.ensure_installed():
            return None
        try:
            result = subprocess.run(
                ["aider", "--map", str(project_path)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            return None
        except Exception:
            return None