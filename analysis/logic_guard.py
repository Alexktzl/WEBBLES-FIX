"""
Logic Guard — deterministic AST-based contract comparison for Python.

Q.1 invariants (signature, return_paths, exception_surface):
  I-1  REQUIRED_PARAM_ADDED  high   — required param count grew
  I-1  PARAM_REMOVED         high   — a param name disappeared
  I-2  RETURN_PATH_LOST      high   — value-returning path eliminated
  I-3  EXCEPTION_ADDED       medium — new exception type raised

Q.2 invariants (calls, branch_count):
  I-4  CALL_REMOVED          medium — a previously-made call is gone
  I-4  CALL_ADDED            medium — a new call appeared
  I-5  COMPLEXITY_SPIKE      medium — branch count grew by >= threshold

Q.3 invariants (side effects):
  I-6  SIDE_EFFECT_ADDED     high   — new subprocess/network/file_write effect
                              medium — new file_read or global_write effect
  I-6  SIDE_EFFECT_REMOVED   medium — a side effect category disappeared

Side-effect categories: subprocess, network, file_write, file_read, global_write.
HIGH = subprocess | network | file_write (contract-breaking in most contexts).
MEDIUM = file_read | global_write (informational, less risky).

Research note (2026-06-12):
  mccabe 0.7.0 is the only installed library that computes cyclomatic
  complexity, but it uses class-qualified keys ("ClassName.method") which
  don't align with our node.name-keyed snapshots and requires its own
  visitor framework.  We use stdlib ast for consistency and to avoid
  collisions when two classes have identically-named methods.

Only Python is supported (Q.1–Q.3).  Other languages return
skipped_reason="unsupported_language".
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LogicSnapshot:
    name: str
    param_names: List[str]
    required_param_count: int
    has_value_return: bool
    raised_exceptions: Set[str]
    line_start: int
    line_end: int
    # Q.2 additions (default values for backward compatibility)
    called_functions: Set[str] = field(default_factory=set)
    branch_count: int = 0
    # Q.3 additions
    side_effects: FrozenSet[str] = field(default_factory=frozenset)


@dataclass
class LogicViolation:
    function_name: str
    type: str
    severity: str  # "high" | "medium"
    detail: str
    before_value: object
    after_value: object


@dataclass
class LogicGuardResult:
    violations: List[LogicViolation] = field(default_factory=list)
    checked_functions: int = 0
    skipped_reason: str = ""


# ---------------------------------------------------------------------------
# Internal constants (Q.2)
# ---------------------------------------------------------------------------

_BRANCH_NODES = (
    ast.If, ast.BoolOp, ast.For, ast.AsyncFor,
    ast.While, ast.ExceptHandler, ast.IfExp,
)

_COMPLEXITY_SPIKE_THRESHOLD = 5

# ---------------------------------------------------------------------------
# Internal constants (Q.3 — side-effect detection)
# ---------------------------------------------------------------------------

_SUBPROCESS_CALLS = frozenset({
    "subprocess.run", "subprocess.call", "subprocess.Popen",
    "subprocess.check_output", "subprocess.check_call",
    "subprocess.getoutput", "subprocess.getstatusoutput",
    "os.system", "os.popen",
})

_NETWORK_CALLS = frozenset({
    "requests.get", "requests.post", "requests.put", "requests.delete",
    "requests.patch", "requests.head", "requests.request", "requests.Session",
    "socket.connect", "socket.create_connection",
    "httpx.get", "httpx.post", "httpx.Client", "httpx.AsyncClient",
    "aiohttp.ClientSession", "urllib.urlopen",
})

_FILE_WRITE_CALLS = frozenset({
    "shutil.copy", "shutil.copy2", "shutil.copyfile",
    "shutil.move", "shutil.rmtree",
    "os.remove", "os.unlink", "os.rename",
    "os.makedirs", "os.mkdir", "os.rmdir",
})

# Attribute-only names that imply file I/O (e.g. path.write_text(...))
_WRITE_ATTR_NAMES = frozenset({"write_text", "write_bytes"})
_READ_ATTR_NAMES = frozenset({"read_text", "read_bytes"})

# These categories force HIGH severity on SIDE_EFFECT_ADDED
_HIGH_SIDE_EFFECTS = frozenset({"subprocess", "network", "file_write"})


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _direct_walk(node: ast.AST):
    """Yield all nodes reachable from *node* without crossing nested function/class scopes."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            pass  # skip nested scopes
        else:
            yield from _direct_walk(child)


def _has_value_return(func_node: ast.AST) -> bool:
    for node in _direct_walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            return True
    return False


def _raised_exceptions(func_node: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in _direct_walk(func_node):
        if not (isinstance(node, ast.Raise) and node.exc is not None):
            continue
        exc = node.exc
        if isinstance(exc, ast.Call):
            func = exc.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        elif isinstance(exc, ast.Name):
            names.add(exc.id)
        elif isinstance(exc, ast.Attribute):
            names.add(exc.attr)
    return names


def _extract_call_name(func_expr: ast.expr) -> Optional[str]:
    """Return a human-readable name for the called function expression, or None."""
    if isinstance(func_expr, ast.Name):
        return func_expr.id
    if isinstance(func_expr, ast.Attribute):
        if isinstance(func_expr.value, ast.Name):
            return f"{func_expr.value.id}.{func_expr.attr}"
        return func_expr.attr
    return None


def _called_functions(func_node: ast.AST) -> Set[str]:
    """Return names of all functions/methods called directly in this scope."""
    calls: Set[str] = set()
    for node in _direct_walk(func_node):
        if isinstance(node, ast.Call):
            name = _extract_call_name(node.func)
            if name:
                calls.add(name)
    return calls


def _branch_count(func_node: ast.AST) -> int:
    """Count decision-producing nodes (McCabe algorithm, no nested scopes)."""
    count = 0
    for node in _direct_walk(func_node):
        if isinstance(node, _BRANCH_NODES):
            count += 1
    return count


def _is_write_open(call_node: ast.Call) -> bool:
    """Return True if open() is called with a write/append/exclusive mode."""
    # Check 2nd positional arg
    if len(call_node.args) >= 2:
        m = call_node.args[1]
        if isinstance(m, ast.Constant) and isinstance(m.value, str):
            return any(c in m.value for c in ("w", "a", "x"))
    # Check keyword arg 'mode'
    for kw in call_node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return any(c in kw.value.value for c in ("w", "a", "x"))
    return False  # unknown mode → treat as read


def _classify_call_side_effect(name: str, call_node: ast.Call) -> Optional[str]:
    """Map a resolved call name to a side-effect category, or None."""
    if name in _SUBPROCESS_CALLS:
        return "subprocess"
    if name.startswith("os.exec") or name.startswith("os.spawn"):
        return "subprocess"
    if name in _NETWORK_CALLS:
        return "network"
    if name in _FILE_WRITE_CALLS:
        return "file_write"
    if name == "open":
        return "file_write" if _is_write_open(call_node) else "file_read"
    # Attribute suffix patterns (for e.g. path.write_text, path.read_bytes)
    attr = name.rsplit(".", 1)[-1] if "." in name else name
    if attr in _WRITE_ATTR_NAMES:
        return "file_write"
    if attr in _READ_ATTR_NAMES:
        return "file_read"
    return None


def _side_effects(func_node: ast.AST) -> FrozenSet[str]:
    """Detect I/O and mutation side-effect categories in a function scope."""
    effects: Set[str] = set()

    for node in _direct_walk(func_node):
        # global_write: presence of any `global` statement
        if isinstance(node, ast.Global):
            effects.add("global_write")
            continue
        if not isinstance(node, ast.Call):
            continue
        name = _extract_call_name(node.func)
        if name is None:
            continue
        category = _classify_call_side_effect(name, node)
        if category:
            effects.add(category)

    return frozenset(effects)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class LogicGuardExtractor:
    @staticmethod
    def extract_python(source: str) -> Dict[str, LogicSnapshot]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {}

        snapshots: Dict[str, LogicSnapshot] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            all_positional = args.posonlyargs + args.args
            required_positional = len(all_positional) - len(args.defaults)
            kw_required = sum(1 for d in args.kw_defaults if d is None)
            required_count = max(0, required_positional) + kw_required

            param_names = [
                a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)
            ]

            snapshots[node.name] = LogicSnapshot(
                name=node.name,
                param_names=param_names,
                required_param_count=required_count,
                has_value_return=_has_value_return(node),
                raised_exceptions=_raised_exceptions(node),
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                called_functions=_called_functions(node),
                branch_count=_branch_count(node),
                side_effects=_side_effects(node),
            )
        return snapshots


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _compare_snapshots(
    b: LogicSnapshot, a: LogicSnapshot, violations: List[LogicViolation]
) -> None:
    # I-1: required param count grew — callers without the new arg break
    if a.required_param_count > b.required_param_count:
        violations.append(LogicViolation(
            function_name=b.name,
            type="REQUIRED_PARAM_ADDED",
            severity="high",
            detail=(
                f"required params grew from {b.required_param_count} "
                f"to {a.required_param_count}"
            ),
            before_value=b.required_param_count,
            after_value=a.required_param_count,
        ))

    # I-1: a param name disappeared — callers using keyword syntax break
    removed = [p for p in b.param_names if p not in a.param_names]
    if removed:
        violations.append(LogicViolation(
            function_name=b.name,
            type="PARAM_REMOVED",
            severity="high",
            detail=f"params removed: {', '.join(removed)}",
            before_value=list(b.param_names),
            after_value=list(a.param_names),
        ))

    # I-2: value-returning path eliminated
    if b.has_value_return and not a.has_value_return:
        violations.append(LogicViolation(
            function_name=b.name,
            type="RETURN_PATH_LOST",
            severity="high",
            detail="function had a value-returning path; after patch it does not",
            before_value=True,
            after_value=False,
        ))

    # I-3: new exception types introduced
    new_exc = sorted(a.raised_exceptions - b.raised_exceptions)
    if new_exc:
        violations.append(LogicViolation(
            function_name=b.name,
            type="EXCEPTION_ADDED",
            severity="medium",
            detail=f"new exceptions: {', '.join(new_exc)}",
            before_value=sorted(b.raised_exceptions),
            after_value=sorted(a.raised_exceptions),
        ))

    # I-4: call surface changed
    removed_calls = sorted(b.called_functions - a.called_functions)
    added_calls = sorted(a.called_functions - b.called_functions)
    if removed_calls:
        violations.append(LogicViolation(
            function_name=b.name,
            type="CALL_REMOVED",
            severity="medium",
            detail=f"calls removed: {', '.join(removed_calls)}",
            before_value=sorted(b.called_functions),
            after_value=sorted(a.called_functions),
        ))
    if added_calls:
        violations.append(LogicViolation(
            function_name=b.name,
            type="CALL_ADDED",
            severity="medium",
            detail=f"calls added: {', '.join(added_calls)}",
            before_value=sorted(b.called_functions),
            after_value=sorted(a.called_functions),
        ))

    # I-5: complexity spike
    bc_delta = a.branch_count - b.branch_count
    if bc_delta >= _COMPLEXITY_SPIKE_THRESHOLD:
        violations.append(LogicViolation(
            function_name=b.name,
            type="COMPLEXITY_SPIKE",
            severity="medium",
            detail=(
                f"branch count grew from {b.branch_count} to {a.branch_count} "
                f"(delta={bc_delta:+d})"
            ),
            before_value=b.branch_count,
            after_value=a.branch_count,
        ))

    # I-6: side effects added/removed
    added_fx = sorted(a.side_effects - b.side_effects)
    removed_fx = sorted(b.side_effects - a.side_effects)
    for fx in added_fx:
        sev = "high" if fx in _HIGH_SIDE_EFFECTS else "medium"
        violations.append(LogicViolation(
            function_name=b.name,
            type="SIDE_EFFECT_ADDED",
            severity=sev,
            detail=f"new side effect: {fx}",
            before_value=sorted(b.side_effects),
            after_value=sorted(a.side_effects),
        ))
    for fx in removed_fx:
        violations.append(LogicViolation(
            function_name=b.name,
            type="SIDE_EFFECT_REMOVED",
            severity="medium",
            detail=f"side effect removed: {fx}",
            before_value=sorted(b.side_effects),
            after_value=sorted(a.side_effects),
        ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class LogicGuard:
    @staticmethod
    def check(before: str, after: str, language: str = "") -> LogicGuardResult:
        lang = (language or "").strip().lower()
        if lang not in ("python", "py"):
            return LogicGuardResult(skipped_reason="unsupported_language")

        before_snaps = LogicGuardExtractor.extract_python(before)
        after_snaps = LogicGuardExtractor.extract_python(after)

        common = set(before_snaps) & set(after_snaps)
        violations: List[LogicViolation] = []
        for name in sorted(common):
            _compare_snapshots(before_snaps[name], after_snaps[name], violations)

        return LogicGuardResult(violations=violations, checked_functions=len(common))
