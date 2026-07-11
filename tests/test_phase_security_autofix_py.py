"""
RuleBasedFixer: авто-фиксы Python security — hardcoded_secret и dangerous_eval.

Запуск: python3 tests/test_phase_security_autofix_py.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.rule_based_fixer import RuleBasedFixer

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ----------------------------------------------------------------------
# hardcoded_secret
# ----------------------------------------------------------------------
def test_hardcoded_secret_classic():
    src = (
        "# config.py\n"
        "SECRET_KEY = \"supersecretkey123\"\n"
        "DATABASE = 'notes.db'\n"
    )
    err = {"file": "config.py", "line": 2, "code": "hardcoded_secret",
           "message": "hardcoded secret literal in source"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    check("hs_returns_editset", es is not None)
    if not es:
        return
    new_files = es.apply({"config.py": src})
    out = new_files["config.py"]
    check("hs_replaced_with_env", "os.environ['SECRET_KEY']" in out
                                  or 'os.environ["SECRET_KEY"]' in out)
    check("hs_import_added", "import os" in out)
    # DATABASE — не должно быть тронуто.
    check("hs_other_untouched", "DATABASE = 'notes.db'" in out)


def test_hardcoded_secret_already_has_import_os():
    src = (
        "import os\n"
        "API_TOKEN = \"abcd1234\"\n"
    )
    err = {"file": "x.py", "line": 2, "code": "hardcoded_secret",
           "message": "hardcoded secret"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    check("hs2_returns", es is not None)
    if not es:
        return
    new_files = es.apply({"x.py": src})
    out = new_files["x.py"]
    # import os не дублируется.
    check("hs2_no_dup_import", out.count("import os") == 1)
    check("hs2_replaced", "os.environ" in out and "API_TOKEN" in out)


def test_hardcoded_secret_skips_non_uppercase():
    """Не трогаем `password = "..."` — это не каноничная константа."""
    src = "password = \"weak\"\n"
    err = {"file": "x.py", "line": 1, "code": "hardcoded_secret",
           "message": "hardcoded secret"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    check("hs_skip_lowercase_name", es is None)


def test_hardcoded_secret_skips_short_value():
    """Литералы короче 4 символов — обычно тип/енум, не секрет."""
    src = "KEY = \"x\"\n"
    err = {"file": "x.py", "line": 1, "code": "hardcoded_secret",
           "message": "hardcoded secret"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    check("hs_skip_short_literal", es is None)


# ----------------------------------------------------------------------
# dangerous_eval
# ----------------------------------------------------------------------
def test_dangerous_eval_input():
    src = (
        "def menu():\n"
        "    eval(input('Введите код: '))\n"
    )
    err = {"file": "main.py", "line": 2, "code": "dangerous_eval",
           "message": "use of eval()/exec() on dynamic input"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    check("de_returns", es is not None)
    if not es:
        return
    new_files = es.apply({"main.py": src})
    out = new_files["main.py"]
    check("de_no_eval_call", "eval(input(" not in out)
    check("de_comment_inserted", "SECURITY" in out)


def test_dangerous_eval_static_string_skipped():
    """eval('1+1') — не паттерн eval(input(...)), оставляем LLM."""
    src = "eval('1+1')\n"
    err = {"file": "main.py", "line": 1, "code": "dangerous_eval",
           "message": "use of eval"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    check("de_skip_static", es is None)


# ----------------------------------------------------------------------
# Языковая изоляция
# ----------------------------------------------------------------------
def test_hardcoded_secret_js_not_handled_by_python_rule():
    """Авто-фикс должен сработать только для python, не для js."""
    err = {"file": "x.js", "line": 1, "code": "hardcoded_secret",
           "message": "hardcoded secret"}
    es = RuleBasedFixer().try_fix(err, "const SECRET = 'x';\n", "javascript")
    check("js_not_handled_by_py_rule", es is None)


if __name__ == "__main__":
    print("security autofix python smoke:")
    test_hardcoded_secret_classic()
    test_hardcoded_secret_already_has_import_os()
    test_hardcoded_secret_skips_non_uppercase()
    test_hardcoded_secret_skips_short_value()
    test_dangerous_eval_input()
    test_dangerous_eval_static_string_skipped()
    test_hardcoded_secret_js_not_handled_by_python_rule()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nsecurity autofix python: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
