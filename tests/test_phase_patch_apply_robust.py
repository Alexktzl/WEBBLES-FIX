"""
PatchEngine.apply_patch — устойчивость к плохим @@-хедерам и сдвигам контекста.

Реальный кейс из логов прогона на Python:
LLM прислал корректный body diff'а («разнести print + else на две строки»),
но в `@@ -11,7 +11,8 @@` число «7» завышено на 1, а старт-смещение `11` не
бьётся с реальным файлом. Старая реализация PatchEngine верила заголовку, в
результате:
  - дублировалась строка `query = ...`,
  - съедался `def hash_password(pwd):`.

Тесты проверяют, что новый apply_patch:
  1) пересчитывает счётчики из body, игнорируя завышенный old_count;
  2) дрейфует ±5 строк через difflib, чтобы найти реальный контекст;
  3) отказывает (return False), если контекст не находится — не калечит файл;
  4) сохраняет совместимость со старыми кейсами (точный заголовок).

Запуск: python3 tests/test_phase_patch_apply_robust.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.patch_engine import PatchEngine

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def write_tmp(content: str, name: str = "x.py") -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# 1. Реальный кейс из лога: завышенный old_count + сдвиг на 1 строку
# ----------------------------------------------------------------------
_USER_AUTH_BEFORE = (
    "# auth.py\n"
    "import sqlite3\n"
    "import hashlib\n"
    "import nonexistent_module\n"
    "def login():\n"
    "    username = input('Логин: ')\n"
    "    conn = sqlite3.connect('notes.db')\n"
    "    cursor = conn.cursor()\n"
    "    # SQL-инъекция\n"
    "    query = 'SELECT'\n"
    "    user = cursor.fetchone()\n"
    "    if user:\n"
    "        print('Вход выполнен')    else:\n"
    "        print('Неверные данные')\n"
    "    conn.close()\n"
    "\n"
    "def hash_password(pwd):\n"
    "    # md5\n"
    "    return hashlib.md5(pwd.encode()).hexdigest()\n"
)
# LLM-патч: body корректен, но @@-хедер ВРЁТ.
# Реально удаляется 1 строка, добавляется 2 → итог +1 строка.
# Хедер должен быть `-13,3 +13,4` (старт=13 — «print('Вход выполнен')    else:»),
# но LLM прислал `-11,7 +11,8` (старт на 2 строки выше, count завышен).
_USER_BAD_PATCH = (
    "--- a/auth.py\n"
    "+++ b/auth.py\n"
    "@@ -11,7 +11,8 @@\n"
    "     query = 'SELECT'\n"
    "     user = cursor.fetchone()\n"
    "     if user:\n"
    "-        print('Вход выполнен')    else:\n"
    "+        print('Вход выполнен')\n"
    "+    else:\n"
    "         print('Неверные данные')\n"
    "     conn.close()\n"
)


def test_user_real_case_fixed_correctly():
    p = write_tmp(_USER_AUTH_BEFORE, "auth.py")
    eng = PatchEngine()
    ok = eng.apply_patch(p, _USER_BAD_PATCH)
    check("user_apply_ok", ok is True)
    out = p.read_text(encoding="utf-8")
    lines = out.splitlines()
    # Главные проверки:
    # 1. Нет дубля query.
    queries = [i for i, ln in enumerate(lines) if "query = 'SELECT'" in ln]
    check("user_no_query_dup", len(queries) == 1)
    # 2. def hash_password сохранён.
    check("user_hash_def_preserved", "def hash_password(pwd):" in out)
    # 3. return hashlib.md5 сохранён на отдельной строке.
    check("user_return_preserved", "    return hashlib.md5(pwd.encode()).hexdigest()" in out)
    # 4. `else:` на отдельной строке.
    has_else_alone = any(ln.strip() == "else:" for ln in lines)
    check("user_else_on_own_line", has_else_alone)
    # 5. `print('Вход выполнен')` тоже на отдельной строке (без `    else:`)
    has_print_clean = any(ln.rstrip() == "        print('Вход выполнен')" for ln in lines)
    check("user_print_clean", has_print_clean)


# ----------------------------------------------------------------------
# 2. Точный хедер (как раньше) — продолжает работать
# ----------------------------------------------------------------------
def test_exact_header_still_works():
    src = "a\nb\nc\nd\ne\n"
    patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -2,3 +2,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    # Старт ханка указывает на строку 2 (контекст 'a'). Хотя 'a' реально
    # стоит на строке 1, ideal=1 не совпадёт, но fuzzy дрейфнёт на -1.
    # Чтобы тест действительно проверял exact-path, сделаем правильный старт.
    patch2 = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    p = write_tmp(src, "x.py")
    eng = PatchEngine()
    ok = eng.apply_patch(p, patch2)
    check("exact_apply_ok", ok is True)
    out = p.read_text(encoding="utf-8")
    check("exact_b_replaced", "B\n" in out and out.splitlines()[1] == "B")
    check("exact_no_corruption", out == "a\nB\nc\nd\ne\n")


# ----------------------------------------------------------------------
# 3. Завышенный old_count, контекст СОВПАДАЕТ — берём счётчики из body
# ----------------------------------------------------------------------
def test_inflated_old_count():
    src = "L1\nL2\nL3\nL4\nL5\n"
    # body: 1 context + 1 remove + 1 context = old=2; new=2 context = new=2.
    # Заголовок врёт: -2,5 +2,5
    patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -2,5 +2,5 @@\n"
        " L2\n"
        "-L3\n"
        "+X3\n"
        " L4\n"
    )
    p = write_tmp(src, "x.py")
    eng = PatchEngine()
    ok = eng.apply_patch(p, patch)
    check("inflated_apply_ok", ok is True)
    out = p.read_text(encoding="utf-8")
    # Должно стать L1, L2, X3, L4, L5
    check("inflated_result", out == "L1\nL2\nX3\nL4\nL5\n")


# ----------------------------------------------------------------------
# 4. Сдвиг ±N строк — fuzzy находит правильное место
# ----------------------------------------------------------------------
def test_drift_offset():
    src = "h1\nh2\nh3\nh4\ntarget\nh6\nh7\n"
    # Реально 'target' на строке 5, но LLM написал старт на строке 2.
    patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -2,1 +2,1 @@\n"
        "-target\n"
        "+FIXED\n"
    )
    p = write_tmp(src, "x.py")
    eng = PatchEngine()
    ok = eng.apply_patch(p, patch)
    check("drift_apply_ok", ok is True)
    out = p.read_text(encoding="utf-8")
    check("drift_fixed", out == "h1\nh2\nh3\nh4\nFIXED\nh6\nh7\n")


# ----------------------------------------------------------------------
# 5. Контекст не находится → отказ, файл не повреждён
# ----------------------------------------------------------------------
def test_no_match_refuses():
    src = "ONLY\nTHESE\nLINES\n"
    patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,2 @@\n"
        " not_in_file\n"
        "-also_not\n"
        "+inserted\n"
    )
    p = write_tmp(src, "x.py")
    eng = PatchEngine()
    ok = eng.apply_patch(p, patch)
    check("nomatch_refused", ok is False)
    out = p.read_text(encoding="utf-8")
    check("nomatch_file_intact", out == src)


# ----------------------------------------------------------------------
# 6. Pure-insert (нет старого контекста) — применяется по offset как есть
# ----------------------------------------------------------------------
def test_pure_insert_no_context():
    src = "a\nb\nc\n"
    patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -2,0 +2,1 @@\n"
        "+X\n"
    )
    p = write_tmp(src, "x.py")
    eng = PatchEngine()
    ok = eng.apply_patch(p, patch)
    check("pure_insert_ok", ok is True)
    out = p.read_text(encoding="utf-8")
    # Вставили X перед строкой 2 → a, X, b, c
    check("pure_insert_result", out == "a\nX\nb\nc\n")


# ----------------------------------------------------------------------
# 7. LLM-плейсхолдер не попадает в файл
# ----------------------------------------------------------------------
def test_llm_placeholder_skipped():
    src = "a\nb\nc\n"
    patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,4 @@\n"
        " a\n"
        "+# ... existing code\n"
        " b\n"
        " c\n"
    )
    p = write_tmp(src, "x.py")
    eng = PatchEngine()
    ok = eng.apply_patch(p, patch)
    check("placeholder_apply_ok", ok is True)
    out = p.read_text(encoding="utf-8")
    check("placeholder_not_in_file", "existing code" not in out)
    check("placeholder_preserves_rest", out == "a\nb\nc\n")


if __name__ == "__main__":
    print("PatchEngine robust apply smoke:")
    test_user_real_case_fixed_correctly()
    test_exact_header_still_works()
    test_inflated_old_count()
    test_drift_offset()
    test_no_match_refuses()
    test_pure_insert_no_context()
    test_llm_placeholder_skipped()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nPatchEngine robust: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
