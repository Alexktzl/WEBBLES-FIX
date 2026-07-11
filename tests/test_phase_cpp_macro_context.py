"""
Regress: cpp_macro_context — препроцессор-context (мини) для C/C++.

Проверяет:
  * MacroScanner.build — сканирует #define во всём проекте, поддерживает
    object-like, function-like, multi-line (backslash continuation), пропуск
    `//`-комментариев и блочных `/* */`.
  * find_relevant_macros — матчит uppercase-идентификаторы из исходной
    строки и сообщения компилятора в таблицу макросов.
  * render_macro_context — формирует читаемый блок для case-file.

Без сети/LLM/внешних тулов. Запуск:
    python3 tests/test_phase_cpp_macro_context.py
"""

import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.cpp_macro_context import (
    MacroScanner, MacroDef, render_macro_context,
)


def _proj(files):
    d = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


# === 1. Object-like #define ===
def test_object_like_define():
    d = _proj({"defs.h": "#define MAX_SIZE 1024\n#define NAME \"hello\"\n"})
    s = MacroScanner.build(d)
    assert s.get("MAX_SIZE") is not None
    assert s.get("MAX_SIZE").body == "1024"
    assert s.get("MAX_SIZE").is_function_like is False
    assert s.get("NAME").body == '"hello"'


# === 2. Function-like #define ===
def test_function_like_define():
    d = _proj({"defs.h": "#define MIN(a, b) ((a) < (b) ? (a) : (b))\n"})
    s = MacroScanner.build(d)
    m = s.get("MIN")
    assert m is not None
    assert m.is_function_like is True
    assert m.params == ["a", "b"]
    assert "((a) < (b)" in m.body


# === 3. Multi-line via backslash continuation ===
def test_multiline_define():
    d = _proj({"defs.h":
        "#define LONG_MACRO(x) \\\n"
        "    do { \\\n"
        "        foo(x); \\\n"
        "        bar(x); \\\n"
        "    } while(0)\n"})
    s = MacroScanner.build(d)
    m = s.get("LONG_MACRO")
    assert m is not None
    assert m.is_function_like and m.params == ["x"]
    # Тело многострочное
    assert "foo(x)" in m.body
    assert "bar(x)" in m.body
    assert "while(0)" in m.body


# === 4. //-комментарии скипаются ===
def test_line_comment_does_not_break_define():
    d = _proj({"defs.h": "// comment\n#define X 42 // inline trail\n"})
    s = MacroScanner.build(d)
    assert s.get("X") is not None
    # Trailing // должен быть отрезан
    assert s.get("X").body == "42"


# === 5. Блочный /* */ комментарий пропускается ===
def test_block_comment_skipped():
    d = _proj({"defs.h":
        "/*\n"
        " * banner\n"
        " * #define HIDDEN should_not_appear\n"
        " */\n"
        "#define VISIBLE 1\n"})
    s = MacroScanner.build(d)
    assert s.get("HIDDEN") is None, "макрос внутри /* */ не должен ловиться"
    assert s.get("VISIBLE") is not None


# === 6. find_relevant_macros — по error_line_text (uppercase) ===
def test_find_by_uppercase_in_source_line():
    d = _proj({"defs.h": "#define BUFFER_SIZE 256\n"})
    s = MacroScanner.build(d)
    err_line = "    char buf[BUFFER_SIZE];"
    res = s.find_relevant_macros(error_line_text=err_line, error_message="")
    names = [m.name for m in res.matched]
    assert "BUFFER_SIZE" in names


# === 7. find_relevant_macros — по error_message ===
def test_find_by_uppercase_in_error_message():
    d = _proj({"defs.h": "#define ASSERT_EQ(a,b) ((a)==(b))\n"})
    s = MacroScanner.build(d)
    res = s.find_relevant_macros(
        error_line_text="",
        error_message="expansion of macro 'ASSERT_EQ' produces invalid token",
    )
    assert any(m.name == "ASSERT_EQ" for m in res.matched)


# === 8. find_relevant_macros — uppercase, но НЕ макрос — игнорируется ===
def test_uppercase_non_macro_ignored():
    d = _proj({"defs.h": "#define KNOWN 1\n"})
    s = MacroScanner.build(d)
    res = s.find_relevant_macros(
        error_line_text="UNKNOWN_NAME = KNOWN + 1;",
        error_message="",
    )
    names = [m.name for m in res.matched]
    assert names == ["KNOWN"], f"UNKNOWN_NAME не должен попасть: {names}"


# === 9. Multi-file: define в defs.h, использование в main.cpp ===
def test_cross_file_lookup():
    d = _proj({
        "include/cfg.h": "#define VERSION_MAJOR 3\n",
        "src/main.cpp": "int v = VERSION_MAJOR;\n",
    })
    s = MacroScanner.build(d)
    res = s.find_relevant_macros(
        error_line_text="int v = VERSION_MAJOR;",
        error_message="",
    )
    found = [m for m in res.matched if m.name == "VERSION_MAJOR"]
    assert found and found[0].file.endswith("cfg.h")


# === 10. Limit ===
def test_limit():
    d = _proj({"defs.h": "\n".join(f"#define M{i} {i}" for i in range(10))})
    s = MacroScanner.build(d)
    err_line = " ".join(f"M{i}" for i in range(10))
    res = s.find_relevant_macros(error_line_text=err_line, limit=3)
    assert len(res.matched) == 3


# === 11. render_macro_context — формат ===
def test_render_basic():
    md = MacroDef(name="MAX_SIZE", file="defs.h", line=4, body="1024",
                  is_function_like=False)
    out = render_macro_context([md])
    assert "## MACRO CONTEXT" in out
    assert "MAX_SIZE" in out
    assert "defs.h:4" in out


def test_render_function_like_with_params():
    md = MacroDef(name="MIN", file="util.h", line=10,
                  body="((a) < (b) ? (a) : (b))",
                  is_function_like=True, params=["a", "b"])
    out = render_macro_context([md])
    assert "MIN(a, b)" in out


def test_render_empty_no_section():
    assert render_macro_context([]) == ""


# === 12. Лимит длины тела ===
def test_render_truncates_long_body():
    body = "\n".join(f"step{i}();" for i in range(20))
    md = MacroDef(name="LONG", file="util.h", line=1, body=body,
                  is_function_like=False)
    out = render_macro_context([md], max_body_lines=3)
    assert "truncated" in out


# === 13. Skip dirs (.git/build/node_modules) ===
def test_scanner_skips_build_dirs():
    d = _proj({
        "src/main.cpp": "#define REAL 1\n",
        "build/garbage.cpp": "#define FAKE_FROM_BUILD 999\n",
        "node_modules/lib.h": "#define FAKE_FROM_NM 999\n",
    })
    s = MacroScanner.build(d)
    assert s.get("REAL") is not None
    assert s.get("FAKE_FROM_BUILD") is None
    assert s.get("FAKE_FROM_NM") is None


if __name__ == "__main__":
    tests = [
        ("object_like_define", test_object_like_define),
        ("function_like_define", test_function_like_define),
        ("multiline_define", test_multiline_define),
        ("line_comment_does_not_break", test_line_comment_does_not_break_define),
        ("block_comment_skipped", test_block_comment_skipped),
        ("find_by_uppercase_in_source_line", test_find_by_uppercase_in_source_line),
        ("find_by_uppercase_in_error_message", test_find_by_uppercase_in_error_message),
        ("uppercase_non_macro_ignored", test_uppercase_non_macro_ignored),
        ("cross_file_lookup", test_cross_file_lookup),
        ("limit", test_limit),
        ("render_basic", test_render_basic),
        ("render_function_like_with_params", test_render_function_like_with_params),
        ("render_empty_no_section", test_render_empty_no_section),
        ("render_truncates_long_body", test_render_truncates_long_body),
        ("scanner_skips_build_dirs", test_scanner_skips_build_dirs),
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
    print(f"C++ macro context regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
