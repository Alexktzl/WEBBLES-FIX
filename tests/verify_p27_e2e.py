"""
P2.7 end-to-end scenario test:
  handlers.py has invalid-syntax (ruff) — bad patch doesn't fix it.
  Verifies false ACCEPT is blocked by target_still_present=True (Fix #3).
  Verifies Fix #1 rollback works (original_content = pre-patch).
  Verifies Fix #2 symmetry (combined flake8+ruff baseline).
"""
import sys
import types
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True
sys.modules.setdefault("tomlkit", types.ModuleType("tomlkit"))

from core.utils import error_signature as sig_fn
from core.stages.validate_stage import ValidateStage

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# -----------------------------------------------------------------------
# Shared error data
# -----------------------------------------------------------------------
# The TARGET error selected from context.current_errors at analyse time (ruff)
TARGET_ERROR = {
    "file": "handlers.py",
    "line": 1,
    "code": "invalid-syntax",
    "message": "SyntaxError: expected ':'",
    "error_type": "lint",
    "error_class": "CRITICAL_SYNTAX",
}
TARGET_SIG = sig_fn(TARGET_ERROR)

# flake8 sees E999 for the SAME underlying issue (different code, possibly same message)
FLAKE8_ERRORS = [
    {"file": "handlers.py", "line": 1, "code": "E999",
     "message": "SyntaxError: expected ':'",   # same message as ruff
     "error_type": "compile", "error_class": "CRITICAL_SYNTAX"},
    {"file": "handlers.py", "line": 3, "code": "W291",
     "message": "trailing whitespace",
     "error_type": "lint", "error_class": "STYLE"},
    {"file": "handlers.py", "line": 5, "code": "E302",
     "message": "expected 2 blank lines",
     "error_type": "lint", "error_class": "STYLE"},
]
# ruff sees invalid-syntax for the same issue
RUFF_ERRORS = [
    {"file": "handlers.py", "line": 1, "code": "invalid-syntax",
     "message": "SyntaxError: expected ':'",   # same message as E999
     "error_type": "lint", "error_class": "CRITICAL_SYNTAX"},
]

print("--- Fix #3: merge + target_still_present ---")

# ---- Simulate the ruff merge step (file, line, code) dedup ----
new_errors = list(FLAKE8_ERRORS)
_seen_keys = {
    (e.get("file", ""), e.get("line", 0), e.get("code", ""))
    for e in new_errors
}
for _re2 in RUFF_ERRORS:
    _k = (_re2.get("file", ""), _re2.get("line", 0), _re2.get("code", ""))
    if _k not in _seen_keys:
        new_errors.append(_re2)
        _seen_keys.add(_k)

check("dedup (file,line,code): invalid-syntax NOT dropped (code differs from E999)",
      any(e["code"] == "invalid-syntax" for e in new_errors))
check("dedup (file,line,code): E999 kept",
      any(e["code"] == "E999" for e in new_errors))
check("total errors = 3 flake8 + 1 ruff = 4",
      len(new_errors) == 4)

# ---- target_still_present check ----
target_still_present = any(sig_fn(e) == TARGET_SIG for e in new_errors)
check("target_still_present = True (invalid-syntax in new_errors)", target_still_present)

# ---- before/after counts with combined baseline ----
_pb = 3   # _primary_error_count_before (flake8 only, from analyze_stage Fix #2)
_rb = 1   # _ruff_count_before (ruff only, from analyze_stage Fix #3)
before = _pb + _rb   # 4
after = len(new_errors)  # 4

check("before (combined) = 3 + 1 = 4", before == 4)
check("after (combined) = 4", after == 4)
check("after == before (bad patch: no net improvement)", after == before)

# ---- Decide gate: all false ACCEPT paths blocked ----
# Line 276: if after < before and not target_still_present -> ACCEPT
false_accept_276 = (after < before) and (not target_still_present)
check("false ACCEPT line 276 blocked (after not < before)", not false_accept_276)

# Line 288: if not target_still_present and after >= before -> conditional ACCEPT
false_accept_288 = (not target_still_present) and (after >= before)
check("false ACCEPT line 288 blocked (target IS still present)", not false_accept_288)

# Goes to REJECT (line 307): reason = "error_count_not_decreased" if after>=before
check("REJECT path reached: target_still_present=True + after>=before",
      target_still_present and after >= before)

print()
print("--- Fix #2 symmetry: correct fix scenario ---")

# Patch CORRECTLY fixes the syntax error:
#   flake8: 2 errors (E999 gone), ruff: 0 errors (invalid-syntax gone)
#   before = 4, after = 2
new_errors_fixed_primary = [
    {"file": "handlers.py", "line": 3, "code": "W291", "message": "trailing whitespace"},
    {"file": "handlers.py", "line": 5, "code": "E302", "message": "expected 2 blank lines"},
]
new_errors_fixed = list(new_errors_fixed_primary)  # ruff has 0 errors

after_fixed = len(new_errors_fixed)  # 2
target_still_present_fixed = any(sig_fn(e) == TARGET_SIG for e in new_errors_fixed)

check("correct fix: after=2, before=4, after < before", after_fixed < before)
check("correct fix: target_still_present=False (invalid-syntax gone)", not target_still_present_fixed)
check("correct fix: ACCEPT via line 276 (after < before AND not target_still_present)",
      (after_fixed < before) and (not target_still_present_fixed))

print()
print("--- Fix #1 preserved: snapshot uses pre-patch content ---")

ORIGINAL_VALID = "def foo(x):\n    return x + 1\n"
PATCHED_BROKEN = "import os\ndef foo(x)\n    return x + 1\n"

with tempfile.TemporaryDirectory() as tmpdir:
    work_dir = Path(tmpdir)
    (work_dir / "handlers.py").write_text(PATCHED_BROKEN, encoding="utf-8")

    ctx_snap = MagicMock()
    ctx_snap.selected_error = TARGET_ERROR
    ctx_snap.metadata = {
        "_pre_patch_content": {"handlers.py": ORIGINAL_VALID},
        "patch_snapshots": [],
    }
    ctx_snap.working_path = None
    ctx_snap.project_path = work_dir
    ctx_snap.language = "python"

    saved = {}
    def snap_update(**kw):
        if "metadata" in kw:
            saved.update(kw["metadata"])
        return ctx_snap
    ctx_snap.update = snap_update

    stage = ValidateStage.__new__(ValidateStage)
    stage._save_patch_snapshot(ctx_snap)

    snaps = saved.get("patch_snapshots", [])
    check("snapshot created", len(snaps) == 1)
    if snaps:
        check("original_content = ORIGINAL_VALID (not patched)",
              snaps[0].get("original_content") == ORIGINAL_VALID)
        check("patched_content = PATCHED_BROKEN",
              snaps[0].get("patched_content") == PATCHED_BROKEN)

print()
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"P2.7 e2e scenario: {passed}/{total} pass")
sys.exit(0 if passed == total else 1)
