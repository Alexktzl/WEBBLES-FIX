"""
Edge-cases по Python-стеку (после ревью):

  1) python_ast_repair: `elsewhere = 1` НЕ интерпретируется как `else`-блок →
     лишний `:` не добавляется; `tryout = 1` НЕ интерпретируется как `try`.
  2) python_dependency_inference: relative imports (`from . import x`) не
     попадают в requirements; локальные подпакеты глубже верхнего уровня
     корректно считаются локальными.
  3) rule_based_fixer._py_hardcoded_secret: `import os` в нижней части файла
     детектится — дубль НЕ добавляется.
  4) pip_audit_scan: корень-список JSON парсится без AttributeError.
  5) python_healer._heal_missing_colon: `if(x)` (без пробела) тоже чинится.

Запуск: python3 tests/test_phase_python_edge_cases.py
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.python_ast_repair import try_repair, _is_block_opener
from analysis.python_dependency_inference import (
    collect_imports, infer_missing, _local_top_modules,
)
from fixers.rule_based_fixer import RuleBasedFixer
from analysis.pip_audit_scan import PipAuditScanner
from fixers.language_syntax.python_healer import PythonSyntaxHealer

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ----------------------------------------------------------------------
# 1) _BLOCK_OPENERS / _is_block_opener — без ложных срабатываний
# ----------------------------------------------------------------------
def test_block_opener_strict_match():
    # положительные
    for s in ("if x:", "if(x):", "if x", "elif x:", "else:", "else",
              "else  #", "try:", "try", "try # noqa", "for x in y:",
              "while True:", "with x:", "def f():", "async def f():",
              "class C:", "except ValueError:", "finally:"):
        check(f"opener_pos_{s!r}", _is_block_opener(s) is True)
    # отрицательные — типовые ложные срабатывания
    for s in ("elsewhere = 1", "tryout = 0", "forall = []", "whilex = True",
              "defmacro = 1", "classroom = 'C'", "ifx = True", "elifoo = 1"):
        check(f"opener_neg_{s!r}", _is_block_opener(s) is False)


def test_ast_repair_does_not_break_lookalikes():
    """`elsewhere = 1` остаётся, AST-репэр не добавляет `:`."""
    src = (
        "def f():\n"
        "    elsewhere = 1\n"
        "    tryout = 0\n"
        "    forall = []\n"
        "    return elsewhere + tryout + len(forall\n"  # незакрытая `(` →SyntaxError
    )
    out = try_repair(src)
    if out is None:
        # репэр не справился — ок: главное, что `elsewhere`/`tryout` не были порчены
        check("lookalike_preserved_in_input", "elsewhere = 1" in src)
        return
    check("lookalike_elsewhere_intact", "elsewhere = 1" in out)
    check("lookalike_no_else_colon", "elsewhere = 1:" not in out and
          "elsewhere = 1 :" not in out)
    check("lookalike_tryout_intact", "tryout = 0" in out)


# ----------------------------------------------------------------------
# 2) relative imports и локальные подпакеты
# ----------------------------------------------------------------------
def test_relative_import_not_in_requirements():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        # пакет
        (proj / "app").mkdir()
        (proj / "app" / "__init__.py").write_text("", encoding="utf-8")
        (proj / "app" / "main.py").write_text(
            "from . import utils\n"
            "from .sub import helper\n"
            "import requests\n",
            encoding="utf-8",
        )
        (proj / "app" / "utils.py").write_text("", encoding="utf-8")
        (proj / "app" / "sub" / "__init__.py").parent.mkdir(exist_ok=True)
        (proj / "app" / "sub" / "__init__.py").write_text("", encoding="utf-8")
        (proj / "app" / "sub" / "helper.py").write_text("", encoding="utf-8")
        imports = collect_imports(proj)
        check("rel_no_empty_string", "" not in imports)
        check("rel_no_dot", "." not in imports)
        check("rel_only_external_in_missing",
              "requests" in imports)
        missing = infer_missing(proj)
        check("rel_skip_app", "app" not in missing)
        check("rel_adds_requests", "requests" in missing)


def test_deep_local_subpackages_recognized():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        # лежит глубоко: lib/internal/helpers.py
        (proj / "lib" / "internal").mkdir(parents=True)
        (proj / "lib" / "__init__.py").write_text("", encoding="utf-8")
        (proj / "lib" / "internal" / "__init__.py").write_text("", encoding="utf-8")
        (proj / "lib" / "internal" / "helpers.py").write_text("", encoding="utf-8")
        # модуль импортирует из lib.internal — это всё локально.
        (proj / "main.py").write_text(
            "from lib.internal.helpers import x\n"
            "import requests\n",
            encoding="utf-8",
        )
        locals_set = _local_top_modules(proj)
        check("local_has_lib", "lib" in locals_set)
        missing = infer_missing(proj)
        check("deep_local_lib_not_in_missing", "lib" not in missing)
        check("deep_external_requests_in_missing", "requests" in missing)


# ----------------------------------------------------------------------
# 3) `import os` в нижней части файла — не дублируем
# ----------------------------------------------------------------------
def test_hardcoded_secret_does_not_duplicate_import_os():
    src = (
        "# 1\n# 2\n# 3\n# 4\n# 5\n# 6\n# 7\n# 8\n# 9\n# 10\n"
        "# 11\n# 12\n# 13\n# 14\n# 15\n# 16\n# 17\n# 18\n# 19\n# 20\n"
        "# 21\n# 22\n# 23\n# 24\n# 25\n# 26\n# 27\n# 28\n# 29\n# 30\n"
        "import os\n"             # 31 — позже первых 30 строк
        "SECRET_KEY = \"supersecretkey123\"\n"
    )
    err = {"file": "x.py", "line": 32, "code": "hardcoded_secret",
           "message": "hardcoded secret"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    check("hc_returns_editset", es is not None)
    if not es:
        return
    out_files = es.apply({"x.py": src})
    out = out_files["x.py"]
    check("hc_no_duplicate_import_os", out.count("import os") == 1)
    check("hc_secret_replaced", "os.environ" in out)


def test_hardcoded_secret_adds_import_os_when_absent():
    src = "SECRET_KEY = \"supersecretkey123\"\n"
    err = {"file": "x.py", "line": 1, "code": "hardcoded_secret",
           "message": "hardcoded secret"}
    es = RuleBasedFixer().try_fix(err, src, "python")
    out = es.apply({"x.py": src})["x.py"]
    check("hc_added_import_os", "import os" in out and out.count("import os") == 1)


# ----------------------------------------------------------------------
# 4) pip-audit: корень-список не падает
# ----------------------------------------------------------------------
def test_pip_audit_list_root_format():
    """Старый формат: корень — плоский список dependencies."""
    payload = [
        {"name": "requests", "version": "2.30.0",
         "vulns": [{"id": "GHSA-x", "fix_versions": ["2.31.0"],
                    "description": "RCE"}]},
        {"name": "flask", "version": "3.0", "vulns": []},
    ]
    with tempfile.TemporaryDirectory() as d:
        req = Path(d) / "requirements.txt"
        req.write_text("requests\nflask\n")
        out = PipAuditScanner._parse(json.dumps(payload), req)
        check("list_root_count", len(out) == 1)
        check("list_root_code", out[0]["code"] == "cve_GHSA-x")


def test_pip_audit_random_root():
    """Корень — число/строка/None → []."""
    with tempfile.TemporaryDirectory() as d:
        req = Path(d) / "requirements.txt"
        req.write_text("requests\n")
        check("random_int", PipAuditScanner._parse("42", req) == [])
        check("random_str", PipAuditScanner._parse('"hi"', req) == [])
        check("random_null", PipAuditScanner._parse("null", req) == [])


# ----------------------------------------------------------------------
# 5) python_healer: `if(x)` без пробела
# ----------------------------------------------------------------------
def test_python_healer_colon_no_space_before_paren():
    import tempfile as _tf
    fd, name = _tf.mkstemp(suffix=".py")
    import os as _os
    _os.close(fd)
    p = Path(name)
    p.write_text("def f():\n    x = 1\n    if(x == 1)\n        return 1\n",
                 encoding="utf-8")
    try:
        h = PythonSyntaxHealer()
        err = {"file": p.name, "line": 3, "code": "E999",
               "message": "expected ':'"}
        patch = h.heal(err, p)
        check("no_space_paren_returns_patch", isinstance(patch, str) and patch)
        if isinstance(patch, str) and patch:
            check("no_space_paren_has_colon", "if(x == 1):" in patch)
    finally:
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    print("python edge-cases smoke:")
    test_block_opener_strict_match()
    test_ast_repair_does_not_break_lookalikes()
    test_relative_import_not_in_requirements()
    test_deep_local_subpackages_recognized()
    test_hardcoded_secret_does_not_duplicate_import_os()
    test_hardcoded_secret_adds_import_os_when_absent()
    test_pip_audit_list_root_format()
    test_pip_audit_random_root()
    test_python_healer_colon_no_space_before_paren()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\npython edge-cases: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
