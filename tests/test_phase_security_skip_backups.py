"""
SecurityScanner.SKIP_DIRS должен включать `.webbles_backups` и `.webbles`.

Контекст: в логах прогона на Python в списке ошибок мелькала запись
`[sql_injection] .webbles_backups\\auth.py:10 ...` — сканер тащил копию файла
из прошлого прогона. Это шум: пользователь не правит бэкапы, и реальная
ошибка уже учтена в основном файле.

Запуск: python3 tests/test_phase_security_skip_backups.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.security_scanner import SecurityScanner

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


_VULN_PY = (
    "import sqlite3\n"
    "def login(username, password):\n"
    "    conn = sqlite3.connect('x.db')\n"
    "    c = conn.cursor()\n"
    "    c.execute('SELECT * FROM users WHERE u=%s' % username)\n"
)


def test_backups_dir_skipped():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # боевой файл
        (root / "auth.py").write_text(_VULN_PY, encoding="utf-8")
        # копия в backups — НЕ должна попасть
        backups = root / ".webbles_backups"
        backups.mkdir()
        (backups / "auth.py").write_text(_VULN_PY, encoding="utf-8")
        # также проверим вложенную копию
        (backups / "sub" / "old.py").parent.mkdir(parents=True, exist_ok=True)
        (backups / "sub" / "old.py").write_text(_VULN_PY, encoding="utf-8")

        scanner = SecurityScanner()
        errs = scanner.scan_project(root, "python")
        files = sorted({e.get("file", "") for e in errs})
        check("backups_not_scanned",
              not any(".webbles_backups" in f for f in files))
        check("real_file_still_scanned",
              any(f.endswith("auth.py") and ".webbles_backups" not in f for f in files))


def test_other_skip_dirs():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "ok.py").write_text(_VULN_PY, encoding="utf-8")
        for skip in (".git", "__pycache__", "node_modules", ".vscode",
                     "venv", ".venv", "dist", "build", ".webbles", ".tox",
                     ".pytest_cache"):
            (root / skip).mkdir()
            (root / skip / "x.py").write_text(_VULN_PY, encoding="utf-8")
        scanner = SecurityScanner()
        errs = scanner.scan_project(root, "python")
        files = sorted({e.get("file", "") for e in errs})
        # ни одного файла из skip-каталогов
        bad = [f for f in files if any(seg in f.replace("\\", "/").split("/")
                                       for seg in {".git", "__pycache__", "node_modules",
                                                   ".vscode", "venv", ".venv", "dist", "build",
                                                   ".webbles", ".tox", ".pytest_cache"})]
        check("no_skip_dirs_in_results", bad == [])


if __name__ == "__main__":
    print("SecurityScanner SKIP_DIRS:")
    test_backups_dir_skipped()
    test_other_skip_dirs()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nSecurityScanner skip-dirs: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
