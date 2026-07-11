"""
P.5 — PythonRuntimeAnalyzer: тесты модуля + интеграция в pipeline.

PythonRuntimeAnalyzer запускает реальный код Python-проекта в подпроцессе
(pytest -> unittest -> entrypoints) и парсит traceback, чтобы ловить
runtime-ошибки (NameError/TypeError/ImportError/AssertionError), которые
не видит статический анализ.

Покрывает:
  1) `_is_skipped_path` — служебные каталоги отсекаются.
  2) `_has_pytest_project`, `_has_unittest_project` — детекторы проекта.
  3) `_find_exception` — парсер строки исключения.
  4) `_find_relevant_frame` — берёт последний project-frame, не служебный.
  5) `_parse_runtime_output` — собирает error-dict, формат как у других
     анализаторов.
  6) `_runtime_env` — выставляет PYTHONDONTWRITEBYTECODE и
     WEBBLES_RUNTIME_ANALYZER.
  7) `_run_command` — graceful timeout, returncode==0 -> пустой список,
     FileNotFoundError -> пустой список.
  8) `analyze()` — pytest > unittest > entrypoints (диспетчер).
  9) AnalyzeStage._use_python_runtime_enabled — флаг on/off/default.

Запуск: python tests/test_phase_python_runtime.py
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# Заглушка для tomlkit.
try:
    import tomlkit  # noqa: F401
except ImportError:
    import types as _types
    sys.modules["tomlkit"] = _types.ModuleType("tomlkit")

from analyzers.python_runtime_analyzer import (
    PythonRuntimeAnalyzer,
    SKIP_DIRS,
    ENTRYPOINTS,
    _is_skipped_path,
)


results = []


def case(name, fn):
    try:
        fn()
        results.append((name, True, None))
        print(f"  [OK ] {name}")
    except AssertionError as e:
        results.append((name, False, str(e)))
        print(f"  [FAIL] {name}: {e}")
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  [ERR ] {name}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------
# 1) _is_skipped_path / SKIP_DIRS
# ---------------------------------------------------------------------
def test_skip_dirs_contains_essential():
    must = (
        ".webbles_backups", ".webbles", ".webbles_fix",
        ".webles_sandbox", ".webbles_sandbox",
        ".git", "__pycache__", "venv", ".venv",
        "node_modules", "dist", "build",
        ".pytest_cache", ".mypy_cache", "statistic",
    )
    for d in must:
        assert d in SKIP_DIRS, f"SKIP_DIRS missing: {d}"


def test_is_skipped_path_positive():
    assert _is_skipped_path(Path("project/.webles_sandbox/base/main.py"))
    assert _is_skipped_path(Path(".git/objects/pack/x"))
    assert _is_skipped_path(Path("project/venv/lib/site.py"))
    assert _is_skipped_path(Path("project/__pycache__/x.cpython-311.pyc"))


def test_is_skipped_path_negative():
    assert not _is_skipped_path(Path("project/main.py"))
    assert not _is_skipped_path(Path("project/auth/login.py"))
    assert not _is_skipped_path(Path("tests/test_auth.py"))


# ---------------------------------------------------------------------
# 2) детекторы проекта
# ---------------------------------------------------------------------
def test_has_pytest_project_tests_dir(tmp_path=None):
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tests").mkdir()
        assert PythonRuntimeAnalyzer._has_pytest_project(root) is True


def test_has_pytest_project_pyproject():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "pyproject.toml").write_text("[tool.pytest]\n")
        assert PythonRuntimeAnalyzer._has_pytest_project(root) is True


def test_has_pytest_project_test_file():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "test_auth.py").write_text("def test_x(): pass\n")
        assert PythonRuntimeAnalyzer._has_pytest_project(root) is True


def test_has_pytest_project_none():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "main.py").write_text("print(1)")
        assert PythonRuntimeAnalyzer._has_pytest_project(root) is False


def test_has_unittest_project():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "auth_test.py").write_text("import unittest\n")
        assert PythonRuntimeAnalyzer._has_unittest_project(root) is True


# ---------------------------------------------------------------------
# 3) _find_exception
# ---------------------------------------------------------------------
def test_find_exception_nameerror():
    out = (
        'Traceback (most recent call last):\n'
        '  File "main.py", line 5, in <module>\n'
        '    print(x)\n'
        "NameError: name 'x' is not defined\n"
    )
    m = PythonRuntimeAnalyzer._find_exception(out)
    assert m is not None
    assert m.group(1) == "NameError"
    assert "name 'x' is not defined" in m.group(2)


def test_find_exception_takes_last():
    out = (
        "TypeError: first error\n"
        "RuntimeError: second error\n"
        "AssertionError: last error\n"
    )
    m = PythonRuntimeAnalyzer._find_exception(out)
    assert m is not None
    assert m.group(1) == "AssertionError"


def test_find_exception_module_not_found():
    out = "ModuleNotFoundError: No module named 'foo'\n"
    m = PythonRuntimeAnalyzer._find_exception(out)
    assert m is not None
    assert m.group(1) == "ModuleNotFoundError"


def test_find_exception_none_in_clean_output():
    out = "Hello world\nrunning tests...\nPASS\n"
    m = PythonRuntimeAnalyzer._find_exception(out)
    assert m is None


# ---------------------------------------------------------------------
# 4) _find_relevant_frame
# ---------------------------------------------------------------------
def test_find_relevant_frame_relative():
    out = (
        '  File "auth/login.py", line 12, in login\n'
        '    return verify(password)\n'
        "NameError: name 'verify' is not defined\n"
    )
    p, ln = PythonRuntimeAnalyzer._find_relevant_frame(out, Path("."))
    assert p == "auth/login.py"
    assert ln == 12


def test_find_relevant_frame_skips_sandbox():
    out = (
        '  File "C:\\proj\\.webles_sandbox\\base\\main.py", line 5, in <module>\n'
        '  File "main.py", line 3, in <module>\n'
        "NameError: name 'x' is not defined\n"
    )
    p, ln = PythonRuntimeAnalyzer._find_relevant_frame(out, Path("."))
    # Должен взять main.py (не sandbox-копию).
    assert ".webles_sandbox" not in p
    assert "main.py" in p


def test_find_relevant_frame_skips_venv():
    out = (
        '  File "/proj/venv/lib/site-packages/x.py", line 10\n'
        '  File "myapp.py", line 7, in handler\n'
        "RuntimeError: oops\n"
    )
    p, ln = PythonRuntimeAnalyzer._find_relevant_frame(out, Path("."))
    assert "venv" not in p
    assert p == "myapp.py"
    assert ln == 7


def test_find_relevant_frame_empty():
    p, ln = PythonRuntimeAnalyzer._find_relevant_frame("no traceback here", Path("."))
    assert p == ""
    assert ln == 0


# ---------------------------------------------------------------------
# 5) _parse_runtime_output
# ---------------------------------------------------------------------
def test_parse_runtime_output_full():
    analyzer = PythonRuntimeAnalyzer()
    out = (
        'Traceback (most recent call last):\n'
        '  File "auth.py", line 13, in login\n'
        '    user = users[username]\n'
        "NameError: name 'users' is not defined\n"
    )
    errs = analyzer._parse_runtime_output(out, Path("."), source="pytest")
    assert len(errs) == 1
    e = errs[0]
    assert e["code"] == "NameError"
    assert "users" in e["message"]
    assert e["file"] == "auth.py"
    assert e["line"] == 13
    assert e["error_type"] == "runtime"
    assert e["severity"] == "error"
    assert e["source"] == "pytest"
    assert "Traceback" in e["traceback"]


def test_parse_runtime_output_empty():
    analyzer = PythonRuntimeAnalyzer()
    assert analyzer._parse_runtime_output("", Path("."), source="pytest") == []


def test_parse_runtime_output_no_exception_pattern():
    analyzer = PythonRuntimeAnalyzer()
    # Текст есть, но нет паттерна `XxxError: msg` — должны вернуть [].
    out = "Just some non-exception text\nrunning step 1\nrunning step 2\n"
    assert analyzer._parse_runtime_output(out, Path("."), source="pytest") == []


# ---------------------------------------------------------------------
# 6) _runtime_env
# ---------------------------------------------------------------------
def test_runtime_env_flags():
    env = PythonRuntimeAnalyzer._runtime_env()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["WEBBLES_RUNTIME_ANALYZER"] == "1"
    # PATH унаследован (или хотя бы есть)
    import os
    if "PATH" in os.environ:
        assert "PATH" in env


# ---------------------------------------------------------------------
# 7) _run_command (timeout / returncode / FileNotFoundError)
# ---------------------------------------------------------------------
def test_run_command_returncode_zero_returns_empty():
    analyzer = PythonRuntimeAnalyzer()
    fake = SimpleNamespace(returncode=0, stdout="ok", stderr="")
    with patch("subprocess.run", return_value=fake):
        out = analyzer._run_command(Path("."), ["python", "-c", "pass"],
                                     source="pytest", timeout=5)
    assert out == []


def test_run_command_timeout_produces_runtime_timeout():
    analyzer = PythonRuntimeAnalyzer()
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="python", timeout=5, output="partial\n", stderr="")
    with patch("subprocess.run", side_effect=boom):
        out = analyzer._run_command(Path("."), ["python", "-c", "while 1: pass"],
                                     source="pytest", timeout=5)
    assert len(out) == 1
    assert out[0]["code"] == "RuntimeTimeout"
    assert out[0]["error_type"] == "runtime"
    assert "timed out" in out[0]["message"]


def test_run_command_file_not_found_silent():
    analyzer = PythonRuntimeAnalyzer()
    with patch("subprocess.run", side_effect=FileNotFoundError("no python")):
        out = analyzer._run_command(Path("."), ["python", "x"], source="pytest", timeout=5)
    assert out == []


def test_run_command_returncode_nonzero_with_traceback():
    analyzer = PythonRuntimeAnalyzer()
    tb = (
        'Traceback (most recent call last):\n'
        '  File "main.py", line 5, in <module>\n'
        "NameError: name 'foo' is not defined\n"
    )
    fake = SimpleNamespace(returncode=1, stdout="", stderr=tb)
    with patch("subprocess.run", return_value=fake):
        out = analyzer._run_command(Path("."), ["python", "main.py"],
                                     source="pytest", timeout=5)
    assert len(out) == 1
    assert out[0]["code"] == "NameError"
    assert out[0]["file"] == "main.py"
    assert out[0]["line"] == 5


def test_run_command_returncode_nonzero_unparseable_returns_empty():
    """Если код != 0, но traceback не распознан — пустой список (а не мусор)."""
    analyzer = PythonRuntimeAnalyzer()
    fake = SimpleNamespace(returncode=1, stdout="usage: ...\nerror: arg required\n",
                           stderr="")
    with patch("subprocess.run", return_value=fake):
        out = analyzer._run_command(Path("."), ["python", "main.py"],
                                     source="pytest", timeout=5)
    assert out == []


# ---------------------------------------------------------------------
# 8) analyze() dispatcher
# ---------------------------------------------------------------------
def test_analyze_uses_pytest_when_detected():
    analyzer = PythonRuntimeAnalyzer()
    with patch.object(PythonRuntimeAnalyzer, "_has_pytest_project", return_value=True), \
         patch.object(PythonRuntimeAnalyzer, "_run_pytest",
                      return_value=[{"code": "NameError", "file": "a.py", "line": 1,
                                     "message": "x", "severity": "error",
                                     "error_type": "runtime", "source": "pytest",
                                     "traceback": "..."}]) as mp:
        errs = analyzer.analyze(Path("."))
    assert mp.called
    assert errs and errs[0]["code"] == "NameError"


def test_analyze_falls_through_to_unittest():
    analyzer = PythonRuntimeAnalyzer()
    with patch.object(PythonRuntimeAnalyzer, "_has_pytest_project", return_value=False), \
         patch.object(PythonRuntimeAnalyzer, "_has_unittest_project", return_value=True), \
         patch.object(PythonRuntimeAnalyzer, "_run_unittest",
                      return_value=[{"code": "AssertionError", "file": "t.py", "line": 2,
                                     "message": "fail", "severity": "error",
                                     "error_type": "runtime", "source": "unittest",
                                     "traceback": "..."}]) as mu:
        errs = analyzer.analyze(Path("."))
    assert mu.called
    assert errs and errs[0]["code"] == "AssertionError"


def test_analyze_falls_through_to_entrypoints():
    analyzer = PythonRuntimeAnalyzer()
    with patch.object(PythonRuntimeAnalyzer, "_has_pytest_project", return_value=False), \
         patch.object(PythonRuntimeAnalyzer, "_has_unittest_project", return_value=False), \
         patch.object(PythonRuntimeAnalyzer, "_run_entrypoints",
                      return_value=[{"code": "ImportError", "file": "main.py", "line": 3,
                                     "message": "no module", "severity": "error",
                                     "error_type": "runtime",
                                     "source": "entrypoint:main.py",
                                     "traceback": "..."}]) as me:
        errs = analyzer.analyze(Path("."))
    assert me.called
    assert errs[0]["source"].startswith("entrypoint:")


def test_run_entrypoints_skips_missing_files():
    import tempfile
    analyzer = PythonRuntimeAnalyzer()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # Никаких entrypoint'ов нет -> пустой список.
        assert analyzer._run_entrypoints(root) == []


def test_run_entrypoints_first_existing_used():
    import tempfile
    analyzer = PythonRuntimeAnalyzer()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "main.py").write_text("print('ok')\n")
        called = []
        def fake(self, project_path, command, source, timeout):
            called.append(source)
            return [{"code": "NameError", "file": "main.py", "line": 1,
                     "message": "x", "severity": "error", "error_type": "runtime",
                     "source": source, "traceback": "..."}]
        with patch.object(PythonRuntimeAnalyzer, "_run_command", fake):
            out = analyzer._run_entrypoints(root)
        assert called and called[0] == "entrypoint:main.py"
        assert out and out[0]["source"] == "entrypoint:main.py"


# ---------------------------------------------------------------------
# 9) AnalyzeStage._use_python_runtime_enabled
# ---------------------------------------------------------------------
def test_use_python_runtime_enabled_flag():
    from core.stages.analyze_stage import AnalyzeStage

    def mk(value):
        return SimpleNamespace(config={"pipeline": {"use_python_runtime": value}})

    assert AnalyzeStage._use_python_runtime_enabled(mk(True)) is True
    assert AnalyzeStage._use_python_runtime_enabled(mk(False)) is False
    # default — отсутствие ключа -> False
    assert AnalyzeStage._use_python_runtime_enabled(
        SimpleNamespace(config={"pipeline": {}})
    ) is False
    # config отсутствует
    assert AnalyzeStage._use_python_runtime_enabled(
        SimpleNamespace(config=None)
    ) is False


def test_entrypoints_constant_complete():
    """ENTRYPOINTS должен содержать основные имена точек входа Python-проектов."""
    must = {"main.py", "app.py", "run.py", "manage.py", "server.py"}
    assert must.issubset(set(ENTRYPOINTS)), f"ENTRYPOINTS missing: {must - set(ENTRYPOINTS)}"


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("P.5 PythonRuntimeAnalyzer regression")
    print("=" * 60)

    case("skip_dirs: contains essential entries", test_skip_dirs_contains_essential)
    case("is_skipped_path: positive (sandbox/git/venv/__pycache__)", test_is_skipped_path_positive)
    case("is_skipped_path: negative (clean paths)", test_is_skipped_path_negative)

    case("_has_pytest_project: tests/ dir", test_has_pytest_project_tests_dir)
    case("_has_pytest_project: pyproject.toml", test_has_pytest_project_pyproject)
    case("_has_pytest_project: test_*.py file", test_has_pytest_project_test_file)
    case("_has_pytest_project: none -> False", test_has_pytest_project_none)
    case("_has_unittest_project: *_test.py", test_has_unittest_project)

    case("_find_exception: NameError", test_find_exception_nameerror)
    case("_find_exception: takes last", test_find_exception_takes_last)
    case("_find_exception: ModuleNotFoundError", test_find_exception_module_not_found)
    case("_find_exception: none on clean output", test_find_exception_none_in_clean_output)

    case("_find_relevant_frame: relative path", test_find_relevant_frame_relative)
    case("_find_relevant_frame: skips .webles_sandbox", test_find_relevant_frame_skips_sandbox)
    case("_find_relevant_frame: skips venv", test_find_relevant_frame_skips_venv)
    case("_find_relevant_frame: empty input", test_find_relevant_frame_empty)

    case("_parse_runtime_output: full error dict", test_parse_runtime_output_full)
    case("_parse_runtime_output: empty input", test_parse_runtime_output_empty)
    case("_parse_runtime_output: no exception pattern -> []", test_parse_runtime_output_no_exception_pattern)

    case("_runtime_env: PYTHONDONTWRITEBYTECODE/UNBUFFERED/WEBBLES flags", test_runtime_env_flags)

    case("_run_command: returncode 0 -> []", test_run_command_returncode_zero_returns_empty)
    case("_run_command: timeout -> RuntimeTimeout entry", test_run_command_timeout_produces_runtime_timeout)
    case("_run_command: FileNotFoundError silent -> []", test_run_command_file_not_found_silent)
    case("_run_command: rc!=0 with traceback -> parsed", test_run_command_returncode_nonzero_with_traceback)
    case("_run_command: rc!=0 unparseable -> []", test_run_command_returncode_nonzero_unparseable_returns_empty)

    case("analyze: uses pytest when detected", test_analyze_uses_pytest_when_detected)
    case("analyze: falls through to unittest", test_analyze_falls_through_to_unittest)
    case("analyze: falls through to entrypoints", test_analyze_falls_through_to_entrypoints)
    case("_run_entrypoints: no files -> []", test_run_entrypoints_skips_missing_files)
    case("_run_entrypoints: first existing used", test_run_entrypoints_first_existing_used)

    case("AnalyzeStage._use_python_runtime_enabled: flag on/off/default/None", test_use_python_runtime_enabled_flag)
    case("ENTRYPOINTS: main/app/run/manage/server", test_entrypoints_constant_complete)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\nP.5 PythonRuntimeAnalyzer: {passed}/{total} pass")
    if passed != total:
        for name, ok, err in results:
            if not ok:
                print(f"  - {name}: {err}")
        sys.exit(1)
