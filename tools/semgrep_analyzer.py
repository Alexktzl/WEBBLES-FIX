import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any
from tools.base_tool import BaseTool

class SemgrepAnalyzer(BaseTool):
    """Запускает Semgrep для поиска ошибок."""

    def is_available(self) -> bool:
        try:
            subprocess.run(["semgrep", "--version"], capture_output=True, timeout=5)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def ensure_installed(self) -> bool:
        # При вызове из safe_run() доступность уже проверена и закэширована
        # в self._available_cache — не гоняем `semgrep --version` повторно
        # на каждом global_cycle прогона (см. BaseTool.safe_run).
        if self._available_cache is True:
            return True
        if self.is_available():
            return True
        return self._prompt_install("pip install semgrep", "Semgrep")

    # Расследование scan-growth (2026-07-02, tenacity 75→«80»): semgrep был
    # ЕДИНСТВЕННЫМ анализатором без исключения служебных каталогов —
    # flake8 (O.12), mypy, bandit и security_scanner их скипают. Before-скан
    # прогона чист (sandbox копируется без .webbles_backups), а финальный
    # after-скан идёт по project_path уже С бэкапами: semgrep считал ошибки
    # в КОПИЯХ исправленных файлов (там лежит их до-фиксовое содержимое), и
    # independent_scan_after завышался ровно на исправленное — реальная
    # дельта tenacity была −8, метрика показывала +5.
    _EXCLUDE_DIRS = (
        ".webbles_backups", ".webbles", ".webbles_fix",
        ".webles_sandbox", ".webbles_sandbox",
        ".git", "__pycache__", ".venv", "venv", "env",
        "node_modules", ".tox", ".mypy_cache", ".ruff_cache",
        "target", "dist", "build",
    )

    def run(self, project_path: Path) -> List[Dict[str, Any]]:
        if not self.ensure_installed():
            return []

        try:
            cmd = ["semgrep", "--config=auto", "--json"]
            for d in self._EXCLUDE_DIRS:
                cmd.append(f"--exclude={d}")
            cmd.append(str(project_path))
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=120,
                encoding='utf-8', errors='replace'   # ← исправление кодировки
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return data.get("results", [])
            return []
        except Exception:
            return []