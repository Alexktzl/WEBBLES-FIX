"""
Анализатор Kotlin для Webbles Fix.

Запускает `kotlinc` (или `./gradlew compileKotlin` если есть Gradle-проект)
и парсит вывод формата:
    file.kt:10:5: error: unresolved reference: foo
    file.kt:7:3: warning: parameter 'x' is never used

Коды Kotlin не числовые — синтезируем `KT_*` по тексту.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# kotlinc:  path/to/file.kt:LINE:COL: severity: message
_KT_LINE = re.compile(
    r"^(?P<file>.+?\.kts?):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<severity>error|warning|info):\s+(?P<msg>.+)$"
)

_SKIP_DIRS = {"build", ".gradle", ".idea", "out", ".git", "node_modules",
              ".webbles_fix", ".webbles_backups", ".webbles",
              ".vscode", "target", "dist", "__pycache__"}

_PATTERNS = [
    # resolve / dependency
    (re.compile(r"unresolved reference"),                "KT_UNRESOLVED", "resolve"),
    (re.compile(r"cannot resolve symbol|cannot access"), "KT_UNRESOLVED", "resolve"),
    # types
    (re.compile(r"type mismatch"),                       "KT_TYPES",      "type"),
    (re.compile(r"argument type mismatch"),              "KT_TYPES",      "type"),
    (re.compile(r"no value passed for parameter"),       "KT_ARGS",       "type"),
    (re.compile(r"too many arguments"),                  "KT_ARGS",       "type"),
    # null safety
    (re.compile(r"only safe \(\?\.\) or non-null"),      "KT_NULLSAFE",   "null_safety"),
    (re.compile(r"null can not be a value"),             "KT_NULLSAFE",   "null_safety"),
    # syntax
    (re.compile(r"expecting (?:'\}'|'\)'|'\{'|'\('|';'|an expression)"),
                                                          "KT_SYNTAX",     "syntax"),
    (re.compile(r"syntax error"),                        "KT_SYNTAX",     "syntax"),
    # warnings
    (re.compile(r"parameter '\w+' is never used"),       "KT_UNUSED_PARAM", "warning"),
    (re.compile(r"variable '\w+' is never used"),        "KT_UNUSED_VAR",   "warning"),
    (re.compile(r"unused expression"),                   "KT_UNUSED_EXPR",  "warning"),
    (re.compile(r"deprecated"),                          "KT_DEPRECATED",   "warning"),
    # control
    (re.compile(r"a 'return' expression required"),      "KT_RETURN",     "control"),
]


def _classify(message: str) -> tuple:
    for rx, code, etype in _PATTERNS:
        if rx.search(message):
            return code, etype
    return "KT_UNKNOWN", "unknown"


class KotlinAnalyzer:
    """Анализирует Kotlin-проект через `kotlinc` (graceful если не найден)."""

    def analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        project_path = Path(project_path)
        kt_files = [
            p for p in project_path.rglob("*.kt")
            if not any(part in _SKIP_DIRS for part in p.parts)
        ]
        kts_files = [
            p for p in project_path.rglob("*.kts")
            if not any(part in _SKIP_DIRS for part in p.parts)
        ]
        all_files = kt_files + kts_files
        if not all_files:
            return []

        cmd = ["kotlinc", "-nowarn", "-d", str(project_path / ".webbles_kotlin_out"),
               *[str(p) for p in all_files]]
        # Снимаем -nowarn чтобы получать предупреждения тоже:
        cmd = ["kotlinc", "-d", str(project_path / ".webbles_kotlin_out"),
               "-Werror=false", *[str(p) for p in all_files]]
        try:
            proc = subprocess.run(
                cmd, cwd=str(project_path),
                capture_output=True, text=True, timeout=240,
            )
        except FileNotFoundError:
            logger.warning("kotlinc не найден в PATH — пропускаем Kotlin-анализ")
            return []
        except subprocess.TimeoutExpired:
            logger.warning("kotlinc таймаут")
            return []
        except Exception as e:
            logger.warning("Ошибка kotlinc: %s", e)
            return []

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        errors = self._parse(output, project_path)
        # cleanup output dir
        try:
            out_dir = project_path / ".webbles_kotlin_out"
            if out_dir.exists():
                import shutil
                shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass

        logger.info("Kotlin: найдено %d ошибок", len(errors))
        return errors

    @staticmethod
    def _parse(output: str, project_path: Path) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for raw in output.splitlines():
            m = _KT_LINE.match(raw.strip())
            if not m:
                continue
            sev = m.group("severity")
            if sev == "info":
                continue
            file_path = m.group("file")
            try:
                rel = str(Path(file_path).resolve().relative_to(project_path.resolve()))
            except (ValueError, OSError):
                rel = Path(file_path).name
            msg = m.group("msg").strip()
            code, etype = _classify(msg)
            out.append({
                "file": rel,
                "line": int(m.group("line")),
                "column": int(m.group("col")),
                "message": msg,
                "code": code,
                "severity": sev,
                "error_type": etype,
            })
        return out
