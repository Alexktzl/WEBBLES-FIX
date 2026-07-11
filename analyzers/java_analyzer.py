"""
Анализатор Java для Webbles Fix.

Подход: запускает `javac` по найденным `.java` файлам (или Maven/Gradle если
обнаружены) и парсит вывод. Коды javac не имеют числовых ID — мы синтезируем
синтетические `JAVAC_*` коды по тексту сообщения (как `GCC_*` в cpp_analyzer
и `analysis/security_scanner` для безопасности).

Контракт error-dict — как у других analyzers:
    {file, line, column, message, code, severity, error_type}
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# Шаблоны для классификации javac-сообщений в синтетические коды.
_PATTERNS = [
    # синтаксис
    (re.compile(r"';' expected"),                              "JAVAC_SEMI",       "syntax"),
    (re.compile(r"'\}' expected|class, interface, .* expected"), "JAVAC_BRACE",    "syntax"),
    (re.compile(r"'\)' expected|'\(' expected"),               "JAVAC_PAREN",      "syntax"),
    (re.compile(r"reached end of file while parsing"),         "JAVAC_EOF",        "syntax"),
    (re.compile(r"illegal start of (expression|type)"),        "JAVAC_SYNTAX",     "syntax"),
    # резолвинг
    (re.compile(r"cannot find symbol"),                        "JAVAC_SYMBOL",     "resolve"),
    (re.compile(r"package .* does not exist"),                 "JAVAC_PACKAGE",    "dependency"),
    # типы
    (re.compile(r"incompatible types"),                        "JAVAC_TYPES",      "type"),
    (re.compile(r"bad operand types"),                         "JAVAC_OPERAND",    "type"),
    (re.compile(r"missing return statement"),                  "JAVAC_RETURN",     "control"),
    (re.compile(r"variable .* might not have been initialized"),
                                                                "JAVAC_UNINIT",     "control"),
    # warnings (через -Xlint)
    (re.compile(r"\[unchecked\]"),                             "JAVAC_UNCHECKED",  "warning"),
    (re.compile(r"\[deprecation\]"),                           "JAVAC_DEPRECATED", "warning"),
]


def _classify(message: str) -> tuple:
    """Возвращает (code, error_type). По умолчанию JAVAC_UNKNOWN."""
    for rx, code, etype in _PATTERNS:
        if rx.search(message):
            return code, etype
    return "JAVAC_UNKNOWN", "unknown"


class JavaAnalyzer:
    """Анализирует Java-проект через `javac` (graceful если javac не найден)."""

    def analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []
        project_path = Path(project_path)
        java_files = list(project_path.rglob("*.java"))
        # пропускаем target/build артефакты + бэкап-копии (O.12)
        _SKIP = ("target", "build", "out", ".gradle", ".git",
                 ".webbles_backups", ".webbles", ".webbles_fix",
                 ".idea", ".vscode", "node_modules", "__pycache__")
        java_files = [p for p in java_files if not any(
            part in _SKIP for part in p.parts
        )]
        if not java_files:
            return errors

        cmd = ["javac", "-Xlint:all", "-d", str(project_path / ".webbles_javac_out"),
               *[str(p) for p in java_files]]
        try:
            proc = subprocess.run(
                cmd, cwd=str(project_path),
                capture_output=True, text=True, timeout=180,
            )
        except FileNotFoundError:
            logger.warning("javac не найден в PATH — пропускаем Java-анализ")
            return errors
        except subprocess.TimeoutExpired:
            logger.warning("javac таймаут — частичный результат")
            return errors
        except Exception as e:
            logger.warning("Ошибка запуска javac: %s", e)
            return errors

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        errors = self._parse_javac_output(output, project_path)
        # очищаем выходной каталог по best-effort
        try:
            out_dir = project_path / ".webbles_javac_out"
            if out_dir.exists():
                import shutil
                shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass

        logger.info("Java: найдено %d ошибок", len(errors))
        return errors

    @staticmethod
    def _parse_javac_output(output: str, project_path: Path) -> List[Dict[str, Any]]:
        """Парсит формат `File.java:LINE: severity: message`."""
        errors: List[Dict[str, Any]] = []
        # пример: src/main/java/P.java:10: error: ';' expected
        rx = re.compile(r"^(.+?\.java):(\d+):\s*(error|warning|note):\s*(.+)$")
        for line in output.splitlines():
            m = rx.match(line.strip())
            if not m:
                continue
            file_path, lineno, severity, message = m.groups()
            if severity == "note":
                continue
            try:
                rel = str(Path(file_path).resolve().relative_to(project_path.resolve()))
            except (ValueError, OSError):
                rel = Path(file_path).name
            code, etype = _classify(message)
            errors.append({
                "file": rel,
                "line": int(lineno),
                "column": 0,
                "message": message.strip(),
                "code": code,
                "severity": severity,
                "error_type": etype,
            })
        return errors
