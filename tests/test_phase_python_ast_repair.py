"""
Python AST-репэр — каскад правок развалившихся Python-файлов.

Запуск: python3 tests/test_phase_python_ast_repair.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.python_ast_repair import try_repair

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def parses(src):
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


# ----------------------------------------------------------------------
# 1. Дубликат подряд идущих строк — реальный кейс пользователя
# ----------------------------------------------------------------------
def test_duplicate_lines_removed():
    # Реальный кейс: missing colon + дубль строки. AST-репэр должен закрыть
    # обе проблемы каскадом и оставить ровно одну `query = ...`.
    broken = (
        "def login()\n"                       # нет двоеточия
        "    cursor = None\n"
        "    query = 'SELECT * FROM users'\n"
        "    query = 'SELECT * FROM users'\n"  # дубль
        "    print(query)\n"
    )
    out = try_repair(broken)
    check("dup_returns_something", isinstance(out, str) and out)
    if out:
        check("dup_line_removed", out.count("query = 'SELECT * FROM users'") == 1)
        check("dup_result_parses", parses(out))


# ----------------------------------------------------------------------
# 2. Missing colon после def — репэр добавляет :
# ----------------------------------------------------------------------
def test_missing_colon_def():
    src = "def init_db()\n    return 1\n"
    out = try_repair(src)
    check("colon_returns", isinstance(out, str) and out)
    if out:
        check("colon_added_def", "def init_db():" in out)
        check("colon_result_parses", parses(out))


def test_missing_colon_if():
    src = "x = 1\nif x == 1\n    print('ok')\n"
    out = try_repair(src)
    check("if_returns", isinstance(out, str) and out)
    if out:
        check("if_colon_added", "if x == 1:" in out)
        check("if_result_parses", parses(out))


# ----------------------------------------------------------------------
# 3. Отступы — нечётные → выровнять до кратного 4
# ----------------------------------------------------------------------
def test_indent_normalize():
    src = (
        "def f():\n"
        "    a = 1\n"
        "  b = 2\n"  # 2 → должно стать 4
    )
    out = try_repair(src)
    check("indent_returns", isinstance(out, str) and out)
    if out:
        third = out.splitlines()[2]
        check("indent_normalized_4",
              third.startswith("    ") and not third.startswith("     "))
        check("indent_result_parses", parses(out))


# ----------------------------------------------------------------------
# 4. Уже валидный файл — None (не трогаем)
# ----------------------------------------------------------------------
def test_already_valid_file_noop():
    src = "def f():\n    return 1\n"
    out = try_repair(src)
    check("valid_returns_none", out is None)


# ----------------------------------------------------------------------
# 5. Полностью неисправимый — None
# ----------------------------------------------------------------------
def test_unsalvageable_returns_none():
    src = "def f(\n# нет закрытия и продолжения\n\n"
    out = try_repair(src)
    check("unsalvageable_none", out is None)


# ----------------------------------------------------------------------
# 6. Лишняя закрывающая скобка в конце файла
# ----------------------------------------------------------------------
def test_trailing_close_paren():
    src = "x = 1\nprint(x)\n)\n"
    out = try_repair(src)
    check("trail_close_returns", isinstance(out, str) and out)
    if out:
        check("trail_close_paren_dropped", out.rstrip().endswith("print(x)"))
        check("trail_close_result_parses", parses(out))


if __name__ == "__main__":
    print("python AST-репэр smoke:")
    test_duplicate_lines_removed()
    test_missing_colon_def()
    test_missing_colon_if()
    test_indent_normalize()
    test_already_valid_file_noop()
    test_unsalvageable_returns_none()
    test_trailing_close_paren()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\npython AST-репэр: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
