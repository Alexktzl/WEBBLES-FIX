"""
O.14 — анти-регрессионный валидатор символов.

Покрывает:
1) `analysis.symbol_regression.check_symbol_regression`: python (AST) +
   regex-фоллбэк для rust/js/ts/go/java/kotlin/c/cpp/cs.
2) `ValidateStage._save_patch_snapshot` ставит metadata["symbol_regression"]
   при пропадании def/class из файла после патча.
3) `DecideStage._dispatch_after_success` при наличии флага выдаёт
   NEEDS_REVIEW (с откатом файла из снапшота) вместо ACCEPT.

Реальный кейс: 2026-06-02, прогон на test_python — движок «выпрямил»
`auth.py`, потеряв `def hash_password`. Аудит этого не видел, потому что
синтаксис файла остался валидным, и патч ушёл в ACCEPT.

Запуск: python3 tests/test_phase_symbol_regression.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# Заглушка для tomlkit (core.stages.__init__ тянет toml_patcher).
try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from analysis.symbol_regression import check_symbol_regression

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------------
# 1. check_symbol_regression — Python AST
# ---------------------------------------------------------------------

_PY_BEFORE = (
    "import hashlib\n"
    "\n"
    "def hash_password(pwd):\n"
    "    return hashlib.md5(pwd.encode()).hexdigest()\n"
    "\n"
    "def login(user, pwd):\n"
    "    return user, pwd\n"
)


def test_python_no_regression():
    after = _PY_BEFORE + "\n# trivial comment added\n"
    r = check_symbol_regression(_PY_BEFORE, after, "python")
    check("py: no_regression.ok", r["ok"] is True)
    check("py: no_regression.missing_defs",
          r["missing_defs"] == [])
    check("py: no_regression.before_defs_has_hash_password",
          "hash_password" in r["before_defs"])


def test_python_function_lost():
    after = (
        "import hashlib\n"
        "\n"
        "def login(user, pwd):\n"
        "    return hashlib.md5(pwd.encode()).hexdigest()\n"
    )
    r = check_symbol_regression(_PY_BEFORE, after, "python")
    check("py: function_lost.not_ok", r["ok"] is False)
    check("py: function_lost.missing has hash_password",
          "hash_password" in r["missing_defs"])
    check("py: function_lost.classes_unchanged",
          r["missing_classes"] == [])


def test_python_overload_signatures_lost():
    """2026-06-24 (agronholm/anyio, реальный кейс): "retry с full-file"
    дописал @overload-декораторы БЕЗ их сигнатур, потеряв 4 из 5 перегрузок
    connect_tcp. Имя как def НЕ пропадает (реализация осталась) — set-based
    missing_defs этого не видит, нужен count-based missing_overloads."""
    before = (
        "from typing import overload\n"
        "\n"
        "@overload\n"
        "def connect_tcp(host: str, *, tls: bool) -> int: ...\n"
        "\n"
        "@overload\n"
        "def connect_tcp(host: str) -> str: ...\n"
        "\n"
        "def connect_tcp(host, *, tls=False):\n"
        "    return host\n"
    )
    after = (
        "from typing import overload\n"
        "\n"
        "@overload\n"
        "\n"
        "\n"
        "@overload\n"
        "\n"
        "\n"
        "def connect_tcp(host, *, tls=False):\n"
        "    return host\n"
    )
    r = check_symbol_regression(before, after, "python")
    check("py: overload_lost.not_ok", r["ok"] is False)
    check("py: overload_lost.missing_overloads has connect_tcp",
          any(o["name"] == "connect_tcp" for o in r["missing_overloads"]))
    overload_entry = next(o for o in r["missing_overloads"] if o["name"] == "connect_tcp")
    check("py: overload_lost.before_count == 3", overload_entry["before_count"] == 3)
    check("py: overload_lost.after_count == 1", overload_entry["after_count"] == 1)
    # def как таковой НЕ потерян (это и есть суть находки — set-based
    # missing_defs не видит проблему).
    check("py: overload_lost.missing_defs_empty", r["missing_defs"] == [])


def test_python_overload_count_unchanged_is_ok():
    """Контроль: если количество @overload для имени не уменьшилось —
    не должно быть false positive."""
    before = (
        "from typing import overload\n"
        "\n"
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "@overload\n"
        "def f(x: str) -> str: ...\n"
        "def f(x):\n"
        "    return x\n"
    )
    after = before + "\nz = 1\n"
    r = check_symbol_regression(before, after, "python")
    check("py: overload_unchanged.ok", r["ok"] is True)
    check("py: overload_unchanged.no_missing_overloads", r["missing_overloads"] == [])


def test_python_class_lost():
    before = (
        "class Auth:\n"
        "    def login(self): pass\n"
        "\n"
        "class Profile:\n"
        "    def name(self): return 'x'\n"
    )
    after = (
        "class Auth:\n"
        "    def login(self): pass\n"
    )
    r = check_symbol_regression(before, after, "py")
    check("py: class_lost.not_ok", r["ok"] is False)
    check("py: class_lost.missing_classes has Profile",
          "Profile" in r["missing_classes"])
    check("py: class_lost.missing_defs has name",
          "name" in r["missing_defs"])


def test_python_unparseable_before_ignored():
    """Если before не парсится — нечего регрессировать (empty_before)."""
    before = "def foo(:\n   # broken\n"
    after = "def bar(): pass\n"
    r = check_symbol_regression(before, after, "python")
    check("py: unparseable_before.ok", r["ok"] is True)


def test_python_nested_def_tracked():
    before = (
        "class Outer:\n"
        "    def method(self):\n"
        "        def helper(): return 1\n"
        "        return helper()\n"
    )
    after = (
        "class Outer:\n"
        "    def method(self):\n"
        "        return 1\n"
    )
    r = check_symbol_regression(before, after, "python")
    check("py: nested.not_ok", r["ok"] is False)
    check("py: nested.helper_missing",
          "helper" in r["missing_defs"])


# ---------------------------------------------------------------------
# 2. Regex-фоллбэк — Rust
# ---------------------------------------------------------------------

def test_rust_no_regression():
    before = (
        "struct Player { name: String }\n"
        "fn play(p: Player) {}\n"
    )
    r = check_symbol_regression(before, before, "rust")
    check("rs: no_regression.ok", r["ok"] is True)
    check("rs: before has fn play",
          "play" in r["before_defs"])
    check("rs: before has struct Player",
          "Player" in r["before_classes"])


def test_rust_fn_lost():
    before = (
        "fn helper() -> u32 { 42 }\n"
        "fn main() { helper(); }\n"
    )
    after = "fn main() { 42; }\n"
    r = check_symbol_regression(before, after, "rs")
    check("rs: fn_lost.not_ok", r["ok"] is False)
    check("rs: fn_lost.missing has helper",
          "helper" in r["missing_defs"])


# ---------------------------------------------------------------------
# 3. Regex-фоллбэк — JS/TS
# ---------------------------------------------------------------------

def test_js_class_lost():
    before = (
        "class Handler {\n"
        "    handle() { return 1; }\n"
        "}\n"
        "function process(h) { return h.handle(); }\n"
    )
    after = "function process() { return 1; }\n"
    r = check_symbol_regression(before, after, "javascript")
    check("js: class_lost.not_ok", r["ok"] is False)
    check("js: class_lost.missing_classes has Handler",
          "Handler" in r["missing_classes"])


def test_ts_function_lost():
    before = (
        "interface User { name: string }\n"
        "function getUser(): User { return { name: 'a' } }\n"
        "function deleteUser(u: User) {}\n"
    )
    after = (
        "interface User { name: string }\n"
        "function getUser(): User { return { name: 'a' } }\n"
    )
    r = check_symbol_regression(before, after, "typescript")
    check("ts: fn_lost.not_ok", r["ok"] is False)
    check("ts: fn_lost.missing has deleteUser",
          "deleteUser" in r["missing_defs"])


# ---------------------------------------------------------------------
# 4. Regex-фоллбэк — Go
# ---------------------------------------------------------------------

def test_go_func_lost():
    before = (
        "package main\n"
        "type Server struct { port int }\n"
        "func (s *Server) Start() {}\n"
        "func main() {}\n"
    )
    after = (
        "package main\n"
        "type Server struct { port int }\n"
        "func main() {}\n"
    )
    r = check_symbol_regression(before, after, "go")
    check("go: func_lost.not_ok", r["ok"] is False)
    check("go: func_lost.missing has Start",
          "Start" in r["missing_defs"])


# ---------------------------------------------------------------------
# 5. Java/Kotlin
# ---------------------------------------------------------------------

def test_java_class_lost():
    before = (
        "public class A { public void foo() {} }\n"
        "public class B { public void bar() {} }\n"
    )
    after = "public class A { public void foo() {} }\n"
    r = check_symbol_regression(before, after, "java")
    check("java: class_lost.not_ok", r["ok"] is False)
    check("java: class_lost.missing has B",
          "B" in r["missing_classes"])


def test_kotlin_fun_lost():
    before = (
        "fun greet() {}\n"
        "fun farewell() {}\n"
    )
    after = "fun greet() {}\n"
    r = check_symbol_regression(before, after, "kotlin")
    check("kt: fun_lost.not_ok", r["ok"] is False)
    check("kt: fun_lost.missing has farewell",
          "farewell" in r["missing_defs"])


# ---------------------------------------------------------------------
# 6. Безопасные edge-кейсы (noop)
# ---------------------------------------------------------------------

def test_empty_before_is_noop():
    r = check_symbol_regression("", "def x(): pass\n", "python")
    check("edge: empty_before.ok", r["ok"] is True)


def test_unknown_language_is_noop():
    r = check_symbol_regression("def foo(): pass\n", "", "haskell")
    check("edge: unknown_lang.ok", r["ok"] is True)


def test_non_string_input_is_noop():
    r = check_symbol_regression(None, None, "python")  # type: ignore[arg-type]
    check("edge: non_string.ok", r["ok"] is True)


# ---------------------------------------------------------------------
# 7. Интеграция: ValidateStage._save_patch_snapshot ставит флаг
# ---------------------------------------------------------------------

class _FakeContext:
    """Минимальный context, имитирующий PipelineContext: language, metadata,
    selected_error, working_path, и `update` возвращающий self с новыми meta."""

    def __init__(self, language, metadata, selected_error, work_path):
        self.language = language
        self.metadata = metadata
        self.selected_error = selected_error
        self.working_path = work_path
        self.project_path = work_path

    def update(self, metadata=None, **_kw):
        if metadata is not None:
            self.metadata = metadata
        return self


def _make_validate_stage_for_snapshot():
    """Поднимаем ValidateStage минимально для вызова _save_patch_snapshot."""
    from core.stages.validate_stage import ValidateStage
    # __init__ требует пять параметров — кладём MagicMock'ов.
    vs = ValidateStage(
        compiler=MagicMock(),
        linter=MagicMock(),
        security=MagicMock(),
        analyzer=MagicMock(),
        degradation=MagicMock(),
    )
    return vs


def test_validate_stage_sets_symbol_regression_flag():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # backup-файл — original_content (before патча)
        backup = root / "auth_backup.py"
        backup.write_text(_PY_BEFORE, encoding="utf-8")
        # рабочий файл — patched_content (после патча, потерял hash_password)
        target = root / "auth.py"
        target.write_text(
            "import hashlib\n"
            "def login(user, pwd):\n"
            "    return hashlib.md5(pwd.encode()).hexdigest()\n",
            encoding="utf-8",
        )
        ctx = _FakeContext(
            language="python",
            metadata={"last_backup_path": str(backup)},
            selected_error={
                "file": "auth.py", "line": 3,
                "code": "E001", "message": "x",
            },
            work_path=root,
        )
        vs = _make_validate_stage_for_snapshot()
        ctx2 = vs._save_patch_snapshot(ctx)
        sym = ctx2.metadata.get("symbol_regression")
        check("vs: snapshot symbol_regression set", isinstance(sym, dict))
        if isinstance(sym, dict):
            check("vs: snapshot file == auth.py",
                  sym.get("file") == "auth.py")
            check("vs: snapshot missing hash_password",
                  "hash_password" in (sym.get("missing_defs") or []))


def test_validate_stage_no_flag_when_no_regression():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        backup = root / "ok_backup.py"
        backup.write_text(_PY_BEFORE, encoding="utf-8")
        target = root / "auth.py"
        # после патча всё на месте + добавили helper
        target.write_text(
            _PY_BEFORE + "\ndef helper(): return 42\n",
            encoding="utf-8",
        )
        ctx = _FakeContext(
            language="python",
            metadata={
                "last_backup_path": str(backup),
                # симулируем артефакт прошлой итерации — должен быть стёрт
                "symbol_regression": {"file": "old", "missing_defs": ["x"]},
            },
            selected_error={
                "file": "auth.py", "line": 1,
                "code": "E001", "message": "x",
            },
            work_path=root,
        )
        vs = _make_validate_stage_for_snapshot()
        ctx2 = vs._save_patch_snapshot(ctx)
        check("vs: no_regression flag cleared",
              "symbol_regression" not in ctx2.metadata)


# ---------------------------------------------------------------------
# 8. Интеграция: DecideStage._dispatch_after_success
# ---------------------------------------------------------------------

class _DecideCtx:
    """Подделка PipelineContext с минимальным API, которое использует
    `_dispatch_after_success` при decision=NEEDS_REVIEW.
    Цель — поймать `add_state_to_history(State.NEEDS_REVIEW)` без подъёма
    реального движка."""

    def __init__(self, metadata, error):
        self.metadata = metadata
        self.selected_error = error
        self.history_states = []
        self.accepted = []
        self.rejected = []
        self.processed = []
        self.iterations = 0
        self.working_path = None
        self.project_path = None
        # Реальный PipelineContext всегда имеет validation_results (default {}) —
        # decide_stage._build_attempt_data() читает его без getattr-защиты.
        self.validation_results = {}

    def update(self, metadata=None, **_kw):
        if metadata is not None:
            self.metadata = metadata
        return self

    def add_accepted_patch(self, p):
        self.accepted.append(p)
        return self

    def add_rejected_patch(self, p):
        self.rejected.append(p)
        return self

    def record_processed_error(self, sig):
        self.processed.append(sig)
        return self

    def increment_iteration(self):
        self.iterations += 1
        return self

    def add_state_to_history(self, state):
        self.history_states.append(state)
        return self

    def remove_patch_snapshot(self, file_name):
        snaps = list(self.metadata.get("patch_snapshots", []))
        self.metadata = dict(self.metadata)
        self.metadata["patch_snapshots"] = [
            s for s in snaps if s.get("file") != file_name
        ]
        return self

    def set_errors(self, errs):
        return self


def _build_decide_stage():
    from core.stages.decide_stage import DecideStage
    ds = DecideStage(quality_evaluator=MagicMock(), analyzer=None)
    return ds


def test_decide_returns_needs_review_on_symbol_regression():
    from core.state_machine import State
    ds = _build_decide_stage()
    error = {"file": "auth.py", "line": 3, "code": "E001", "message": "x"}
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "symbol_regression": {
                "file": "auth.py",
                "missing_defs": ["hash_password"],
                "missing_classes": [],
            },
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(ctx, error, "sig-1", reason="test")
    # last_decision == NEEDS_REVIEW
    check("ds: last_decision = NEEDS_REVIEW",
          out.metadata.get("last_decision") == "NEEDS_REVIEW")
    # история состояний кончается на NEEDS_REVIEW
    check("ds: history.last = State.NEEDS_REVIEW",
          out.history_states and out.history_states[-1] == State.NEEDS_REVIEW)
    # accepted_patches пустой (мы НЕ ACCEPT-нули)
    check("ds: not accepted", out.accepted == [])


def test_decide_accepts_when_no_symbol_regression():
    from core.state_machine import State
    ds = _build_decide_stage()
    error = {"file": "auth.py", "line": 3, "code": "E001", "message": "x"}
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            # symbol_regression отсутствует → обычный ACCEPT-путь
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(ctx, error, "sig-2", reason="test")
    check("ds(ok): last_decision = ACCEPT",
          out.metadata.get("last_decision") == "ACCEPT")
    check("ds(ok): history.last = State.NEXT_ERROR",
          out.history_states and out.history_states[-1] == State.NEXT_ERROR)
    check("ds(ok): accepted has 1", len(out.accepted) == 1)


def test_decide_empty_symbol_regression_does_not_trigger():
    """Если флаг есть, но missing-списки пустые — НЕ блокируем ACCEPT."""
    from core.state_machine import State
    ds = _build_decide_stage()
    error = {"file": "auth.py", "line": 3, "code": "E001", "message": "x"}
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "symbol_regression": {
                "file": "auth.py",
                "missing_defs": [],
                "missing_classes": [],
            },
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(ctx, error, "sig-3", reason="test")
    check("ds(empty_reg): last_decision = ACCEPT",
          out.metadata.get("last_decision") == "ACCEPT")


def test_decide_returns_needs_review_on_missing_imports():
    """2026-06-23: missing_imports добавлен в check_symbol_regression
    (control series, malinkang/toggl2notion — F401 в RuffAutoFixStage терял
    имена), но _dispatch_after_success раньше проверял только
    missing_defs/missing_classes — пропавшие импорты НЕ блокировали ACCEPT."""
    from core.state_machine import State
    ds = _build_decide_stage()
    error = {"file": "notion_helper.py", "line": 3, "code": "import-not-found", "message": "x"}
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "symbol_regression": {
                "file": "notion_helper.py",
                "missing_defs": [],
                "missing_classes": [],
                "missing_imports": ["TAG_ICON_URL", "USER_ICON_URL"],
            },
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(ctx, error, "sig-4", reason="test")
    check("ds(missing_imports): last_decision = NEEDS_REVIEW",
          out.metadata.get("last_decision") == "NEEDS_REVIEW")
    check("ds(missing_imports): not accepted", out.accepted == [])


def test_decide_returns_needs_review_on_missing_overloads():
    """2026-06-24 (control series на 10 проектах, agronholm/anyio): "retry
    с full-file" дописал @overload-декораторы БЕЗ их сигнатур, потеряв 4 из
    5 перегрузок connect_tcp — имя как def НЕ пропадает (missing_defs этого
    не видит), но missing_overloads должен заблокировать ACCEPT."""
    from core.state_machine import State
    ds = _build_decide_stage()
    error = {"file": "_sockets.py", "line": 64, "code": "attr-defined", "message": "x"}
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "symbol_regression": {
                "file": "_sockets.py",
                "missing_defs": [],
                "missing_classes": [],
                "missing_imports": [],
                "missing_overloads": [
                    {"name": "connect_tcp", "before_count": 5, "after_count": 1},
                ],
            },
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(ctx, error, "sig-overload", reason="test")
    check("ds(missing_overloads): last_decision = NEEDS_REVIEW",
          out.metadata.get("last_decision") == "NEEDS_REVIEW")
    check("ds(missing_overloads): not accepted", out.accepted == [])


def test_decide_returns_needs_review_on_type_erosion():
    """2026-06-23 (Skyscanner/pycfmodel): Any/cast(Any) для фиксируемого
    mypy-кода — НЕ ACCEPT, должно уйти в NEEDS_REVIEW (O.18)."""
    from core.state_machine import State
    ds = _build_decide_stage()
    error = {"file": "resolver.py", "line": 69, "code": "assignment", "message": "x"}
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "type_erosion": {"file": "resolver.py", "markers": ["any_type"]},
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(ctx, error, "sig-5", reason="test")
    check("ds(type_erosion): last_decision = NEEDS_REVIEW",
          out.metadata.get("last_decision") == "NEEDS_REVIEW")
    check("ds(type_erosion): not accepted", out.accepted == [])


def test_decide_accepts_when_type_erosion_for_allowed_code_absent():
    """type_erosion ключ отсутствует (например, код в allowlist — guard
    его не выставил вовсе) → обычный ACCEPT."""
    from core.state_machine import State
    ds = _build_decide_stage()
    error = {"file": "x.py", "line": 1, "code": "import-not-found", "message": "x"}
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(ctx, error, "sig-6", reason="test")
    check("ds(no_erosion): last_decision = ACCEPT",
          out.metadata.get("last_decision") == "ACCEPT")


if __name__ == "__main__":
    print("O.14 — анти-регрессионный валидатор символов:")
    test_python_no_regression()
    test_python_function_lost()
    test_python_overload_signatures_lost()
    test_python_overload_count_unchanged_is_ok()
    test_python_class_lost()
    test_python_unparseable_before_ignored()
    test_python_nested_def_tracked()
    test_rust_no_regression()
    test_rust_fn_lost()
    test_js_class_lost()
    test_ts_function_lost()
    test_go_func_lost()
    test_java_class_lost()
    test_kotlin_fun_lost()
    test_empty_before_is_noop()
    test_unknown_language_is_noop()
    test_non_string_input_is_noop()
    test_validate_stage_sets_symbol_regression_flag()
    test_validate_stage_no_flag_when_no_regression()
    test_decide_returns_needs_review_on_symbol_regression()
    test_decide_accepts_when_no_symbol_regression()
    test_decide_empty_symbol_regression_does_not_trigger()
    test_decide_returns_needs_review_on_missing_imports()
    test_decide_returns_needs_review_on_missing_overloads()
    test_decide_returns_needs_review_on_type_erosion()
    test_decide_accepts_when_type_erosion_for_allowed_code_absent()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nSymbol regression: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
