import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from tools.base_tool import BaseTool

class CorrectrAnalyzer(BaseTool):
    """Сканирует AI-сгенерированный код на баги через correctr."""

    def is_available(self) -> bool:
        try:
            # shell=True, чтобы видеть глобальные npm-пакеты в Windows
            subprocess.run("correctr --version", shell=True, capture_output=True, timeout=5)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def ensure_installed(self) -> bool:
        if self.is_available():
            return True
        return self._prompt_install("npm install -g correctr", "correctr")

    def run(self, file_path: Path, json_output: bool = True) -> Optional[List[Dict[str, Any]]]:
        if not self.ensure_installed():
            return None
        try:
            cmd = f"correctr check \"{str(file_path)}\""
            if json_output:
                cmd += " --json"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                if json_output:
                    return json.loads(result.stdout)
                return result.stdout.strip().splitlines()
            return []
        except Exception:
            return None

    def fix(self, file_path: Path, auto_yes: bool = False) -> bool:
        """
        Автоматически исправляет обнаруженные проблемы.
        Возвращает True, если исправление прошло успешно.
        """
        if not self.ensure_installed():
            return False
        try:
            cmd = f"correctr fix \"{str(file_path)}\""
            if auto_yes:
                cmd += " --yes"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            return result.returncode == 0
        except Exception:
            return False