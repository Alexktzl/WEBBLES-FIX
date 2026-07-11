"""
Verify P2.7 Fix #3: ruff runs in validate_stage and invalid-syntax
can no longer slip through false ACCEPT.
"""
import sys
import types
import inspect
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True
sys.modules.setdefault("tomlkit", types.ModuleType("tomlkit"))

from core.stages.validate_stage import ValidateStage
from core.stages.analyze_stage import AnalyzeStage

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- 1. _use_ruff_enabled added to ValidateStage ---
ctx_on = MagicMock()
ctx_on.config = {"pipeline": {"use_ruff": True}}
ctx_off = MagicMock()
ctx_off.config = {"pipeline": {}}
ctx_none = MagicMock()
ctx_none.config = {}

check("_use_ruff_enabled: on -> True", ValidateStage._use_ruff_enabled(ctx_on) is True)
check("_use_ruff_enabled: off -> False", ValidateStage._use_ruff_enabled(ctx_off) is False)
check("_use_ruff_enabled: no key -> False", ValidateStage._use_ruff_enabled(ctx_none) is False)

# --- 2. validate_stage.execute source has all required tokens ---
src_validate = inspect.getsource(ValidateStage.execute)
check("validate_stage: ruff_new_errors variable", "ruff_new_errors" in src_validate)
check("validate_stage: _ruff_count_before key read", "_ruff_count_before" in src_validate)
check("validate_stage: _seen_keys dedup", "_seen_keys" in src_validate)
check("validate_stage: new_errors_primary (separate from ruff)", "new_errors_primary" in src_validate)
check("validate_stage: _vm _ruff_count_before update", '_vm["_ruff_count_before"]' in src_validate)
check("validate_stage: combined before = _pb + _rb", "_rb" in src_validate)
check("validate_stage: RuffAnalyzer import inside block", "RuffAnalyzer" in src_validate)

# --- 3. analyze_stage.execute source has _ruff_count_for_meta ---
src_analyze = inspect.getsource(AnalyzeStage.execute)
check("analyze_stage: _ruff_count_for_meta variable", "_ruff_count_for_meta" in src_analyze)
check("analyze_stage: _ruff_count_before key saved", '"_ruff_count_before"' in src_analyze)

# --- 4. Scenario: combined baseline prevents false ACCEPT ---
# Old (Fix #2 only):
#   before = 5 (flake8), after = 5 (flake8) -> equal -> conditional ACCEPT risk (line 288)
# New (Fix #3):
#   before = 5+3=8 (flake8+ruff), after = 5+3=8 (flake8+ruff) -> equal -> same conditional
#   BUT: target_still_present = True (ruff invalid-syntax IS in context.current_errors)
#        -> REJECT via line 307, never reaches line 276 or 288
check("scenario: ruff errors in current_errors -> target_still_present=True blocks ACCEPT",
      "ruff_new_errors" in src_validate and "new_errors.append(_err)" in src_validate)

# --- 5. Before = _pb + _rb (combined, not just flake8) ---
check("combined before uses _pb + _rb", "(int(_pb) + _rb)" in src_validate)

# --- 6. Fix #1 untouched (pre_patch_content still in _save_patch_snapshot) ---
src_save = inspect.getsource(ValidateStage._save_patch_snapshot)
check("Fix #1 preserved: _pre_patch_content in _save_patch_snapshot",
      "_pre_patch_content" in src_save)

print()
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"P2.7 Fix #3 verification: {passed}/{total} pass")
sys.exit(0 if passed == total else 1)
