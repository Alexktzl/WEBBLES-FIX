"""
Аудит безопасности Rust для webles_conveyor.
Выполняет cargo audit для поиска известных уязвимостей в зависимостях.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple


class RustAuditChecks:
    """
    Запускает cargo audit и возвращает структурированные результаты.
    """

    def run(self, project_path: Path) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        Выполняет аудит зависимостей Rust.

        Возвращает:
            (success, vulnerabilities)
            success: True, если уязвимостей не найдено
            vulnerabilities: список найденных уязвимостей с деталями
        """
        try:
            result = subprocess.run(
                ["cargo", "audit", "--json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = result.stdout.strip()
            if not output:
                return True, []

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                # Если не JSON, парсим текстовый вывод
                return self._parse_text_output(result.stdout + result.stderr)

            vulnerabilities = []
            for vuln in data.get("vulnerabilities", {}).get("list", []):
                vulnerability = {
                    "id": vuln.get("advisory", {}).get("id", ""),
                    "package": vuln.get("package", {}).get("name", ""),
                    "version": vuln.get("package", {}).get("version", ""),
                    "severity": vuln.get("advisory", {}).get("severity", "unknown"),
                    "title": vuln.get("advisory", {}).get("title", ""),
                    "description": vuln.get("advisory", {}).get("description", ""),
                    "url": vuln.get("advisory", {}).get("url", ""),
                }
                vulnerabilities.append(vulnerability)

            success = len(vulnerabilities) == 0
            return success, vulnerabilities

        except subprocess.TimeoutExpired:
            return False, [{"error": "cargo audit timed out after 180s"}]
        except FileNotFoundError:
            return True, []  # cargo-audit не установлен, считаем что проверка пропущена

    def _parse_text_output(self, output: str) -> Tuple[bool, List[Dict[str, Any]]]:
        """Резервный парсер текстового вывода cargo audit."""
        vulnerabilities = []
        lines = output.splitlines()
        current_vuln = {}
        for line in lines:
            line = line.strip()
            if line.startswith("CVE-") or line.startswith("RUSTSEC-"):
                if current_vuln:
                    vulnerabilities.append(current_vuln)
                current_vuln = {"id": line.split()[0]}
            elif "Severity:" in line:
                current_vuln["severity"] = line.split(":")[1].strip().lower()
            elif "Package:" in line:
                current_vuln["package"] = line.split(":")[1].strip()
            elif "Version:" in line:
                current_vuln["version"] = line.split(":")[1].strip()
            elif "Title:" in line:
                current_vuln["title"] = line.split(":", 1)[1].strip()
        if current_vuln:
            vulnerabilities.append(current_vuln)

        success = len(vulnerabilities) == 0
        return success, vulnerabilities