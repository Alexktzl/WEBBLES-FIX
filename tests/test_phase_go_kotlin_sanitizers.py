"""
Regress: Go + Kotlin language providers/analyzers + sanitizer output parser.

Без реальных вызовов go/kotlinc — мокаем subprocess.

Запуск: python3 tests/test_phase_go_kotlin_sanitizers.py
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ==========================================================================
# Go analyzer
# ==========================================================================

def test_go_analyzer_undefined():
    from analyzers.go_analyzer import GoAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "main.go").write_text("package main\nfunc main() { fmt.Println(x) }\n",
                                     encoding="utf-8")
    (project / "go.mod").write_text("module test\n", encoding="utf-8")
    fake = MagicMock(stdout="",
                     stderr="./main.go:2:14: undefined: fmt\n./main.go:2:26: undefined: x\n",
                     returncode=1)
    with patch("analyzers.go_analyzer.subprocess.run", return_value=fake):
        a = GoAnalyzer()
        errs = a.analyze(project)
    codes = [e["code"] for e in errs]
    # GO_IMPORT для fmt, GO_UNDEFINED для x
    assert "GO_IMPORT" in codes or "GO_UNDEFINED" in codes
    assert any(e["line"] == 2 for e in errs)


def test_go_analyzer_unused_import():
    from analyzers.go_analyzer import GoAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "main.go").write_text(
        "package main\nimport \"fmt\"\nfunc main() {}\n", encoding="utf-8")
    fake = MagicMock(stdout="",
                     stderr="./main.go:2:8: imported and not used: \"fmt\"\n",
                     returncode=1)
    with patch("analyzers.go_analyzer.subprocess.run", return_value=fake):
        a = GoAnalyzer()
        errs = a.analyze(project)
    assert errs and errs[0]["code"] == "GO_UNUSED_IMP"
    assert errs[0]["error_type"] == "warning"


def test_go_analyzer_missing_tool_graceful():
    from analyzers.go_analyzer import GoAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "main.go").write_text("package main\n", encoding="utf-8")
    with patch("analyzers.go_analyzer.subprocess.run",
               side_effect=FileNotFoundError):
        a = GoAnalyzer()
        assert a.analyze(project) == []


def test_go_support_registered():
    from core.language_support import LANGUAGE_REGISTRY
    from core.languages import _discover_and_register
    assert "go" in LANGUAGE_REGISTRY, f"Зарегистрировано: {sorted(LANGUAGE_REGISTRY)}"


def test_go_healer_removes_unused_import():
    from fixers.language_syntax.go_healer import GoSyntaxHealer
    f = Path(tempfile.mkdtemp()) / "main.go"
    f.write_text("package main\nimport \"fmt\"\nfunc main() {}\n", encoding="utf-8")
    healer = GoSyntaxHealer()
    new_text = healer.heal(
        {"code": "GO_UNUSED_IMP", "line": 2,
         "message": "imported and not used: \"fmt\""},
        f)
    assert new_text and "import \"fmt\"" not in new_text


# ==========================================================================
# Kotlin analyzer
# ==========================================================================

def test_kotlin_analyzer_unresolved():
    from analyzers.kotlin_analyzer import KotlinAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "Main.kt").write_text("fun main() { println(y) }\n", encoding="utf-8")
    fake = MagicMock(stdout="",
                     stderr="Main.kt:1:22: error: unresolved reference: y\n",
                     returncode=1)
    with patch("analyzers.kotlin_analyzer.subprocess.run", return_value=fake):
        a = KotlinAnalyzer()
        errs = a.analyze(project)
    assert errs and errs[0]["code"] == "KT_UNRESOLVED"


def test_kotlin_analyzer_nullsafe():
    from analyzers.kotlin_analyzer import KotlinAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "Main.kt").write_text("fun f(s: String?) { val n = s.length }\n",
                                     encoding="utf-8")
    fake = MagicMock(stdout="",
                     stderr=(
                         "Main.kt:1:30: error: only safe (?.) or non-null asserted "
                         "(!!.) calls are allowed on a nullable receiver of type String?\n"),
                     returncode=1)
    with patch("analyzers.kotlin_analyzer.subprocess.run", return_value=fake):
        errs = KotlinAnalyzer().analyze(project)
    assert errs and errs[0]["code"] == "KT_NULLSAFE"


def test_kotlin_analyzer_type_mismatch():
    from analyzers.kotlin_analyzer import KotlinAnalyzer
    project = Path(tempfile.mkdtemp())
    (project / "Main.kt").write_text("fun main() { val n: Int = \"hi\" }\n",
                                     encoding="utf-8")
    fake = MagicMock(stdout="",
                     stderr=("Main.kt:1:27: error: type mismatch: inferred type is "
                             "String but Int was expected\n"),
                     returncode=1)
    with patch("analyzers.kotlin_analyzer.subprocess.run", return_value=fake):
        errs = KotlinAnalyzer().analyze(project)
    assert errs and errs[0]["code"] == "KT_TYPES"


def test_kotlin_support_registered():
    from core.language_support import LANGUAGE_REGISTRY
    assert "kotlin" in LANGUAGE_REGISTRY


def test_kotlin_healer_nullsafe():
    from fixers.language_syntax.kotlin_healer import KotlinSyntaxHealer
    f = Path(tempfile.mkdtemp()) / "Main.kt"
    f.write_text("fun f(s: String?) {\n    val n = s.length()\n}\n", encoding="utf-8")
    out = KotlinSyntaxHealer().heal(
        {"code": "KT_NULLSAFE", "line": 2,
         "message": "only safe (?.) or non-null asserted calls allowed"},
        f)
    assert out and "s?.length()" in out


# ==========================================================================
# Sanitizer output parser
# ==========================================================================

_ASAN_FIXTURE = """\
==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000010 at pc 0x000000401234 bp 0x7ffd8a3b0c30 sp 0x7ffd8a3b0c28
READ of size 4 at 0x602000000010 thread T0
    #0 0x401234 in main /home/u/proj/src/main.cpp:42:14
    #1 0x7f1234 in __libc_start_main /usr/lib/libc.so.6
    #2 0x4010a4 in _start (proj+0x4010a4)
"""

_ASAN_USE_AFTER_FREE = """\
==99==ERROR: AddressSanitizer: heap-use-after-free on address 0x614000000040
READ of size 8
    #0 0x55ab in foo /home/u/proj/src/widget.cpp:17:9
    #1 0x55cd in main /home/u/proj/src/main.cpp:7
"""

_UBSAN_FIXTURE = "/home/u/proj/src/util.cpp:88:9: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'\n"

_TSAN_FIXTURE = """\
==42==WARNING: ThreadSanitizer: data race (pid=42)
  Read of size 4 at 0x7fff:
    #0 0xabc in foo /home/u/proj/src/race.cpp:31:5
"""

_LEAKSAN_FIXTURE = """\
==12==ERROR: LeakSanitizer: detected memory leaks
Direct leak of 16 byte(s) in 1 object(s) allocated from:
    #0 0xdeadbeef in operator new(unsigned long) /usr/lib/clang/asan.cpp:99
    #1 0xfffe in alloc_thing /home/u/proj/src/main.cpp:13:11
"""

_VALGRIND_FIXTURE = """\
==12345== Invalid read of size 4
==12345==    at 0x10923A: bar (main.cpp:55)
==12345==    by 0x109200: main (main.cpp:88)
"""


def test_asan_heap_overflow_picks_project_frame():
    from analysis.sanitizer_output import parse_sanitizer_output
    project = Path("/home/u/proj")
    errs = parse_sanitizer_output(_ASAN_FIXTURE, project)
    assert len(errs) == 1
    e = errs[0]
    assert e["code"] == "SAN_HEAP_OVERFLOW"
    assert e["error_class"] == "RUNTIME_UB"
    assert e["sanitizer"] == "AddressSanitizer"
    assert e["file"].endswith("main.cpp")
    assert e["line"] == 42


def test_asan_use_after_free():
    from analysis.sanitizer_output import parse_sanitizer_output
    project = Path("/home/u/proj")
    errs = parse_sanitizer_output(_ASAN_USE_AFTER_FREE, project)
    assert errs and errs[0]["code"] == "SAN_USE_AFTER_FREE"
    # Первый user-фрейм — widget.cpp:17
    assert errs[0]["file"].endswith("widget.cpp")
    assert errs[0]["line"] == 17


def test_ubsan_runtime_error():
    from analysis.sanitizer_output import parse_sanitizer_output
    project = Path("/home/u/proj")
    errs = parse_sanitizer_output(_UBSAN_FIXTURE, project)
    assert errs
    e = errs[0]
    assert e["sanitizer"] == "UndefinedBehaviorSanitizer"
    assert e["error_class"] == "RUNTIME_UB"
    assert e["line"] == 88
    assert "signed integer overflow" in e["message"]


def test_tsan_data_race():
    from analysis.sanitizer_output import parse_sanitizer_output
    project = Path("/home/u/proj")
    errs = parse_sanitizer_output(_TSAN_FIXTURE, project)
    assert errs and errs[0]["code"] == "SAN_DATA_RACE"
    assert errs[0]["file"].endswith("race.cpp")


def test_leak_sanitizer():
    from analysis.sanitizer_output import parse_sanitizer_output
    project = Path("/home/u/proj")
    errs = parse_sanitizer_output(_LEAKSAN_FIXTURE, project)
    # Должен поймать leak или memory leaks
    assert errs and any("LEAK" in e["code"] for e in errs)


def test_valgrind_invalid_read():
    from analysis.sanitizer_output import parse_sanitizer_output
    errs = parse_sanitizer_output(_VALGRIND_FIXTURE)
    assert errs and errs[0]["sanitizer"] == "Valgrind"
    assert errs[0]["line"] == 55
    assert "Invalid read" in errs[0]["message"]


def test_empty_input_returns_empty():
    from analysis.sanitizer_output import parse_sanitizer_output
    assert parse_sanitizer_output("") == []
    assert parse_sanitizer_output("just some build noise\n") == []


def test_log_file_wrapper_missing_file():
    from analysis.sanitizer_output import parse_sanitizer_log_file
    assert parse_sanitizer_log_file(Path("/nonexistent/path/log.txt")) == []


def test_dedup_same_finding():
    """Один отчёт упомянут дважды — выдаём один error_dict."""
    from analysis.sanitizer_output import parse_sanitizer_output
    doubled = _ASAN_FIXTURE + "\n" + _ASAN_FIXTURE
    errs = parse_sanitizer_output(doubled, Path("/home/u/proj"))
    assert len(errs) == 1


def test_skips_system_frames():
    """Если первый user-фрейм есть, системные (/usr/lib/...) НЕ выбираются."""
    from analysis.sanitizer_output import parse_sanitizer_output
    errs = parse_sanitizer_output(_ASAN_FIXTURE, Path("/home/u/proj"))
    assert errs and not errs[0]["file"].startswith("/usr/")


if __name__ == "__main__":
    tests = [
        # Go
        ("go_analyzer_undefined", test_go_analyzer_undefined),
        ("go_analyzer_unused_import", test_go_analyzer_unused_import),
        ("go_analyzer_missing_tool", test_go_analyzer_missing_tool_graceful),
        ("go_support_registered", test_go_support_registered),
        ("go_healer_unused_import", test_go_healer_removes_unused_import),
        # Kotlin
        ("kotlin_analyzer_unresolved", test_kotlin_analyzer_unresolved),
        ("kotlin_analyzer_nullsafe", test_kotlin_analyzer_nullsafe),
        ("kotlin_analyzer_type_mismatch", test_kotlin_analyzer_type_mismatch),
        ("kotlin_support_registered", test_kotlin_support_registered),
        ("kotlin_healer_nullsafe", test_kotlin_healer_nullsafe),
        # Sanitizers
        ("asan_heap_overflow", test_asan_heap_overflow_picks_project_frame),
        ("asan_use_after_free", test_asan_use_after_free),
        ("ubsan_runtime_error", test_ubsan_runtime_error),
        ("tsan_data_race", test_tsan_data_race),
        ("leak_sanitizer", test_leak_sanitizer),
        ("valgrind_invalid_read", test_valgrind_invalid_read),
        ("empty_input", test_empty_input_returns_empty),
        ("log_file_wrapper_missing", test_log_file_wrapper_missing_file),
        ("dedup_same_finding", test_dedup_same_finding),
        ("skips_system_frames", test_skips_system_frames),
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
    print(f"Go+Kotlin+Sanitizers regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
