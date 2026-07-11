"""
Stage K.8 — обёртка над `pip-audit` для поиска уязвимостей в pinned зависимостях.

`pip-audit` — внешний тул (см. https://pypi.org/project/pip-audit/). Если он
не установлен или его не оказалось в PATH — мы тихо возвращаем `[]` и НЕ
ломаем анализ. По аналогии с `cargo audit` для Rust.

Выдача: список error-dict'ов с `code = "cve_<id>"`, `error_class = "SECURITY"`,
`error_type = "security"`, `confidence = 0.9`. UI/DECIDE их не автофиксят —
они идут в `needs_review` с указанием CVE, фикса и рекомендуемой версии.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PipAuditScanner:
    """Тонкая обёртка. Чистый детерминированный сканер: один subprocess + парсер."""

    EXECUTABLE = "pip-audit"
    TIMEOUT_SECONDS = 120

    def is_available(self) -> bool:
        return shutil.which(self.EXECUTABLE) is not None

    def scan(self, project_path: Path, *,
             requirements: str = "requirements.txt") -> List[Dict[str, Any]]:
        """Запускает `pip-audit -r requirements.txt -f json` и возвращает
        список error-dict'ов. Если тула нет или вывод не парсится — `[]`.
        """
        if not self.is_available():
            logger.debug("pip-audit не найден в PATH — пропуск")
            return []
        req_path = Path(project_path) / requirements
        if not req_path.exists():
            logger.debug("pip-audit: %s отсутствует — пропуск", requirements)
            return []
        try:
            r = subprocess.run(
                [self.EXECUTABLE, "-r", str(req_path), "-f", "json",
                 "--strict", "--no-deps"],
                cwd=str(project_path),
                capture_output=True, text=True, timeout=self.TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning("pip-audit: таймаут")
            return []
        except Exception as e:
            logger.warning("pip-audit упал: %s", e)
            return []
        # pip-audit exit 1 = найдены уязвимости; exit 0 = чисто.
        # Любой другой код считаем неуспехом.
        if r.returncode not in (0, 1):
            logger.debug("pip-audit returncode=%s stderr=%s",
                         r.returncode, (r.stderr or "")[:200])
            return []
        return self._parse(r.stdout or "", req_path)

    @staticmethod
    def _parse(raw: str, req_path: Path) -> List[Dict[str, Any]]:
        try:
            data = json.loads(raw)
        except Exception as e:
            logger.debug("pip-audit: невалидный JSON: %s", e)
            return []
        # pip-audit может отдать корнем либо dict, либо плоский список
        # `[{"name": ..., "vulns": [...]}, ...]` (старые версии). Аккуратно
        # обрабатываем оба варианта без AttributeError.
        out: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            deps = data.get("dependencies")
        elif isinstance(data, list):
            deps = data
        else:
            return []
        if not isinstance(deps, list):
            return []
        rel_req = req_path.name
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            name = str(dep.get("name") or "?")
            ver = str(dep.get("version") or "?")
            vulns = dep.get("vulns") or []
            if not isinstance(vulns, list):
                continue
            for v in vulns:
                if not isinstance(v, dict):
                    continue
                vid = str(v.get("id") or "")
                if not vid:
                    continue
                fix_vers = v.get("fix_versions") or []
                fix_tag = (" — фикс в " + ", ".join(map(str, fix_vers))
                           if fix_vers else "")
                desc = str(v.get("description") or "").replace("\n", " ")
                out.append({
                    "file": rel_req,
                    "line": 0,
                    "column": 0,
                    "message": f"{name} {ver}: {vid}{fix_tag}. {desc[:300]}",
                    "code": f"cve_{vid}",
                    "severity": "high",
                    "error_type": "security",
                    "error_class": "SECURITY",
                    "confidence": 0.9,
                    "autofixable": False,
                    "package": name,
                    "installed_version": ver,
                    "fix_versions": list(map(str, fix_vers)),
                })
        return out
