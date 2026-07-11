"""
P.1 — Анализатор ruff для Python (паритет с cargo clippy у Rust).

ruff — современный Python-линтер, который покрывает суперсет flake8 (E,W,F)
плюс десятки доп. категорий: bugbear (B), pyupgrade (UP), isort (I),
pep8-naming (N), bandit-lite (S), simplify (SIM), comprehensions (C4),
flake8-async (ASYNC) и т.д. Параллель clippy у Rust: best-practices линтер,
который ловит то, что компилятор пропускает.

Подключается опционально (`pipeline.use_ruff`, default False). Можно
ПАРАЛЛЕЛЬНО с flake8 (ruff покрывает доп. категории) или вместо него
(ruff обычно строже и быстрее).

Контракт error-dict — как у других analyzer\'ов:
    {file, line, column, message, code, severity, error_type}

JSON-вывод `ruff check --output-format=json`:
    [{"code": "F401", "message": "...", "filename": "/abs/x.py",
      "location": {"row": 10, "column": 5}, ...}, ...]
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


SKIP_DIRS = frozenset({
    ".webbles_backups", ".webbles", ".webbles_fix",
    ".webles_sandbox", ".webbles_sandbox",
    ".git",
    "__pycache__", "venv", ".venv", "env", ".env", "node_modules",
    "target", "dist", "build", ".idea", ".vscode", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".next",
    "statistic",
})


def _is_skipped_path(path: Path) -> bool:
    try:
        return any(part in SKIP_DIRS for part in path.parts)
    except Exception:
        return False


# Карта префиксов ruff-кодов → наш error_type. Длинные префиксы важнее
# коротких (SIM не путаем с S).
_PREFIX_TO_TYPE = [
    ("ASYNC", "lint"),
    ("SIM",   "lint"),
    ("COM",   "lint"),
    ("TCH",   "lint"),
    ("RUF",   "lint"),
    ("PLE",   "compile"),
    ("PLR",   "lint"),
    ("PLW",   "warning"),
    ("PL",    "lint"),
    ("UP",    "lint"),
    ("C4",    "lint"),
    ("B",     "compile"),
    ("S",     "security"),
    ("F",     "lint"),
    ("I",     "lint"),
    ("N",     "lint"),
    ("E",     "compile"),
    ("W",     "warning"),
    ("D",     "lint"),
    ("T",     "lint"),
    ("Q",     "lint"),
    ("A",     "lint"),
]


def _split_prefix(code: str):
    """Возвращает (alpha_prefix, digit_tail). На пустом коде — ('', '')."""
    if not code:
        return "", ""
    i = 0
    while i < len(code) and code[i].isalpha():
        i += 1
    return code[:i], code[i:]


def _classify_ruff(code: str, message: str) -> str:
    """Классифицирует ruff-код. Полное совпадение префикса (буквы + цифры)."""
    if not isinstance(code, str) or not code:
        return "lint"
    prefix, tail = _split_prefix(code)
    if not prefix:
        return "lint"
    # Точное совпадение префикса.
    for p, etype in _PREFIX_TO_TYPE:
        if prefix == p:
            return etype
    return "lint"


def _severity_for(code: str) -> str:
    """Severity для ruff-кода. F/E/B/S/PLE — error; остальное — warning.

    Важно: проверяем по разделённому буквенному префиксу, чтобы `SIM102`
    не попал в `S` (security) и не пометился как error.
    """
    if not isinstance(code, str) or not code:
        return "warning"
    prefix, _ = _split_prefix(code)
    if prefix in ("F", "E", "B", "S", "PLE"):
        return "error"
    return "warning"


class RuffAnalyzer:
    """Анализирует Python-проект через `ruff check`.

    Использует JSON-вывод для надёжного парсинга. ruff опциональный
    (`pipeline.use_ruff`); если бинарника нет в PATH — возвращает [].
    """

    def __init__(self, timeout: int = 60):
        self.timeout = int(timeout)

    def available(self) -> bool:
        try:
            r = subprocess.run(
                ["ruff", "--version"],
                capture_output=True, timeout=5, check=False,
            )
            return r.returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def analyze(self, project_path: Path, files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """`files` — опциональный список путей для инкрементального анализа
        (сканируется только эти файлы). ruff, как и flake8, не делает
        межмодульный анализ — сужение безопасно. None — весь project_path."""
        project_path = Path(project_path)
        if not self.available():
            logger.warning("ruff not found in PATH - skipping")
            return []

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
            py_files = [
                p for p in project_path.rglob("*.py")
                if not _is_skipped_path(p)
            ]
            if not py_files:
                logger.info("ruff: no .py files")
                return []
            targets = [str(project_path)]

        exclude_arg = ",".join(sorted(SKIP_DIRS))
        cmd = [
            "ruff", "check",
            "--output-format=json",
            "--exclude", exclude_arg,
            *targets,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(project_path),
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("ruff timeout")
            return []
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning("ruff failed: %s", e)
            return []

        output = proc.stdout or ""
        errors = self._parse_json_output(output, project_path)
        logger.info("ruff: %d findings", len(errors))
        return errors

    @staticmethod
    def _parse_json_output(output: str, project_path: Path) -> List[Dict[str, Any]]:
        """Парсит JSON stdout ruff. Любой сбой → []."""
        if not (output or "").strip():
            return []
        try:
            findings = json.loads(output)
        except Exception:
            return []
        if not isinstance(findings, list):
            return []

        errors: List[Dict[str, Any]] = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            code = (f.get("code") or "").strip()
            filename = f.get("filename") or ""
            message = (f.get("message") or "").strip()
            loc = f.get("location") or {}
            line_num = int(loc.get("row") or 0) if isinstance(loc, dict) else 0
            col_num = int(loc.get("column") or 0) if isinstance(loc, dict) else 0

            if _is_skipped_path(Path(filename)):
                continue
            try:
                rel = str(Path(filename).resolve().relative_to(project_path.resolve()))
            except (ValueError, OSError):
                rel = Path(filename).name
            if _is_skipped_path(Path(rel.replace("\\", "/"))):
                continue

            errors.append({
                "file": rel,
                "line": line_num,
                "column": col_num,
                "message": message,
                "code": code,
                "severity": _severity_for(code),
                "error_type": _classify_ruff(code, message),
            })
        return errors
