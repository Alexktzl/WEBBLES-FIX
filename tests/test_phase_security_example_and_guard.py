"""
Security-фиксеры: курируемый пример в промпт + страж-подлинности O.20
(2026-07-09, идея Алекса + находка wxVk sql_injection).

Механизм: для контекст-зависимых security-кодов (осознанно оставлены LLM,
§3 CLAUDE.md) в промпт инжектится курируемый before→after пример
(SECURITY FIX EXAMPLE), а безопасность держит детерминированный страж:
LLM не должна «чинить» уязвимость косметическим suppression-комментарием
(# nosec / # noqa / nosemgrep / #[allow]) — это ложная безопасность, хуже
честного флага. Такой патч понижается в NEEDS_REVIEW (§4).

Запуск: python tests/test_phase_security_example_and_guard.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analysis.constraints.error_constraints import get_security_example  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


# ---- 1. курируемые примеры ----

def test_sql_injection_example_python():
    ex = get_security_example("sql_injection", "python")
    check("py_example_nonempty", bool(ex))
    check("py_example_has_parameterized", "?" in ex and "execute" in ex)
    check("py_example_warns_suppression", "nosec" in ex.lower())


def test_sql_injection_example_rust():
    ex = get_security_example("sql_injection", "rust")
    check("rust_example_nonempty", bool(ex))
    check("rust_example_has_bind", "bind" in ex and "$1" in ex)


def test_command_injection_example_both_langs():
    check("cmd_py", "shell=False" in get_security_example("command_injection", "python") or
          "shell" in get_security_example("command_injection", "python"))
    check("cmd_rust", "Command::new" in get_security_example("command_injection", "rust"))


def test_non_security_code_no_example():
    check("e501_no_example", get_security_example("E501", "python") == "")
    check("unknown_no_example", get_security_example("nope", "python") == "")


# ---- 2. страж O.20 ----

class _Ctx:
    def __init__(self, before, after_on_disk, work):
        self.metadata = {"_pre_patch_content": {"app.py": before}}
        self.working_path = work
        self.project_path = work
        self._after = after_on_disk


def _guard(before, after, code="sql_injection", etype="security"):
    st = DecideStage.__new__(DecideStage)
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "app.py").write_text(after, encoding="utf-8")
    ctx = _Ctx(before, after, d)
    err = {"file": "app.py", "code": code, "error_type": etype}
    return st._security_fix_added_suppression(ctx, err)


def test_guard_flags_added_nosec():
    before = 'cur.execute("SELECT * FROM t WHERE id=%s" % uid)\n'
    after = 'cur.execute("SELECT * FROM t WHERE id=%s" % uid)  # nosec\n'
    check("added_nosec_flagged", _guard(before, after) is True)


def test_guard_flags_added_noqa():
    before = 'os.system(f"ls {p}")\n'
    after = 'os.system(f"ls {p}")  # noqa\n'
    check("added_noqa_flagged", _guard(before, after, code="command_injection") is True)


def test_guard_flags_rust_allow():
    before = 'sqlx::query(&format!("... {}", id))\n'
    after = '#[allow(clippy::all)]\nsqlx::query(&format!("... {}", id))\n'
    check("rust_allow_flagged", _guard(before, after) is True)


def test_guard_passes_genuine_parameterization():
    before = 'cur.execute(f"SELECT * FROM t WHERE id={uid}")\n'
    after = 'cur.execute("SELECT * FROM t WHERE id=?", (uid,))\n'
    check("genuine_fix_not_flagged", _guard(before, after) is False)


def test_guard_ignores_non_security_code():
    before = 'x = 1\n'
    after = 'x = 1  # noqa\n'
    check("non_security_ignored", _guard(before, after, code="E501", etype="") is False)


def test_guard_no_before_does_not_block():
    st = DecideStage.__new__(DecideStage)
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "app.py").write_text("x  # nosec\n", encoding="utf-8")
    ctx = _Ctx.__new__(_Ctx)
    ctx.metadata = {"_pre_patch_content": {}}
    ctx.working_path = d
    ctx.project_path = d
    check("no_before_no_block",
          st._security_fix_added_suppression(ctx, {"file": "app.py", "code": "sql_injection", "error_type": "security"}) is False)


def test_expanded_examples_all_codes():
    """2026-07-09: примеры расширены на все 6 security-кодов детектора."""
    checks = [
        ("dangerous_eval", "python", "literal_eval"),
        ("hardcoded_secret", "python", "os.environ"),
        ("hardcoded_secret", "rust", "env::var"),
        ("unsafe_deserialization", "python", "safe_load"),
        ("xss", "javascript", "textContent"),
        ("xss", "js", "textContent"),        # алиас языка
        ("xss", "python", "mark_safe"),
    ]
    for code, lang, marker in checks:
        ex = get_security_example(code, lang)
        check(f"{code}/{lang}_has_{marker}", marker in ex, f"ex: {ex[:80]!r}")


def test_every_scanner_code_has_example():
    """Каждый security-код security_scanner имеет хотя бы один язык-пример."""
    scanner_codes = ["sql_injection", "command_injection", "dangerous_eval",
                     "hardcoded_secret", "unsafe_deserialization", "xss"]
    for code in scanner_codes:
        any_lang = any(get_security_example(code, l)
                       for l in ("python", "rust", "javascript"))
        check(f"{code}_has_some_example", any_lang)


def test_rust_sql_injection_detector():
    """2026-07-09 (Rust-детектор SQL): format!/конкатенация в query → инъекция;
    параметризованный .bind($1) и не-SQL format! — не триггерят."""
    from analysis.security_scanner import _is_sql_injection_rust as f
    bad = [
        'sqlx::query(&format!("SELECT * FROM u WHERE id = {}", id))',
        'conn.execute(&format!("DELETE FROM t WHERE k = {}", key), [])',
        'diesel::sql_query(format!("UPDATE t SET v = {}", v))',
        'let q = "SELECT * FROM t WHERE n = ".to_string() + &name; conn.query(&q)',
    ]
    good = [
        'sqlx::query("SELECT * FROM u WHERE id = $1").bind(id)',
        'let x = format!("hello {}", name);',
        'conn.execute("DELETE FROM t WHERE k = ?1", params![key])',
        'println!("SELECT stuff");',
        'sqlx::query(AssertSqlSafe(format!("SELECT {i}::int4", i)))',  # sqlx escape-hatch
    ]
    for l in bad:
        check(f"rust_sqli_detects_{bad.index(l)}", f(l) is True, l[:50])
    for l in good:
        check(f"rust_sqli_skips_{good.index(l)}", f(l) is False, l[:50])


def test_rust_sql_injection_in_scanner_rules():
    """sql_injection зарегистрирован в rust-правилах security_scanner."""
    from analysis.security_scanner import _RULES
    codes = {r.code for r in _RULES.get("rust", [])}
    check("rust_rules_have_sql_injection", "sql_injection" in codes, f"codes: {codes}")


if __name__ == "__main__":
    test_sql_injection_example_python()
    test_sql_injection_example_rust()
    test_command_injection_example_both_langs()
    test_non_security_code_no_example()
    test_guard_flags_added_nosec()
    test_guard_flags_added_noqa()
    test_guard_flags_rust_allow()
    test_guard_passes_genuine_parameterization()
    test_guard_ignores_non_security_code()
    test_guard_no_before_does_not_block()
    test_expanded_examples_all_codes()
    test_every_scanner_code_has_example()
    test_rust_sql_injection_detector()
    test_rust_sql_injection_in_scanner_rules()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"security_example_and_guard: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
