"""
Q.1/Q.2/Q.3 Logic Guard — AST contract comparison for Python.

Covers:
1) LogicGuardExtractor.extract_python — snapshot extraction
2) LogicGuard.check — violation detection (I-1, I-2, I-3, I-4, I-5, I-6)
3) LogicGuardResult routing: HIGH ->NEEDS_REVIEW

Run: python3 tests/test_phase_logic_guard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.logic_guard import (
    LogicGuard,
    LogicGuardExtractor,
    LogicGuardResult,
    LogicViolation,
)

results = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    status = "OK " if cond else "FAIL"
    try:
        print(f"  [{status}] {name}")
    except UnicodeEncodeError:
        print(f"  [{status}] {name.encode('ascii', 'replace').decode('ascii')}")


# ---------------------------------------------------------------------------
# 1. LogicGuardExtractor.extract_python — snapshot extraction
# ---------------------------------------------------------------------------

def test_extract_empty_source():
    snaps = LogicGuardExtractor.extract_python("")
    check("extract: empty source ->{}", snaps == {})


def test_extract_syntax_error():
    snaps = LogicGuardExtractor.extract_python("def foo(: pass")
    check("extract: syntax_error ->{}", snaps == {})


def test_extract_simple_function():
    src = "def greet(name, greeting): return greeting + name\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: simple.found", "greet" in snaps)
    s = snaps["greet"]
    check("extract: simple.param_names", s.param_names == ["name", "greeting"])
    check("extract: simple.required_count", s.required_param_count == 2)
    check("extract: simple.has_value_return", s.has_value_return is True)
    check("extract: simple.no_exceptions", s.raised_exceptions == set())


def test_extract_optional_params():
    src = "def foo(a, b, c=1, d=2): pass\n"
    snaps = LogicGuardExtractor.extract_python(src)
    s = snaps["foo"]
    check("extract: optional.required_count=2", s.required_param_count == 2)
    check("extract: optional.param_names", s.param_names == ["a", "b", "c", "d"])


def test_extract_kwonly_required():
    src = "def foo(a, *, b, c=1): pass\n"
    snaps = LogicGuardExtractor.extract_python(src)
    s = snaps["foo"]
    # a is positional required, b is kwonly required, c is kwonly with default
    check("extract: kwonly.required_count=2", s.required_param_count == 2)
    check("extract: kwonly.param_names", s.param_names == ["a", "b", "c"])


def test_extract_no_return():
    src = "def noop(): pass\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: no_return.has_value_return=False",
          snaps["noop"].has_value_return is False)


def test_extract_bare_return():
    src = "def early(): return\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: bare_return.has_value_return=False",
          snaps["early"].has_value_return is False)


def test_extract_value_return():
    src = "def compute(x): return x * 2\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: value_return.has_value_return=True",
          snaps["compute"].has_value_return is True)


def test_extract_nested_return_not_counted():
    src = (
        "def outer():\n"
        "    def inner(): return 42\n"
        "    pass\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: nested_return.outer.has_value_return=False",
          snaps["outer"].has_value_return is False)
    check("extract: nested_return.inner.has_value_return=True",
          snaps["inner"].has_value_return is True)


def test_extract_raised_exception_name():
    src = "def validate(x):\n    raise ValueError\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: raise_name.ValueError in raised_exceptions",
          "ValueError" in snaps["validate"].raised_exceptions)


def test_extract_raised_exception_instance():
    src = 'def check(x):\n    raise KeyError("missing")\n'
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: raise_instance.KeyError",
          "KeyError" in snaps["check"].raised_exceptions)


def test_extract_raised_exception_attribute():
    src = "def run():\n    raise errors.AppError\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: raise_attr.AppError",
          "AppError" in snaps["run"].raised_exceptions)


def test_extract_nested_raise_not_counted():
    src = (
        "def outer():\n"
        "    def inner(): raise TypeError\n"
        "    return 1\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: nested_raise.outer.no_exceptions",
          "TypeError" not in snaps["outer"].raised_exceptions)
    check("extract: nested_raise.inner.TypeError",
          "TypeError" in snaps["inner"].raised_exceptions)


def test_extract_multiple_functions():
    src = "def foo(): return 1\ndef bar(x): pass\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: multiple.foo found", "foo" in snaps)
    check("extract: multiple.bar found", "bar" in snaps)


def test_extract_method_in_class():
    src = (
        "class Service:\n"
        "    def run(self, cfg):\n"
        "        return cfg\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: class_method.run found", "run" in snaps)
    s = snaps["run"]
    check("extract: class_method.param_names", s.param_names == ["self", "cfg"])
    check("extract: class_method.has_value_return", s.has_value_return is True)


def test_extract_async_function():
    src = "async def fetch(url): return url\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: async.found", "fetch" in snaps)
    check("extract: async.has_value_return", snaps["fetch"].has_value_return is True)


# ---------------------------------------------------------------------------
# 2. LogicGuard.check — violation detection
# ---------------------------------------------------------------------------

_BASE = (
    "def process(data, mode='fast'):\n"
    "    return data\n"
)


def test_check_identical_no_violations():
    r = LogicGuard.check(_BASE, _BASE, "python")
    check("check: identical.0 violations", len(r.violations) == 0)
    check("check: identical.checked_functions=1", r.checked_functions == 1)


def test_check_unsupported_language():
    r = LogicGuard.check(_BASE, _BASE, "rust")
    check("check: rust.skipped", r.skipped_reason == "unsupported_language")
    check("check: rust.0 violations", len(r.violations) == 0)


def test_check_empty_language_skipped():
    r = LogicGuard.check(_BASE, _BASE, "")
    check("check: empty_lang.skipped", r.skipped_reason == "unsupported_language")


def test_check_language_py():
    r = LogicGuard.check(_BASE, _BASE, "py")
    check("check: py_alias.no violations", len(r.violations) == 0)
    check("check: py_alias.not skipped", r.skipped_reason == "")


def test_check_required_param_added():
    after = "def process(data, mode='fast', timeout=None, strict=True):\n    return data\n"
    r = LogicGuard.check(_BASE, after, "python")
    # strict=True is required=False (has default), timeout=None is required=False
    # ->no REQUIRED_PARAM_ADDED here
    check("check: optional_added.no_high", not any(
        v.type == "REQUIRED_PARAM_ADDED" for v in r.violations
    ))

    # Now add a genuinely required param
    after2 = "def process(data, mode, timeout):\n    return data\n"
    r2 = LogicGuard.check(_BASE, after2, "python")
    high = [v for v in r2.violations if v.type == "REQUIRED_PARAM_ADDED"]
    check("check: required_added.violation found", len(high) >= 1)
    check("check: required_added.severity=high", high[0].severity == "high")


def test_check_param_removed():
    after = "def process(data):\n    return data\n"
    r = LogicGuard.check(_BASE, after, "python")
    removed = [v for v in r.violations if v.type == "PARAM_REMOVED"]
    check("check: param_removed.violation found", len(removed) == 1)
    check("check: param_removed.severity=high", removed[0].severity == "high")
    check("check: param_removed.mentions mode",
          "mode" in removed[0].detail)


def test_check_return_path_lost():
    before = "def compute(x):\n    return x * 2\n"
    after = "def compute(x):\n    print(x)\n"
    r = LogicGuard.check(before, after, "python")
    lost = [v for v in r.violations if v.type == "RETURN_PATH_LOST"]
    check("check: return_lost.violation found", len(lost) == 1)
    check("check: return_lost.severity=high", lost[0].severity == "high")
    check("check: return_lost.function_name=compute",
          lost[0].function_name == "compute")


def test_check_return_path_preserved():
    before = "def compute(x):\n    return x * 2\n"
    after = "def compute(x):\n    return x + 1\n"
    r = LogicGuard.check(before, after, "python")
    lost = [v for v in r.violations if v.type == "RETURN_PATH_LOST"]
    check("check: return_preserved.no violation", len(lost) == 0)


def test_check_exception_added():
    before = "def fetch(url):\n    return url\n"
    after = "def fetch(url):\n    raise ValueError('bad')\n    return url\n"
    r = LogicGuard.check(before, after, "python")
    added = [v for v in r.violations if v.type == "EXCEPTION_ADDED"]
    check("check: exception_added.violation found", len(added) == 1)
    check("check: exception_added.severity=medium", added[0].severity == "medium")
    check("check: exception_added.mentions ValueError",
          "ValueError" in added[0].detail)


def test_check_exception_already_existed():
    src = "def fetch(url):\n    raise ValueError('bad')\n    return url\n"
    r = LogicGuard.check(src, src, "python")
    added = [v for v in r.violations if v.type == "EXCEPTION_ADDED"]
    check("check: exception_existed.no violation", len(added) == 0)


def test_check_new_function_no_violation():
    before = "def foo(): return 1\n"
    after = "def foo(): return 1\ndef bar(x, y): return x + y\n"
    r = LogicGuard.check(before, after, "python")
    check("check: new_function.0 violations", len(r.violations) == 0)
    check("check: new_function.checked=1", r.checked_functions == 1)


def test_check_removed_function_not_flagged():
    before = "def foo(): return 1\ndef bar(): pass\n"
    after = "def foo(): return 1\n"
    r = LogicGuard.check(before, after, "python")
    # Logic Guard only compares common functions; O.14 handles removal
    check("check: removed_function.0 violations", len(r.violations) == 0)
    check("check: removed_function.checked=1", r.checked_functions == 1)


def test_check_syntax_error_before():
    r = LogicGuard.check("def (: bad", "def foo(): return 1\n", "python")
    # before fails to parse ->no common functions ->0 violations
    check("check: syntax_before.0 violations", len(r.violations) == 0)


def test_check_syntax_error_after():
    r = LogicGuard.check("def foo(): return 1\n", "def (: bad", "python")
    check("check: syntax_after.0 violations", len(r.violations) == 0)


def test_check_multiple_violations_one_function():
    before = "def transform(data, mode):\n    return data\n"
    after = "def transform(data):\n    print(data)\n"
    r = LogicGuard.check(before, after, "python")
    types = {v.type for v in r.violations}
    check("check: multi_violation.PARAM_REMOVED", "PARAM_REMOVED" in types)
    check("check: multi_violation.RETURN_PATH_LOST", "RETURN_PATH_LOST" in types)


def test_check_no_violation_bare_return_both():
    before = "def reset():\n    self.x = 0\n    return\n"
    after = "def reset():\n    self.x = 1\n    return\n"
    r = LogicGuard.check(before, after, "python")
    lost = [v for v in r.violations if v.type == "RETURN_PATH_LOST"]
    check("check: bare_return_both.no RETURN_PATH_LOST", len(lost) == 0)


# ---------------------------------------------------------------------------
# 3. Q.2 — I-4 called_functions extraction
# ---------------------------------------------------------------------------

def test_extract_called_direct():
    src = "def foo():\n    bar()\n    baz()\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: calls.direct.bar", "bar" in snaps["foo"].called_functions)
    check("extract: calls.direct.baz", "baz" in snaps["foo"].called_functions)


def test_extract_called_method():
    src = "def foo():\n    obj.method()\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: calls.method", "obj.method" in snaps["foo"].called_functions)


def test_extract_called_builtin():
    src = "def foo(x):\n    return len(str(x))\n"
    snaps = LogicGuardExtractor.extract_python(src)
    cf = snaps["foo"].called_functions
    check("extract: calls.builtin.len", "len" in cf)
    check("extract: calls.builtin.str", "str" in cf)


def test_extract_called_deep_chain():
    src = "def foo():\n    a.b.c()\n"
    snaps = LogicGuardExtractor.extract_python(src)
    # deeper than 2 levels -> just the attr name
    check("extract: calls.deep_chain.c", "c" in snaps["foo"].called_functions)


def test_extract_called_nested_not_counted():
    src = (
        "def outer():\n"
        "    def inner(): secret_call()\n"
        "    public_call()\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    cf_outer = snaps["outer"].called_functions
    check("extract: calls.nested.public_call in outer", "public_call" in cf_outer)
    check("extract: calls.nested.secret_call NOT in outer", "secret_call" not in cf_outer)
    check("extract: calls.nested.secret_call in inner", "secret_call" in snaps["inner"].called_functions)


def test_extract_called_empty():
    src = "def foo():\n    x = 1 + 2\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: calls.empty", snaps["foo"].called_functions == set())


# ---------------------------------------------------------------------------
# 4. Q.2 — I-5 branch_count extraction
# ---------------------------------------------------------------------------

def test_extract_branch_no_branches():
    src = "def foo(x):\n    return x + 1\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: branch.no_branches=0", snaps["foo"].branch_count == 0)


def test_extract_branch_single_if():
    src = "def foo(x):\n    if x > 0:\n        return x\n    return -x\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: branch.single_if=1", snaps["foo"].branch_count == 1)


def test_extract_branch_for_while():
    src = (
        "def foo(lst):\n"
        "    for x in lst:\n"
        "        pass\n"
        "    while True:\n"
        "        break\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: branch.for_while=2", snaps["foo"].branch_count == 2)


def test_extract_branch_except():
    src = (
        "def foo():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        pass\n"
        "    except KeyError:\n"
        "        pass\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: branch.two_excepts=2", snaps["foo"].branch_count == 2)


def test_extract_branch_boolop():
    src = "def foo(a, b):\n    if a and b:\n        return True\n"
    snaps = LogicGuardExtractor.extract_python(src)
    # 1 If + 1 BoolOp = 2
    check("extract: branch.if_and=2", snaps["foo"].branch_count == 2)


def test_extract_branch_ternary():
    src = "def foo(x):\n    return x if x > 0 else -x\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: branch.ternary=1", snaps["foo"].branch_count == 1)


def test_extract_branch_nested_not_counted():
    src = (
        "def outer():\n"
        "    def inner():\n"
        "        if True: pass\n"
        "        for x in []: pass\n"
        "    return 1\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: branch.nested.outer=0", snaps["outer"].branch_count == 0)
    check("extract: branch.nested.inner=2", snaps["inner"].branch_count == 2)


# ---------------------------------------------------------------------------
# 5. Q.2 — violation detection for I-4 and I-5
# ---------------------------------------------------------------------------

def test_check_call_removed():
    before = "def run():\n    db.commit()\n    return True\n"
    after  = "def run():\n    return True\n"
    r = LogicGuard.check(before, after, "python")
    rem = [v for v in r.violations if v.type == "CALL_REMOVED"]
    check("check: call_removed.violation found", len(rem) == 1)
    check("check: call_removed.severity=medium", rem[0].severity == "medium")
    check("check: call_removed.detail mentions db.commit", "db.commit" in rem[0].detail)


def test_check_call_added():
    before = "def run():\n    return True\n"
    after  = "def run():\n    os.system('rm -rf /')\n    return True\n"
    r = LogicGuard.check(before, after, "python")
    add = [v for v in r.violations if v.type == "CALL_ADDED"]
    check("check: call_added.violation found", len(add) == 1)
    check("check: call_added.severity=medium", add[0].severity == "medium")
    check("check: call_added.detail mentions os.system", "os.system" in add[0].detail)


def test_check_call_replaced():
    before = "def run():\n    result = eval(cmd)\n    return result\n"
    after  = "def run():\n    result = ast.literal_eval(cmd)\n    return result\n"
    r = LogicGuard.check(before, after, "python")
    types = {v.type for v in r.violations}
    check("check: call_replaced.CALL_REMOVED", "CALL_REMOVED" in types)
    check("check: call_replaced.CALL_ADDED", "CALL_ADDED" in types)


def test_check_calls_identical_no_violation():
    src = "def run():\n    x = len(items)\n    return x\n"
    r = LogicGuard.check(src, src, "python")
    call_v = [v for v in r.violations if v.type in ("CALL_REMOVED", "CALL_ADDED")]
    check("check: calls_identical.no violations", len(call_v) == 0)


def test_check_complexity_spike():
    before = "def process(x):\n    return x + 1\n"
    after = (
        "def process(x):\n"
        "    if x is None:\n"
        "        raise ValueError\n"
        "    if x < 0:\n"
        "        x = abs(x)\n"
        "    elif x == 0:\n"
        "        return 0\n"
        "    try:\n"
        "        result = x * 2\n"
        "    except TypeError:\n"
        "        result = 0\n"
        "    except ValueError:\n"
        "        result = -1\n"
        "    for i in range(x):\n"
        "        if i % 2 == 0:\n"
        "            result += i\n"
        "    return result\n"
    )
    r = LogicGuard.check(before, after, "python")
    spikes = [v for v in r.violations if v.type == "COMPLEXITY_SPIKE"]
    check("check: complexity_spike.found", len(spikes) == 1)
    check("check: complexity_spike.severity=medium", spikes[0].severity == "medium")
    check("check: complexity_spike.delta>=5", spikes[0].after_value - spikes[0].before_value >= 5)


def test_check_no_complexity_spike_small_delta():
    before = "def fn(x):\n    return x\n"
    after = (
        "def fn(x):\n"
        "    if x is None: return 0\n"
        "    if x < 0: x = 0\n"
        "    return x\n"
    )
    r = LogicGuard.check(before, after, "python")
    spikes = [v for v in r.violations if v.type == "COMPLEXITY_SPIKE"]
    # delta = 2, below threshold of 5
    check("check: no_spike.small_delta=2", len(spikes) == 0)


def test_check_complexity_identical_no_spike():
    src = (
        "def fn(x):\n"
        "    if x > 0:\n"
        "        for i in range(x): pass\n"
        "    return x\n"
    )
    r = LogicGuard.check(src, src, "python")
    spikes = [v for v in r.violations if v.type == "COMPLEXITY_SPIKE"]
    check("check: complexity_identical.no spike", len(spikes) == 0)


def test_check_q2_benchmark_attribute_fix():
    """Attribute typo fix: s.lenght ->len(s). Expect CALL_REMOVED + CALL_ADDED, no HIGH."""
    before = "def get_length(s):\n    return s.lenght\n"
    after  = "def get_length(s):\n    return len(s)\n"
    r = LogicGuard.check(before, after, "python")
    high = [v for v in r.violations if v.severity == "high"]
    medium = [v for v in r.violations if v.severity == "medium"]
    check("check: attr_fix.no HIGH", len(high) == 0)
    check("check: attr_fix.medium violations (CALL_REMOVED/ADDED)", len(medium) >= 1)


def test_check_q2_security_fix_eval_to_literal():
    """Security fix: eval ->ast.literal_eval. CALL_REMOVED+ADDED, no HIGH."""
    before = "def run(cmd):\n    return eval(cmd)\n"
    after  = "def run(cmd):\n    return ast.literal_eval(cmd)\n"
    r = LogicGuard.check(before, after, "python")
    types = {v.type for v in r.violations}
    high = [v for v in r.violations if v.severity == "high"]
    check("check: eval_fix.no HIGH", len(high) == 0)
    check("check: eval_fix.CALL_REMOVED", "CALL_REMOVED" in types)
    check("check: eval_fix.CALL_ADDED", "CALL_ADDED" in types)


# ---------------------------------------------------------------------------
# 6. Q.3 — side_effects extraction (I-6)
# ---------------------------------------------------------------------------

def test_extract_side_effects_none():
    src = "def fn(x):\n    return x + 1\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.empty", snaps["fn"].side_effects == frozenset())


def test_extract_side_effects_subprocess_run():
    src = "def fn():\n    subprocess.run(['ls'])\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.subprocess.run", "subprocess" in snaps["fn"].side_effects)


def test_extract_side_effects_os_system():
    src = "def fn(cmd):\n    os.system(cmd)\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.os.system=subprocess", "subprocess" in snaps["fn"].side_effects)


def test_extract_side_effects_network_requests():
    src = "def fn():\n    requests.get('http://example.com')\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.requests.get=network", "network" in snaps["fn"].side_effects)


def test_extract_side_effects_open_write():
    src = "def fn(path):\n    open(path, 'w')\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.open_w=file_write", "file_write" in snaps["fn"].side_effects)


def test_extract_side_effects_open_read():
    src = "def fn(path):\n    open(path, 'r')\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.open_r=file_read", "file_read" in snaps["fn"].side_effects)
    check("extract: side_effects.open_r.not_file_write", "file_write" not in snaps["fn"].side_effects)


def test_extract_side_effects_open_no_mode():
    src = "def fn(path):\n    open(path)\n"
    snaps = LogicGuardExtractor.extract_python(src)
    # неизвестный режим -> file_read (безопаснее)
    check("extract: side_effects.open_no_mode=file_read", "file_read" in snaps["fn"].side_effects)


def test_extract_side_effects_write_text():
    src = "def fn(p):\n    p.write_text('hello')\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.write_text=file_write", "file_write" in snaps["fn"].side_effects)


def test_extract_side_effects_read_bytes():
    src = "def fn(p):\n    return p.read_bytes()\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.read_bytes=file_read", "file_read" in snaps["fn"].side_effects)


def test_extract_side_effects_global_write():
    src = "def fn():\n    global counter\n    counter += 1\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.global_write", "global_write" in snaps["fn"].side_effects)


def test_extract_side_effects_nested_not_counted():
    src = (
        "def outer():\n"
        "    return 1\n"
        "    def inner():\n"
        "        subprocess.run(['x'])\n"
    )
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.nested.outer_clean",
          "subprocess" not in snaps["outer"].side_effects)
    check("extract: side_effects.nested.inner_has",
          "subprocess" in snaps["inner"].side_effects)


def test_extract_side_effects_shutil_move():
    src = "def fn(src, dst):\n    shutil.move(src, dst)\n"
    snaps = LogicGuardExtractor.extract_python(src)
    check("extract: side_effects.shutil.move=file_write", "file_write" in snaps["fn"].side_effects)


# ---------------------------------------------------------------------------
# 7. Q.3 — violation detection (SIDE_EFFECT_ADDED / SIDE_EFFECT_REMOVED)
# ---------------------------------------------------------------------------

def test_check_side_effect_added_subprocess_high():
    before = "def fn():\n    return 1\n"
    after  = "def fn():\n    subprocess.run(['ls'])\n    return 1\n"
    r = LogicGuard.check(before, after, "python")
    added = [v for v in r.violations if v.type == "SIDE_EFFECT_ADDED"]
    check("check: side_effect_added.subprocess.found", len(added) == 1)
    check("check: side_effect_added.subprocess.HIGH", added[0].severity == "high")
    check("check: side_effect_added.subprocess.detail", "subprocess" in added[0].detail)


def test_check_side_effect_added_network_high():
    before = "def fn():\n    return 1\n"
    after  = "def fn():\n    requests.get('http://x.com')\n    return 1\n"
    r = LogicGuard.check(before, after, "python")
    added = [v for v in r.violations if v.type == "SIDE_EFFECT_ADDED"]
    check("check: side_effect_added.network.HIGH", any(v.severity == "high" for v in added))


def test_check_side_effect_added_file_write_high():
    before = "def fn(path):\n    pass\n"
    after  = "def fn(path):\n    open(path, 'w')\n"
    r = LogicGuard.check(before, after, "python")
    added = [v for v in r.violations if v.type == "SIDE_EFFECT_ADDED"]
    check("check: side_effect_added.file_write.HIGH", any(v.severity == "high" for v in added))


def test_check_side_effect_added_file_read_medium():
    before = "def fn(path):\n    pass\n"
    after  = "def fn(path):\n    open(path)\n"
    r = LogicGuard.check(before, after, "python")
    added = [v for v in r.violations if v.type == "SIDE_EFFECT_ADDED"]
    check("check: side_effect_added.file_read.MEDIUM", any(v.severity == "medium" for v in added))


def test_check_side_effect_added_global_write_medium():
    before = "def fn():\n    return 1\n"
    after  = "def fn():\n    global counter\n    counter += 1\n    return 1\n"
    r = LogicGuard.check(before, after, "python")
    added = [v for v in r.violations if v.type == "SIDE_EFFECT_ADDED"]
    check("check: side_effect_added.global_write.MEDIUM",
          any(v.severity == "medium" for v in added))


def test_check_side_effect_removed_medium():
    before = "def fn():\n    subprocess.run(['x'])\n    return 1\n"
    after  = "def fn():\n    return 1\n"
    r = LogicGuard.check(before, after, "python")
    removed = [v for v in r.violations if v.type == "SIDE_EFFECT_REMOVED"]
    check("check: side_effect_removed.found", len(removed) == 1)
    check("check: side_effect_removed.severity=medium", removed[0].severity == "medium")


def test_check_side_effect_unchanged_no_violation():
    src = "def fn():\n    subprocess.run(['x'])\n    return 1\n"
    r = LogicGuard.check(src, src, "python")
    fx_v = [v for v in r.violations
            if v.type in ("SIDE_EFFECT_ADDED", "SIDE_EFFECT_REMOVED")]
    check("check: side_effect_unchanged.no violation", len(fx_v) == 0)


def test_check_q3_benchmark_os_getcwd_not_flagged():
    """os.getcwd() не входит ни в один side-effect список — патч не должен давать HIGH."""
    before = "def fn():\n    return '/tmp'\n"
    after  = "def fn():\n    return os.getcwd()\n"
    r = LogicGuard.check(before, after, "python")
    high = [v for v in r.violations if v.severity == "high"]
    check("check: os_getcwd.no HIGH side-effect", len(high) == 0)


# ---------------------------------------------------------------------------
# 8. Routing helper: HIGH violations ->NEEDS_REVIEW
# ---------------------------------------------------------------------------

def test_routing_high_triggers_needs_review():
    """Simulate DecideStage routing logic with a HIGH violation payload."""
    lg_meta = {
        "violations": [
            {"function_name": "foo", "type": "REQUIRED_PARAM_ADDED",
             "severity": "high", "detail": "grew from 1 to 2"},
        ],
        "checked_functions": 1,
        "skipped_reason": "",
    }
    high = [v for v in lg_meta["violations"] if v["severity"] == "high"]
    check("routing: HIGH triggers NEEDS_REVIEW", len(high) == 1)


def test_routing_medium_only_does_not_trigger():
    lg_meta = {
        "violations": [
            {"function_name": "bar", "type": "EXCEPTION_ADDED",
             "severity": "medium", "detail": "new: ValueError"},
        ],
        "checked_functions": 1,
        "skipped_reason": "",
    }
    high = [v for v in lg_meta["violations"] if v["severity"] == "high"]
    check("routing: MEDIUM only ->no auto-NEEDS_REVIEW", len(high) == 0)


def test_routing_empty_violations():
    lg_meta = {"violations": [], "checked_functions": 3, "skipped_reason": ""}
    high = [v for v in lg_meta["violations"] if v["severity"] == "high"]
    check("routing: empty violations ->ok", len(high) == 0)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n=== Q.1 + Q.2 + Q.3 Logic Guard Tests ===\n")
    test_extract_empty_source()
    test_extract_syntax_error()
    test_extract_simple_function()
    test_extract_optional_params()
    test_extract_kwonly_required()
    test_extract_no_return()
    test_extract_bare_return()
    test_extract_value_return()
    test_extract_nested_return_not_counted()
    test_extract_raised_exception_name()
    test_extract_raised_exception_instance()
    test_extract_raised_exception_attribute()
    test_extract_nested_raise_not_counted()
    test_extract_multiple_functions()
    test_extract_method_in_class()
    test_extract_async_function()
    test_check_identical_no_violations()
    test_check_unsupported_language()
    test_check_empty_language_skipped()
    test_check_language_py()
    test_check_required_param_added()
    test_check_param_removed()
    test_check_return_path_lost()
    test_check_return_path_preserved()
    test_check_exception_added()
    test_check_exception_already_existed()
    test_check_new_function_no_violation()
    test_check_removed_function_not_flagged()
    test_check_syntax_error_before()
    test_check_syntax_error_after()
    test_check_multiple_violations_one_function()
    test_check_no_violation_bare_return_both()
    # Q.2 — I-4 calls extraction
    test_extract_called_direct()
    test_extract_called_method()
    test_extract_called_builtin()
    test_extract_called_deep_chain()
    test_extract_called_nested_not_counted()
    test_extract_called_empty()
    # Q.2 — I-5 branch_count extraction
    test_extract_branch_no_branches()
    test_extract_branch_single_if()
    test_extract_branch_for_while()
    test_extract_branch_except()
    test_extract_branch_boolop()
    test_extract_branch_ternary()
    test_extract_branch_nested_not_counted()
    # Q.2 — violation detection
    test_check_call_removed()
    test_check_call_added()
    test_check_call_replaced()
    test_check_calls_identical_no_violation()
    test_check_complexity_spike()
    test_check_no_complexity_spike_small_delta()
    test_check_complexity_identical_no_spike()
    test_check_q2_benchmark_attribute_fix()
    test_check_q2_security_fix_eval_to_literal()
    # Q.3 — side_effects extraction
    test_extract_side_effects_none()
    test_extract_side_effects_subprocess_run()
    test_extract_side_effects_os_system()
    test_extract_side_effects_network_requests()
    test_extract_side_effects_open_write()
    test_extract_side_effects_open_read()
    test_extract_side_effects_open_no_mode()
    test_extract_side_effects_write_text()
    test_extract_side_effects_read_bytes()
    test_extract_side_effects_global_write()
    test_extract_side_effects_nested_not_counted()
    test_extract_side_effects_shutil_move()
    # Q.3 — violation detection
    test_check_side_effect_added_subprocess_high()
    test_check_side_effect_added_network_high()
    test_check_side_effect_added_file_write_high()
    test_check_side_effect_added_file_read_medium()
    test_check_side_effect_added_global_write_medium()
    test_check_side_effect_removed_medium()
    test_check_side_effect_unchanged_no_violation()
    test_check_q3_benchmark_os_getcwd_not_flagged()
    # Q.1/Q.2/Q.3 routing
    test_routing_high_triggers_needs_review()
    test_routing_medium_only_does_not_trigger()
    test_routing_empty_violations()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        print("\nFailed:")
        for name, ok in results:
            if not ok:
                print(f"  FAIL: {name}")
        sys.exit(1)
    else:
        print("All tests passed.")
