"""
Net-delta smart classification: distinguishes patch regressions from diagnostic unmasking.

After each patch, if error count grew, we classify new errors into:
  - regression        new errors ON / NEAR changed lines in patched file → rollback
  - unmasked          errors in OTHER files, or far from changes after fixing a
                      blocking/syntax/type mask → keep (pre-existing, now visible)
  - uncertain         can't confidently determine cause → NEEDS_REVIEW

Decision table:
  any regression   → rollback    (revert files, skip error)
  any uncertain    → needs_review (revert files, save to NR queue)
  only unmasked    → keep        (proceed to REVIEWING normally)

Masking error classes (BLOCKING / CRITICAL_SYNTAX / CRITICAL):
  Fixing them removes an analysis blocker, so errors that appear afterwards —
  especially in other files — are very likely pre-existing but previously hidden.
  Same-location new errors after a masking fix are classified as uncertain
  (could be the actual fix or newly uncovered regression).

Style error classes (CLEANUP / FORMATTING / STYLE):
  Formatting patches do not mask analysis, so new errors anywhere in the same
  file are either regression (near changed lines) or uncertain (far from changes).
  Other-file errors from a style change are also uncertain (style shouldn't affect them).

Other classes (LOGIC / SECURITY / UNKNOWN / etc.):
  Same-file near-change errors → regression.
  Everything else → uncertain.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, NamedTuple, Optional, Set

logger = logging.getLogger(__name__)

# Unified diff hunk header
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Error classes whose fix removes an analysis blocker (unmasks errors in other parts).
# NOTE: BLOCKING is intentionally excluded — many BLOCKING errors are logic issues
# (B006, B008, F821...) that do NOT stop analysis. True stoppers are identified by
# _MASKING_CODES (E999, E902) or CRITICAL_SYNTAX class.
_MASKING_CLASSES: frozenset = frozenset({"CRITICAL_SYNTAX", "CRITICAL"})
# Specific codes known to be file-level analysis stoppers
_MASKING_CODES: frozenset = frozenset({"E999", "E902", "invalid-syntax"})
# Style / formatting classes that never mask analysis
_STYLE_CLASSES: frozenset = frozenset({"CLEANUP", "FORMATTING", "STYLE"})

# Lines within this distance of a changed line are considered "near"
_PROXIMITY_LINES = 10

# Sibling code pairs: fixing code A naturally introduces code B as an
# expected side-effect (mutually exclusive lint rules). New errors with
# a sibling code are NOT counted as regressions.
_SIBLING_CODES: Dict[str, frozenset] = {
    "W503": frozenset({"W504"}),  # move operator to prev-line end → W504
    "W504": frozenset({"W503"}),  # move operator to next-line start → W503
    "E501": frozenset({"W503", "W504"}),  # wrapping long line may shift operator position
}


class NetDeltaClassification(NamedTuple):
    regression: List[dict]   # new errors near changed lines → rollback
    unmasked: List[dict]     # pre-existing errors now visible → keep
    uncertain: List[dict]    # unclear origin → NEEDS_REVIEW
    changed_lines: Set[int]  # 1-based new-file line numbers modified by patch

    @property
    def decision(self) -> str:
        """One of: rollback | needs_review | keep"""
        if self.regression:
            return "rollback"
        if self.uncertain:
            return "needs_review"
        return "keep"

    def to_metadata(self) -> Dict:
        return {
            "decision": self.decision,
            "regression_count": len(self.regression),
            "unmasked_count": len(self.unmasked),
            "uncertain_count": len(self.uncertain),
            "changed_lines_count": len(self.changed_lines),
        }


def parse_patch_changed_lines(patch: str, target_file: str) -> Set[int]:
    """Return 1-based line numbers in the NEW file that were touched by the patch.

    Parses a unified diff and tracks only the section for target_file.
    Returns an empty set if the patch can't be parsed or target_file is not found.
    """
    if not patch or not target_file:
        return set()

    target_norm = target_file.replace("\\", "/").lstrip("/")
    changed: Set[int] = set()

    in_target = False
    new_line = 0

    for raw in patch.splitlines():
        # File-boundary markers
        if raw.startswith("--- "):
            in_target = False
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            path = path.replace("\\", "/").lstrip("/")
            # Match if either path is a suffix of the other (handles git a/b prefixes)
            in_target = (
                path == target_norm
                or path.endswith("/" + target_norm)
                or target_norm.endswith("/" + path)
                or target_norm == path.split("/")[-1]  # basename fallback
            )
            new_line = 0
            continue

        if not in_target:
            continue

        m = _HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(2))
            continue

        # Skip combined header lines that slipped through
        if raw.startswith("+++") or raw.startswith("---"):
            continue

        if raw.startswith("+"):
            changed.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            # Removed line has no position in new file; mark the gap position
            if new_line > 0:
                changed.add(new_line)
            # Do NOT advance new_line — removed lines don't exist in new file
        else:
            # Context line
            new_line += 1

    return changed


def classify_net_delta(
    before_errors: List[dict],
    after_errors: List[dict],
    patched_file: str,
    patch_text: str,
    original_error: dict,
) -> Optional[NetDeltaClassification]:
    """Classify error growth after a patch.

    Returns None when there are no truly new errors (nothing to classify).
    Returns NetDeltaClassification with .decision and per-bucket lists otherwise.
    """
    patched_norm = patched_file.replace("\\", "/").lstrip("/")

    # Build exact-match key set for before-errors.
    # L9 (аудит 2026-07-01): пути нормализуются с обеих сторон — before может
    # содержать смешанные стили разделителей (инкрементальные merge на
    # Windows), и «та же» ошибка ложно классифицировалась как новая.
    def _key(e: dict) -> tuple:
        return (
            _f(e, "file").replace("\\", "/").lstrip("/"),
            _i(e, "line"),
            _s(e, "code"),
        )

    before_keys: Set[tuple] = {_key(e) for e in before_errors}

    # Identify truly new errors
    new_errors = [e for e in after_errors if _key(e) not in before_keys]
    if not new_errors:
        return None

    changed_lines = parse_patch_changed_lines(patch_text, patched_file)

    orig_class = _s(original_error, "error_class").upper()
    orig_code = _s(original_error, "code")

    is_masking = orig_class in _MASKING_CLASSES or orig_code in _MASKING_CODES
    is_style = orig_class in _STYLE_CLASSES

    # Filter out line-shifted errors for style patches.
    # When a style fix inserts or removes lines (e.g. E302 adding a blank line),
    # ALL subsequent errors in the same file shift their line numbers without
    # changing their count. A "new" error whose code count did NOT increase is a
    # line-shifted copy of a pre-existing error — not an actual regression.
    # Strip all such errors so the uncertain/regression classification does not
    # fire on noise from line-number shifting.
    if is_style:
        _code_cnt_b: Dict[str, int] = {}
        _code_cnt_a: Dict[str, int] = {}
        for _e in before_errors:
            if _f(_e, "file").replace("\\", "/").lstrip("/") == patched_norm:
                _c = _s(_e, "code")
                _code_cnt_b[_c] = _code_cnt_b.get(_c, 0) + 1
        for _e in after_errors:
            if _f(_e, "file").replace("\\", "/").lstrip("/") == patched_norm:
                _c = _s(_e, "code")
                _code_cnt_a[_c] = _code_cnt_a.get(_c, 0) + 1
        new_errors = [
            e for e in new_errors
            if not (
                _f(e, "file").replace("\\", "/").lstrip("/") == patched_norm
                and _code_cnt_a.get(_s(e, "code"), 0) <= _code_cnt_b.get(_s(e, "code"), 0)
            )
        ]
        if not new_errors:
            return None

    regression: List[dict] = []
    unmasked: List[dict] = []
    uncertain: List[dict] = []

    orig_siblings: frozenset = _SIBLING_CODES.get(orig_code, frozenset())

    for err in new_errors:
        err_code = _s(err, "code")

        # Sibling codes are expected side-effects of fixing the original error
        # (e.g. W503 fix moves operator → introduces W504). Not a regression.
        if err_code in orig_siblings:
            continue

        err_file = _f(err, "file").replace("\\", "/").lstrip("/")
        err_line = _i(err, "line")

        other_file = bool(err_file) and err_file != patched_norm

        if other_file:
            # Errors in files other than the patched one
            if is_masking:
                # Syntax/blocking fix unblocks project-wide analysis → pre-existing
                unmasked.append(err)
            else:
                # Style/logic patch shouldn't affect other files → uncertain
                uncertain.append(err)
            continue

        # Errors inside the patched file
        if changed_lines:
            near = any(abs(err_line - cl) <= _PROXIMITY_LINES for cl in changed_lines)
        else:
            # Can't parse patch → assume near (conservative)
            near = True

        if is_masking:
            # After fixing syntax/blocking: new errors on the same lines might be
            # the actual fix surfacing something, or might be pre-existing.
            if near:
                uncertain.append(err)   # same location as fix → can't tell
            else:
                unmasked.append(err)    # far from fix → definitely pre-existing
        elif is_style:
            # Formatting patch — errors near changes are almost certainly regressions
            if near:
                regression.append(err)
            else:
                uncertain.append(err)
        else:
            # Logic / security / unknown / other
            if near:
                regression.append(err)
            else:
                uncertain.append(err)

    return NetDeltaClassification(
        regression=regression,
        unmasked=unmasked,
        uncertain=uncertain,
        changed_lines=changed_lines,
    )


# ---- small helpers to avoid key-error noise ----

def _f(d: dict, k: str) -> str:
    return str(d.get(k) or "")


def _i(d: dict, k: str) -> int:
    try:
        return int(d.get(k) or 0)
    except (TypeError, ValueError):
        return 0


def _s(d: dict, k: str) -> str:
    return str(d.get(k) or "").strip()
