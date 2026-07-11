"""
O.12: language analyzers (Python/JS/Go/Kotlin/TS/Java/C++/C#) должны
исключать `.webbles_backups`, `.webbles` и стандартные служебные каталоги
из анализа. Без этого flake8/AST/go build/etc. видят копии повреждённых
файлов из прошлого прогона как боевой код, и движок начинает чинить
бэкапы вместо реальных файлов проекта (наблюдалось в логе 2026-06-02 —
35-минутный прогон починил 0 реальных файлов).

Этот suite проверяет два уровня защиты:
1) Константа `SKIP_DIRS` каждого analyzer'а содержит `.webbles_backups`
   и `.webbles` (+ стандартный набор: `node_modules`, `target`, `build`,
   `dist`, `__pycache__`, `.idea`, `.vscode`, `.git`).
2) `PythonAnalyzer._run_ast_analysis` физически НЕ заходит в
   `.webbles_backups/*.py` даже если flake8 не установлен (фоллбэк-путь).
3) flake8-команда (если flake8 не установлен — пропускаем) передаёт
   `--exclude=...,.webbles_backups,.webbles,...`.

Запуск: python3 tests/test_phase_analyzer_skip_backups.py
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True


results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------------
# 1. Все analyzer'ы содержат `.webbles_backups` и `.webbles` в SKIP_DIRS
# ---------------------------------------------------------------------

ANALYZERS_AND_SETS = [
    # (module_path, attr_name)
    ("analyzers.python_analyzer", "SKIP_DIRS"),
    ("analyzers.js_analyzer", "SKIP_DIRS"),
    ("analyzers.go_analyzer", "_SKIP_DIRS"),
    ("analyzers.kotlin_analyzer", "_SKIP_DIRS"),
    ("analyzers.typescript_analyzer", "_SKIP_DIRS"),
    ("analyzers.cpp_static_extra", "_SKIP_DIRS"),
    ("analyzers.cpp_analyzer", "_CPP_SKIP_DIRS"),
]


def test_skip_dirs_present():
    for mod_path, attr in ANALYZERS_AND_SETS:
        try:
            mod = importlib.import_module(mod_path)
        except Exception as e:
            check(f"{mod_path}: importable", False)
            print(f"      import error: {e}")
            continue
        s = getattr(mod, attr, None)
        if s is None:
            check(f"{mod_path}: has {attr}", False)
            continue
        s = set(s)
        check(f"{mod_path}.{attr} has .webbles_backups",
              ".webbles_backups" in s)
        check(f"{mod_path}.{attr} has .webbles",
              ".webbles" in s)


# ---------------------------------------------------------------------
# 2. PythonAnalyzer._run_ast_analysis НЕ заходит в `.webbles_backups`
# ---------------------------------------------------------------------

_BROKEN_PY = "def foo(\n    pass\n"


def test_python_ast_skips_backups():
    from analyzers.python_analyzer import PythonAnalyzer, SKIP_DIRS

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # боевой файл — валидный
        (root / "main.py").write_text("x = 1\n", encoding="utf-8")
        # копия в backups — сломана
        backups = root / ".webbles_backups"
        backups.mkdir()
        (backups / "main.py").write_text(_BROKEN_PY, encoding="utf-8")
        # и вложенная копия
        (backups / "sub").mkdir()
        (backups / "sub" / "old.py").write_text(_BROKEN_PY, encoding="utf-8")
        # сломанный файл в venv — тоже skip
        (root / "venv").mkdir()
        (root / "venv" / "lib.py").write_text(_BROKEN_PY, encoding="utf-8")

        an = PythonAnalyzer()
        errs = an._run_ast_analysis(root)
        files = [e.get("file", "").replace("\\", "/") for e in errs]
        check("python_ast: no .webbles_backups in results",
              not any(".webbles_backups" in f for f in files))
        check("python_ast: no venv in results",
              not any("/venv/" in f or f.startswith("venv/") for f in files))


def test_python_ast_finds_real_broken():
    """Контрольный сценарий: реальный битый файл всё ещё ловится."""
    from analyzers.python_analyzer import PythonAnalyzer

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "broken.py").write_text(_BROKEN_PY, encoding="utf-8")
        an = PythonAnalyzer()
        errs = an._run_ast_analysis(root)
        check("python_ast: real broken file detected",
              any("broken.py" in e.get("file", "") for e in errs))


# ---------------------------------------------------------------------
# 3. flake8 — команда содержит --exclude=...,.webbles_backups,...
# ---------------------------------------------------------------------

class _FakeRunResult:
    def __init__(self):
        self.stdout = ""
        self.stderr = ""
        self.returncode = 0


def test_flake8_command_has_exclude():
    from analyzers.python_analyzer import PythonAnalyzer

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeRunResult()

    an = PythonAnalyzer()
    # Принудительно считаем flake8 доступным.
    with patch.object(PythonAnalyzer, "_flake8_available", return_value=True), \
         patch("analyzers.python_analyzer.subprocess.run", side_effect=fake_run):
        an._run_flake8_analysis(Path("/tmp/nonexistent_project"))

    cmd = captured.get("cmd") or []
    exclude_arg = None
    for tok in cmd:
        if isinstance(tok, str) and tok.startswith("--exclude="):
            exclude_arg = tok[len("--exclude="):]
            break
    check("flake8: --exclude=... передан", exclude_arg is not None)
    if exclude_arg:
        parts = set(exclude_arg.split(","))
        check("flake8: exclude содержит .webbles_backups",
              ".webbles_backups" in parts)
        check("flake8: exclude содержит .webbles",
              ".webbles" in parts)
        check("flake8: exclude содержит __pycache__",
              "__pycache__" in parts)
        check("flake8: exclude содержит venv",
              "venv" in parts)
        check("flake8: exclude содержит node_modules",
              "node_modules" in parts)


# ---------------------------------------------------------------------
# 4. JavaAnalyzer + CsharpAnalyzer + CppAnalyzer: inline SKIP при rglob
# ---------------------------------------------------------------------

def test_java_inline_skip():
    """JavaAnalyzer фильтрует .java через локальную _SKIP tuple."""
    from analyzers import java_analyzer
    src = Path(java_analyzer.__file__).read_text(encoding="utf-8")
    check("java_analyzer.py: skip .webbles_backups",
          ".webbles_backups" in src)
    check("java_analyzer.py: skip .webbles",
          ".webbles" in src)


def test_csharp_inline_skip():
    from analyzers import csharp_analyzer
    src = Path(csharp_analyzer.__file__).read_text(encoding="utf-8")
    check("csharp_analyzer.py: skip .webbles_backups",
          ".webbles_backups" in src)
    check("csharp_analyzer.py: skip .webbles",
          ".webbles" in src)


def test_cpp_uses_skip():
    """CppAnalyzer.analyze фильтрует source_files через _cpp_excluded."""
    from analyzers import cpp_analyzer
    src = Path(cpp_analyzer.__file__).read_text(encoding="utf-8")
    check("cpp_analyzer.py: _cpp_excluded helper",
          "_cpp_excluded" in src)
    check("cpp_analyzer.py: rglob фильтруется",
          "if not _cpp_excluded" in src)


if __name__ == "__main__":
    print("Analyzer SKIP_DIRS / exclude (.webbles_backups, .webbles, ...):")
    test_skip_dirs_present()
    test_python_ast_skips_backups()
    test_python_ast_finds_real_broken()
    test_flake8_command_has_exclude()
    test_java_inline_skip()
    test_csharp_inline_skip()
    test_cpp_uses_skip()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nAnalyzer skip-backups: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
