"""
Анализатор Go для Webbles Fix.

Запускает `go build ./...` и (опционально) `go vet ./...`, парсит вывод:
    file.go:10:5: undefined: foo
    file.go:7:3: syntax error: unexpected }, expecting expression

Коды Go не числовые — синтезируем `GO_*` по тексту сообщения (как `GCC_*`
в cpp_analyzer и `JAVAC_*` в java_analyzer).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# go build/vet output:  path/to/file.go:LINE:COL: message
_GO_LINE = re.compile(r"^(?P<file>.+?\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$")

_SKIP_DIRS = {"vendor", ".git", "node_modules", ".webbles_fix",
              ".webbles_backups", ".webbles", "target", "dist", "build",
              ".idea", ".vscode", "__pycache__"}

_PATTERNS = [
    # syntax
    (re.compile(r"syntax error: unexpected", re.IGNORECASE), "GO_SYNTAX",      "syntax"),
    (re.compile(r"missing return at end of function"),       "GO_RETURN",      "control"),
    (re.compile(r"expected (?:\{|\}|\(|\))"),                "GO_SYNTAX",      "syntax"),
    # resolve / dependency
    (re.compile(r"undefined:"),                              "GO_UNDEFINED",   "resolve"),
    (re.compile(r"imported and not used"),                   "GO_UNUSED_IMP",  "warning"),
    (re.compile(r"no required module provides|cannot find module|"
                r"could not import|package .* is not in (?:GOROOT|std)"),
                                                              "GO_IMPORT",     "dependency"),
    # types
    (re.compile(r"cannot use .* as .* in"),                  "GO_TYPES",       "type"),
    (re.compile(r"too many arguments|not enough arguments"), "GO_ARGS",        "type"),
    (re.compile(r"invalid operation"),                       "GO_OPERAND",     "type"),
    (re.compile(r"cannot convert"),                          "GO_TYPES",       "type"),
    # vet/static
    (re.compile(r"declared (?:but )?not used"),              "GO_UNUSED_VAR",  "warning"),
    (re.compile(r"shadows declaration"),                     "GO_SHADOW",      "warning"),
    (re.compile(r"possible misuse of unsafe.Pointer"),       "GO_UNSAFE",      "warning"),
]


def _classify(message: str) -> tuple:
    for rx, code, etype in _PATTERNS:
        if rx.search(message):
            return code, etype
    return "GO_UNKNOWN", "unknown"


class GoAnalyzer:
    """Анализирует Go-проект через `go build ./...` + `go vet ./...`."""

    def analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        project_path = Path(project_path)
        # Минимальный признак Go-проекта.
        if not (project_path / "go.mod").exists() and not any(
            p.suffix == ".go" and not any(part in _SKIP_DIRS for part in p.parts)
            for p in project_path.rglob("*.go")
        ):
            return []
        errors: List[Dict[str, Any]] = []
        # 1) go build
        errors.extend(self._run(["go", "build", "./..."], project_path))
        # 2) go vet — даёт warnings, нужно для shadow/unused
        try:
            errors.extend(self._run(["go", "vet", "./..."], project_path))
        except Exception as e:
            logger.debug("go vet пропущен: %s", e)
        # дедуп
        seen = set()
        uniq: List[Dict[str, Any]] = []
        for e in errors:
            key = (e.get("file", ""), e.get("line", 0),
                   e.get("code", ""), e.get("message", "")[:120])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(e)
        logger.info("Go: найдено %d уникальных ошибок", len(uniq))
        return uniq

    @staticmethod
    def _run(cmd: List[str], project_path: Path) -> List[Dict[str, Any]]:
        try:
            proc = subprocess.run(
                cmd, cwd=str(project_path),
                capture_output=True, text=True, timeout=180,
            )
        except FileNotFoundError:
            logger.warning("`%s` не найден в PATH — пропускаем", cmd[0])
            return []
        except subprocess.TimeoutExpired:
            logger.warning("`%s` таймаут", " ".join(cmd))
            return []
        except Exception as e:
            logger.warning("Ошибка запуска `%s`: %s", " ".join(cmd), e)
            return []
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        out: List[Dict[str, Any]] = []
        for raw in output.splitlines():
            m = _GO_LINE.match(raw.strip())
            if not m:
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
                "severity": "warning" if etype == "warning" else "error",
                "error_type": etype,
            })
        return out
