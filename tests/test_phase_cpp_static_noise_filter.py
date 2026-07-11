"""
C++ статический анализ — фильтр стилевого шума (инцидент fmt, 2026-07-10).

МОТИВАЦИЯ. clang-tidy с дефолтным/проектным чек-сетом включает семейства
`modernize-*`/`readability-*`/`misc-*`/`cppcoreguidelines-*` — это СТИЛЕВЫЕ
политики автора для контрибьюторов, а не дефекты. На header-only fmt один
`modernize-use-trailing-return-type` дал 2078 «находок»; всего static_extra
выдавал 2314, реальный сигнал тонул, движок уходил в project_timeout
(initial_error_count=2315). Аналогично cppcheck `style,performance` даёт вал
`functionStatic`/`passedByValue`/`useInitializationList`.

ИНВАРИАНТ (что кодирует тест): наша система чинит БАГИ, а не навязывает стиль.
  * clang-tidy: чек-сет ограничен `bugprone-*`/`clang-analyzer-*`, стилевые
    семейства подавлены ведущим `-*`; `clang-diagnostic-*` (фантомные
    compile-ошибки без билд-конфига) отбрасываются — авторитет g++.
  * cppcheck: точечный денилист `_CPPCHECK_NOISE_IDS` гасит оптимизационный/
    config-шум, СОХРАНЯЯ высокосигнальные style-коды (noExplicitConstructor).

Запуск: python tests/test_phase_cpp_static_noise_filter.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analyzers.cpp_static_extra import (  # noqa: E402
    run_cppcheck, run_clang_tidy, _TIDY_CHECKS, _CPPCHECK_NOISE_IDS,
)

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def test_tidy_checks_are_bug_focused():
    """Чек-сет начинается с `-*` (глушит проектный/дефолтный конфиг) и
    включает только bug-семейства; стилевых семейств быть не должно."""
    check("tidy_leads_with_disable_all", _TIDY_CHECKS.startswith("-*"))
    check("tidy_has_bugprone", "bugprone-*" in _TIDY_CHECKS)
    check("tidy_has_analyzer", "clang-analyzer-*" in _TIDY_CHECKS)
    # ни одно стилевое семейство не включено (только как отрицание с '-' допустимо)
    for fam in ("modernize-*", "readability-*", "misc-*", "cppcoreguidelines-*",
                "performance-*", "llvm-*", "google-*", "fuchsia-*"):
        check(f"tidy_excludes_{fam}", fam not in _TIDY_CHECKS.replace("-" + fam, ""))


# clang-tidy: фантомные compile-ошибки на файле вне compile_db отбрасываются.
_TIDY_FIXTURE = """\
/tmp/proj/src/a.cpp:10:5: warning: possible null dereference [clang-analyzer-core.NullDereference]
/tmp/proj/src/a.cpp:20:8: error: use of undeclared identifier 'foo' [clang-diagnostic-error]
/tmp/proj/src/b.cpp:3:1: warning: throwing in static init [bugprone-throwing-static-initialization]
"""


def test_clang_diagnostic_dropped_signal_kept():
    project = Path(__file__).resolve().parent
    fake = MagicMock(stdout=_TIDY_FIXTURE, stderr="", returncode=0)
    # эмулируем наличие .cpp-файлов
    with patch("analyzers.cpp_static_extra.shutil.which", return_value="/usr/bin/clang-tidy"), \
         patch("analyzers.cpp_static_extra.subprocess.run", return_value=fake), \
         patch("pathlib.Path.rglob", return_value=[Path("/tmp/proj/src/a.cpp")]):
        errors = run_clang_tidy(project)
    codes = {e["code"] for e in errors}
    check("analyzer_finding_kept", "clang-analyzer-core.NullDereference" in codes)
    check("bugprone_finding_kept", "bugprone-throwing-static-initialization" in codes)
    check("clang_diagnostic_dropped", "clang-diagnostic-error" not in codes)


_CPPCHECK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<results version="2">
  <cppcheck version="2.19"/>
  <errors>
    <error id="nullPointer" severity="error" msg="Null pointer dereference">
      <location file="src/a.cpp" line="12" column="5"/>
    </error>
    <error id="functionStatic" severity="performance" msg="could be static">
      <location file="src/a.cpp" line="3" column="1"/>
    </error>
    <error id="passedByValue" severity="performance" msg="pass by const ref">
      <location file="src/a.cpp" line="5" column="1"/>
    </error>
    <error id="noExplicitConstructor" severity="style" msg="ctor should be explicit">
      <location file="src/a.cpp" line="8" column="3"/>
    </error>
    <error id="unknownMacro" severity="error" msg="unknown macro">
      <location file="src/a.cpp" line="1" column="1"/>
    </error>
  </errors>
</results>
"""


def test_cppcheck_denylist_filters_noise_keeps_signal():
    project = Path(__file__).resolve().parent
    fake = MagicMock(stdout="", stderr=_CPPCHECK_XML, returncode=0)
    with patch("analyzers.cpp_static_extra.shutil.which", return_value="/usr/bin/cppcheck"), \
         patch("analyzers.cpp_static_extra._has_cxx_sources", return_value=True):
        with patch("analyzers.cpp_static_extra.subprocess.run", return_value=fake):
            errors = run_cppcheck(project)
    codes = {e["code"] for e in errors}
    check("cppcheck_nullptr_kept", "cppcheck::nullPointer" in codes)
    check("cppcheck_explicit_kept", "cppcheck::noExplicitConstructor" in codes)
    check("cppcheck_functionStatic_dropped", "cppcheck::functionStatic" not in codes)
    check("cppcheck_passedByValue_dropped", "cppcheck::passedByValue" not in codes)
    check("cppcheck_unknownMacro_dropped", "cppcheck::unknownMacro" not in codes)
    check("denylist_has_perf_noise", "functionStatic" in _CPPCHECK_NOISE_IDS
          and "passedByValue" in _CPPCHECK_NOISE_IDS)


if __name__ == "__main__":
    test_tidy_checks_are_bug_focused()
    test_clang_diagnostic_dropped_signal_kept()
    test_cppcheck_denylist_filters_noise_keeps_signal()
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"cpp_static_noise_filter: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
