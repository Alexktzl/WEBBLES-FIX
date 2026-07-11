"""
Regress: статические анализаторы C++ (cppcheck/clang-tidy) и расширенный
парсер Roslyn-кодов C# (CA*/IDE*/IDISP*/SA*/RCS*).

Без реальных вызовов внешних тулов — тестируем парсеры в изоляции:
  * cppcheck XML фикстура → корректные error-dict;
  * clang-tidy text фикстура → корректные error-dict;
  * csharp regex ловит коды CA/IDE/IDISP/SA/RCS наряду с CS;
  * классификатор маршрутизирует префиксы в нужный error_type.

Запуск: python3 tests/test_phase_cpp_static_csharp_roslyn.py
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzers.cpp_static_extra import (
    run_cppcheck, run_clang_tidy, run_all, _TIDY_LINE,
)
from analyzers.csharp_analyzer import CsharpAnalyzer


# ==========================================================================
# cppcheck XML parsing
# ==========================================================================

_CPPCHECK_XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<results version="2">
  <cppcheck version="2.10"/>
  <errors>
    <error id="nullPointer" severity="error" msg="Null pointer dereference" cwe="476">
      <location file="src/main.cpp" line="12" column="5"/>
    </error>
    <error id="uninitMemberVar" severity="warning" msg="Member 'x' not initialised in the constructor">
      <location file="src/widget.cpp" line="20" column="9"/>
    </error>
    <error id="passedByValue" severity="performance" msg="Function parameter 's' should be passed by const reference">
      <location file="src/main.cpp" line="5" column="14"/>
    </error>
    <error id="noExplicitConstructor" severity="style" msg="Class constructor should be explicit">
      <location file="src/widget.cpp" line="8" column="3"/>
    </error>
  </errors>
</results>
"""


def test_cppcheck_xml_parsing_via_subprocess_stub():
    """Эмулируем cppcheck через мок subprocess.run; проверяем парс XML.

    2026-07-10: `passedByValue` теперь в `_CPPCHECK_NOISE_IDS` (оптимизационный
    шум) → отфильтрован. Высокосигнальный style-код `noExplicitConstructor`
    (реальный доставленный фикс) сохраняется — денилист точечный, не по
    категории. Проверяем и то, и другое."""
    project = Path(tempfile.mkdtemp())
    (project / "main.cpp").write_text("int main(){}\n", encoding="utf-8")
    fake_proc = MagicMock(stdout="", stderr=_CPPCHECK_XML_FIXTURE, returncode=0)
    with patch("analyzers.cpp_static_extra.shutil.which", return_value="/usr/bin/cppcheck"), \
         patch("analyzers.cpp_static_extra.subprocess.run", return_value=fake_proc):
        errors = run_cppcheck(project)
    codes = [e["code"] for e in errors]
    assert "cppcheck::nullPointer" in codes
    assert "cppcheck::uninitMemberVar" in codes
    assert "cppcheck::noExplicitConstructor" in codes  # style-сигнал сохранён
    assert "cppcheck::passedByValue" not in codes       # perf-шум отфильтрован
    assert len(errors) == 3
    # severity нормализация
    sevs = {e["code"]: e["severity"] for e in errors}
    assert sevs["cppcheck::nullPointer"] == "error"
    assert sevs["cppcheck::uninitMemberVar"] == "warning"
    assert sevs["cppcheck::noExplicitConstructor"] == "warning"  # style → warning
    assert all(e["error_type"] == "static" for e in errors)


def test_cppcheck_missing_tool_returns_empty():
    project = Path(tempfile.mkdtemp())
    (project / "main.cpp").write_text("int main(){}\n", encoding="utf-8")
    with patch("analyzers.cpp_static_extra.shutil.which", return_value=None):
        assert run_cppcheck(project) == []


def test_cppcheck_no_cxx_sources_returns_empty():
    project = Path(tempfile.mkdtemp())  # пустая директория
    with patch("analyzers.cpp_static_extra.shutil.which", return_value="/usr/bin/cppcheck"):
        assert run_cppcheck(project) == []


# ==========================================================================
# clang-tidy text parsing
# ==========================================================================

_TIDY_FIXTURE = """\
/tmp/proj/src/main.cpp:12:5: warning: do not use C-style casts [google-readability-casting]
/tmp/proj/src/main.cpp:20:8: error: use of undeclared identifier 'foo' [clang-diagnostic-error]
/tmp/proj/src/util.cpp:3:1: warning: header guard does not follow preferred style [llvm-header-guard]
/tmp/proj/src/main.cpp:35:5: note: dropped because expansion lacks symbol
some unrelated noise
"""


def test_clang_tidy_text_parsing_via_subprocess_stub():
    project = Path(tempfile.mkdtemp())
    src = project / "src"
    src.mkdir()
    (src / "main.cpp").write_text("int main(){}\n", encoding="utf-8")
    fake_proc = MagicMock(stdout=_TIDY_FIXTURE, stderr="", returncode=0)
    with patch("analyzers.cpp_static_extra.shutil.which", return_value="/usr/bin/clang-tidy"), \
         patch("analyzers.cpp_static_extra.subprocess.run", return_value=fake_proc):
        errors = run_clang_tidy(project)
    # 2026-07-10: `clang-diagnostic-error` теперь отбрасывается (фантомные
    # compile-ошибки clang-tidy на файлах вне compile_db; авторитет — g++).
    # note — пропущена. Остаётся 2 записи из 4 строк фикстуры.
    codes = [e["code"] for e in errors]
    assert "google-readability-casting" in codes
    assert "llvm-header-guard" in codes
    assert "clang-diagnostic-error" not in codes
    assert len(errors) == 2, f"got {len(errors)}: {errors}"


def test_tidy_line_regex_handles_multi_checks():
    m = _TIDY_LINE.match(
        "/x/y.cpp:1:2: warning: msg [check-one,check-two]"
    )
    assert m is not None
    # Берём первый check
    checks = m.group("check").split(",")[0].strip()
    assert checks == "check-one"


# ==========================================================================
# C#: Roslyn-кодов regex
# ==========================================================================

def test_csharp_parses_classic_cs_code():
    out = "Program.cs(10,5): error CS1002: ; expected\n"
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    assert len(errs) == 1 and errs[0]["code"] == "CS1002"


def test_csharp_parses_netanalyzers_ca():
    out = "Service.cs(42,9): warning CA1822: Member can be marked as static\n"
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    assert len(errs) == 1 and errs[0]["code"] == "CA1822"
    assert errs[0]["error_type"] == "lint"


def test_csharp_parses_ide_style():
    out = "Program.cs(3,1): warning IDE0005: Using directive is unnecessary\n"
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    assert errs[0]["code"] == "IDE0005"
    assert errs[0]["error_type"] == "style"


def test_csharp_parses_idisposable_idisp():
    out = "Foo.cs(15,5): warning IDISP001: Dispose created\n"
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    assert errs[0]["code"] == "IDISP001"
    assert errs[0]["error_type"] == "resource"


def test_csharp_parses_stylecop_sa():
    out = "Bar.cs(1,1): warning SA1633: File must have header\n"
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    assert errs[0]["code"] == "SA1633"
    assert errs[0]["error_type"] == "style"


def test_csharp_parses_roslynator_rcs():
    out = "Baz.cs(7,2): warning RCS1001: Add braces to if statement\n"
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    assert errs[0]["code"] == "RCS1001"
    assert errs[0]["error_type"] == "lint"


def test_csharp_parses_sonar_s():
    out = "Svc.cs(20,3): warning S2259: Null pointers should not be dereferenced\n"
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    assert errs[0]["code"] == "S2259"
    assert errs[0]["error_type"] == "lint"


def test_csharp_mixed_codes_all_captured():
    """Все нужные коды в одном выводе — не теряем ни один."""
    out = (
        "Program.cs(1,1): error CS1002: ; expected\n"
        "Program.cs(2,1): warning CA1822: Member can be static\n"
        "Program.cs(3,1): warning IDE0005: Unnecessary using\n"
        "Program.cs(4,1): warning IDISP001: Dispose created\n"
        "Program.cs(5,1): warning SA1633: File header missing\n"
    )
    a = CsharpAnalyzer()
    errs = a._parse_dotnet_output(out, Path("/tmp"))
    codes = sorted(e["code"] for e in errs)
    assert codes == ["CA1822", "CS1002", "IDE0005", "IDISP001", "SA1633"]


# ==========================================================================
# Интеграция в cpp_analyzer.analyze
# ==========================================================================

def test_cpp_analyzer_calls_static_extras_by_default():
    """cpp_analyzer.analyze() с static_extra=True (default) дёргает run_all."""
    from analyzers.cpp_analyzer import CppAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    fake_run = MagicMock(stdout="", stderr="", returncode=0)
    with patch("analyzers.cpp_analyzer.subprocess.run", return_value=fake_run), \
         patch("analyzers.cpp_static_extra.run_all",
               return_value=[{"file": "main.cpp", "line": 1, "column": 1,
                              "message": "extra finding", "code": "cppcheck::test",
                              "severity": "warning", "error_type": "static"}]):
        a = CppAnalyzer()
        errs = a.analyze(project)
    codes = [e["code"] for e in errs]
    assert "cppcheck::test" in codes, f"static extras не подмерджились: {errs}"


def test_cpp_analyzer_static_extra_disabled():
    """static_extra=False — run_all не вызывается."""
    from analyzers.cpp_analyzer import CppAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    fake_run = MagicMock(stdout="", stderr="", returncode=0)
    with patch("analyzers.cpp_analyzer.subprocess.run", return_value=fake_run), \
         patch("analyzers.cpp_static_extra.run_all") as mock_run_all:
        a = CppAnalyzer()
        errs = a.analyze(project, static_extra=False)
    assert mock_run_all.call_count == 0, "static_extra=False, не должен дёргать run_all"


if __name__ == "__main__":
    tests = [
        ("cppcheck_xml_parsing", test_cppcheck_xml_parsing_via_subprocess_stub),
        ("cppcheck_missing_tool", test_cppcheck_missing_tool_returns_empty),
        ("cppcheck_no_cxx_sources", test_cppcheck_no_cxx_sources_returns_empty),
        ("clang_tidy_text_parsing", test_clang_tidy_text_parsing_via_subprocess_stub),
        ("tidy_regex_multi_checks", test_tidy_line_regex_handles_multi_checks),
        ("csharp_classic_cs", test_csharp_parses_classic_cs_code),
        ("csharp_ca_netanalyzers", test_csharp_parses_netanalyzers_ca),
        ("csharp_ide_style", test_csharp_parses_ide_style),
        ("csharp_idisp_resource", test_csharp_parses_idisposable_idisp),
        ("csharp_sa_stylecop", test_csharp_parses_stylecop_sa),
        ("csharp_rcs_roslynator", test_csharp_parses_roslynator_rcs),
        ("csharp_s_sonar", test_csharp_parses_sonar_s),
        ("csharp_mixed_codes", test_csharp_mixed_codes_all_captured),
        ("cpp_analyzer_calls_extras", test_cpp_analyzer_calls_static_extras_by_default),
        ("cpp_analyzer_extras_disabled", test_cpp_analyzer_static_extra_disabled),
    ]
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
    print()
    print(f"C++ static + C# Roslyn regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
