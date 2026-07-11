"""
C++ инкрементальный анализ (2026-07-10) — ускорение ValidateStage.

МОТИВАЦИЯ. ValidateStage/DecideStage пере-сканировали ВЕСЬ C++-проект
(cmake+cppcheck+clang-tidy по ~140 файлам, минуты) после КАЖДОЙ попытки
патча — главный перф-хвост C++-этапа. Инкрементал: пере-анализ только
изменённого файла.

ГЛАВНЫЙ ИНВАРИАНТ (что кодирует тест). `merge_file_errors(X, errs)` заменяет
в снимке ошибок ТОЛЬКО слайс файла X, ожидая, что `errs` содержит ошибки
исключительно X. Нарушение → задвоение/искажение учёта → риск false ACCEPT.
Поэтому:
  1. Инкрементал допустим ТОЛЬКО для единиц трансляции (.cc/.cpp/.cxx/.c):
     заголовок влияет на все включающие TU, по нему инкрементал неверен →
     fail-closed на полный скан.
  2. Результат analyze(files=[X]) жёстко фильтруется до X — диагностика чужих
     заголовков, всплывшая при компиляции X, отбрасывается.
  3. Не-TU цель (заголовок/.py) в files → полный скан, а НЕ пустой результат
     (иначе merge стёр бы чужие ошибки как «исправленные»).

Запуск: python tests/test_phase_cpp_incremental_analysis.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analyzers.cpp_analyzer import CppAnalyzer  # noqa: E402
from core.stages.validate_stage import ValidateStage  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def test_incremental_lang_gate():
    """_incremental_lang_ok: python — всегда; cpp — только TU; иначе — нет."""
    ok = ValidateStage._incremental_lang_ok
    check("py_any_file", ok("python", "a.py") is True)
    check("cpp_source_tu", ok("cpp", "src/format.cc") is True)
    check("c_source_tu", ok("c", "lib/x.c") is True)
    check("cpp_header_no", ok("cpp", "include/fmt/format.h") is False)
    check("cpp_hpp_no", ok("c++", "inc/foo.hpp") is False)
    check("cpp_py_file_no", ok("cpp", "support/tool.py") is False)
    check("unknown_lang_no", ok("rust", "src/main.rs") is False)


def _mk_project(tmp: Path):
    (tmp / "src").mkdir(parents=True, exist_ok=True)
    (tmp / "inc").mkdir(parents=True, exist_ok=True)
    (tmp / "src" / "a.cc").write_text("#include \"foo.h\"\nint a(){return 0;}\n", encoding="utf-8")
    (tmp / "src" / "b.cc").write_text("int b(){return 1;}\n", encoding="utf-8")
    (tmp / "inc" / "foo.h").write_text("#pragma once\nint foo();\n", encoding="utf-8")
    return tmp


def test_incremental_restricts_and_filters(tmp_path):
    """analyze(files=[a.cc]): компилируется ТОЛЬКО a.cc; диагностика чужого
    заголовка inc/foo.h отфильтрована (safety-инвариант merge_file_errors)."""
    proj = _mk_project(tmp_path)
    a = CppAnalyzer()

    compiled_files = []

    def fake_run(cmd, *args, **kwargs):
        # эмулируем g++: запоминаем, какие файлы компилировались, и отдаём
        # диагностику И на целевой .cc, И на включённый заголовок.
        class R:
            returncode = 1
            stdout = ""
            stderr = (
                f"{proj}/src/a.cc:2:5: warning: unused variable [-Wunused]\n"
                f"{proj}/inc/foo.h:2:1: warning: header nit [-Wextra]\n"
            )
        for tok in cmd:
            if str(tok).endswith(".cc") or str(tok).endswith(".c"):
                compiled_files.append(str(tok))
        return R()

    # heuristic-путь (без compile_commands) + без реальных статических тулов
    with patch.object(CppAnalyzer, "_find_or_gen_compile_db", return_value=None), \
         patch("analyzers.cpp_analyzer.subprocess.run", side_effect=fake_run):
        errs = a.analyze(proj, files=[proj / "src" / "a.cc"], static_extra=False)

    # 1. компилировался только a.cc, не b.cc
    check("only_target_compiled", all("a.cc" in f for f in compiled_files),
          f"compiled: {compiled_files}")
    check("sibling_not_compiled", not any("b.cc" in f for f in compiled_files),
          f"compiled: {compiled_files}")
    # 2. в результате — только ошибки a.cc; заголовок отфильтрован
    files = {str(e.get("file", "")).replace("\\", "/").rsplit("/", 1)[-1] for e in errs}
    check("target_error_kept", "a.cc" in files, f"files: {files}")
    check("foreign_header_filtered", "foo.h" not in files, f"files: {files}")


def test_header_target_falls_back_to_full(tmp_path):
    """analyze(files=[foo.h]): заголовок — не TU → ПОЛНЫЙ скан (компилируются
    ОБА .cc), а не пустой/частичный результат."""
    proj = _mk_project(tmp_path)
    a = CppAnalyzer()
    compiled_files = []

    def fake_run(cmd, *args, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        for tok in cmd:
            if str(tok).endswith(".cc") or str(tok).endswith(".c"):
                compiled_files.append(str(tok))
        return R()

    with patch.object(CppAnalyzer, "_find_or_gen_compile_db", return_value=None), \
         patch("analyzers.cpp_analyzer.subprocess.run", side_effect=fake_run):
        a.analyze(proj, files=[proj / "inc" / "foo.h"], static_extra=False)

    # полный скан: оба исходника скомпилированы (заголовок не ограничил set)
    names = {Path(f).name for f in compiled_files}
    check("full_scan_both_sources", {"a.cc", "b.cc"} <= names, f"compiled: {names}")


if __name__ == "__main__":
    import tempfile
    test_incremental_lang_gate()
    for fn in (test_incremental_restricts_and_filters, test_header_target_falls_back_to_full):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"cpp_incremental_analysis: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
