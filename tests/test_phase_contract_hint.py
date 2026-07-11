"""
Q.4 Contract Hint — тесты для analysis/contract_hint.py.

Проверяет:
1) _find_function_at_line — нахождение функции по номеру строки
2) _format_contract — форматирование снапшота как секции case-file
3) build_contract_hint — публичный API

Run: python3 tests/test_phase_contract_hint.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.contract_hint import (
    _find_function_at_line,
    _format_contract,
    build_contract_hint,
)
from analysis.logic_guard import LogicGuardExtractor

results = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    status = "OK " if cond else "FAIL"
    try:
        print(f"  [{status}] {name}")
    except UnicodeEncodeError:
        print(f"  [{status}] {name.encode('ascii', 'replace').decode('ascii')}")


# ---------------------------------------------------------------------------
# 1. _find_function_at_line
# ---------------------------------------------------------------------------

_SRC_TWO_FUNCS = (
    "def foo(a, b):\n"           # line 1
    "    return a + b\n"          # line 2
    "\n"                          # line 3
    "def bar(x):\n"               # line 4
    "    if x > 0:\n"             # line 5
    "        return x\n"          # line 6
    "    return 0\n"              # line 7
)


def test_find_func_exact_def_line():
    snap = _find_function_at_line(_SRC_TWO_FUNCS, 1)
    check("find: line1->foo", snap is not None and snap.name == "foo")


def test_find_func_body_line():
    snap = _find_function_at_line(_SRC_TWO_FUNCS, 2)
    check("find: line2->foo", snap is not None and snap.name == "foo")


def test_find_func_second_function():
    snap = _find_function_at_line(_SRC_TWO_FUNCS, 5)
    check("find: line5->bar", snap is not None and snap.name == "bar")


def test_find_func_last_line():
    snap = _find_function_at_line(_SRC_TWO_FUNCS, 7)
    check("find: line7->bar", snap is not None and snap.name == "bar")


def test_find_func_out_of_range():
    snap = _find_function_at_line(_SRC_TWO_FUNCS, 99)
    check("find: line99->None", snap is None)


def test_find_func_zero_line():
    snap = _find_function_at_line(_SRC_TWO_FUNCS, 0)
    check("find: line0->None", snap is None)


def test_find_func_syntax_error():
    snap = _find_function_at_line("def foo(: pass", 1)
    check("find: syntax_error->None", snap is None)


def test_find_func_empty_source():
    snap = _find_function_at_line("", 1)
    check("find: empty_source->None", snap is None)


# ---------------------------------------------------------------------------
# 2. _format_contract
# ---------------------------------------------------------------------------

_SRC_FULL = (
    "def process(data, mode='r'):\n"
    "    global _counter\n"
    "    with open(data, mode) as f:\n"
    "        return f.read_bytes()\n"
)


def test_format_header_present():
    snap = LogicGuardExtractor.extract_python(_SRC_FULL)["process"]
    out = _format_contract(snap)
    check("format: header present", "## FUNCTION CONTRACT: `process`" in out)


def test_format_parameters_line():
    snap = LogicGuardExtractor.extract_python(_SRC_FULL)["process"]
    out = _format_contract(snap)
    check("format: parameters line", "- Parameters:" in out)
    check("format: param name data", "data" in out)


def test_format_returns_value_yes():
    src = "def fn():\n    return 1\n"
    snap = LogicGuardExtractor.extract_python(src)["fn"]
    out = _format_contract(snap)
    check("format: returns_value=yes", "- Returns value: yes" in out)


def test_format_returns_value_no():
    src = "def fn():\n    pass\n"
    snap = LogicGuardExtractor.extract_python(src)["fn"]
    out = _format_contract(snap)
    check("format: returns_value=no", "- Returns value: no" in out)


def test_format_raises_present():
    src = "def fn():\n    raise ValueError('x')\n"
    snap = LogicGuardExtractor.extract_python(src)["fn"]
    out = _format_contract(snap)
    check("format: raises line", "- Raises:" in out)
    check("format: raises ValueError", "ValueError" in out)


def test_format_no_raises_not_in_output():
    src = "def fn():\n    return 1\n"
    snap = LogicGuardExtractor.extract_python(src)["fn"]
    out = _format_contract(snap)
    check("format: no raises->no Raises line", "- Raises:" not in out)


def test_format_side_effects_present():
    src = "def fn():\n    subprocess.run(['x'])\n"
    snap = LogicGuardExtractor.extract_python(src)["fn"]
    out = _format_contract(snap)
    check("format: side_effects line", "- Side effects:" in out)
    check("format: subprocess mentioned", "subprocess" in out)


def test_format_branch_count_line():
    src = "def fn(x):\n    if x: return x\n    return 0\n"
    snap = LogicGuardExtractor.extract_python(src)["fn"]
    out = _format_contract(snap)
    check("format: branch_count line", "- Branch count:" in out)


# ---------------------------------------------------------------------------
# 3. build_contract_hint — public API
# ---------------------------------------------------------------------------

def test_build_hint_python_found():
    src = (
        "def save(path, data):\n"
        "    with open(path, 'w') as f:\n"
        "        f.write(data)\n"
    )
    error = {"line": 2, "file": "x.py", "code": "E0"}
    out = build_contract_hint(src, error, "python")
    check("build: python found returns non-empty", len(out) > 0)
    check("build: section header present", "## FUNCTION CONTRACT: `save`" in out)


def test_build_hint_non_python_skipped():
    src = "def fn():\n    return 1\n"
    out = build_contract_hint(src, {"line": 1}, "rust")
    check("build: non-python->empty", out == "")


def test_build_hint_no_language_skipped():
    src = "def fn():\n    return 1\n"
    out = build_contract_hint(src, {"line": 1}, "")
    check("build: no language->empty", out == "")


def test_build_hint_line_zero_skipped():
    src = "def fn():\n    return 1\n"
    out = build_contract_hint(src, {"line": 0}, "python")
    check("build: line=0->empty", out == "")


def test_build_hint_no_function_at_line():
    src = "x = 1\n"
    out = build_contract_hint(src, {"line": 1}, "python")
    check("build: no function->empty", out == "")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n=== Q.4 Contract Hint Tests ===\n")
    test_find_func_exact_def_line()
    test_find_func_body_line()
    test_find_func_second_function()
    test_find_func_last_line()
    test_find_func_out_of_range()
    test_find_func_zero_line()
    test_find_func_syntax_error()
    test_find_func_empty_source()
    test_format_header_present()
    test_format_parameters_line()
    test_format_returns_value_yes()
    test_format_returns_value_no()
    test_format_raises_present()
    test_format_no_raises_not_in_output()
    test_format_side_effects_present()
    test_format_branch_count_line()
    test_build_hint_python_found()
    test_build_hint_non_python_skipped()
    test_build_hint_no_language_skipped()
    test_build_hint_line_zero_skipped()
    test_build_hint_no_function_at_line()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        print("\nFailed:")
        for name, ok in results:
            if not ok:
                print(f"  FAIL: {name}")
        sys.exit(1)
    else:
        print("All tests passed.")
