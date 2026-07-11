"""
C++ сужение области скана (2026-07-10) — перф-фиксы №1 и №2.

№1: в C++-прогоне патч НЕ-C++-файла (.py/.yml/.md) не может изменить вывод
CppAnalyzer → полный пере-скан не нужен, переиспользуем снимок. Тест кодирует
классификацию «C++-исходник vs нет» (от неё зависит reuse).

№2: полный static-скан (cppcheck/clang-tidy) ограничивается файлами реального
билда из compile_commands (не всё дерево из ~140 файлов), вендорный код
(gtest/gmock/third_party) исключён. Тест кодирует _db_source_files и
_cpp_excluded.

ИНВАРИАНТ: сужаем ГДЕ ищем (билд-файлы, не вендор/докиs), не ЧТО ищем
(bug-семейства). Это перф-оптимизация, а не ослабление проверок.

Запуск: python tests/test_phase_cpp_scan_scope.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analyzers.cpp_analyzer import CppAnalyzer, _cpp_excluded  # noqa: E402
from core.stages.validate_stage import ValidateStage  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def test_cpp_src_classification_for_reuse():
    """_CPP_SRC_SUFFIXES: .py/.yml/.md — НЕ C++-исходник (→ reuse снимка);
    .cc/.c/.h/.hpp — C++-исходник (→ реальный скан)."""
    S = ValidateStage._CPP_SRC_SUFFIXES
    def is_src(f): return f.lower().endswith(S)
    check("py_not_cpp_src", not is_src("support/tool.py"))
    check("yml_not_cpp_src", not is_src(".github/dependabot.yml"))
    check("md_not_cpp_src", not is_src("README.md"))
    check("cc_is_cpp_src", is_src("src/format.cc"))
    check("c_is_cpp_src", is_src("lib/x.c"))
    check("header_is_cpp_src", is_src("include/fmt/format.h"))
    check("hpp_is_cpp_src", is_src("inc/foo.hpp"))


def test_db_source_files_filters():
    """_db_source_files: только реальные пути-исходники (TU), без ::name::
    дублей и без вендорного кода (gtest/third_party)."""
    a = CppAnalyzer()
    db = {
        "/proj/src/a.cc": ["-std=c++17"],
        "::name::a.cc": ["-std=c++17"],
        "/proj/test/gtest/gmock-gtest-all.cc": ["-std=c++17"],  # вендор
        "/proj/third_party/dep/x.cpp": ["-std=c++17"],          # вендор
        "/proj/include/foo.h": ["-std=c++17"],                  # заголовок — не TU
        "/proj/test/my_test.cc": ["-std=c++17"],                # наш тест — оставить
    }
    got = set(a._db_source_files(db))
    check("keeps_real_source", "/proj/src/a.cc" in got)
    check("keeps_own_test", "/proj/test/my_test.cc" in got)
    check("drops_name_alias", "::name::a.cc" not in got)
    check("drops_header", "/proj/include/foo.h" not in got)
    check("drops_vendored_gtest", "/proj/test/gtest/gmock-gtest-all.cc" not in got)
    check("drops_third_party", "/proj/third_party/dep/x.cpp" not in got)


def test_cpp_excluded_vendored():
    """_cpp_excluded отсекает вендорные/служебные каталоги."""
    check("excl_gtest", _cpp_excluded(Path("proj/test/gtest/x.cc")))
    check("excl_gmock", _cpp_excluded(Path("proj/gmock/y.cc")))
    check("excl_third_party", _cpp_excluded(Path("proj/third_party/z.cpp")))
    check("excl_extern", _cpp_excluded(Path("proj/extern/dep/a.cc")))
    check("keeps_own_src", not _cpp_excluded(Path("proj/src/format.cc")))
    check("keeps_own_test", not _cpp_excluded(Path("proj/test/format-test.cc")))


if __name__ == "__main__":
    test_cpp_src_classification_for_reuse()
    test_db_source_files_filters()
    test_cpp_excluded_vendored()
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"cpp_scan_scope: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
