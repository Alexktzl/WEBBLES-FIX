"""
Анализатор Python для Webbles Fix.
Использует flake8 (если доступен) для поиска всех синтаксических и стилистических ошибок.
Если flake8 не найден — используется встроенный AST (находит только первую ошибку).
"""

import ast
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Директории, которые НЕ должны попадать в анализ. Раньше flake8 сканил
# `.webbles_backups/*.py` как боевой код, и движок чинил **копии** вместо
# реальных файлов проекта (O.12). Список объединяет служебные/системные
# каталоги Python/Node/IDE и наши собственные служебные папки.
SKIP_DIRS = frozenset({
    ".webbles_backups",
    ".webbles",
    ".webbles_fix",
    ".webles_sandbox",      # legacy sandbox-копии (видны в логах 2026-06-05)
    ".webbles_sandbox",     # вариант написания
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
    "statistic",            # наша же папка отчётов — не сканируем
    "_vendor",              # встроенные вендорные зависимости (tablib/_vendor/dbfpy и т.п.)
})


def _is_skipped_path(path: Path) -> bool:
    """True если путь лежит в одной из служебных директорий."""
    try:
        return any(part in SKIP_DIRS for part in path.parts)
    except Exception:
        return False


class PythonAnalyzer:
    """Анализирует код Python с помощью flake8 или AST."""

    def analyze(self, project_path: Path, files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Находит ошибки в проекте Python.

        `files` — опциональный список путей (относительных к `project_path` или
        абсолютных) для ИНКРЕМЕНТАЛЬНОГО анализа: сканируется только эти файлы,
        а не весь проект. flake8/pyflakes не делают межмодульный анализ — F821/
        F401/E-коды для одного файла идентичны при сканировании файла отдельно
        или всего проекта целиком, так что сужение безопасно для этого анализатора.
        None (по умолчанию) — старое поведение, весь project_path.
        """
        # Пробуем flake8 – он найдёт все ошибки сразу
        if self._flake8_available():
            logger.info("flake8 найден, выполняем %s анализ",
                       "инкрементальный" if files else "полный")
            return self._run_flake8_analysis(project_path, files=files)
        else:
            logger.warning("flake8 не найден, выполняем только проверку синтаксиса через AST")
            return self._run_ast_analysis(project_path, files=files)

    # -----------------------------------------------------------------
    # Анализ через flake8
    # -----------------------------------------------------------------
    def _flake8_available(self) -> bool:
        """Проверяет, доступен ли flake8 в системе."""
        try:
            subprocess.run(
                ["flake8", "--version"],
                capture_output=True,
                timeout=5,
                check=False
            )
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    @staticmethod
    def _detect_line_length(project_path: Path) -> int:
        """Читает line-length из pyproject.toml/setup.cfg проекта.

        Если в проекте есть ruff/flake8 конфиг с line-length — используем его
        вместо flake8 default (79), чтобы инструменты не расходились.
        Если нашли ruff-секцию без явного line-length — возвращаем 88 (ruff default).
        """
        import re as _re
        has_ruff_config = False
        for cfg_file in ("pyproject.toml", "setup.cfg", ".flake8"):
            p = project_path / cfg_file
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                # Явный line-length имеет приоритет
                m = _re.search(r"line[_-]length\s*=\s*(\d+)", text)
                if m:
                    return int(m.group(1))
                # Наличие [tool.ruff] секции без явного line-length → 88 (ruff default)
                if "[tool.ruff" in text:
                    has_ruff_config = True
            except Exception:
                pass
        return 88 if has_ruff_config else 79

    def _run_flake8_analysis(self, project_path: Path, files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Запускает flake8 и собирает все ошибки.

        `files` — если задан, передаём flake8 конкретные пути файлов вместо
        директории целиком (инкрементальный режим). Файлы из SKIP_DIRS
        отфильтровываются до вызова — --exclude матчит только при обходе
        дерева, явно перечисленные пути он не отфильтрует.
        """
        errors: List[Dict[str, Any]] = []
        try:
            # O.12: явно исключаем служебные/бэкап-каталоги через --exclude.
            # Раньше flake8 сканил .webbles_backups/*.py как боевой код, и
            # движок чинил копии вместо реальных файлов.
            exclude_arg = ",".join(sorted(SKIP_DIRS))
            line_length = self._detect_line_length(project_path)
            if files:
                targets = []
                for f in files:
                    fp = Path(f)
                    if not fp.is_absolute():
                        fp = project_path / fp
                    if _is_skipped_path(fp) or not fp.exists():
                        continue
                    targets.append(str(fp))
                if not targets:
                    return []
            else:
                targets = [str(project_path)]
            cmd = [
                "flake8",
                "--select=E,W,F",
                # W503 (разрыв строки ДО бинарного оператора) и W504 (ПОСЛЕ)
                # взаимно противоречат друг другу — fix одного гарантированно
                # создаёт другой. flake8 по умолчанию игнорирует ОБА, но наш
                # --select=E,W,F выше переопределяет дефолт и включает их
                # обратно без разбора. Игнорируем ОБА явно (как и сам flake8
                # по умолчанию) — иначе оба жёстко уходят в no_llm_codes
                # (generate_patch_stage.py) без реальной пользы. W504 был
                # источником 86% (264/307) всех NEEDS_REVIEW в контрольной
                # серии 2026-06-22 — просто шум в очереди ручного просмотра.
                "--extend-ignore=W503,W504",
                "--exit-zero",
                "--format=default",
                f"--max-line-length={line_length}",
                f"--exclude={exclude_arg}",
                *targets,
            ]
            # Запускаем flake8 с максимальной детализацией
            result = subprocess.run(
                cmd,
                cwd=project_path,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=60,
            )
            # Разбираем вывод построчно
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Формат: путь:строка:колонка: код сообщение
                match = re.match(r"^(.+?):(\d+):(\d+):\s+(\w\d+)\s+(.+)$", line)
                if not match:
                    continue
                file_path = match.group(1)
                line_num = int(match.group(2))
                col_num = int(match.group(3))
                code = match.group(4)
                message = match.group(5)

                # Доп. защита от flake8 в старых сборках, которые могут
                # игнорировать --exclude в относительных путях.
                if _is_skipped_path(Path(file_path)):
                    continue

                # Определяем тип ошибки для Webbles
                error_type = self._classify_flake8_error(code, message)

                # Приводим путь к относительному
                try:
                    rel_path = Path(file_path).resolve().relative_to(project_path.resolve())
                except ValueError:
                    rel_path = Path(file_path).name

                # Финальная страховка: проверяем уже относительный путь.
                if _is_skipped_path(Path(str(rel_path))):
                    continue

                errors.append({
                    "file": str(rel_path),
                    "line": line_num,
                    "column": col_num,
                    "message": message,
                    "code": code,
                    "severity": "error" if code.startswith("E") else "warning",
                    "error_type": error_type,
                })
        except Exception as e:
            logger.error(f"flake8 analysis failed: {e}")
            # Если flake8 упал, пробуем AST как запасной вариант
            return self._run_ast_analysis(project_path, files=files)

        # Если flake8 ничего не нашёл, но проект существует – всё равно возвращаем пустой список
        if not errors:
            logger.info("flake8 не обнаружил ошибок")
        else:
            logger.info(f"flake8 нашёл {len(errors)} ошибок/предупреждений")
        return errors

    # -----------------------------------------------------------------
    # Анализ через встроенный AST (фоллбэк)
    # -----------------------------------------------------------------
    def _run_ast_analysis(self, project_path: Path, files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Использует встроенный AST для поиска синтаксических ошибок (только первой!)."""
        errors = []
        if files:
            candidates = []
            for f in files:
                fp = Path(f)
                if not fp.is_absolute():
                    fp = project_path / fp
                if fp.exists():
                    candidates.append(fp)
        else:
            candidates = list(project_path.rglob("*.py"))
        for py_file in candidates:
            # O.12: skip служебных каталогов (`.webbles_backups`, `venv`, ...).
            # Без этого AST читал бэкап-копии повреждённых файлов и движок
            # чинил их вместо реальных файлов проекта.
            try:
                rel = py_file.relative_to(project_path)
            except ValueError:
                rel = py_file
            if _is_skipped_path(Path(str(rel))) or _is_skipped_path(py_file):
                continue
            try:
                with open(py_file, "r", encoding="utf-8") as f:
                    source = f.read()
                compile(source, str(py_file), "exec")
            except SyntaxError as e:
                errors.append({
                    "file": str(rel),
                    "line": e.lineno or 0,
                    "column": e.offset or 0,
                    "message": e.msg,
                    "code": "E999",
                    "severity": "error",
                    "error_type": "syntax",
                })
                logger.info(f"AST-анализ нашёл 1 синтаксическую ошибку в {rel}")
                break  # AST видит только первую ошибку, поэтому останавливаемся
            except Exception:
                continue
        return errors

    # -----------------------------------------------------------------
    # Классификация ошибок flake8
    # -----------------------------------------------------------------
    @staticmethod
    def _classify_flake8_error(code: str, message: str) -> str:
        """Определяет тип ошибки на основе кода flake8."""
        if not isinstance(code, str):
            return "unknown"
        if code.startswith("E999"):
            return "syntax"
        if code.startswith("E"):
            return "compile"  # большинство E-ошибок — синтаксис/структура
        if code.startswith("W"):
            return "warning"
        if code.startswith("F"):
            # F-ошибки от pyflakes — часто неопределённые переменные, импорт
            if "undefined" in message.lower() or "name" in message.lower():
                return "runtime"
            if "import" in message.lower():
                return "dependency"
        return "lint"
