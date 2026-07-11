"""
Проверки компиляции для webles_conveyor.
Запускает компилятор или статический анализатор для поддерживаемых языков.
"""

import subprocess
from pathlib import Path
from typing import List, Tuple


class CompilerChecks:
    """
    Запускает компилятор или проверку типов для поддерживаемых языков.
    """

    def run(self, project_path: Path, language: str) -> Tuple[bool, List[str]]:
        language_lower = language.lower()

        if language_lower == "rust":
            return self._run_cargo_check(project_path)
        elif language_lower == "python":
            return self._run_pyright(project_path)
        elif language_lower in ("javascript", "typescript", "js", "ts"):
            return self._run_tsc(project_path)
        else:
            return True, []

    def _run_cargo_check(self, project_path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ["cargo", "check"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["cargo check timed out after 300s"]
        except FileNotFoundError:
            return False, ["cargo not found in PATH"]

    def _run_pyright(self, project_path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ["pyright", str(project_path)],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["pyright timed out after 120s"]
        except FileNotFoundError:
            return self._run_mypy(project_path)

    def _run_mypy(self, project_path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ["mypy", str(project_path)],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["mypy timed out after 120s"]
        except FileNotFoundError:
            return True, ["mypy not found; skipping type check"]

    def _run_tsc(self, project_path: Path) -> Tuple[bool, List[str]]:
        if not (project_path / "tsconfig.json").exists():
            return True, ["No tsconfig.json found; skipping tsc"]

        try:
            result = subprocess.run(
                ["tsc", "--noEmit"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout.splitlines() + result.stderr.splitlines()
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, ["tsc timed out after 120s"]
        except FileNotFoundError:
            return True, ["tsc not found in PATH; skipping type check"]