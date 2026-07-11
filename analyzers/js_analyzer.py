"""
Анализатор JavaScript/TypeScript для webles_conveyor.
Извлекает ошибки из JS/TS кода с помощью ESLint или TypeScript компилятора.
Добавлена фильтрация ошибок из node_modules и других внешних директорий.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Директории, которые следует исключить из анализа.
# O.12: расширено .webbles_backups/.webbles/.webbles_fix и стандартными
# служебными каталогами — иначе анализатор читает бэкап-копии и движок
# чинит их вместо реальных файлов проекта.
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv",
             ".webles_sandbox", ".webbles_sandbox",
             "dist", "build",
             ".webbles_backups", ".webbles", ".webbles_fix",
             ".idea", ".vscode", ".next", ".cache", "target", "statistic"}

SKIP_PATH_PATTERNS = ["/node_modules/", "/.git/", "/dist/", "/build/",
                     "/.webbles_backups/", "/.webbles/", "/.webbles_fix/"]


class JSAnalyzer:
    """
    Анализирует код JavaScript и TypeScript на наличие ошибок.
    Предпочитает ESLint, для TypeScript также запускает tsc.
    """

    def analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        """
        Находит ошибки в JS/TS файлах по указанному пути.
        """
        errors: List[Dict[str, Any]] = []

        eslint_errors = self._run_eslint(project_path)
        if eslint_errors is not None:
            logger.info(f"ESLint нашёл {len(eslint_errors)} ошибок/предупреждений")
            errors.extend(eslint_errors)

        tsc_errors = self._run_tsc(project_path)
        if tsc_errors is not None:
            logger.info(f"TypeScript нашёл {len(tsc_errors)} ошибок")
            errors.extend(tsc_errors)

        # Дедупликация
        seen = set()
        unique_errors = []
        for err in errors:
            key = (err["file"], err["line"], err["message"])
            if key not in seen:
                seen.add(key)
                unique_errors.append(err)

        logger.info(f"Всего уникальных ошибок JS/TS: {len(unique_errors)}")
        return unique_errors

    def _should_skip_path(self, file_path: str) -> bool:
        """Проверяет, находится ли файл в игнорируемой директории."""
        normalized = file_path.replace('\\', '/')
        for pattern in SKIP_PATH_PATTERNS:
            if pattern in normalized:
                return True
        parts = Path(file_path).parts
        for part in parts:
            if part in SKIP_DIRS:
                return True
        return False

    def _run_eslint(self, project_path: Path) -> Optional[List[Dict[str, Any]]]:
        """Запускает ESLint с JSON-выводом."""
        config_files = [".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
                        ".eslintrc", "eslint.config.js"]
        if not any((project_path / cf).exists() for cf in config_files):
            logger.info("Конфигурация ESLint не найдена, пропускаем")
            return None

        try:
            result = subprocess.run(
                ["npx", "eslint", ".", "--ext", ".js,.ts", "--format=json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode not in (0, 1):
                logger.warning(f"eslint завершился с кодом {result.returncode}")
            data = json.loads(result.stdout) if result.stdout else []
            errors = []
            for file_info in data:
                file_path = file_info.get("filePath", "")
                if self._should_skip_path(file_path):
                    logger.debug(f"ESLint: пропущен внешний файл {file_path}")
                    continue
                try:
                    rel_path = Path(file_path).relative_to(project_path)
                except ValueError:
                    rel_path = Path(file_path)

                for msg in file_info.get("messages", []):
                    severity = "error" if msg.get("severity") == 2 else "warning"
                    rule_id = msg.get("ruleId", "")
                    if not isinstance(rule_id, str):
                        rule_id = ""
                    message = msg.get("message", "")
                    error_type = self._classify_eslint_rule(rule_id, message)

                    errors.append({
                        "file": str(rel_path),
                        "line": msg.get("line", 0),
                        "column": msg.get("column", 0),
                        "message": message,
                        "code": rule_id,
                        "severity": severity,
                        "error_type": error_type,
                    })
            return errors
        except subprocess.TimeoutExpired:
            logger.error("eslint timed out")
            return None
        except FileNotFoundError:
            logger.info("eslint не найден")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"eslint вернул невалидный JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"eslint unexpected error: {e}")
            return None

    def _run_tsc(self, project_path: Path) -> Optional[List[Dict[str, Any]]]:
        """Запускает TypeScript компилятор для поиска ошибок типов."""
        if not (project_path / "tsconfig.json").exists():
            logger.info("tsconfig.json не найден, пропускаем tsc")
            return None

        try:
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--pretty", "false"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout + result.stderr
            return self._parse_tsc_output(output, project_path)
        except subprocess.TimeoutExpired:
            logger.error("tsc timed out")
            return None
        except FileNotFoundError:
            logger.info("tsc не найден")
            return None
        except Exception as e:
            logger.error(f"tsc unexpected error: {e}")
            return None

    def _parse_tsc_output(self, output: str, project_path: Path) -> List[Dict[str, Any]]:
        """Разбирает сообщения tsc с фильтрацией node_modules и т.п."""
        errors = []
        pattern = re.compile(r"^(.+?)\((\d+),(\d+)\):\s+(error|warning)\s+(TS\d+):\s+(.+)$", re.MULTILINE)

        for match in pattern.finditer(output):
            file_path = match.group(1)
            if self._should_skip_path(file_path):
                logger.debug(f"tsc: пропущен внешний файл {file_path}")
                continue
            line = int(match.group(2))
            column = int(match.group(3))
            severity = match.group(4)
            code = match.group(5)
            message = match.group(6)

            if not isinstance(code, str):
                code = str(code)

            try:
                rel_path = Path(file_path).relative_to(project_path)
            except ValueError:
                rel_path = Path(file_path)

            error_type = self._classify_ts_code(code, message)

            errors.append({
                "file": str(rel_path),
                "line": line,
                "column": column,
                "message": message,
                "code": code,
                "severity": severity,
                "error_type": error_type,
            })
        return errors

    @staticmethod
    def _classify_eslint_rule(rule_id: str, message: str) -> str:
        if not isinstance(rule_id, str) or not rule_id:
            return "unknown"
        if "import" in rule_id or "no-unresolved" in rule_id:
            return "dependency"
        if "no-undef" in rule_id or "no-unused-vars" in rule_id:
            return "runtime"
        return "lint"

    @staticmethod
    def _classify_ts_code(code: str, message: str) -> str:
        if not isinstance(code, str) or not code:
            return "unknown"
        if code in ("TS2307", "TS2304", "TS2552"):
            return "dependency"
        if code.startswith("TS2"):
            return "type"
        if code.startswith("TS1"):
            return "syntax"
        return "compile"