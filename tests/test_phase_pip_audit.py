"""
pip-audit обёртка — парсер JSON + graceful когда тула нет.

Реальный pip-audit не запускаем (требует сети). Подменяем subprocess через
monkey-patch и проверяем только парсер и контракт error-dict'ов.

Запуск: python3 tests/test_phase_pip_audit.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.pip_audit_scan import PipAuditScanner

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ----------------------------------------------------------------------
# 1. Парсер JSON: реальный формат pip-audit
# ----------------------------------------------------------------------
def test_parser_real_format():
    payload = {
        "dependencies": [
            {
                "name": "requests",
                "version": "2.30.0",
                "vulns": [
                    {"id": "GHSA-1111-aaaa-2222",
                     "fix_versions": ["2.31.0"],
                     "description": "Critical regex DoS in cookies."}
                ],
            },
            {
                "name": "flask",
                "version": "3.0.0",
                "vulns": [],  # чисто
            },
            {
                "name": "yaml",
                "version": "5.4.1",
                "vulns": [
                    {"id": "PYSEC-2022-42", "fix_versions": [],
                     "description": "yaml.load arbitrary code execution."}
                ],
            },
        ]
    }
    with tempfile.TemporaryDirectory() as d:
        req = Path(d) / "requirements.txt"
        req.write_text("requests==2.30.0\nflask==3.0.0\npyyaml==5.4.1\n")
        out = PipAuditScanner._parse(json.dumps(payload), req)
        check("parse_count", len(out) == 2)
        codes = sorted(e["code"] for e in out)
        check("parse_codes",
              codes == ["cve_GHSA-1111-aaaa-2222", "cve_PYSEC-2022-42"])
        # контракт error-dict'а:
        for e in out:
            for key in ("file", "line", "message", "code", "severity",
                        "error_type", "error_class", "confidence", "autofixable"):
                check(f"parse_contract_{key}_{e['code']}", key in e)
            check(f"parse_class_{e['code']}", e["error_class"] == "SECURITY")
            check(f"parse_autofixable_off_{e['code']}", e["autofixable"] is False)
            check(f"parse_file_is_req_{e['code']}", e["file"] == "requirements.txt")
        # доп. поля
        req_e = [e for e in out if e["code"] == "cve_GHSA-1111-aaaa-2222"][0]
        check("parse_package_field", req_e.get("package") == "requests")
        check("parse_installed_version", req_e.get("installed_version") == "2.30.0")
        check("parse_fix_versions", req_e.get("fix_versions") == ["2.31.0"])


# ----------------------------------------------------------------------
# 2. Битый JSON → []
# ----------------------------------------------------------------------
def test_broken_json():
    with tempfile.TemporaryDirectory() as d:
        req = Path(d) / "requirements.txt"
        req.write_text("requests\n")
        out = PipAuditScanner._parse("not json", req)
        check("broken_json_empty", out == [])


# ----------------------------------------------------------------------
# 3. Тула нет → scan() возвращает []
# ----------------------------------------------------------------------
def test_no_pip_audit_graceful():
    sc = PipAuditScanner()
    # подменим `is_available` чтобы было False
    sc.is_available = lambda: False  # type: ignore
    with tempfile.TemporaryDirectory() as d:
        out = sc.scan(Path(d))
        check("no_tool_empty", out == [])


# ----------------------------------------------------------------------
# 4. Нет requirements.txt → []
# ----------------------------------------------------------------------
def test_no_requirements_graceful():
    sc = PipAuditScanner()
    sc.is_available = lambda: True  # type: ignore
    with tempfile.TemporaryDirectory() as d:
        out = sc.scan(Path(d))
        check("no_req_empty", out == [])


# ----------------------------------------------------------------------
# 5. Пустой `dependencies` → []
# ----------------------------------------------------------------------
def test_empty_deps_list():
    with tempfile.TemporaryDirectory() as d:
        req = Path(d) / "requirements.txt"
        req.write_text("requests\n")
        out = PipAuditScanner._parse(json.dumps({"dependencies": []}), req)
        check("empty_deps_empty", out == [])


if __name__ == "__main__":
    print("pip-audit smoke:")
    test_parser_real_format()
    test_broken_json()
    test_no_pip_audit_graceful()
    test_no_requirements_graceful()
    test_empty_deps_list()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\npip-audit: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
