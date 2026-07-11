"""
P.0 — Анализатор mypy для Python (паритет с Rust типовой проверкой).

mypy для Python это то же, что `rustc --type-check` для Rust: статический
анализатор типов, который видит ошибки до runtime. Без него Python в
webbles_fix полагается только на flake8 (стиль/синтаксис) и AST (только
синтаксис, только первая ошибка). После подключения mypy появляются
типовые ошибки: `name-defined` (E0425/E0432 аналог), `attr-defined`
(E0609 аналог), `arg-type`/`return-value` (E0308 аналог),
`union-attr`/`assignment`/`import-not-found` и т.д.

Контракт error-dict — как у других analyzer'ов:
    {file, line, column, message, code, severity, error_type}

mypy — опциональный (флаг `pipeline.use_mypy`, default False). Без mypy в
PATH модуль возвращает `[]` без падения, как остальные внешние тулы.

Поддерживаются оба формата строки mypy:
    file.py:10:5: error: msg  [code]
    file.py:15: error: msg  [code]    (без column в --no-error-summary)
    file.py:20: note: ...              (пропускаем — это подсказка)
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Бэкап/служебные директории, которые исключаем из анализа (см. O.12).
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
    "statistic",
})


def _is_skipped_path(path: Path) -> bool:
    try:
        return any(part in SKIP_DIRS for part in path.parts)
    except Exception:
        return False


# Формат вывода mypy. Поддерживает оба варианта (с column и без).
# Примеры:
#   src/auth.py:10:5: error: Name "foo" is not defined  [name-defined]
#   src/auth.py:15: error: Incompatible return value (got "int", expected "str")  [return-value]
#   src/auth.py:20: note: Revealed type is "builtins.int"
_MYPY_LINE = re.compile(
    r"^(?P<file>.+?\.py):(?P<line>\d+)(?::(?P<col>\d+))?:\s+"
    r"(?P<severity>error|warning|note):\s+"
    r"(?P<msg>.+?)"
    r"(?:\s+\[(?P<code>[a-zA-Z][a-zA-Z0-9_-]*)\])?\s*$"
)


# Классификация mypy кодов → наш error_type. По смыслу — параллель с
# rustc-кодами:
#   name-defined         → E0425 (unresolved name)        → resolve
#   import-not-found     → E0432 (unresolved import)      → dependency
#   import               → E0432                          → dependency
#   attr-defined         → E0609 (no field on type)       → type
#   arg-type             → E0308 (type mismatch arg)      → type
#   return-value         → E0308 (type mismatch return)   → type
#   assignment           → E0308 (type mismatch assign)   → type
#   union-attr           → optional-чёрный атрибут        → type
#   call-arg             → неверные аргументы вызова       → type
#   index                → не индексируется               → type
#   operator             → нет операции для типов          → type
#   list-item            → элемент списка неверного типа   → type
#   dict-item            → элемент словаря неверного типа  → type
#   misc                 → разное (фоллбэк)               → type
#   no-untyped-def       → функция без аннотаций (style)  → warning
#   var-annotated        → нужна аннотация переменной     → warning
#   no-redef             → переопределение имени          → warning
#   unreachable          → недостижимый код               → warning
#   type-arg             → неверные type args             → type
#   type-var             → неверный TypeVar               → type
#   override             → несовместимое переопределение  → type
#   syntax               → синтаксическая ошибка          → syntax
_CODE_TO_TYPE = {
    "name-defined": "resolve",
    "used-before-def": "resolve",
    "import": "dependency",
    "import-not-found": "dependency",
    "import-untyped": "dependency",
    "attr-defined": "type",
    "arg-type": "type",
    "return-value": "type",
    "return": "type",
    "assignment": "type",
    "union-attr": "type",
    "call-arg": "type",
    "call-overload": "type",
    "index": "type",
    "operator": "type",
    "list-item": "type",
    "dict-item": "type",
    "type-arg": "type",
    "type-var": "type",
    "type-abstract": "type",
    "override": "type",
    "valid-type": "type",
    "misc": "type",
    "no-untyped-def": "warning",
    "no-untyped-call": "warning",
    "no-redef": "warning",
    "var-annotated": "warning",
    "unreachable": "warning",
    "redundant-cast": "warning",
    "unused-coroutine": "warning",
    "syntax": "syntax",
    "annotation-unchecked": "warning",
}


def _synthesize_mypy_code(message: str) -> str:
    """F6: некоторые mypy-сообщения приходят без `[code]` (старый mypy /
    специфичные диагностики). Без кода они проваливаются через
    `_UNFIXABLE_MYPY_CODES` (точное сравнение по коду) прямо в LLM, который
    не может починить отсутствие type stubs — и патч уходит в rejected.
    Синтезируем код из текста сообщения для двух известных безкодовых
    случаев, чтобы они маршрутизировались как остальные dependency-ошибки.
    """
    m = (message or "").lower()
    if "cannot find implementation or library stub" in m or "library stub file not found" in m:
        return "import-untyped"
    if "untyped decorator makes function" in m:
        return "untyped-decorator"
    return ""


def _classify_mypy(code: str, message: str) -> str:
    """Сводит mypy-код к нашему error_type. Без кода — эвристика по тексту."""
    if isinstance(code, str) and code in _CODE_TO_TYPE:
        return _CODE_TO_TYPE[code]
    # Эвристика по сообщению — для очень старого mypy без кодов.
    m = (message or "").lower()
    if "is not defined" in m or "has no attribute" in m:
        return "resolve" if "is not defined" in m else "type"
    if "cannot find module" in m or "cannot find implementation" in m:
        return "dependency"
    if "incompatible" in m and ("type" in m or "return" in m or "argument" in m):
        return "type"
    if "unreachable" in m:
        return "warning"
    return "type"


class MypyAnalyzer:
    """Анализирует Python-проект через `mypy`.

    Запускает `mypy <project_path>` со стандартными настройками + список
    `--exclude` под бэкап/служебные каталоги. Парсит вывод и эмитит
    error-dict'ы с полем `code` равным mypy-коду (без префикса MYPY_,
    они и так уникальные).

    Honest-ограничения:
      * mypy опциональный (`pipeline.use_mypy`); если бинарника нет в
        PATH — `analyze()` возвращает `[]` без падения.
      * Default настройки mypy достаточно консервативны — без `--strict`
        большая часть untyped-кода молчит. Это правильно: мы хотим
        ловить РЕАЛЬНЫЕ ошибки, не «отсутствие аннотаций».
      * Третьи-party пакеты без stubs выдают `import-untyped`/`import` —
        классифицируем как `dependency`; они уйдут в pip-инференс / NEEDS_REVIEW.
    """

    def __init__(self, timeout: int = 120):
        self.timeout = int(timeout)

    def available(self) -> bool:
        """Проверка наличия `mypy` в PATH."""
        try:
            r = subprocess.run(
                ["mypy", "--version"],
                capture_output=True, timeout=5, check=False,
            )
            return r.returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def analyze(self, project_path: Path,
                files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Запускает mypy и парсит вывод. Любой сбой → `[]` (graceful).

        `files` — опциональный список путей (относительных к `project_path`)
        для ТОЧЕЧНОГО анализа: mypy запускается только на этих файлах вместо
        всего проекта. Используется ValidateStage._recheck_mypy_target
        (конверсия-1, 2026-07-02): flake8-only rescan после патча не видит
        mypy-ошибки, из-за чего для mypy-кодов «error_count_decreased» был
        невыполним в принципе — детерминированные type:ignore-фиксы уходили
        в REJECT error_count_not_decreased при реально исправленной ошибке.
        """
        project_path = Path(project_path)
        if not self.available():
            logger.warning("mypy не найден в PATH — пропускаем")
            return []
        if files:
            targets = []
            for f in files:
                p = Path(f)
                if not p.is_absolute():
                    p = project_path / p
                if p.exists() and not _is_skipped_path(p):
                    targets.append(str(p))
            if not targets:
                logger.info("mypy: целевые файлы не найдены (%s)", files)
                return []
        else:
            # Список python-файлов проекта (учёт SKIP_DIRS), чтобы не отдавать
            # mypy лишнее. Если ничего не нашли — возвращаемся.
            py_files = [
                p for p in project_path.rglob("*.py")
                if not _is_skipped_path(p)
            ]
            if not py_files:
                logger.info("mypy: .py файлов не найдено")
                return []
            targets = [str(project_path)]

        exclude_arg = "|".join(re.escape(d) for d in sorted(SKIP_DIRS))
        # `--exclude` принимает regex (mypy ≥ 0.760).
        # `--show-error-codes` гарантирует, что в выводе будет `[code]`.
        # `--no-color-output` стабилизирует парсинг.
        # `--show-column-numbers` даёт колонки в ошибках (если поддерживается).
        cmd = [
            "mypy",
            "--show-error-codes",
            "--no-color-output",
            "--show-column-numbers",
            "--no-error-summary",
            f"--exclude={exclude_arg}",
            *targets,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(project_path),
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("mypy таймаут — частичный результат")
            return []
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning("Ошибка запуска mypy: %s", e)
            return []

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        errors = self._parse_output(output, project_path)
        logger.info("mypy: найдено %d ошибок типов", len(errors))
        return errors

    # -----------------------------------------------------------------
    # Парсер вывода — статический (тестируем без подъёма mypy).
    # -----------------------------------------------------------------
    @staticmethod
    def _parse_output(output: str, project_path: Path) -> List[Dict[str, Any]]:
        """Парсит сырой stdout mypy в список error-dict'ов.

        Игнорирует `note:` строки (это подсказки, не ошибки). Если в
        выводе нет `[code]` — code будет пустой, но error попадёт всё
        равно (с эвристикой по тексту).
        """
        errors: List[Dict[str, Any]] = []
        for raw in (output or "").splitlines():
            line = raw.rstrip()
            if not line:
                continue
            m = _MYPY_LINE.match(line)
            if not m:
                continue
            severity = (m.group("severity") or "").strip()
            if severity == "note":
                # это подсказка, не отдельная ошибка
                continue
            file_path = m.group("file")
            # Доп. защита от свежеустановленного mypy: если путь под
            # бэкап/служебной директорией — пропускаем.
            if _is_skipped_path(Path(file_path)):
                continue

            # mypy запускается с cwd=project_path (см. analyze()), поэтому
            # `file_path` в его выводе — ОТНОСИТЕЛЬНЫЙ путь от project_path,
            # а не от текущего рабочего каталога ЭТОГО python-процесса.
            # `Path(file_path).resolve()` без явной привязки к project_path
            # резолвит относительный путь от cwd процесса (например,
            # C:\dev\webbles_fix, откуда запущен run_agent.py) — почти
            # всегда ДРУГАЯ директория, чем project_path. relative_to() тогда
            # падает с ValueError, и старый fallback `Path(file_path).name`
            # отбрасывал директорию целиком (например "kedro_light/kedro.py"
            # → просто "kedro.py"). ErrorContextValidator.is_valid() искал
            # этот несуществующий "kedro.py" в корне project_path, не находил
            # — и PrioritizeStage отбрасывал ВСЕ такие ошибки как невалидные
            # (control series, 2026-06-23: ellwise/kedro-light,
            # ClimateImpactLab/dodola — "Все ошибки невалидны — завершаем
            # цикл", 0 циклов, хотя mypy реально нашёл 16-21 ошибку).
            fp = Path(file_path)
            if not fp.is_absolute():
                fp = project_path / fp
            try:
                rel = str(fp.resolve().relative_to(project_path.resolve()))
            except (ValueError, OSError):
                rel = Path(file_path).name
            if _is_skipped_path(Path(rel.replace("\\", "/"))):
                continue

            line_num = int(m.group("line") or 0)
            col_num = int(m.group("col") or 0)
            message = (m.group("msg") or "").strip()
            code = (m.group("code") or "").strip()
            if not code:
                code = _synthesize_mypy_code(message)

            errors.append({
                "file": rel,
                "line": line_num,
                "column": col_num,
                "message": message,
                "code": code,
                "severity": severity if severity in ("error", "warning") else "error",
                "error_type": _classify_mypy(code, message),
            })
        return errors
