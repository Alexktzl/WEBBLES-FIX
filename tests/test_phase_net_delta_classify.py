"""
Net-delta smart classification — unit tests.
Covers parse_patch_changed_lines and classify_net_delta in core/stages/net_delta_check.py.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from core.stages.net_delta_check import (
    parse_patch_changed_lines,
    classify_net_delta,
    _PROXIMITY_LINES,
)

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


# ─────────────────────────────────────────────
# parse_patch_changed_lines
# ─────────────────────────────────────────────

_PATCH_SIMPLE = """\
--- a/foo/bar.py
+++ b/foo/bar.py
@@ -10,4 +10,5 @@
 context
-old line
+new line
+extra line
 context
"""

def test_parse_added_lines():
    lines = parse_patch_changed_lines(_PATCH_SIMPLE, "foo/bar.py")
    # '+' lines: new_line=11 and 12
    check("parse_added_line_11", 11 in lines)
    check("parse_added_line_12", 12 in lines)

def test_parse_removed_line_marks_position():
    lines = parse_patch_changed_lines(_PATCH_SIMPLE, "foo/bar.py")
    # '-' removal at new_line=11 position (before the + lines advance)
    # The context line brings new_line to 11 (10+1), removal marks 11
    check("parse_removed_marks_position", len(lines) > 0)

def test_parse_wrong_file_returns_empty():
    lines = parse_patch_changed_lines(_PATCH_SIMPLE, "other/file.py")
    check("parse_wrong_file_empty", lines == set())

def test_parse_empty_patch():
    check("parse_empty_string", parse_patch_changed_lines("", "foo.py") == set())
    check("parse_empty_file", parse_patch_changed_lines(_PATCH_SIMPLE, "") == set())

def test_parse_basename_match():
    lines = parse_patch_changed_lines(_PATCH_SIMPLE, "bar.py")
    check("parse_basename_match_nonempty", len(lines) > 0)

_PATCH_BACKSLASH = """\
--- a/src\\utils.py
+++ b/src\\utils.py
@@ -5,3 +5,4 @@
 context
+added
 context
"""

def test_parse_backslash_path():
    lines = parse_patch_changed_lines(_PATCH_BACKSLASH, "src/utils.py")
    check("parse_backslash_nonempty", len(lines) > 0)


# ─────────────────────────────────────────────
# classify_net_delta helpers
# ─────────────────────────────────────────────

def _err(file, line, code, error_class="CLEANUP"):
    return {"file": file, "line": line, "code": code, "error_class": error_class}

_PATCH_FILE_A = """\
--- a/main.py
+++ b/main.py
@@ -20,3 +20,4 @@
 context
+fixed line
 context
"""

def test_no_new_errors_returns_none():
    before = [_err("main.py", 5, "E501")]
    after  = [_err("main.py", 5, "E501")]
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, _err("main.py", 5, "E501"))
    check("no_new_errors_none", result is None)

def test_regression_near_changed_line():
    # Changed line is 21; new error at 21 → regression
    before = []
    after  = [_err("main.py", 21, "E131")]
    orig   = _err("main.py", 10, "W291", "CLEANUP")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("regression_near_line", result is not None)
    check("regression_decision_rollback", result.decision == "rollback")
    check("regression_list_nonempty", len(result.regression) == 1)
    check("regression_unmasked_empty", len(result.unmasked) == 0)

def test_uncertain_far_from_change_style():
    # Changed line is 21; error at line 100 (far) → uncertain (style class)
    before = []
    after  = [_err("main.py", 100, "E203")]
    orig   = _err("main.py", 10, "W291", "CLEANUP")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("uncertain_far_style", result is not None)
    check("uncertain_far_decision", result.decision == "needs_review")
    check("uncertain_list_one", len(result.uncertain) == 1)

def test_other_file_style_uncertain():
    # Style patch; new error in another file → uncertain (not unmasked)
    before = []
    after  = [_err("utils.py", 10, "E501", "CLEANUP")]
    orig   = _err("main.py", 10, "W291", "CLEANUP")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("other_file_style_uncertain", result.decision == "needs_review")

def test_other_file_masking_unmasked():
    # Masking (BLOCKING) fix; new errors in other file → unmasked → keep
    before = []
    after  = [_err("utils.py", 10, "F401", "CLEANUP")]
    orig   = _err("main.py", 5, "E999", "BLOCKING")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("other_file_masking_keep", result is not None)
    check("other_file_masking_decision", result.decision == "keep")
    check("other_file_masking_unmasked_list", len(result.unmasked) == 1)

def test_same_file_masking_near_uncertain():
    # Masking fix; new error near changed line in same file → uncertain
    before = []
    after  = [_err("main.py", 21, "F401", "CLEANUP")]
    orig   = _err("main.py", 5, "E999", "BLOCKING")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("masking_near_uncertain", result.decision == "needs_review")

def test_same_file_masking_far_unmasked():
    # Masking fix; new error far from changed line in same file → unmasked
    before = []
    after  = [_err("main.py", 80, "F401", "CLEANUP")]
    orig   = _err("main.py", 5, "E999", "BLOCKING")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("masking_far_unmasked", result.decision == "keep")

def test_logic_patch_near_regression():
    # Logic patch; new error near changed line → regression
    before = []
    after  = [_err("main.py", 22, "B006", "BLOCKING")]
    orig   = _err("main.py", 20, "B008", "BLOCKING")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("logic_near_regression", result.decision == "rollback")

def test_logic_patch_far_uncertain():
    # Logic patch; new error far from changed line → uncertain (not regression)
    before = []
    after  = [_err("main.py", 99, "B006", "BLOCKING")]
    orig   = _err("main.py", 20, "B008", "BLOCKING")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("logic_far_uncertain", result.decision == "needs_review")

def test_pre_existing_error_not_new():
    # Error with same (file, line, code) as before → not classified as new
    before = [_err("main.py", 21, "E131")]
    after  = [_err("main.py", 21, "E131"), _err("main.py", 21, "E501")]
    orig   = _err("main.py", 10, "W291", "CLEANUP")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    # Only E501 is new; it's near changed lines → regression
    check("pre_existing_not_counted", result is not None)
    check("only_new_code_classified", len(result.regression) == 1)
    check("new_code_is_E501", result.regression[0]["code"] == "E501")

def test_mixed_regression_and_unmasked():
    # Some errors are regressions, some are unmasked (masking fix + near + far)
    before = []
    after  = [
        _err("main.py", 21, "E131"),    # near → uncertain (masking)
        _err("other.py", 5, "F401"),    # other file → unmasked (masking)
    ]
    orig = _err("main.py", 5, "E999", "BLOCKING")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("mixed_uncertain_and_unmasked_decision", result.decision == "needs_review")
    check("mixed_uncertain_count", len(result.uncertain) == 1)
    check("mixed_unmasked_count", len(result.unmasked) == 1)

def test_proximity_boundary():
    # Error exactly at PROXIMITY_LINES distance → near
    changed_line = 21  # from _PATCH_FILE_A
    boundary_line = changed_line + _PROXIMITY_LINES
    before = []
    after  = [_err("main.py", boundary_line, "E131")]
    orig   = _err("main.py", 10, "W291", "CLEANUP")
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("proximity_boundary_near", result.decision == "rollback")

    # One line beyond proximity → uncertain
    beyond_line = changed_line + _PROXIMITY_LINES + 1
    after2 = [_err("main.py", beyond_line, "E131")]
    result2 = classify_net_delta(before, after2, "main.py", _PATCH_FILE_A, orig)
    check("proximity_beyond_uncertain", result2.decision == "needs_review")

def test_no_patch_text_conservative():
    # No patch → can't determine changed lines → assume near → regression for style
    before = []
    after  = [_err("main.py", 50, "E131")]
    orig   = _err("main.py", 10, "W291", "CLEANUP")
    result = classify_net_delta(before, after, "main.py", "", orig)
    check("no_patch_conservative_regression", result.decision == "rollback")

def test_critical_syntax_code_triggers_masking():
    # E999 code (masking code regardless of class) → masking behavior
    before = []
    after  = [_err("other.py", 5, "F401")]
    orig   = {"file": "main.py", "line": 5, "code": "E999", "error_class": "UNKNOWN"}
    result = classify_net_delta(before, after, "main.py", _PATCH_FILE_A, orig)
    check("e999_code_masking", result.decision == "keep")


# ─────────────────────────────────────────────
# Run and report
# ─────────────────────────────────────────────

test_parse_added_lines()
test_parse_removed_line_marks_position()
test_parse_wrong_file_returns_empty()
test_parse_empty_patch()
test_parse_basename_match()
test_parse_backslash_path()
test_no_new_errors_returns_none()
test_regression_near_changed_line()
test_uncertain_far_from_change_style()
test_other_file_style_uncertain()
test_other_file_masking_unmasked()
test_same_file_masking_near_uncertain()
test_same_file_masking_far_unmasked()
test_logic_patch_near_regression()
test_logic_patch_far_uncertain()
test_pre_existing_error_not_new()
test_mixed_regression_and_unmasked()
test_proximity_boundary()
test_no_patch_text_conservative()
test_critical_syntax_code_triggers_masking()

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, note) for n, ok, note in results if not ok]

print(f"net_delta_classify: {passed}/{len(results)} passed")
if failed:
    for name, note in failed:
        print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
    raise SystemExit(1)
