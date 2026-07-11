"""
C++ cmake-configure устойчивость: отключение тестов/примеров/бенчмарков
(2026-07-10, серия, leveldb).

ИНЦИДЕНТ: у многих C++-проектов cmake configure тянет внешние тест-зависимости
(GTest/Catch2/benchmark). Если их нет в системе, `find_package(... REQUIRED)`
роняет configure → нет compile_commands.json → анализатор падает в эвристику →
флуд фантомов (GCC_INCLUDE/GCC_UNDECLARED/syntaxError; leveldb: 299+). Фикс:
детектить проектные `option(<NAME>_BUILD_TESTS ...)` и пр. + `-DBUILD_TESTING=OFF`,
передавать `-D<NAME>=OFF` в configure — не собирать тесты, не тянуть их deps.

ИНВАРИАНТ: детектим ИМЕННО тест/пример/бенчмарк/докс-опции, НЕ трогаем
функциональные опции проекта (иначе можно отключить нужный код и исказить скан).

NB: не панацея — проект с БЕЗУСЛОВНЫМ `find_package(GTest REQUIRED)` вне гейта
опции (leveldb, стр.120) не чинится флагами; там нужен установленный GTest.

Запуск: python tests/test_phase_cpp_cmake_disable_tests.py
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analyzers.cpp_analyzer import CppAnalyzer  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


A = CppAnalyzer()


def test_detects_test_example_benchmark_options(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(
        'option(LEVELDB_BUILD_TESTS "Build tests" ON)\n'
        'option(LEVELDB_BUILD_BENCHMARKS "Build benchmarks" ON)\n'
        'option(FOO_BUILD_EXAMPLES "" ON)\n'
        'option(FOO_ENABLE_TESTING "" ON)\n'
        'option(FOO_BUILD_DOCS "" ON)\n', encoding="utf-8")
    flags = A._cmake_disable_flags(tmp_path)
    check("has_build_testing_off", "-DBUILD_TESTING=OFF" in flags)
    check("tests_off", "-DLEVELDB_BUILD_TESTS=OFF" in flags)
    check("benchmarks_off", "-DLEVELDB_BUILD_BENCHMARKS=OFF" in flags)
    check("examples_off", "-DFOO_BUILD_EXAMPLES=OFF" in flags)
    check("enable_testing_off", "-DFOO_ENABLE_TESTING=OFF" in flags)
    check("docs_off", "-DFOO_BUILD_DOCS=OFF" in flags)
    check("all_are_OFF", all(f.endswith("=OFF") for f in flags))


def test_does_not_touch_functional_options(tmp_path):
    """НЕ отключать функциональные опции — только тест/пример/бенч/докс."""
    (tmp_path / "CMakeLists.txt").write_text(
        'option(FOO_USE_SSL "" ON)\n'
        'option(FOO_SHARED_LIBS "" ON)\n'
        'option(FOO_WITH_ZLIB "" ON)\n'
        'option(FOO_BUILD_TESTS "" ON)\n', encoding="utf-8")
    flags = A._cmake_disable_flags(tmp_path)
    joined = " ".join(flags)
    check("ssl_untouched", "FOO_USE_SSL" not in joined)
    check("shared_untouched", "FOO_SHARED_LIBS" not in joined)
    check("zlib_untouched", "FOO_WITH_ZLIB" not in joined)
    check("tests_still_off", "-DFOO_BUILD_TESTS=OFF" in flags)


def test_no_cmakelists_returns_baseline(tmp_path):
    """Без CMakeLists — только стандартный -DBUILD_TESTING=OFF."""
    flags = A._cmake_disable_flags(tmp_path)
    check("baseline_only", flags == ["-DBUILD_TESTING=OFF"])


if __name__ == "__main__":
    for fn in (test_detects_test_example_benchmark_options,
               test_does_not_touch_functional_options,
               test_no_cmakelists_returns_baseline):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"cpp_cmake_disable_tests: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
