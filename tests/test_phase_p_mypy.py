"""
P.0 — mypy паритет: тесты MypyAnalyzer + интеграция в pipeline.

mypy для Python это то же, что `rustc --type-check` для Rust: статический
анализатор типов. До P.0 Python в webbles_fix полагался только на flake8
(стилевой/синтаксический) и AST (только первая ошибка). Подключение
mypy закрывает разрыв с Rust по типовой проверке (E0308/E0425/E0599
у Rust ≈ arg-type/name-defined/attr-defined у mypy).

Покрывает:
  1) Чистый парсер вывода `MypyAnalyzer._parse_output` — с column / без,
     с кодом / без, note-строки игнорируются.
  2) `_classify_mypy` — основные коды и эвристика по сообщению.
  3) SKIP_DIRS включает `.webbles_backups` и пути с этой папкой
     парсер игнорирует.
  4) `analyze()` — graceful если mypy не в PATH (`available()`==False -> []).
  5) `AnalyzeStage._use_mypy_enabled` — флаг on/off/default.
  6) `python_support.get_classification_rules` содержит mypy-коды.
  7) `error_constraints.get_constraints` возвращает DO/DON'T для mypy-кодов.

Запуск: python3 tests/test_phase_p_mypy.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# Заглушка для tomlkit (core.stages.__init__ тянет toml_patcher).
try:
    import tomlkit  # noqa: F401
except ImportError:
    import types as _types
    sys.modules["tomlkit"] = _types.ModuleType("tomlkit")

from analyzers.mypy_analyzer import (
    MypyAnalyzer,
    _classify_mypy,
    _is_skipped_path,
    SKIP_DIRS,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------------
# 1. Парсер: классические форматы mypy
# ---------------------------------------------------------------------

def test_parse_with_column():
    out = 'src/auth.py:10:5: error: Name "foo" is not defined  [name-defined]\n'
    errors = MypyAnalyzer._parse_output(out, Path("."))
    check("parse: one error", len(errors) == 1)
    if errors:
        e = errors[0]
        check("parse: file", e["file"].endswith("auth.py"))
        check("parse: line", e["line"] == 10)
        check("parse: column", e["column"] == 5)
        check("parse: severity", e["severity"] == "error")
        check("parse: code", e["code"] == "name-defined")
        check("parse: error_type", e["error_type"] == "resolve")


def test_parse_without_column():
    out = 'src/m.py:15: error: Incompatible return value type (got "int", expected "str")  [return-value]\n'
    errors = MypyAnalyzer._parse_output(out, Path("."))
    check("parse(no_col): one error", len(errors) == 1)
    if errors:
        e = errors[0]
        check("parse(no_col): col == 0", e["column"] == 0)
        check("parse(no_col): code", e["code"] == "return-value")
        check("parse(no_col): error_type", e["error_type"] == "type")


def test_parse_without_code():
    """Старый mypy без --show-error-codes."""
    out = 'src/x.py:7:1: error: Cannot find implementation or library stub for module "foo"\n'
    errors = MypyAnalyzer._parse_output(out, Path("."))
    check("parse(no_code): one error", len(errors) == 1)
    if errors:
        # F6 (2026-06-19): без [code] эти ошибки раньше проваливались мимо
        # _UNFIXABLE_MYPY_CODES прямо в LLM (не умеет ставить stub-пакеты) и
        # уходили в rejected. _synthesize_mypy_code восстанавливает код по
        # тексту сообщения для известных безкодовых случаев.
        check("parse(no_code): synthesized code -> import-untyped",
              errors[0]["code"] == "import-untyped")
        # Эвристика _classify_mypy по тексту даёт dependency:
        check("parse(no_code): classified by text -> dependency",
              errors[0]["error_type"] == "dependency")


def test_parse_skips_notes():
    """`note:` — это подсказка, не отдельная ошибка."""
    out = (
        'src/y.py:20: note: Revealed type is "builtins.int"\n'
        'src/y.py:21: error: Bad assignment  [assignment]\n'
    )
    errors = MypyAnalyzer._parse_output(out, Path("."))
    check("parse(notes): skipped notes", len(errors) == 1)
    if errors:
        check("parse(notes): only error kept",
              errors[0]["code"] == "assignment")


def test_parse_multiple_errors():
    out = (
        'a/foo.py:1:1: error: msg1  [name-defined]\n'
        'a/foo.py:5:3: error: msg2  [attr-defined]\n'
        'a/bar.py:10: error: msg3  [arg-type]\n'
    )
    errors = MypyAnalyzer._parse_output(out, Path("."))
    check("parse(multi): 3 errors", len(errors) == 3)
    if len(errors) == 3:
        codes = [e["code"] for e in errors]
        check("parse(multi): codes",
              codes == ["name-defined", "attr-defined", "arg-type"])


def test_parse_skips_garbage_lines():
    out = (
        "Success: no issues found in 12 source files\n"
        "Some random text\n"
        'src/q.py:3: error: real error  [misc]\n'
    )
    errors = MypyAnalyzer._parse_output(out, Path("."))
    check("parse(garbage): 1 error kept", len(errors) == 1)


# ---------------------------------------------------------------------
# 2. SKIP_DIRS — пропуск бэкап/служебных каталогов
# ---------------------------------------------------------------------

def test_skip_dirs_constants():
    check("SKIP_DIRS has .webbles_backups",
          ".webbles_backups" in SKIP_DIRS)
    check("SKIP_DIRS has .webbles",
          ".webbles" in SKIP_DIRS)
    check("SKIP_DIRS has __pycache__",
          "__pycache__" in SKIP_DIRS)
    check("SKIP_DIRS has venv",
          "venv" in SKIP_DIRS)
    check("SKIP_DIRS has node_modules",
          "node_modules" in SKIP_DIRS)


def test_is_skipped_path():
    check("_is_skipped: backup",
          _is_skipped_path(Path(".webbles_backups/auth.py")))
    check("_is_skipped: nested backup",
          _is_skipped_path(Path("project/.webbles_backups/sub/x.py")))
    check("_is_skipped: __pycache__",
          _is_skipped_path(Path("__pycache__/m.cpython-310.pyc")))
    check("_is_skipped: normal file",
          not _is_skipped_path(Path("src/main.py")))


def test_parse_filters_backup_paths():
    out = (
        '.webbles_backups/auth.py:10: error: msg  [name-defined]\n'
        'src/real.py:5: error: msg  [name-defined]\n'
    )
    errors = MypyAnalyzer._parse_output(out, Path("."))
    check("parse(backup): backup filtered", len(errors) == 1)
    if errors:
        check("parse(backup): real kept",
              errors[0]["file"].endswith("real.py"))


# ---------------------------------------------------------------------
# 3. _classify_mypy — по кодам и эвристика
# ---------------------------------------------------------------------

def test_classify_resolve():
    check("classify: name-defined -> resolve",
          _classify_mypy("name-defined", "x") == "resolve")
    check("classify: used-before-def -> resolve",
          _classify_mypy("used-before-def", "x") == "resolve")


def test_classify_dependency():
    check("classify: import -> dependency",
          _classify_mypy("import", "x") == "dependency")
    check("classify: import-not-found -> dependency",
          _classify_mypy("import-not-found", "x") == "dependency")


def test_classify_type():
    for code in ("arg-type", "return-value", "assignment",
                 "union-attr", "attr-defined", "call-arg",
                 "operator", "index", "type-arg"):
        check(f"classify: {code} -> type",
              _classify_mypy(code, "x") == "type")


def test_classify_warning():
    for code in ("no-untyped-def", "var-annotated", "unreachable",
                 "no-redef", "redundant-cast"):
        check(f"classify: {code} -> warning",
              _classify_mypy(code, "x") == "warning")


def test_classify_fallback_by_message():
    """Без кода — эвристика по тексту сообщения (старый mypy)."""
    check("classify(text): is not defined -> resolve",
          _classify_mypy("", 'Name "foo" is not defined') == "resolve")
    check("classify(text): has no attribute -> type",
          _classify_mypy("", 'X has no attribute "bar"') == "type")
    check("classify(text): cannot find module -> dependency",
          _classify_mypy("", "Cannot find module 'foo'") == "dependency")
    check("classify(text): incompatible types -> type",
          _classify_mypy("", "Incompatible types in return") == "type")
    check("classify(text): unreachable -> warning",
          _classify_mypy("", "Statement is unreachable") == "warning")


# ---------------------------------------------------------------------
# 4. analyze() — graceful если mypy не в PATH
# ---------------------------------------------------------------------

def test_analyze_returns_empty_when_mypy_missing():
    an = MypyAnalyzer()
    with patch.object(MypyAnalyzer, "available", return_value=False):
        out = an.analyze(Path("/tmp/nonexistent_project"))
    check("analyze: no mypy -> []", out == [])


def test_analyze_returns_empty_on_no_py_files(tmp_factory=None):
    """Если в проекте нет .py файлов — возвращаем пустой список."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # только не-python файл
        (root / "README.md").write_text("hi", encoding="utf-8")
        an = MypyAnalyzer()
        with patch.object(MypyAnalyzer, "available", return_value=True):
            out = an.analyze(root)
        check("analyze: no .py -> []", out == [])


# ---------------------------------------------------------------------
# 5. AnalyzeStage._use_mypy_enabled — флаг
# ---------------------------------------------------------------------

def test_flag_default_off():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={"pipeline": {}})
    check("flag: default off",
          AnalyzeStage._use_mypy_enabled(ctx) is False)


def test_flag_explicit_on():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={"pipeline": {"use_mypy": True}})
    check("flag: explicit on",
          AnalyzeStage._use_mypy_enabled(ctx) is True)


def test_flag_explicit_off():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={"pipeline": {"use_mypy": False}})
    check("flag: explicit off",
          AnalyzeStage._use_mypy_enabled(ctx) is False)


def test_flag_no_config():
    from core.stages.analyze_stage import AnalyzeStage
    ctx = SimpleNamespace(config={})
    check("flag: no config -> off",
          AnalyzeStage._use_mypy_enabled(ctx) is False)


# ---------------------------------------------------------------------
# 6. python_support — mypy-коды в classification rules
# ---------------------------------------------------------------------

def test_python_support_has_mypy_codes():
    from core.languages.python_support import PythonSupport
    rules = PythonSupport().get_classification_rules()
    for code in ("name-defined", "attr-defined", "arg-type",
                 "return-value", "assignment", "union-attr",
                 "import", "import-not-found"):
        check(f"python_support: {code} in rules", code in rules)
    # name-defined -> BLOCKING (как E0425 у Rust).
    check("python_support: name-defined -> BLOCKING",
          rules["name-defined"]["class"] == "BLOCKING")
    # attr-defined -> BLOCKING (как E0609).
    check("python_support: attr-defined -> BLOCKING",
          rules["attr-defined"]["class"] == "BLOCKING")
    # arg-type -> BLOCKING (как E0308).
    check("python_support: arg-type -> BLOCKING",
          rules["arg-type"]["class"] == "BLOCKING")


# ---------------------------------------------------------------------
# 7. error_constraints — DO/DON'T для mypy
# ---------------------------------------------------------------------

def test_constraints_for_mypy_codes():
    from analysis.constraints.error_constraints import (
        get_constraints, is_known,
    )
    for code in ("name-defined", "attr-defined", "arg-type",
                 "return-value", "assignment", "union-attr",
                 "import", "import-not-found", "unreachable"):
        do, dont = get_constraints(code)
        check(f"constraints({code}): non-empty do", len(do) >= 1)
        check(f"constraints({code}): non-empty dont", len(dont) >= 1)
        check(f"constraints({code}): is_known", is_known(code) is True)


def test_constraints_unknown_code_empty():
    from analysis.constraints.error_constraints import get_constraints
    do, dont = get_constraints("not-a-real-code-xyz")
    check("constraints: unknown -> empty do", do == [])
    check("constraints: unknown -> empty dont", dont == [])


if __name__ == "__main__":
    print("P.0 — mypy паритет:")
    test_parse_with_column()
    test_parse_without_column()
    test_parse_without_code()
    test_parse_skips_notes()
    test_parse_multiple_errors()
    test_parse_skips_garbage_lines()
    test_skip_dirs_constants()
    test_is_skipped_path()
    test_parse_filters_backup_paths()
    test_classify_resolve()
    test_classify_dependency()
    test_classify_type()
    test_classify_warning()
    test_classify_fallback_by_message()
    test_analyze_returns_empty_when_mypy_missing()
    test_analyze_returns_empty_on_no_py_files()
    test_flag_default_off()
    test_flag_explicit_on()
    test_flag_explicit_off()
    test_flag_no_config()
    test_python_support_has_mypy_codes()
    test_constraints_for_mypy_codes()
    test_constraints_unknown_code_empty()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nP.0 mypy parity: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
