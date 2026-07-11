"""
Анализатор TypeScript для Webbles Fix.

Запускает `tsc --noEmit` (предпочитает `npx tsc`, чтобы взять локальную версию
из node_modules) и парсит вывод вида:
    src/main.ts(10,5): error TS1005: ';' expected.

Коды TypeScript числовые с префиксом TS — используем их напрямую. Если в
проекте нет `tsconfig.json` — выбираем все `.ts/.tsx` файлы и анализируем по
одному (graceful если `tsc`/`npx` не найдены).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# tsc output format:  path/to/file.ts(LINE,COL): error TSXXXX: message
_TSC_LINE = re.compile(
    r"^(?P<file>.+?\.tsx?)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?P<severity>error|warning)\s+TS(?P<code>\d+):\s*(?P<msg>.+)$"
)

_SKIP_DIRS = {"node_modules", "dist", "build", ".git", ".webbles_fix",
              ".webbles_backups", ".webbles", "target",
              ".idea", ".vscode", ".next", ".cache", "__pycache__"}


def _classify(code: str, message: str) -> str:
    """Сводит TS-код к нашему error_type."""
    cm = message.lower()
    if code in ("1005", "1109", "1128", "1131", "1136", "1003", "1011", "1013",
                "1130", "1135", "1138", "1144"):
        return "syntax"
    if code in ("2304", "2307", "2552"):  # cannot find name/module
        return "resolve" if code != "2307" else "dependency"
    if code in ("2322", "2339", "2345", "2358", "2367", "2532", "2769"):
        return "type"
    if code in ("6133", "6196", "7006", "7053"):
        return "warning"
    return "unknown"


class TypeScriptAnalyzer:
    """Анализирует TypeScript-проект через `tsc --noEmit`."""

    def analyze(self, project_path: Path) -> List[Dict[str, Any]]:
        project_path = Path(project_path)
        ts_files = [
            p for p in project_path.rglob("*.ts")
            if not any(part in _SKIP_DIRS for part in p.parts)
        ]
        tsx_files = [
            p for p in project_path.rglob("*.tsx")
            if not any(part in _SKIP_DIRS for part in p.parts)
        ]
        if not ts_files and not tsx_files:
            return []

        tsconfig = project_path / "tsconfig.json"
        # Предпочитаем npx → возьмёт локальный tsc из node_modules; fallback на глобальный tsc.
        runners = [["npx", "tsc"], ["tsc"]]
        cmd_args = ["--noEmit", "--pretty", "false", "--target", "es2020",
                    "--moduleResolution", "node", "--esModuleInterop"]
        if tsconfig.exists():
            base_cmd = cmd_args + ["--project", str(tsconfig)]
        else:
            base_cmd = cmd_args + [str(p) for p in (ts_files + tsx_files)]

        last_exc = None
        for runner in runners:
            try:
                proc = subprocess.run(
                    runner + base_cmd,
                    cwd=str(project_path),
                    capture_output=True, text=True, timeout=180,
                )
                output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                errors = self._parse_tsc_output(output, project_path)
                logger.info("TypeScript: найдено %d ошибок", len(errors))
                return errors
            except FileNotFoundError as e:
                last_exc = e
                continue
            except subprocess.TimeoutExpired:
                logger.warning("tsc таймаут — частичный результат")
                return []
            except Exception as e:
                logger.warning("Ошибка запуска tsc: %s", e)
                return []

        logger.warning("tsc/npx не найдены в PATH — пропускаем TS-анализ (%s)", last_exc)
        return []

    @staticmethod
    def _parse_tsc_output(output: str, project_path: Path) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []
        for raw in output.splitlines():
            m = _TSC_LINE.match(raw.strip())
            if not m:
                continue
            file_path = m.group("file")
            try:
                rel = str(Path(file_path).resolve().relative_to(project_path.resolve()))
            except (ValueError, OSError):
                rel = Path(file_path).name
            code = "TS" + m.group("code")
            errors.append({
                "file": rel,
                "line": int(m.group("line")),
                "column": int(m.group("col")),
                "message": m.group("msg").strip(),
                "code": code,
                "severity": m.group("severity"),
                "error_type": _classify(m.group("code"), m.group("msg")),
            })
        return errors
