"""
C++ анализатор — постройка этапа (2026-07-09): include-пути, -std детект,
фильтр build-config-шума, .c/.cpp глоб, селектор языка.

Инцидент/мотивация: g++ -fsyntax-only на реальном проекте без билд-конфига
даёт либо GCC_INCLUDE-шум (нет -I), либо лавину GCC_UNDECLARED/SYNTAX (нет
-std). fmt: 0 → 1306 фантомов. Постройка: авто -I по каталогам заголовков,
-std из CMakeLists (дефолт gnu++17), и фильтр файлов с лавиной
build-config-ошибок (>=8 undeclared/syntax/include на файл = нет
конфигурации, не баги — пропускаем целиком, чтобы движок не «чинил» фантомы).

Запуск: python tests/test_phase_cpp_analyzer_buildout.py
"""

import sys
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


def test_std_default_and_from_cmake(tmp_path):
    check("cpp_default_std", A._detect_std(tmp_path, cpp=True) == "gnu++17")
    check("c_default_std", A._detect_std(tmp_path, cpp=False) == "gnu11")
    (tmp_path / "CMakeLists.txt").write_text(
        "set(CMAKE_CXX_STANDARD 20)\nset(CMAKE_C_STANDARD 11)\n", encoding="utf-8")
    check("cpp_std_from_cmake", A._detect_std(tmp_path, cpp=True) == "gnu++20")


def test_include_flags_covers_header_dirs(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "foo.hpp").write_text("#pragma once\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bar.h").write_text("#pragma once\n", encoding="utf-8")
    flags = A._collect_include_flags(tmp_path)
    joined = " ".join(flags)
    check("includes_root", any(str(tmp_path) == f[2:] for f in flags))
    check("includes_header_dir", "include" in joined and "src" in joined)
    check("all_are_I_flags", all(f.startswith("-I") for f in flags))


def test_build_noise_filter_drops_toxic_file():
    # файл A: лавина undeclared (нет билд-конфига) → выкинуть весь файл
    toxic = [{"file": "a.cpp", "code": "GCC_UNDECLARED", "message": "x"} for _ in range(10)]
    # файл B: пара реальных warning-ов → оставить
    real = [{"file": "b.cpp", "code": "", "message": "unused variable"},
            {"file": "b.cpp", "code": "GCC_SEMI", "message": "expected ;"}]
    out = A._filter_build_config_noise(toxic + real)
    files = {e["file"] for e in out}
    check("toxic_file_dropped", "a.cpp" not in files, f"files: {files}")
    check("real_file_kept", "b.cpp" in files, f"files: {files}")


def test_build_noise_filter_keeps_few_includes():
    # 3 GCC_INCLUDE на файл — ниже порога 8, не токсично (реальные внешние deps)
    errs = [{"file": "c.cpp", "code": "GCC_INCLUDE", "message": "no such file"} for _ in range(3)]
    out = A._filter_build_config_noise(errs)
    check("few_includes_kept", len(out) == 3)


def test_parse_compile_db_extracts_flags(tmp_path):
    """compile_commands.json → per-file флаги (-I/-D/-std), выброс -o/-c/-MF."""
    import json
    (tmp_path / "compile_commands.json").write_text(json.dumps([{
        "directory": str(tmp_path),
        "file": str(tmp_path / "src" / "a.cpp"),
        "command": "g++ -std=c++17 -I/proj/include -DFOO=1 -O2 -c src/a.cpp -o a.o -MF a.d",
    }]), encoding="utf-8")
    flags = A._parse_compile_db(tmp_path / "compile_commands.json")
    key = str((tmp_path / "src" / "a.cpp").resolve())
    check("db_has_file", key in flags, f"keys: {list(flags)}")
    fl = flags[key]
    check("keeps_std", "-std=c++17" in fl)
    check("keeps_include", "-I/proj/include" in fl)
    check("keeps_define", "-DFOO=1" in fl)
    check("drops_output", "-o" not in fl and "a.o" not in fl)
    check("drops_compile_c", "-c" not in fl)
    check("drops_depfile", "-MF" not in fl and "a.d" not in fl)


def test_parse_compile_db_arguments_form(tmp_path):
    """Форма 'arguments' (список) тоже парсится."""
    import json
    (tmp_path / "compile_commands.json").write_text(json.dumps([{
        "directory": str(tmp_path),
        "file": str(tmp_path / "b.c"),
        "arguments": ["gcc", "-std=gnu11", "-Iinc", "-c", "b.c", "-o", "b.o"],
    }]), encoding="utf-8")
    flags = A._parse_compile_db(tmp_path / "compile_commands.json")
    key = str((tmp_path / "b.c").resolve())
    check("args_form_parsed", key in flags and "-std=gnu11" in flags[key] and "-Iinc" in flags[key])


if __name__ == "__main__":
    import tempfile
    for fn in (test_std_default_and_from_cmake, test_include_flags_covers_header_dirs,
               test_parse_compile_db_extracts_flags, test_parse_compile_db_arguments_form):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    test_build_noise_filter_drops_toxic_file()
    test_build_noise_filter_keeps_few_includes()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"cpp_analyzer_buildout: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
