"""
P.1 — ruff паритет: тесты RuffAnalyzer + интеграция в pipeline.

ruff для Python это параллель `cargo clippy` у Rust: best-practices линтер
с расширенным набором кодов сверх pyflakes/pycodestyle (bugbear B,
pyupgrade UP, isort I, pep8-naming N, bandit-lite S, simplify SIM,
comprehensions C4 и т.д.).

Покрывает:
  1) Парсер JSON-вывода `RuffAnalyzer._parse_json_output`.
  2) `_classify_ruff` — по префиксам кодов; длинные префиксы важнее коротких.
  3) `_severity_for` — error/warning по семейству кода.
  4) SKIP_DIRS включает `.webbles_backups`.
  5) `analyze()` — graceful если ruff не в PATH или нет .py файлов.
  6) `AnalyzeStage._use_ruff_enabled` — флаг on/off/default.
  7) `python_support` содержит ruff-коды (B006, S101, UP006, I001, и т.д.).

Запуск: python3 tests/test_phase_p_ruff.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types as _types
    sys.modules["tomlkit"] = _types.ModuleType("tomlkit")

from analyzers.ruff_analyzer import (
    RuffAnalyzer,
    _classify_ruff,
    _severity_for,
    _is_skipped_path,
    SKIP_DIRS,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------------
# 1. Парсер JSON-вывода
# ---------------------------------------------------------------------

def _ruff_finding(code, file, row, col, msg):
    return {
        "code": code,
        "message": msg,
        "filename": file,
        "location": {"row": row, "column": col},
        "end_location": {"row": row, "column": col + 1},
        "fix": None,
    }


def test_parse_single_finding():
    out = json.dumps([_ruff_finding("F401", "src/auth.py", 5, 1,
                                     "`os` imported but unused")])
    errors = RuffAnalyzer._parse_json_output(out, Path("."))
    check("parse: one finding", len(errors) == 1)
    if errors:
        e = errors[0]
        check("parse: file", e["file"].endswith("auth.py"))
        check("parse: line", e["line"] == 5)
        check("parse: column", e["column"] == 1)
        check("parse: code F401", e["code"] == "F401")
        check("parse: severity error", e["severity"] == "error")
        check("parse: error_type lint", e["error_type"] == "lint")


def test_parse_multiple_codes():
    out = json.dumps([
        _ruff_finding("B006", "a.py", 1, 1, "mutable default arg"),
        _ruff_finding("S101", "a.py", 2, 1, "assert detected"),
        _ruff_finding("UP006", "a.py", 3, 1, "Use list instead of List"),
        _ruff_finding("I001", "a.py", 4, 1, "Import block is un-sorted"),
        _ruff_finding("N802", "a.py", 5, 1, "Function name should be lowercase"),
    ])
    errors = RuffAnalyzer._parse_json_output(out, Path("."))
    check("parse(multi): 5 findings", len(errors) == 5)
    codes = [e["code"] for e in errors]
    check("parse(multi): codes order",
          codes == ["B006", "S101", "UP006", "I001", "N802"])


def test_parse_empty_output():
    check("parse(empty): []",
          RuffAnalyzer._parse_json_output("", Path(".")) == [])
    check("parse(empty array): []",
          RuffAnalyzer._parse_json_output("[]", Path(".")) == [])


def test_parse_invalid_json():
    check("parse(invalid): []",
          RuffAnalyzer._parse_json_output("not json{", Path(".")) == [])


def test_parse_skips_backup_paths():
    out = json.dumps([
        _ruff_finding("F401", ".webbles_backups/x.py", 1, 1, "unused"),
        _ruff_finding("F401", "src/real.py", 1, 1, "unused"),
    ])
    errors = RuffAnalyzer._parse_json_output(out, Path("."))
    check("parse(backup): backup filtered", len(errors) == 1)
    if errors:
        check("parse(backup): real kept",
              errors[0]["file"].endswith("real.py"))


# ---------------------------------------------------------------------
# 2. _classify_ruff — по префиксам
# ---------------------------------------------------------------------

def test_classify_pyflakes():
    check("classify: F401 -> lint", _classify_ruff("F401", "") == "lint")
    check("classify: F841 -> lint", _classify_ruff("F841", "") == "lint")


def test_classify_pycodestyle():
    check("classify: E501 -> compile", _classify_ruff("E501", "") == "compile")
    check("classify: W291 -> warning", _classify_ruff("W291", "") == "warning")


def test_classify_bugbear():
    check("classify: B006 -> compile", _classify_ruff("B006", "") == "compile")
    check("classify: B904 -> compile", _classify_ruff("B904", "") == "compile")


def test_classify_security():
    check("classify: S101 -> security", _classify_ruff("S101", "") == "security")
    check("classify: S301 -> security", _classify_ruff("S301", "") == "security")
    check("classify: S608 -> security", _classify_ruff("S608", "") == "security")


def test_classify_pyupgrade_isort_naming():
    check("classify: UP006 -> lint", _classify_ruff("UP006", "") == "lint")
    check("classify: I001 -> lint", _classify_ruff("I001", "") == "lint")
    check("classify: N802 -> lint", _classify_ruff("N802", "") == "lint")


def test_classify_simplify_comprehensions():
    """SIM не должен быть классифицирован как S (security) из-за порядка."""
    check("classify: SIM102 -> lint (NOT security)",
          _classify_ruff("SIM102", "") == "lint")
    check("classify: C401 -> lint", _classify_ruff("C401", "") == "lint")
    check("classify: COM812 -> lint", _classify_ruff("COM812", "") == "lint")


def test_classify_unknown():
    check("classify: empty -> lint",
          _classify_ruff("", "") == "lint")
    check("classify: ZZ999 -> lint",
          _classify_ruff("ZZ999", "") == "lint")


# ---------------------------------------------------------------------
# 3. _severity_for
# ---------------------------------------------------------------------

def test_severity():
    for code, expected in [
        ("F401", "error"), ("E501", "error"), ("B006", "error"),
        ("S101", "error"), ("PLE0101", "error"),
        ("W291", "warning"), ("UP006", "warning"),
        ("I001", "warning"), ("N802", "warning"),
        ("SIM102", "warning"),
    ]:
        check(f"severity({code})={expected}",
              _severity_for(code) == expected)


# ---------------------------------------------------------------------
# 4. SKIP_DIRS
# ---------------------------------------------------------------------

def test_skip_dirs():
    check("SKIP_DIRS has .webbles_backups",
          ".webbles_backups" in SKIP_DIRS)
    check("SKIP_DIRS has .ruff_cache",
          ".ruff_cache" in SKIP_DIRS)
    check("_is_skipped: backup",
          _is_skipped_path(Path(".webbles_backups/x.py")))
    check("_is_skipped: ruff_cache",
          _is_skipped_path(Path(".ruff_cache/x")))
    check("_is_skipped: normal",
          not _is_skipped_path(Path("src/main.py")))


# ---------------------------------------------------------------------
# 5. analyze() — graceful
# ---------------------------------------------------------------------

def test_analyze_no_ruff_returns_empty():
    an = RuffAnalyzer()
    with patch.object(RuffAnalyzer, "available", return_value=False):
        out = an.analyze(Path("/tmp/nonexistent"))
    check("analyze: no ruff -> []", out == [])


def test_analyze_no_py_files():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "README.md").write_text("hi", encoding="utf-8")
        an = RuffAnalyzer()
        with patch.object(RuffAnalyzer, "available", return_value=True):
            out = an.analyze(root)
        check("analyze: no .py -> []", out == [])


# ---------------------------------------------------------------------
# 6. AnalyzeStage._use_ruff_enabled
# ---------------------------------------------------------------------

def test_flag_default_off():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={"pipeline": {}})
    check("flag: default off",
          AnalyzeStage._use_ruff_enabled(ctx) is False)


def test_flag_explicit_on():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={"pipeline": {"use_ruff": True}})
    check("flag: explicit on",
          AnalyzeStage._use_ruff_enabled(ctx) is True)


def test_flag_explicit_off():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={"pipeline": {"use_ruff": False}})
    check("flag: explicit off",
          AnalyzeStage._use_ruff_enabled(ctx) is False)


def test_flag_no_config():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={})
    check("flag: no config -> off",
          AnalyzeStage._use_ruff_enabled(ctx) is False)


# ---------------------------------------------------------------------
# 7. python_support содержит ruff-коды
# ---------------------------------------------------------------------

def test_python_support_has_ruff_codes():
    from core.languages.python_support import PythonSupport
    rules = PythonSupport().get_classification_rules()
    # bugbear
    for code in ("B006", "B007", "B008", "B011", "B904"):
        check(f"python_support: {code} in rules", code in rules)
    # security S -> SECURITY-класс
    for code in ("S102", "S105", "S301", "S307", "S324", "S608"):
        check(f"python_support: {code} -> SECURITY",
              code in rules and rules[code]["class"] == "SECURITY")
    # pyflakes F
    for code in ("F401", "F811", "F821"):
        check(f"python_support: {code} in rules", code in rules)
    # pyupgrade UP
    for code in ("UP006", "UP007", "UP032"):
        check(f"python_support: {code} in rules", code in rules)
    # isort / naming / simplify / comprehensions / print
    for code in ("I001", "N802", "SIM102", "C408", "T201"):
        check(f"python_support: {code} in rules", code in rules)
    # B006 -> BLOCKING (mutable default arg — реальный баг)
    check("python_support: B006 -> BLOCKING",
          rules["B006"]["class"] == "BLOCKING")
    # B007 -> CLEANUP (unused loop var)
    check("python_support: B007 -> CLEANUP",
          rules["B007"]["class"] == "CLEANUP")
    # F821 -> BLOCKING (undefined name, ≈ E0425)
    check("python_support: F821 -> BLOCKING",
          rules["F821"]["class"] == "BLOCKING")
    # ruff syntax errors -> CRITICAL_SYNTAX (P2.7a)
    check("python_support: invalid-syntax -> CRITICAL_SYNTAX",
          "invalid-syntax" in rules and rules["invalid-syntax"]["class"] == "CRITICAL_SYNTAX")


if __name__ == "__main__":
    print("P.1 — ruff паритет:")
    test_parse_single_finding()
    test_parse_multiple_codes()
    test_parse_empty_output()
    test_parse_invalid_json()
    test_parse_skips_backup_paths()
    test_classify_pyflakes()
    test_classify_pycodestyle()
    test_classify_bugbear()
    test_classify_security()
    test_classify_pyupgrade_isort_naming()
    test_classify_simplify_comprehensions()
    test_classify_unknown()
    test_severity()
    test_skip_dirs()
    test_analyze_no_ruff_returns_empty()
    test_analyze_no_py_files()
    test_flag_default_off()
    test_flag_explicit_on()
    test_flag_explicit_off()
    test_flag_no_config()
    test_python_support_has_ruff_codes()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nP.1 ruff parity: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
