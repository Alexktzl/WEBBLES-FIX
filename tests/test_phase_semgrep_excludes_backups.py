"""
Расследование scan-growth (2026-07-02): semgrep был единственным
анализатором без исключения служебных каталогов — финальный
independent_scan_after считал ошибки в `.webbles_backups/` (до-фиксовые
копии исправленных файлов) и завышался ровно на исправленное:
tenacity реально 75→67 (−8), метрика показывала 75→80 (+5).

Фикс: --exclude для .webbles_backups и прочих служебных каталогов —
тот же набор-политика, что у flake8 (O.12)/mypy/bandit/security_scanner.

Запуск: python tests/test_phase_semgrep_excludes_backups.py
"""

import sys
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from tools.semgrep_analyzer import SemgrepAnalyzer  # noqa: E402


def test_semgrep_cmd_excludes_service_dirs():
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        r.stdout = '{"results": []}'
        return r

    analyzer = SemgrepAnalyzer()
    analyzer._available_cache = True  # пропускаем ensure_installed
    with mock.patch("subprocess.run", side_effect=fake_run):
        analyzer.run(Path("/fake/project"))

    cmd = captured["cmd"]
    for required in (".webbles_backups", ".webbles_fix", ".git", "__pycache__"):
        assert f"--exclude={required}" in cmd, (
            f"semgrep обязан исключать {required}: {cmd}"
        )
    # Целевая директория — последним аргументом (после всех exclude).
    assert cmd[-1].endswith("project"), cmd


def test_semgrep_parses_results_unchanged():
    analyzer = SemgrepAnalyzer()
    analyzer._available_cache = True
    payload = '{"results": [{"check_id": "x", "path": "a.py"}]}'
    with mock.patch("subprocess.run",
                    return_value=mock.MagicMock(returncode=0, stdout=payload)):
        out = analyzer.run(Path("/fake"))
    assert out == [{"check_id": "x", "path": "a.py"}]


if __name__ == "__main__":
    failed = 0
    for name, fn in [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    sys.exit(0 if failed == 0 else 1)
