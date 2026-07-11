"""
Stage G — smoke / regression для Tests-as-error-source.

Покрывает:
  1. parse_cargo_test: новый и старый формат panic, фоллбэк по секции failures:.
  2. parse_pytest: короткая сводка FAILED/ERROR, дедуп.
  3. parse_jest: bullet ● + "at file:line:col".
  4. TestFailure.to_error → корректный error-dict (code/class/error_type/confidence).
  5. TestRunner.available / run: graceful при отсутствии тула (passed=True, []).
  6. TEST_FAILURE: вес в ErrorClassifier + constraints DO/DON'T.

Сеть/реальные тестовые наборы НЕ запускаются — парсеры проверяются на
зафиксированных образцах вывода.
Запуск: python3 tests/test_phase_g_tests.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from validation.test_runner import TestRunner, TestFailure, TEST_FAILURE_CLASS
from analysis.error_classifier import ErrorClassifier
from analysis.constraints.error_constraints import get_constraints, is_known

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- 1. cargo test --------------------------------------------------
_CARGO_NEW = """
running 2 tests
test tests::adds_correctly ... FAILED
test tests::other ... ok

failures:

---- tests::adds_correctly stdout ----
thread 'tests::adds_correctly' panicked at src/lib.rs:42:9:
assertion `left == right` failed
  left: 4
 right: 5

failures:
    tests::adds_correctly

test result: FAILED. 1 passed; 1 failed; 0 ignored
"""

_CARGO_OLD = """
---- tests::math_works stdout ----
thread 'tests::math_works' panicked at 'assertion failed: `(left == right)`', src/math.rs:10:5

failures:
    tests::math_works

test result: FAILED. 0 passed; 1 failed;
"""

_CARGO_NAMES_ONLY = """
failures:
    tests::a
    tests::b

test result: FAILED. 0 passed; 2 failed;
"""


def test_cargo():
    f = TestRunner.parse_cargo_test(_CARGO_NEW)
    check("cargo_new_one_failure", len(f) == 1)
    check("cargo_new_name", f and f[0].name == "tests::adds_correctly")
    check("cargo_new_file_line", f and f[0].file == "src/lib.rs" and f[0].line == 42)
    check("cargo_new_msg", f and "assertion" in f[0].message)

    f2 = TestRunner.parse_cargo_test(_CARGO_OLD)
    check("cargo_old_file_line", f2 and f2[0].file == "src/math.rs" and f2[0].line == 10)

    f3 = TestRunner.parse_cargo_test(_CARGO_NAMES_ONLY)
    check("cargo_names_only_fallback",
          sorted(x.name for x in f3) == ["tests::a", "tests::b"])

    check("cargo_empty", TestRunner.parse_cargo_test("") == [])


# --- 2. pytest ------------------------------------------------------
_PYTEST = """
=========================== short test summary info ============================
FAILED tests/test_math.py::test_add - assert 3 == 4
FAILED tests/test_math.py::test_add - assert 3 == 4
ERROR tests/test_io.py::test_read - ImportError: No module named 'foo'
1 failed, 2 passed in 0.12s
"""


def test_pytest():
    f = TestRunner.parse_pytest(_PYTEST)
    check("pytest_dedup", len(f) == 2)  # дубликат FAILED схлопнут
    names = {x.name for x in f}
    check("pytest_names", names == {"test_add", "test_read"})
    add = next(x for x in f if x.name == "test_add")
    check("pytest_file", add.file == "tests/test_math.py")
    check("pytest_msg", "assert 3 == 4" in add.message)
    check("pytest_empty", TestRunner.parse_pytest("") == [])


# --- 3. jest --------------------------------------------------------
_JEST = """
 FAIL  test/math.test.js
  math
    ✕ adds two numbers (3 ms)

  ● math › adds two numbers

    expect(received).toBe(expected)

    Expected: 4
    Received: 5

      at Object.<anonymous> (test/math.test.js:5:19)
"""


def test_jest():
    f = TestRunner.parse_jest(_JEST)
    check("jest_found", len(f) >= 1)
    # bullet ● строка с " › " берётся как имя
    bullet = next((x for x in f if "adds two numbers" in x.name), None)
    check("jest_name", bullet is not None)
    check("jest_file_line", bullet and bullet.file == "test/math.test.js"
          and bullet.line == 5)
    check("jest_empty", TestRunner.parse_jest("") == [])


# --- 4. to_error ----------------------------------------------------
def test_to_error():
    e = TestFailure(name="tests::x", file="src/lib.rs", line=42,
                    message="assertion failed").to_error()
    check("to_error_code", e["code"] == "TEST_FAILURE")
    check("to_error_class", e["error_class"] == TEST_FAILURE_CLASS)
    check("to_error_type", e["error_type"] == "test")
    check("to_error_msg_has_name", e["message"].startswith("tests::x:"))
    check("to_error_file_line", e["file"] == "src/lib.rs" and e["line"] == 42)
    check("to_error_confidence", e["confidence"] == 0.9)


# --- 5. TestRunner graceful -----------------------------------------
def test_runner_graceful():
    runner = TestRunner()
    # неизвестный язык → недоступен → (True, [])
    check("runner_unknown_lang_unavail", runner.available("cobol") is False)
    passed, errs = runner.run("/nonexistent/path", "cobol")
    check("runner_unknown_lang_passes", passed is True and errs == [])


# --- 6. TEST_FAILURE класс ------------------------------------------
def test_test_failure_class():
    clf = ErrorClassifier()
    w = clf.get_weight({"error_class": "TEST_FAILURE"})
    check("weight_test_failure", w == 60.0)
    # ниже STRUCTURAL, выше CLEANUP
    check("weight_ordering",
          clf.get_weight({"error_class": "CLEANUP"}) < w
          < clf.get_weight({"error_class": "STRUCTURAL"}))
    do, dont = get_constraints("TEST_FAILURE")
    check("constraints_known", is_known("TEST_FAILURE"))
    check("constraints_have_do_dont", len(do) >= 2 and len(dont) >= 2)
    check("constraints_dont_edit_test",
          any("test" in d.lower() for d in dont))


if __name__ == "__main__":
    print("Stage G — Tests-as-error-source smoke:")
    test_cargo()
    test_pytest()
    test_jest()
    test_to_error()
    test_runner_graceful()
    test_test_failure_class()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage G: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
