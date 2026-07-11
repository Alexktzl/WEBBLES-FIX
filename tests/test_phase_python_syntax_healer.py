"""
Python syntax healer — детерминированный ремонт missing colon / отступов.

Тесты гоняют `PythonSyntaxHealer.heal(...)` напрямую на временных файлах и
проверяют, что возвращается рабочий unified diff либо корректное "HEALED",
что патч действительно чинит исходную ошибку (после применения файл парсится
`ast.parse`).

Сценарии берут реальный кейс пользователя: `def init_db()` без двоеточия +
строка с отступом 2 пробела вместо 4.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.language_syntax.python_healer import PythonSyntaxHealer

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ----------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------
def apply_unified_diff(original: str, patch: str) -> str:
    """Минимальный апплаер одно-/одно-line @@ хедеров (нам этого достаточно)."""
    lines_orig = original.splitlines(keepends=True)
    out = list(lines_orig)
    cursor = 0
    in_hunk = False
    hunk_old_start = 0
    hunk_idx = 0
    for line in patch.splitlines(keepends=True):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            # @@ -L,N +L,N @@
            header = line.split("@@")[1].strip()
            parts = header.split(" ")
            old_part = parts[0]  # «-L,N»
            old_l = int(old_part[1:].split(",")[0])
            hunk_old_start = old_l
            hunk_idx = 0
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("-"):
            # удаляем строку с индексом (hunk_old_start - 1 + hunk_idx)
            del_idx = hunk_old_start - 1 + hunk_idx
            if 0 <= del_idx < len(out):
                out.pop(del_idx)
            # hunk_idx не сдвигается (следующая строка займёт его место)
        elif line.startswith("+"):
            ins_idx = hunk_old_start - 1 + hunk_idx
            new_text = line[1:]
            out.insert(ins_idx, new_text)
            hunk_idx += 1
        else:
            hunk_idx += 1
    return "".join(out)


def write_tmp(content: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    p = Path(name)
    p.write_text(content, encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# 1. Missing colon на `def`
# ----------------------------------------------------------------------
def test_missing_colon_after_def():
    src = "def init_db()\n    pass\n"
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        # Имитируем то, что flake8 даёт E999 на строке 1.
        err = {"file": p.name, "line": 1, "code": "E999",
               "message": "SyntaxError: expected ':'"}
        patch = h.heal(err, p)
        check("colon_returns_patch", isinstance(patch, str) and patch and patch != "HEALED")
        if not isinstance(patch, str) or not patch:
            return
        fixed = apply_unified_diff(src, patch)
        check("colon_added", fixed.splitlines()[0].rstrip() == "def init_db():")
        try:
            ast.parse(fixed)
            check("colon_parses", True)
        except SyntaxError as e:
            check("colon_parses", False)
            print("    parse err:", e)
    finally:
        p.unlink(missing_ok=True)


def test_missing_colon_after_if():
    src = "x = 1\nif x == 1\n    print('ok')\n"
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        err = {"file": p.name, "line": 2, "code": "E999",
               "message": "expected ':'"}
        patch = h.heal(err, p)
        check("if_colon_returns_patch", isinstance(patch, str) and patch)
        if not patch:
            return
        fixed = apply_unified_diff(src, patch)
        check("if_colon_added", fixed.splitlines()[1].rstrip() == "if x == 1:")
        try:
            ast.parse(fixed)
            check("if_colon_parses", True)
        except SyntaxError:
            check("if_colon_parses", False)
    finally:
        p.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# 2. IndentationError: меньший отступ, чем у соседей блока
# ----------------------------------------------------------------------
def test_indentation_two_spaces_should_be_four():
    # точная копия проблемы пользователя
    src = (
        "def init_db():\n"
        "    conn = 1\n"
        "  c = conn\n"          # должно быть 4 пробела
        "    c2 = 2\n"
    )
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        err = {"file": p.name, "line": 3, "code": "E999",
               "message": "IndentationError: unindent does not match any outer indentation level"}
        patch = h.heal(err, p)
        check("indent_returns_patch", isinstance(patch, str) and patch and patch != "HEALED")
        if not isinstance(patch, str) or not patch:
            return
        fixed = apply_unified_diff(src, patch)
        third = fixed.splitlines()[2]
        check("indent_fixed_to_4", third.startswith("    ") and not third.startswith("     "))
        try:
            ast.parse(fixed)
            check("indent_parses", True)
        except SyntaxError as e:
            check("indent_parses", False)
            print("    parse err:", e)
    finally:
        p.unlink(missing_ok=True)


def test_indentation_after_colon_no_indent():
    src = (
        "def f():\n"
        "x = 1\n"     # нет отступа — должен стать 4 пробела
    )
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        err = {"file": p.name, "line": 2, "code": "E999",
               "message": "IndentationError: expected an indented block"}
        patch = h.heal(err, p)
        check("indent_after_colon_patch", isinstance(patch, str) and patch)
        if not patch:
            return
        fixed = apply_unified_diff(src, patch)
        second = fixed.splitlines()[1]
        check("indent_after_colon_4", second.startswith("    "))
    finally:
        p.unlink(missing_ok=True)


def test_indentation_flake8_codes_trigger():
    """Если flake8 отдал W191/E111, без слова 'indent' в message, всё равно
    должен сработать индент-хилер."""
    src = "def f():\n    a = 1\n  b = 2\n"
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        err = {"file": p.name, "line": 3, "code": "E111",
               "message": "indentation is not a multiple of 4"}
        patch = h.heal(err, p)
        check("flake8_e111_triggers", isinstance(patch, str) and patch)
        if patch:
            fixed = apply_unified_diff(src, patch)
            check("flake8_e111_4spaces", fixed.splitlines()[2].startswith("    "))
    finally:
        p.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# 3. Полный сценарий пользователя: colon + indent — две правки подряд
# ----------------------------------------------------------------------
def test_user_scenario_two_step():
    src = (
        "import sqlite3\n"
        "\n"
        "def init_db()\n"             # шаг 1: missing colon
        "    conn = sqlite3.connect('x')\n"
        "  c = conn.cursor()\n"       # шаг 2: indent 2 → 4
        "    c.execute('SELECT 1')\n"
        "    conn.close()\n"
    )
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        # Step 1: colon
        err1 = {"file": p.name, "line": 3, "code": "E999",
                "message": "SyntaxError: expected ':'"}
        patch1 = h.heal(err1, p)
        check("user_step1_patch", bool(patch1) and patch1 != "HEALED")
        if not patch1 or patch1 == "HEALED":
            return
        after1 = apply_unified_diff(src, patch1)
        p.write_text(after1, encoding="utf-8")
        check("user_step1_colon_added", after1.splitlines()[2] == "def init_db():")
        try:
            ast.parse(after1)
            check("user_step1_still_indent_err", False)  # ожидаем что indent остался
        except IndentationError:
            check("user_step1_still_indent_err", True)
        except SyntaxError:
            # Может быть SyntaxError из-за индента — тоже окей
            check("user_step1_still_indent_err", True)

        # Step 2: indent
        err2 = {"file": p.name, "line": 5, "code": "E999",
                "message": "IndentationError: unindent does not match any outer indentation level"}
        patch2 = h.heal(err2, p)
        check("user_step2_patch", bool(patch2) and patch2 != "HEALED")
        if not patch2 or patch2 == "HEALED":
            return
        after2 = apply_unified_diff(after1, patch2)
        check("user_step2_indent_fixed",
              after2.splitlines()[4].startswith("    c = conn.cursor()"))
        # ИТОГ: файл должен парситься чисто.
        try:
            ast.parse(after2)
            check("user_final_parses", True)
        except SyntaxError as e:
            check("user_final_parses", False)
            print("    final parse err:", e)
    finally:
        p.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# 4. None-сценарии: ничего не делаем там, где нечего чинить
# ----------------------------------------------------------------------
def test_no_op_when_clean():
    src = "def f():\n    return 1\n"
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        err = {"file": p.name, "line": 2, "code": "E111",
               "message": "indentation is not a multiple of 4"}
        # Уже 4 пробела и нет таб — ничего не должно произойти.
        patch = h.heal(err, p)
        check("noop_returns_none", patch is None)
    finally:
        p.unlink(missing_ok=True)


def test_unknown_code_returns_none():
    src = "x = 1\n"
    p = write_tmp(src)
    try:
        h = PythonSyntaxHealer()
        err = {"file": p.name, "line": 1, "code": "F401",
               "message": "imported but unused"}
        patch = h.heal(err, p)
        check("unknown_code_none", patch is None)
    finally:
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    print("Python syntax healer smoke:")
    test_missing_colon_after_def()
    test_missing_colon_after_if()
    test_indentation_two_spaces_should_be_four()
    test_indentation_after_colon_no_indent()
    test_indentation_flake8_codes_trigger()
    test_user_scenario_two_step()
    test_no_op_when_clean()
    test_unknown_code_returns_none()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nPython syntax healer: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
