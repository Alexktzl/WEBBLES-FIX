"""
Проверки безопасности для webles_conveyor.
Запускает сканеры уязвимостей для поддерживаемых языков.
"""

import subprocess
from pathlib import Path
from typing import List, Tuple


class SecurityChecks:
    """
    Запускает сканеры безопасности для поддерживаемых языков.
    """

    def run(self, project_path: Path, language: str) -> Tuple[bool, List[str]]:
        language_lower = language.lower()

        if language_lower == "rust":
            return self._run_cargo_audit(project_path)
        elif language_lower == "python":
            return self._run_bandit(project_path)
        elif language_lower in ("javascript", "typescript", "js", "ts"):
            return self._run_npm_audit(project_path)
        else:
            return True, []

    def _run_cargo_audit(self, project_path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ["cargo", "audit"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["cargo audit timed out after 180s"]
        except FileNotFoundError:
            return True, ["cargo-audit not installed; skipping security check"]

    def _run_bandit(self, project_path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ["bandit", "-r", str(project_path), "-ll"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["bandit timed out after 120s"]
        except FileNotFoundError:
            return True, ["bandit not installed; skipping security check"]

    def _run_npm_audit(self, project_path: Path) -> Tuple[bool, List[str]]:
        if not (project_path / "package.json").exists():
            return True, ["No package.json found; skipping npm audit"]

        try:
            result = subprocess.run(
                ["npm", "audit", "--json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["npm audit timed out after 180s"]
        except FileNotFoundError:
            return True, ["npm not found; skipping security check"]