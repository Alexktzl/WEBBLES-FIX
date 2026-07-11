"""
Security-фиксеры для C#/C++ (2026-07-11) — детекторы + few-shot примеры.

Достройка security-контура на все готовые языки (было python/rust/js). Детекторы
в security_scanner (sql_injection/command_injection/hardcoded_secret/
unsafe_deserialization для C#, + buffer_overflow для C++), few-shot примеры в
SECURITY_EXAMPLES под каждый язык. Узко и без ложных (env/snprintf не триггерят).

Запуск: python tests/test_phase_security_csharp_cpp.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analysis.security_scanner import SecurityScanner  # noqa: E402
from analysis.constraints.error_constraints import get_security_example  # noqa: E402

results = []
SC = SecurityScanner()


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def codes(line, lang):
    return [h.get("code") for h in SC.scan_source(line, lang, "f")]


def test_csharp_detects():
    check("cs_sql_interp", "sql_injection" in codes(
        'new SqlCommand($"SELECT * FROM u WHERE id={id}", conn);', "csharp"))
    check("cs_sql_fromraw", "sql_injection" in codes(
        'ctx.Users.FromSqlRaw("SELECT * FROM Users WHERE n = " + name);', "csharp"))
    check("cs_cmd", "command_injection" in codes(
        'Process.Start("cmd.exe", "/c dir " + path);', "csharp"))
    check("cs_secret", "hardcoded_secret" in codes(
        'const string ApiKey = "sk-live-abcd1234";', "csharp"))
    check("cs_deser", "unsafe_deserialization" in codes(
        'new BinaryFormatter().Deserialize(stream);', "csharp"))


def test_cpp_detects():
    check("cpp_cmd", "command_injection" in codes(
        'system(("ls " + path).c_str());', "cpp"))
    check("cpp_bof_strcpy", "buffer_overflow" in codes('strcpy(dst, src);', "cpp"))
    check("cpp_bof_gets", "buffer_overflow" in codes('gets(buf);', "cpp"))
    check("cpp_secret", "hardcoded_secret" in codes(
        'const char* API_KEY = "sk-live-abcd1234";', "cpp"))


def test_no_false_positives():
    check("cs_env_ok", codes('var k = Environment.GetEnvironmentVariable("API_KEY");', "csharp") == [])
    check("cs_param_ok", codes(
        'cmd.Parameters.AddWithValue("@id", id);', "csharp") == [])
    check("cpp_snprintf_ok", codes('snprintf(buf, sizeof buf, "%s", src);', "cpp") == [])
    check("cpp_strncpy_ok", codes('strncpy(dst, src, n);', "cpp") == [])
    check("cpp_getenv_ok", codes('const char* k = getenv("API_KEY");', "cpp") == [])


def test_examples_present():
    for lang, code in [("csharp", "sql_injection"), ("csharp", "command_injection"),
                       ("csharp", "hardcoded_secret"), ("csharp", "unsafe_deserialization"),
                       ("csharp", "xss"), ("csharp", "path_traversal"), ("csharp", "xxe"),
                       ("cpp", "command_injection"), ("cpp", "buffer_overflow"),
                       ("cpp", "hardcoded_secret"), ("cpp", "sql_injection"),
                       ("cpp", "format_string")]:
        check(f"ex_{lang}_{code}", bool(get_security_example(code, lang).strip()))


if __name__ == "__main__":
    test_csharp_detects()
    test_cpp_detects()
    test_no_false_positives()
    test_examples_present()
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"security_csharp_cpp: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
