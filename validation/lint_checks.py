"""
Проверки линтера для webles_conveyor.
Запускает линтеры для поддерживаемых языков.
"""

import subprocess
from pathlib import Path
from typing import List, Tuple


class LintChecks:
    """
    Запускает линтеры для поддерживаемых языков.
    """

    def run(self, project_path: Path, language: str) -> Tuple[bool, List[str]]:
        language_lower = language.lower()

        if language_lower == "rust":
            return self._run_clippy(project_path)
        elif language_lower == "python":
            return self._run_flake8(project_path)
        elif language_lower in ("javascript", "typescript", "js", "ts"):
            return self._run_eslint(project_path)
        else:
            return True, []

    def _run_clippy(self, project_path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ["cargo", "clippy", "--", "-D", "warnings"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["cargo clippy timed out after 300s"]
        except FileNotFoundError:
            return True, ["cargo not found; skipping clippy"]

    def _run_flake8(self, project_path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ["flake8", str(project_path)],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["flake8 timed out after 120s"]
        except FileNotFoundError:
            return True, ["flake8 not found; skipping lint check"]

    def _run_eslint(self, project_path: Path) -> Tuple[bool, List[str]]:
        config_files = [".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", ".eslintrc", "eslint.config.js"]
        if not any((project_path / cf).exists() for cf in config_files):
            return True, ["No ESLint config found; skipping lint"]

        try:
            result = subprocess.run(
                ["npx", "eslint", ".", "--ext", ".js,.ts"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["eslint timed out after 120s"]
        except FileNotFoundError:
            return True, ["npx/eslint not found; skipping lint check"]