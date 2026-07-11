"""
Regress: build-system inference для C# и C++.

C#  — `analysis/csharp_dependency_inference.py` (правка .csproj через NuGet
       PackageReference, курируемая таблица popular packages).
C++ — `analysis/cpp_dependency_inference.py` (правка CMakeLists.txt через
       find_package + target_link_libraries, курируемая таблица).

Запуск: python3 tests/test_phase_csharp_cpp_dep_inference.py
"""

import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.csharp_dependency_inference import (
    CsharpDependencyInference, PackageSpec, USING_TO_PACKAGE, FQN_TO_PACKAGE,
)
from analysis.cpp_dependency_inference import (
    CppDependencyInference, CmakePackage, INCLUDE_TO_CMAKE_PACKAGE,
)


def _proj(files):
    d = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


# ==========================================================================
# C# tests
# ==========================================================================

_CSPROJ_EMPTY = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""

_CSPROJ_WITH_PKGS = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Serilog" Version="3.1.1" />
  </ItemGroup>
</Project>
"""


def test_csharp_detect_newtonsoft():
    d = _proj({
        "App.csproj": _CSPROJ_EMPTY,
        "Program.cs": "using Newtonsoft.Json;\nclass P { static void Main() {} }\n",
    })
    di = CsharpDependencyInference()
    res = di.infer(d)
    assert "Newtonsoft.Json" in res.missing, f"ждали Newtonsoft.Json, missing={res.missing}"
    spec = res.missing["Newtonsoft.Json"]
    assert "PackageReference" in spec.to_xml() and "Newtonsoft.Json" in spec.to_xml()


def test_csharp_skip_bcl():
    """System.* — BCL, пакет не нужен."""
    d = _proj({
        "App.csproj": _CSPROJ_EMPTY,
        "Program.cs": "using System;\nusing System.Collections.Generic;\nclass P {}\n",
    })
    di = CsharpDependencyInference()
    res = di.infer(d)
    assert not res.missing, f"BCL не должен порождать missing-пакеты: {res.missing}"


def test_csharp_efcore_via_fqn():
    """Microsoft.EntityFrameworkCore — через FQN-таблицу, не первый сегмент."""
    d = _proj({
        "App.csproj": _CSPROJ_EMPTY,
        "Program.cs": "using Microsoft.EntityFrameworkCore;\nclass P {}\n",
    })
    di = CsharpDependencyInference()
    res = di.infer(d)
    assert "Microsoft.EntityFrameworkCore" in res.missing


def test_csharp_skip_existing_package():
    """Уже есть Serilog → не дублируем."""
    d = _proj({
        "App.csproj": _CSPROJ_WITH_PKGS,
        "Program.cs": "using Serilog;\nclass P {}\n",
    })
    di = CsharpDependencyInference()
    res = di.infer(d)
    assert "Serilog" not in res.missing, "Существующий Serilog не должен снова добавляться"


def test_csharp_apply_to_csproj_existing_itemgroup():
    """ItemGroup с PackageReference есть → добавляем в неё."""
    d = _proj({
        "App.csproj": _CSPROJ_WITH_PKGS,
        "Program.cs": "using Newtonsoft.Json;\nclass P {}\n",
    })
    di = CsharpDependencyInference()
    res = di.infer(d)
    ok = di.apply_to_csproj(d / "App.csproj", res.missing)
    assert ok
    txt = (d / "App.csproj").read_text(encoding="utf-8")
    assert 'Include="Newtonsoft.Json"' in txt
    assert 'Include="Serilog"' in txt  # сохранился
    # ровно один ItemGroup с PackageReference (не плодим)
    assert txt.count("</ItemGroup>") == 1


def test_csharp_apply_creates_itemgroup_when_missing():
    """ItemGroup с пакетами нет → создаём перед </Project>."""
    d = _proj({
        "App.csproj": _CSPROJ_EMPTY,
        "Program.cs": "using Newtonsoft.Json;\nclass P {}\n",
    })
    di = CsharpDependencyInference()
    res = di.infer(d)
    assert di.apply_to_csproj(d / "App.csproj", res.missing)
    txt = (d / "App.csproj").read_text(encoding="utf-8")
    assert "<ItemGroup>" in txt and "</ItemGroup>" in txt
    assert 'Include="Newtonsoft.Json"' in txt
    assert txt.rstrip().endswith("</Project>")


def test_csharp_recover_idempotent():
    d = _proj({
        "App.csproj": _CSPROJ_EMPTY,
        "Program.cs": "using Newtonsoft.Json;\nclass P {}\n",
    })
    di = CsharpDependencyInference()
    r1 = di.recover(d, run_build_check=False)
    assert r1.has_changes
    # повторно — уже всё на месте
    r2 = di.recover(d, run_build_check=False)
    assert not r2.has_changes, "recover должен быть идемпотентен"


# ==========================================================================
# C++ tests
# ==========================================================================

_CML_EMPTY = """cmake_minimum_required(VERSION 3.15)
project(demo CXX)

add_executable(demo main.cpp)
"""

_CML_WITH_BOOST = """cmake_minimum_required(VERSION 3.15)
project(demo CXX)

find_package(Boost REQUIRED)
add_executable(demo main.cpp)
target_link_libraries(demo PRIVATE Boost::system)
"""


def test_cpp_detect_fmt():
    d = _proj({
        "CMakeLists.txt": _CML_EMPTY,
        "main.cpp": '#include <fmt/format.h>\nint main(){ return 0; }\n',
    })
    di = CppDependencyInference()
    res = di.infer(d)
    assert "fmt" in res.missing
    assert res.missing["fmt"].find_package_line() == "find_package(fmt REQUIRED)"


def test_cpp_skip_stdlib():
    """<iostream>, <vector>, <string> — STD, пакет не нужен."""
    d = _proj({
        "CMakeLists.txt": _CML_EMPTY,
        "main.cpp": "#include <iostream>\n#include <vector>\n#include <string>\nint main(){return 0;}\n",
    })
    di = CppDependencyInference()
    res = di.infer(d)
    assert not res.missing


def test_cpp_skip_existing_find_package():
    """Boost уже в CMakeLists → не добавляем повторно."""
    d = _proj({
        "CMakeLists.txt": _CML_WITH_BOOST,
        "main.cpp": "#include <boost/asio.hpp>\nint main(){return 0;}\n",
    })
    di = CppDependencyInference()
    res = di.infer(d)
    assert "Boost" not in res.missing


def test_cpp_apply_adds_find_package_and_link():
    d = _proj({
        "CMakeLists.txt": _CML_EMPTY,
        "main.cpp": '#include <fmt/format.h>\nint main(){return 0;}\n',
    })
    di = CppDependencyInference()
    res = di.infer(d)
    ok = di.apply_to_cmakelists(d / "CMakeLists.txt", res.missing)
    assert ok
    txt = (d / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "find_package(fmt REQUIRED)" in txt
    # link для существующего таргета `demo`
    assert "target_link_libraries(demo PRIVATE fmt::fmt)" in txt
    # add_executable сохранён
    assert "add_executable(demo main.cpp)" in txt


def test_cpp_multiple_includes_dedup():
    d = _proj({
        "CMakeLists.txt": _CML_EMPTY,
        "main.cpp": "#include <fmt/format.h>\n#include <fmt/color.h>\nint main(){return 0;}\n",
    })
    di = CppDependencyInference()
    res = di.infer(d)
    # fmt должен быть один раз
    assert "fmt" in res.missing
    assert len([k for k in res.missing if k == "fmt"]) == 1


def test_cpp_unknown_skipped():
    d = _proj({
        "CMakeLists.txt": _CML_EMPTY,
        "main.cpp": "#include <some_unknown_lib.hpp>\nint main(){return 0;}\n",
    })
    di = CppDependencyInference()
    res = di.infer(d)
    assert "some_unknown_lib.hpp" in res.skipped_unknown
    assert not res.missing


def test_cpp_recover_idempotent():
    d = _proj({
        "CMakeLists.txt": _CML_EMPTY,
        "main.cpp": '#include <fmt/format.h>\nint main(){return 0;}\n',
    })
    di = CppDependencyInference()
    r1 = di.recover(d, run_cmake_check=False)
    assert r1.has_changes
    r2 = di.recover(d, run_cmake_check=False)
    assert not r2.has_changes


# ==========================================================================
# CrateSpec / CmakePackage rendering
# ==========================================================================

def test_csharp_packagespec_xml_format():
    spec = PackageSpec(name="Foo", version="1.2.3")
    assert spec.to_xml() == '<PackageReference Include="Foo" Version="1.2.3" />'


def test_cpp_cmakepackage_with_components():
    pkg = CmakePackage(name="Qt6", components=["Widgets", "Core"], link_targets=["Qt6::Widgets"])
    line = pkg.find_package_line()
    assert "REQUIRED COMPONENTS Widgets Core" in line


if __name__ == "__main__":
    tests = [
        ("csharp_detect_newtonsoft", test_csharp_detect_newtonsoft),
        ("csharp_skip_bcl", test_csharp_skip_bcl),
        ("csharp_efcore_via_fqn", test_csharp_efcore_via_fqn),
        ("csharp_skip_existing_package", test_csharp_skip_existing_package),
        ("csharp_apply_to_csproj_existing_itemgroup", test_csharp_apply_to_csproj_existing_itemgroup),
        ("csharp_apply_creates_itemgroup_when_missing", test_csharp_apply_creates_itemgroup_when_missing),
        ("csharp_recover_idempotent", test_csharp_recover_idempotent),
        ("cpp_detect_fmt", test_cpp_detect_fmt),
        ("cpp_skip_stdlib", test_cpp_skip_stdlib),
        ("cpp_skip_existing_find_package", test_cpp_skip_existing_find_package),
        ("cpp_apply_adds_find_package_and_link", test_cpp_apply_adds_find_package_and_link),
        ("cpp_multiple_includes_dedup", test_cpp_multiple_includes_dedup),
        ("cpp_unknown_skipped", test_cpp_unknown_skipped),
        ("cpp_recover_idempotent", test_cpp_recover_idempotent),
        ("csharp_packagespec_xml_format", test_csharp_packagespec_xml_format),
        ("cpp_cmakepackage_with_components", test_cpp_cmakepackage_with_components),
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
    print(f"C#/C++ dependency inference regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
