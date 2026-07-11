"""
Regress C2 (PROJECT_AUDIT_REPORT 2026-07-01): файлы с E999 полностью выпадали
из символьной защиты. `_python_symbols` при SyntaxError возвращал пустые
множества БЕЗ regex-фолбэка (в отличие от _python_symbol_counts) →
check_symbol_regression уходил в ранний return `empty_before`/ok=True.

Значит весь путь ремонта синтаксиса (SyntaxRepairStage llm_region,
disaster_recovery, broken_file_mode) не был защищён ни per-patch guard-ом
(before не парсится), ни финальным аудитом (оригинал с E999 так же не
парсится): LLM-«ремонт», стирающий половину функций, проходил все рубежи.

Фикс: regex-фолбэк для defs/classes/imports; при непарсибельном before обе
стороны сравниваются regex-vs-regex (одним способом сбора).

Запуск: python tests/test_phase_symbol_regression_e999_fallback.py
"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.symbol_regression import check_symbol_regression  # noqa: E402

# before: две функции + импорт, синтаксическая ошибка в конце (E999-файл)
_BROKEN_BEFORE = (
    "import os\n"
    "from typing import List, Optional\n"
    "\n"
    "def keep_me(x):\n"
    "    return x + 1\n"
    "\n"
    "def lose_me(y):\n"
    "    return y * 2\n"
    "\n"
    "def broken(:\n"  # SyntaxError
)


# --- 1. LLM-«ремонт» стёр функцию → regression, а не empty_before ---
def test_e999_before_lost_def_flagged():
    after = (
        "import os\n"
        "from typing import List, Optional\n"
        "\n"
        "def keep_me(x):\n"
        "    return x + 1\n"
        "\n"
        "def broken(z):\n"
        "    return z\n"
    )
    r = check_symbol_regression(_BROKEN_BEFORE, after, "python")
    assert r["ok"] is False, f"потеря lose_me на E999-файле обязана детектироваться: {r}"
    assert "lose_me" in r["missing_defs"], r
    assert r["regex_fallback"] is True, r


# --- 2. Ремонт сохранил все имена → ok (нет ложного срабатывания) ---
def test_e999_before_all_names_kept_ok():
    after = (
        "import os\n"
        "from typing import List, Optional\n"
        "\n"
        "def keep_me(x):\n"
        "    return x + 1\n"
        "\n"
        "def lose_me(y):\n"
        "    return y * 2\n"
        "\n"
        "def broken(z):\n"
        "    return z\n"
    )
    r = check_symbol_regression(_BROKEN_BEFORE, after, "python")
    assert r["ok"] is True, f"все имена сохранены — не должно флагаться: {r}"


# --- 3. Потеря импортированного имени на E999-файле тоже детектируется ---
def test_e999_before_lost_import_flagged():
    after = (
        "import os\n"
        "\n"
        "def keep_me(x):\n"
        "    return x + 1\n"
        "\n"
        "def lose_me(y):\n"
        "    return y * 2\n"
        "\n"
        "def broken(z):\n"
        "    return z\n"
    )
    r = check_symbol_regression(_BROKEN_BEFORE, after, "python")
    assert r["ok"] is False, f"потеря from-import имён обязана детектироваться: {r}"
    assert "List" in r["missing_imports"] and "Optional" in r["missing_imports"], r


# --- 4. Класс, потерянный на E999-файле ---
def test_e999_before_lost_class_flagged():
    before = "class Keep:\n    pass\n\nclass Lose:\n    pass\n\ndef broken(:\n"
    after = "class Keep:\n    pass\n\ndef broken(z):\n    return z\n"
    r = check_symbol_regression(before, after, "python")
    assert r["ok"] is False and "Lose" in r["missing_classes"], r


# --- 5. Схема результата едина на всех путях выхода (M8) ---
def test_result_schema_uniform():
    keys = None
    for args in [
        (None, "x", "python"),                        # non_string_input
        ("def a(): pass\n", "def a(): pass\n", "??"),  # unknown_language
        ("", "def a(): pass\n", "python"),             # empty_before
        ("def a(): pass\n", "def a(): pass\n", "python"),  # ok
        ("def a(): pass\n", "", "python"),             # regression
    ]:
        r = check_symbol_regression(*args)
        if keys is None:
            keys = set(r.keys())
        assert set(r.keys()) == keys, f"схема расходится на {args}: {set(r.keys())} != {keys}"
        for k in ("before_defs", "after_defs", "before_classes", "after_classes",
                  "missing_defs", "missing_classes", "missing_imports"):
            assert isinstance(r[k], list), f"{k} должен быть list (JSON-safe), получили {type(r[k])}"


# --- 6. Валидный before по-прежнему идёт через AST (без regex-шума) ---
def test_parsed_before_uses_ast():
    before = 'S = """\ndef fake_in_string(x):\n"""\n\ndef real(x):\n    return x\n'
    after = 'S = """\n"""\n\ndef real(x):\n    return x\n'
    r = check_symbol_regression(before, after, "python")
    assert r["ok"] is True, f"def внутри строки не должен считаться символом при AST-пути: {r}"
    assert r["regex_fallback"] is False


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nSymbol regression E999 fallback: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
