"""
P.2 (BanditAnalyzer) + P.3 (PythonExplainProvider) + P.4 (ruff --fix
автофикс в RuleBasedFixer) — финальные блоки паритета Python с Rust.

Покрытие:
  1) BanditAnalyzer:
     - parse JSON output (одна находка / несколько / пусто / битый JSON);
     - severity маппинг LOW/MEDIUM/HIGH -> warning/error;
     - skip `.webbles_backups`, `__pycache__` и т.п.;
     - graceful если bandit не в PATH;
     - `_use_bandit_enabled` флаг.

  2) PythonExplainProvider:
     - mypy-код -> встроенное описание (имя+URL+snippet);
     - не-mypy-, не-ruff-код -> []; не-Python язык -> [];
     - ruff-код -> если ruff не в PATH, fallback или None;
     - регистрация в `_build_provider` под именами python_explain / ruff_explain.

  3) RuleBasedFixer._py_ruff_autofix:
     - покрывает диспетчер для UP006/I001/SIM102/B007/C408 (через `_dispatch`);
     - без ruff в PATH -> None;
     - mock subprocess: ruff "починил" файл -> EditSet kind=replace_file;
     - ruff ничего не изменил -> None.

Запуск: python3 tests/test_phase_p_bandit_explain_autofix.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types as _types
    sys.modules["tomlkit"] = _types.ModuleType("tomlkit")

from analyzers.bandit_analyzer import (
    BanditAnalyzer, _BANDIT_SEVERITY, SKIP_DIRS, _is_skipped_path,
)
from analysis.external_examples.python_explain import (
    PythonExplainProvider, _RUFF_CODE_RE, _MYPY_CODE_RE,
)
from analysis.external_examples.provider import _build_provider
from fixers.rule_based_fixer import RuleBasedFixer, RULE_CONFIDENCE
from fixers.structured_edit import EditSet, Edit

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------------
# 1. BanditAnalyzer — парсер JSON
# ---------------------------------------------------------------------

def _bandit_finding(test_id, file, line, message, severity="MEDIUM",
                    confidence="HIGH"):
    return {
        "filename": file,
        "test_id": test_id,
        "test_name": "test_" + test_id.lower(),
        "issue_text": message,
        "issue_severity": severity,
        "issue_confidence": confidence,
        "line_number": line,
        "col_offset": 0,
    }


def test_bandit_parse_one():
    out = json.dumps({"results": [
        _bandit_finding("B105", "auth.py", 10, "Possible hardcoded password"),
    ]})
    errors = BanditAnalyzer._parse_json_output(out, Path("."))
    check("bandit: one finding", len(errors) == 1)
    if errors:
        e = errors[0]
        check("bandit: code B105", e["code"] == "B105")
        check("bandit: file auth.py", e["file"].endswith("auth.py"))
        check("bandit: line 10", e["line"] == 10)
        check("bandit: severity=error (MEDIUM)", e["severity"] == "error")
        check("bandit: error_class=SECURITY",
              e["error_class"] == "SECURITY")
        check("bandit: error_type=security",
              e["error_type"] == "security")


def test_bandit_parse_multiple():
    out = json.dumps({"results": [
        _bandit_finding("B101", "a.py", 1, "assert used", severity="LOW"),
        _bandit_finding("B602", "a.py", 5, "subprocess shell=True", severity="HIGH"),
        _bandit_finding("B301", "a.py", 10, "pickle", severity="MEDIUM"),
    ]})
    errors = BanditAnalyzer._parse_json_output(out, Path("."))
    check("bandit(multi): 3 findings", len(errors) == 3)
    if len(errors) == 3:
        check("bandit(multi): LOW -> warning",
              errors[0]["severity"] == "warning")
        check("bandit(multi): HIGH -> error",
              errors[1]["severity"] == "error")
        check("bandit(multi): MEDIUM -> error",
              errors[2]["severity"] == "error")


def test_bandit_parse_empty():
    check("bandit(empty): []",
          BanditAnalyzer._parse_json_output("", Path(".")) == [])
    check("bandit(empty results): []",
          BanditAnalyzer._parse_json_output(
              json.dumps({"results": []}), Path(".")) == [])


def test_bandit_parse_invalid_json():
    check("bandit(invalid): []",
          BanditAnalyzer._parse_json_output("{garbage", Path(".")) == [])


def test_bandit_severity_map():
    check("severity HIGH -> error", _BANDIT_SEVERITY["HIGH"] == "error")
    check("severity MEDIUM -> error", _BANDIT_SEVERITY["MEDIUM"] == "error")
    check("severity LOW -> warning", _BANDIT_SEVERITY["LOW"] == "warning")


def test_bandit_skip_dirs():
    check("bandit SKIP_DIRS has .webbles_backups",
          ".webbles_backups" in SKIP_DIRS)
    check("bandit _is_skipped: backup",
          _is_skipped_path(Path(".webbles_backups/x.py")))
    check("bandit _is_skipped: normal",
          not _is_skipped_path(Path("src/main.py")))


def test_bandit_parse_filters_backup():
    out = json.dumps({"results": [
        _bandit_finding("B105", ".webbles_backups/x.py", 1, "msg"),
        _bandit_finding("B105", "src/real.py", 1, "msg"),
    ]})
    errors = BanditAnalyzer._parse_json_output(out, Path("."))
    check("bandit: backup filtered", len(errors) == 1)
    if errors:
        check("bandit: real kept",
              errors[0]["file"].endswith("real.py"))


def test_bandit_graceful_no_binary():
    an = BanditAnalyzer()
    with patch.object(BanditAnalyzer, "available", return_value=False):
        out = an.analyze(Path("/tmp/no_such"))
    check("bandit: no binary -> []", out == [])


def test_bandit_flag_on_off():
    from core.stages.analyze_stage import AnalyzeStage
    ctx_off = SimpleNamespace(config={"pipeline": {}})
    ctx_on = SimpleNamespace(config={"pipeline": {"use_bandit": True}})
    check("bandit flag: default off",
          AnalyzeStage._use_bandit_enabled(ctx_off) is False)
    check("bandit flag: explicit on",
          AnalyzeStage._use_bandit_enabled(ctx_on) is True)


# ---------------------------------------------------------------------
# 2. PythonExplainProvider
# ---------------------------------------------------------------------

def test_python_explain_mypy_code():
    p = PythonExplainProvider()
    out = p.search({"code": "name-defined", "file": "x.py"},
                   "python", 3)
    check("explain(mypy): 1 example", len(out) == 1)
    if out:
        ex = out[0]
        check("explain(mypy): source", ex.source == "python_explain")
        check("explain(mypy): title contains code",
              "name-defined" in ex.title)
        check("explain(mypy): url docs",
              "mypy.readthedocs.io" in ex.url)
        check("explain(mypy): snippet non-empty",
              len(ex.snippet) > 10)


def test_python_explain_mypy_unknown_code():
    """Неизвестный mypy-код -> []."""
    p = PythonExplainProvider()
    out = p.search({"code": "totally-unknown-code-xyz"}, "python", 3)
    check("explain(mypy unknown): []", out == [])


def test_python_explain_ruff_no_binary():
    """Без ruff в PATH -> ruff-код не даёт results."""
    p = PythonExplainProvider()
    with patch("analysis.external_examples.python_explain.shutil.which",
               return_value=None):
        out = p.search({"code": "F401"}, "python", 3)
    check("explain(ruff, no binary): []", out == [])


def test_python_explain_ruff_with_mock():
    """С моком subprocess: ruff rule вернул описание."""
    p = PythonExplainProvider()
    fake_stdout = """# unused-import (F401)

Derived from the **Pyflakes** linter.

Fix is sometimes available.

## What it does
Checks for unused imports.

## Example
```python
import os  # F401
```

## Fix
```python
# (remove the line)
```
"""
    fake_proc = MagicMock(returncode=0, stdout=fake_stdout, stderr="")
    with patch("analysis.external_examples.python_explain.shutil.which",
               return_value="/usr/bin/ruff"), \
         patch("analysis.external_examples.python_explain.subprocess.run",
               return_value=fake_proc):
        out = p.search({"code": "F401"}, "python", 3)
    check("explain(ruff): 1 example", len(out) == 1)
    if out:
        check("explain(ruff): url ruff docs",
              "docs.astral.sh/ruff" in out[0].url)
        check("explain(ruff): snippet non-empty",
              len(out[0].snippet) > 0)


def test_python_explain_non_python_language():
    p = PythonExplainProvider()
    out = p.search({"code": "name-defined"}, "rust", 3)
    check("explain(rust): []", out == [])


def test_python_explain_empty_code():
    p = PythonExplainProvider()
    out = p.search({"code": ""}, "python", 3)
    check("explain(empty code): []", out == [])


def test_python_explain_registered_in_factory():
    """`_build_provider('python_explain')` создаёт PythonExplainProvider."""
    p = _build_provider("python_explain")
    check("factory: python_explain", p is not None)
    if p is not None:
        check("factory: name", p.name == "python_explain")
    # Alias
    p2 = _build_provider("ruff_explain")
    check("factory: ruff_explain alias", p2 is not None)
    p3 = _build_provider("mypy_explain")
    check("factory: mypy_explain alias", p3 is not None)


def test_python_explain_code_regex():
    """RUFF_CODE_RE и MYPY_CODE_RE точные."""
    for code in ("F401", "B006", "S102", "UP006", "I001", "SIM102", "PLE0101"):
        check(f"ruff_re matches {code}",
              _RUFF_CODE_RE.match(code) is not None)
    for code in ("name-defined", "attr-defined", "arg-type", "return-value"):
        check(f"mypy_re matches {code}",
              _MYPY_CODE_RE.match(code) is not None)
    # И не пересекаются.
    check("ruff_re NOT matches name-defined",
          _RUFF_CODE_RE.match("name-defined") is None)


# ---------------------------------------------------------------------
# 3. RuleBasedFixer._py_ruff_autofix (P.4)
# ---------------------------------------------------------------------

def test_rule_based_dispatch_ruff_codes():
    """`_dispatch` для P.4-кодов возвращает `_py_ruff_autofix`."""
    rbf = RuleBasedFixer()
    for code in ("UP006", "UP032", "I001", "SIM102", "B007", "C408",
                 "RET504", "COM812"):
        handler = rbf._dispatch(code, "python")
        check(f"dispatch({code}): _py_ruff_autofix",
              handler is not None
              and getattr(handler, "__name__", "") == "_py_ruff_autofix")


def test_rule_based_dispatch_unknown_python_code():
    rbf = RuleBasedFixer()
    handler = rbf._dispatch("NOT_A_REAL_CODE", "python")
    check("dispatch(unknown): None", handler is None)


def test_py_ruff_autofix_no_binary():
    """Без ruff в PATH -> None."""
    rbf = RuleBasedFixer()
    err = {"code": "UP006", "file": "x.py", "line": 1}
    with patch("fixers.rule_based_fixer.shutil.which", return_value=None):
        r = rbf._py_ruff_autofix(err, "x: List[int] = []\n")
    check("ruff_autofix(no binary): None", r is None)


def test_py_ruff_autofix_no_change():
    """ruff запущен, но ничего не изменил -> None."""
    rbf = RuleBasedFixer()
    err = {"code": "UP006", "file": "x.py", "line": 1}
    src = "x = 1\n"

    def fake_run(cmd, **kwargs):
        # Эмулируем: ruff "запустился", файл не тронут.
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("fixers.rule_based_fixer.shutil.which", return_value="/usr/bin/ruff"), \
         patch("fixers.rule_based_fixer.subprocess.run", side_effect=fake_run):
        r = rbf._py_ruff_autofix(err, src)
    check("ruff_autofix(no change): None", r is None)


def test_py_ruff_autofix_success():
    """ruff изменил временный файл -> EditSet с kind='replace_file'."""
    rbf = RuleBasedFixer()
    src = "from typing import List\n\nx: List[int] = []\n"
    fixed = "x: list[int] = []\n"
    err = {"code": "UP006", "file": "src/m.py", "line": 3}

    def fake_run(cmd, **kwargs):
        # Эмуляция: записываем "fixed" в tmp_file (последний аргумент).
        tmp_path = cmd[-1]
        Path(tmp_path).write_text(fixed, encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("fixers.rule_based_fixer.shutil.which", return_value="/usr/bin/ruff"), \
         patch("fixers.rule_based_fixer.subprocess.run", side_effect=fake_run):
        r = rbf._py_ruff_autofix(err, src)
    check("ruff_autofix(success): EditSet", isinstance(r, EditSet))
    if isinstance(r, EditSet):
        check("ruff_autofix(success): confidence 0.95",
              abs(r.confidence - RULE_CONFIDENCE) < 1e-6)
        check("ruff_autofix(success): 1 edit",
              len(r.edits) == 1)
        if r.edits:
            ed = r.edits[0]
            check("ruff_autofix(success): kind=replace_file",
                  ed.kind == "replace_file")
            check("ruff_autofix(success): file",
                  ed.file == "src/m.py")
            check("ruff_autofix(success): new=fixed content",
                  ed.new == fixed)


def test_py_ruff_autofix_missing_file_in_error():
    """error без 'file' -> None."""
    rbf = RuleBasedFixer()
    err = {"code": "UP006", "line": 1}  # нет 'file'
    with patch("fixers.rule_based_fixer.shutil.which", return_value="/usr/bin/ruff"):
        r = rbf._py_ruff_autofix(err, "x: List[int] = []\n")
    check("ruff_autofix(no file): None", r is None)


def test_py_ruff_autofix_empty_code():
    rbf = RuleBasedFixer()
    err = {"code": "", "file": "x.py", "line": 1}
    with patch("fixers.rule_based_fixer.shutil.which", return_value="/usr/bin/ruff"):
        r = rbf._py_ruff_autofix(err, "x = 1\n")
    check("ruff_autofix(empty code): None", r is None)


# ---------------------------------------------------------------------
# 4. Try_fix end-to-end через диспетчер
# ---------------------------------------------------------------------

def test_try_fix_routes_to_ruff_autofix():
    """`try_fix` для UP006 -> ruff_autofix path."""
    rbf = RuleBasedFixer()
    src = "from typing import List\n\nx: List[int] = []\n"
    fixed = "x: list[int] = []\n"
    err = {"code": "UP006", "file": "src/m.py", "line": 3}

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_text(fixed, encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("fixers.rule_based_fixer.shutil.which", return_value="/usr/bin/ruff"), \
         patch("fixers.rule_based_fixer.subprocess.run", side_effect=fake_run):
        edit_set = rbf.try_fix(err, src, "python")
    check("try_fix(UP006): returns EditSet",
          isinstance(edit_set, EditSet))


if __name__ == "__main__":
    print("P.2 (bandit) + P.3 (python_explain) + P.4 (ruff-autofix):")
    # Bandit
    test_bandit_parse_one()
    test_bandit_parse_multiple()
    test_bandit_parse_empty()
    test_bandit_parse_invalid_json()
    test_bandit_severity_map()
    test_bandit_skip_dirs()
    test_bandit_parse_filters_backup()
    test_bandit_graceful_no_binary()
    test_bandit_flag_on_off()
    # Python explain
    test_python_explain_mypy_code()
    test_python_explain_mypy_unknown_code()
    test_python_explain_ruff_no_binary()
    test_python_explain_ruff_with_mock()
    test_python_explain_non_python_language()
    test_python_explain_empty_code()
    test_python_explain_registered_in_factory()
    test_python_explain_code_regex()
    # Ruff autofix
    test_rule_based_dispatch_ruff_codes()
    test_rule_based_dispatch_unknown_python_code()
    test_py_ruff_autofix_no_binary()
    test_py_ruff_autofix_no_change()
    test_py_ruff_autofix_success()
    test_py_ruff_autofix_missing_file_in_error()
    test_py_ruff_autofix_empty_code()
    test_try_fix_routes_to_ruff_autofix()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nP.2 + P.3 + P.4: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
