"""
P.2 — Анализатор bandit для Python (параллель cargo audit + clippy security
lints у Rust). Покрывает дополнительные security-паттерны сверх ruff `S*`:
комплементарный набор детекций (path traversal, weak random, shell
injection в подпроцессах, hardcoded sql, etc.).

Опциональный (`pipeline.use_bandit`, default False). Без bandit в PATH —
возвращает `[]`. JSON-вывод парсим напрямую (`bandit -f json -r <path>`).

Контракт error-dict — стандартный + `error_class="SECURITY"`:
    {file, line, column, message, code, severity, error_type, error_class}

Bandit JSON-формат:
    {
        "results": [{
            "filename": "/abs/x.py",
            "line_number": 10,
            "col_offset": 5,
            "test_id": "B105",
            "test_name": "hardcoded_password_string",
            "issue_severity": "MEDIUM",
            "issue_confidence": "HIGH",
            "issue_text": "Possible hardcoded password: 'secret123'",
            ...
        }],
        ...
    }
"""

from __future__ import annotations

import json
import logging
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


# Маппинг bandit severity → наш severity. Bandit отдаёт LOW/MEDIUM/HIGH;
# мы используем «error» для MEDIUM/HIGH (это блокирующие SECURITY) и
# «warning» для LOW.
_BANDIT_SEVERITY = {"HIGH": "error", "MEDIUM": "error", "LOW": "warning"}


class BanditAnalyzer:
    """Анализирует Python-проект через `bandit -r <path> -f json`.

    bandit опциональный; нет в PATH → `[]`. Контракт: error-dict с
    `error_class="SECURITY"` (поскольку bandit покрывает security-домен
    целиком), `code` = bandit test_id (`B101`, `B105`, `B602` и т.д.).
    """

    def __init__(self, timeout: int = 120):
        self.timeout = int(timeout)

    def available(self) -> bool:
        try:
            r = subprocess.run(
                ["bandit", "--version"],
                capture_output=True, timeout=5, check=False,
            )
            return r.returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def analyze(self, project_path: Path, files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """`files` — опциональный список путей для инкрементального анализа.
        Bandit анализирует AST файла без межмодульного контекста — сужение
        безопасно. None — обычный рекурсивный обход всего project_path."""
        project_path = Path(project_path)
        if not self.available():
            logger.warning("bandit not found in PATH - skipping")
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
            cmd = ["bandit", *targets, "-f", "json", "-q"]
        else:
            # Bandit умеет --exclude. Передаём список бэкап/служебных директорий.
            exclude_arg = ",".join(sorted(SKIP_DIRS))
            cmd = [
                "bandit", "-r", str(project_path),
                "-f", "json",
                "--exclude", exclude_arg,
                "-q",  # quiet — без прогрессовых строк
            ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(project_path),
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("bandit timeout")
            return []
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning("bandit failed: %s", e)
            return []

        # bandit возвращает rc != 0 при наличии findings — это норма, не ошибка.
        output = proc.stdout or ""
        errors = self._parse_json_output(output, project_path)
        logger.info("bandit: %d findings", len(errors))
        return errors

    @staticmethod
    def _parse_json_output(output: str, project_path: Path) -> List[Dict[str, Any]]:
        """Парсит JSON stdout bandit → список error-dict. Любой сбой → []."""
        if not (output or "").strip():
            return []
        try:
            data = json.loads(output)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        items = data.get("results")
        if not isinstance(items, list):
            return []

        errors: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            filename = it.get("filename") or ""
            test_id = (it.get("test_id") or "").strip()
            test_name = (it.get("test_name") or "").strip()
            message = (it.get("issue_text") or test_name or "").strip()
            line_num = int(it.get("line_number") or 0)
            col_num = int(it.get("col_offset") or 0)
            sev_raw = (it.get("issue_severity") or "MEDIUM").upper()
            severity = _BANDIT_SEVERITY.get(sev_raw, "error")

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
                "code": test_id,
                "severity": severity,
                "error_type": "security",
                "error_class": "SECURITY",
                # Bandit confidence (LOW/MEDIUM/HIGH) → не пишем в confidence
                # error-dict (там app-level confidence патча, не детектора),
                # но добавим в meta.
                "bandit_confidence": str(it.get("issue_confidence") or "").upper(),
                "bandit_test_name": test_name,
            })
        return errors
