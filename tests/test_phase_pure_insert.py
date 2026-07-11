"""
O.16 — PatchEngine.apply_patch: refuse pure-insert в непустой файл.
O.17 — symbol_regression.check_symbol_duplication.

Реальный кейс 2026-06-04 (test_python/main.py): LLM-путь `_handle_critical_syntax`
выдал «полный новый файл» как один большой `+`-ханк БЕЗ `-`-строк.
apply_patch сделал `original_lines[0:0] = new_body` → вставку, не замену.
Получился файл-дубль: 35 строк вместо 19, две `def main(...)` подряд.
DecideStage случайно принял (E999 ушла из новой части, ревью=ok).

Этот suite проверяет:
  1) PatchEngine._detect_line_merge не реагирует на pure-insert (это
     другой класс багов, его ловит O.16).
  2) apply_patch отказывает на pure-insert при непустом файле.
  3) Пустой файл с pure-insert — НОРМАЛЬНО (создание файла).
  4) Маленькая pure-insert (<5 строк, например 1 импорт) — норм.
  5) check_symbol_duplication: Python AST + regex для js/ts/rust/go.
  6) E2E на синтетике точно повторяет main.py-баг.

Запуск: python3 tests/test_phase_pure_insert.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.patch_engine import PatchEngine
from analysis.symbol_regression import check_symbol_duplication

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------------
# 1. apply_patch отказывает на pure-insert в непустой файл
# ---------------------------------------------------------------------

_ORIGINAL_MAIN_PY = (
    "import auth\n"
    "import notes\n"
    "\n"
    "def main():\n"
    "    print('hello'\n"  # синтаксическая ошибка: нет `)`
    "    auth.login()\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    main()\n"
)

# LLM «полный новый файл» как pure-insert (без -строк).
_PURE_INSERT_PATCH = (
    "--- a/main.py\n"
    "+++ b/main.py\n"
    "@@ -1,0 +1,9 @@\n"
    "+import auth\n"
    "+import notes\n"
    "+\n"
    "+def main():\n"
    "+    print('hello')\n"
    "+    auth.login()\n"
    "+\n"
    "+if __name__ == '__main__':\n"
    "+    main()\n"
)


def test_pure_insert_into_nonempty_rejected():
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "main.py"
        target.write_text(_ORIGINAL_MAIN_PY, encoding="utf-8")

        eng = PatchEngine()
        ok = eng.apply_patch(target, _PURE_INSERT_PATCH)
        check("apply_patch: pure-insert rejected", ok is False)
        # Файл не изменился.
        after = target.read_text(encoding="utf-8")
        check("apply_patch(pure-insert): file unchanged",
              after == _ORIGINAL_MAIN_PY)


# ---------------------------------------------------------------------
# 2. Пустой файл — pure-insert РАЗРЕШЕН (создание файла)
# ---------------------------------------------------------------------

def test_pure_insert_into_empty_allowed():
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "new_file.py"
        target.write_text("", encoding="utf-8")  # пустой
        eng = PatchEngine()
        ok = eng.apply_patch(target, _PURE_INSERT_PATCH.replace("main.py", "new_file.py"))
        check("apply_patch: pure-insert into empty file allowed", ok is True)
        after = target.read_text(encoding="utf-8")
        check("apply_patch(empty target): content written",
              "def main" in after)


# ---------------------------------------------------------------------
# 3. Маленькая pure-insert (<5 строк) — РАЗРЕШЕНА
# ---------------------------------------------------------------------

def test_small_pure_insert_allowed():
    """Например, единственный новый импорт — это валидный вставка."""
    original = "import os\n\ndef foo():\n    pass\n"
    small_patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,0 +1,1 @@\n"
        "+import sys\n"
    )
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "x.py"
        target.write_text(original, encoding="utf-8")
        eng = PatchEngine()
        ok = eng.apply_patch(target, small_patch)
        check("apply_patch: small pure-insert (1 line) allowed", ok is True)
        after = target.read_text(encoding="utf-8")
        check("apply_patch(small): import sys appended",
              "import sys" in after)


# ---------------------------------------------------------------------
# 4. Normal patch (с контекстом и `-`-строками) — продолжает работать
# ---------------------------------------------------------------------

def test_normal_replace_patch_works():
    original = (
        "def foo():\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
    )
    normal_patch = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def foo():\n"
        "-    return 1\n"
        "+    return 42\n"
    )
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "m.py"
        target.write_text(original, encoding="utf-8")
        eng = PatchEngine()
        ok = eng.apply_patch(target, normal_patch)
        check("apply_patch(normal): success", ok is True)
        after = target.read_text(encoding="utf-8")
        check("apply_patch(normal): return 42 in file",
              "return 42" in after and "return 1\n" not in after)


# ---------------------------------------------------------------------
# 5. check_symbol_duplication — Python AST
# ---------------------------------------------------------------------

_BEFORE = (
    "import auth\n"
    "\n"
    "def main():\n"
    "    return 1\n"
)


def test_python_no_dup():
    after = _BEFORE.replace("return 1", "return 2")
    r = check_symbol_duplication(_BEFORE, after, "python")
    check("py(no-dup): ok", r["ok"] is True)


def test_python_def_duplicated():
    """Кейс main.py — две `def main()` подряд."""
    after = _BEFORE + "\n" + _BEFORE  # склейка файла с собой
    r = check_symbol_duplication(_BEFORE, after, "python")
    check("py(dup): not ok", r["ok"] is False)
    check("py(dup): main duplicated", "main" in r["duplicated_defs"])


def test_python_class_duplicated():
    before = "class Auth:\n    pass\n"
    after = before + "\n" + before
    r = check_symbol_duplication(before, after, "python")
    check("py(class-dup): not ok", r["ok"] is False)
    check("py(class-dup): Auth duplicated",
          "Auth" in r["duplicated_classes"])


def test_python_dup_only_in_after_not_before():
    """Если before уже содержал дубль, после — тот же дубль → не флажим
    (это не вновь появившаяся дублирующая регрессия)."""
    before = "def x(): pass\ndef x(): pass\n"  # уже было два
    after = before
    r = check_symbol_duplication(before, after, "python")
    check("py(pre-existing dup): ok", r["ok"] is True)


# ---------------------------------------------------------------------
# 6. check_symbol_duplication — Rust regex-фоллбэк
# ---------------------------------------------------------------------

def test_rust_fn_duplicated():
    before = "fn play(p: Player) {}\n"
    after = before + "\n" + before
    r = check_symbol_duplication(before, after, "rust")
    check("rs(dup): not ok", r["ok"] is False)
    check("rs(dup): play duplicated",
          "play" in r["duplicated_defs"])


# ---------------------------------------------------------------------
# 7. check_symbol_duplication — JS regex
# ---------------------------------------------------------------------

def test_js_function_duplicated():
    before = "function process(x) { return x; }\n"
    after = before + "\n" + before
    r = check_symbol_duplication(before, after, "javascript")
    check("js(dup): not ok", r["ok"] is False)
    check("js(dup): process duplicated",
          "process" in r["duplicated_defs"])


# ---------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------

def test_empty_inputs():
    r = check_symbol_duplication("", "", "python")
    check("edge(empty): ok", r["ok"] is True)


def test_non_string_inputs():
    r = check_symbol_duplication(None, None, "python")  # type: ignore[arg-type]
    check("edge(None): ok", r["ok"] is True)


def test_unknown_language():
    r = check_symbol_duplication("def x(): pass", "def x(): pass", "haskell")
    check("edge(haskell): ok", r["ok"] is True)


# ---------------------------------------------------------------------
# 9. E2E: симуляция кейса main.py — apply_patch отказывает,
#    + если симулировать «уже применённый» дубль, check_symbol_duplication ловит
# ---------------------------------------------------------------------

def test_e2e_main_py_scenario():
    """Точно повторяет кейс 2026-06-04: pure-insert новый-файл патч в main.py."""
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "main.py"
        target.write_text(_ORIGINAL_MAIN_PY, encoding="utf-8")
        eng = PatchEngine()
        ok = eng.apply_patch(target, _PURE_INSERT_PATCH)
        check("e2e: pure-insert rejected", ok is False)
        # И вторая защита: если бы патч прошёл, check_symbol_duplication
        # увидел бы дубль `main` после склейки.
        if not ok:
            simulated_after = _ORIGINAL_MAIN_PY.replace(
                "def main():", "def main():"
            ) + _ORIGINAL_MAIN_PY  # двойной конкатенат
            dup = check_symbol_duplication(_ORIGINAL_MAIN_PY,
                                            simulated_after, "python")
            check("e2e: dup-detector would catch",
                  dup["ok"] is False and "main" in dup["duplicated_defs"])


if __name__ == "__main__":
    print("O.16 (pure-insert) + O.17 (symbol-duplication):")
    test_pure_insert_into_nonempty_rejected()
    test_pure_insert_into_empty_allowed()
    test_small_pure_insert_allowed()
    test_normal_replace_patch_works()
    test_python_no_dup()
    test_python_def_duplicated()
    test_python_class_duplicated()
    test_python_dup_only_in_after_not_before()
    test_rust_fn_duplicated()
    test_js_function_duplicated()
    test_empty_inputs()
    test_non_string_inputs()
    test_unknown_language()
    test_e2e_main_py_scenario()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nO.16 + O.17: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
