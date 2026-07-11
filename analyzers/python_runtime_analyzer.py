"""Runtime-анализатор Python для Webbles Fix.

Собирает ошибки, которые проявляются только при выполнении кода:
NameError, TypeError, AttributeError, ImportError, ModuleNotFoundError,
AssertionError и другие runtime-исключения.

Порядок работы:
1. Если проект похож на pytest-проект (есть каталог tests, pytest.ini,
   conftest.py, pyproject.toml или файлы test_*.py), запускается
   `python -m pytest -q --tb=short`. Если тесты завершаются с
   ошибкой, парсится последний traceback.
2. Если pytest-тестов нет — пробуем `unittest discover`.
3. Если тестов нет вовсе — осторожно запускаем только entrypoint-файлы
   (main.py, app.py, run.py, manage.py, server.py). Не запускаем
   каждый .py подряд, чтобы не выполнить миграции, удаление данных
   или сеть.

Исключаем служебные каталоги (.git, venv, node_modules, caches,
sandboxes), чтобы не анализировать копии и временные файлы.

Если запуск завершается без ошибок (returncode == 0), возвращается
пустой список. Если произошла ошибка — парсится traceback и
возвращается список из одного ErrorRecord-подобного словаря с
полями: file, line, column, message, code, severity, error_type,
source, traceback.

Флаг `use_python_runtime` в конфигурации Pipeline управляет вызовом
анализатора. По умолчанию False, чтобы не запускать произвольный
код без явного указания.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Директории, которые НЕ должны участвовать в runtime-анализе. Не
# трогаем окружения, кэши, бэкапы, sandbox-копии и служебные каталоги.
SKIP_DIRS = frozenset({
    ".webbles_backups",
    ".webbles",
    ".webbles_fix",
    ".webles_sandbox",
    ".webbles_sandbox",
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    ".env",
    "node_modules",
    "target",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".next",
    ".gradle",
    ".mvn",
    "statistic",
})

# Entry-point файлы. Запускаем только эти скрипты, если тестов нет.
ENTRYPOINTS = (
    "main.py",
    "app.py",
    "run.py",
    "manage.py",
    "server.py",
)


def _is_skipped_path(path: Path) -> bool:
    """Проверяет, что путь лежит в одной из служебных директорий."""
    try:
        return any(part in SKIP_DIRS for part in path.parts)
    except Exception:
        return False


class PythonRuntimeAnalyzer:
    """Анализирует runtime-ошибки Python через тестовые запуски."""

    def __init__(self, timeout: int = 20):
        self.timeout = int(timeout)

    def analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        """Находит ошибки выполнения в Python-проекте.

        Порядок:
        1. pytest, если проект похож на pytest-проект
        2. unittest discover, если есть tests/
        3. entrypoint-файлы, если тестов нет
        """
        project_path = Path(project_path)
        errors: List[Dict[str, Any]] = []

        if self._has_pytest_project(project_path):
            logger.info("pytest-проект обнаружен, выполняем runtime-анализ через pytest")
            errors = self._run_pytest(project_path)
            if errors:
                return errors

        if self._has_unittest_project(project_path):
            logger.info("unittest-проект обнаружен, выполняем runtime-анализ через unittest")
            errors = self._run_unittest(project_path)
            if errors:
                return errors

        logger.info("тесты не дали runtime-ошибок, пробуем entrypoint-файлы")
        return self._run_entrypoints(project_path)

    # -----------------------------------------------------------------
    # pytest / unittest / entrypoint
    # -----------------------------------------------------------------
    def _run_pytest(self, project_path: Path) -> List[Dict[str, Any]]:
        """Запускает pytest и парсит traceback."""
        return self._run_command(
            project_path=project_path,
            command=[
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
            ],
            source="pytest",
            timeout=self.timeout,
        )

    def _run_unittest(self, project_path: Path) -> List[Dict[str, Any]]:
        """Запускает unittest discover и парсит traceback."""
        return self._run_command(
            project_path=project_path,
            command=[
                sys.executable,
                "-m",
                "unittest",
                "discover",
            ],
            source="unittest",
            timeout=self.timeout,
        )

    def _run_entrypoints(self, project_path: Path) -> List[Dict[str, Any]]:
        """Осторожно запускает только известные entrypoint-файлы.

        Не запускаем каждый .py подряд: в проекте могут быть миграции,
        скрипты удаления, серверы, боты, долгие задачи и т.д.
        """
        for candidate in ENTRYPOINTS:
            path = project_path / candidate
            if not path.exists() or not path.is_file():
                continue
            if _is_skipped_path(path):
                continue
            logger.info("runtime-анализ: запускаем entrypoint %s", candidate)
            errors = self._run_command(
                project_path=project_path,
                command=[
                    sys.executable,
                    candidate,
                ],
                source=f"entrypoint:{candidate}",
                timeout=min(self.timeout, 10),
            )
            if errors:
                return errors
        return []

    # -----------------------------------------------------------------
    # Запуск процесса
    # -----------------------------------------------------------------
    def _run_command(
        self,
        project_path: Path,
        command: List[str],
        source: str,
        timeout: int,
    ) -> List[Dict[str, Any]]:
        """Запускает команду и превращает traceback в список ошибок Webbles."""
        try:
            result = subprocess.run(
                command,
                cwd=str(project_path),
                env=self._runtime_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            logger.warning("runtime-анализ timeout: %s", source)
            stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            output = stdout + "\n" + stderr
            return [
                {
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "message": f"{source} timed out after {timeout}s",
                    "code": "RuntimeTimeout",
                    "severity": "error",
                    "error_type": "runtime",
                    "source": source,
                    "traceback": output[-8000:],
                }
            ]
        except FileNotFoundError as e:
            # Не установлен pytest/unittest/интерпретатор — пропускаем тихо.
            logger.debug("runtime-анализ: команда не найдена (%s): %s", source, e)
            return []
        except Exception as e:
            logger.error("runtime-анализ упал: %s", e)
            return []

        if result.returncode == 0:
            logger.info("runtime-анализ %s завершился без ошибок", source)
            return []

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        errors = self._parse_runtime_output(output, project_path, source)
        if errors:
            logger.info("runtime-анализ %s нашёл %d ошибок", source, len(errors))
        else:
            logger.info(
                "runtime-анализ %s завершился с ошибкой, но traceback не распознан",
                source,
            )
        return errors

    @staticmethod
    def _runtime_env() -> Dict[str, str]:
        """Окружение для безопасного runtime-запуска.

        PYTHONDONTWRITEBYTECODE нужен из-за проблем с __pycache__.
        PYTHONUNBUFFERED нужен, чтобы stdout/stderr не зависали в буфере.
        WEBBLES_RUNTIME_ANALYZER можно использовать в пользовательском коде,
        если проект хочет отключить опасные side-effect'ы при анализе.
        """
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["WEBBLES_RUNTIME_ANALYZER"] = "1"
        return env

    # -----------------------------------------------------------------
    # Парсинг traceback
    # -----------------------------------------------------------------
    def _parse_runtime_output(
        self,
        output: str,
        project_path: Path,
        source: str,
    ) -> List[Dict[str, Any]]:
        """Парсит stdout/stderr и достаёт последнюю полезную Python-ошибку."""
        errors: List[Dict[str, Any]] = []
        if not output.strip():
            return errors

        # Ограничиваем размер traceback, чтобы не раздувать Case File.
        traceback_text = output[-12000:]

        exc_match = self._find_exception(traceback_text)
        if not exc_match:
            return errors

        code = exc_match.group(1)
        message = exc_match.group(2).strip()

        file_path, line_no = self._find_relevant_frame(traceback_text, project_path)

        errors.append(
            {
                "file": file_path,
                "line": line_no,
                "column": 0,
                "message": message,
                "code": code,
                "severity": "error",
                "error_type": "runtime",
                "source": source,
                "traceback": traceback_text,
            }
        )
        return errors

    @staticmethod
    def _find_exception(output: str):
        """Ищет строку исключения вида:

            NameError: name 'x' is not defined
        """
        matches = list(
            re.finditer(
                r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):\s*(.+)$",
                output,
                re.MULTILINE,
            )
        )
        if not matches:
            return None
        return matches[-1]

    @staticmethod
    def _find_relevant_frame(output: str, project_path: Path) -> Tuple[str, int]:
        """Берёт последний traceback-frame, относящийся к файлам проекта."""
        frames = re.findall(
            r'File "([^"]+)", line (\d+)',
            output,
        )
        if not frames:
            return "", 0

        try:
            root = project_path.resolve()
        except Exception:
            root = Path(str(project_path))

        for filename, line_str in reversed(frames):
            raw_path = Path(filename)
            # Игнорируем служебные пути
            if _is_skipped_path(raw_path):
                continue
            try:
                if raw_path.is_absolute():
                    rel = raw_path.resolve().relative_to(root)
                else:
                    rel = raw_path
            except Exception:
                continue
            if _is_skipped_path(Path(str(rel))):
                continue
            try:
                line_no = int(line_str)
            except ValueError:
                line_no = 0
            return str(rel).replace("\\", "/"), line_no

        # fallback: если frame есть, но относительный путь не удалось построить
        filename, line_str = frames[-1]
        try:
            line_no = int(line_str)
        except ValueError:
            line_no = 0
        return Path(filename).name, line_no

    # -----------------------------------------------------------------
    # Детекторы проекта
    # -----------------------------------------------------------------
    @staticmethod
    def _has_pytest_project(project_path: Path) -> bool:
        """True если проект похож на pytest-проект."""
        if (project_path / "tests").exists():
            return True
        if (project_path / "pytest.ini").exists():
            return True
        if (project_path / "conftest.py").exists():
            return True
        if (project_path / "pyproject.toml").exists():
            return True
        if any(project_path.glob("test_*.py")):
            return True
        return False

    @staticmethod
    def _has_unittest_project(project_path: Path) -> bool:
        """True если проект похож на unittest-проект."""
        if (project_path / "tests").exists():
            return True
        if any(project_path.glob("*_test.py")):
            return True
        return False
