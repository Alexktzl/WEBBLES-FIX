"""
Регресс-тесты на находки независимого Codex-ревью 2026-07-02 (агентная
схема: находки — codex:rescue, реализация — deep-reasoner, тесты дописаны
оркестратором после обрыва агента по session-limit).

Codex-1: audit_run игнорировал metadata["_rollback_failed_files"] — файл с
  проваленным REJECT-откатом проходил в passed_files и доставлялся.
Codex-2: dep-recovery регистрировал манифест в accepted_patches без
  additive-проверки — не-аддитивная правка могла доехать до оригинала.
Codex-3: при regex_fallback (before не парсится) overload-проверка была
  отключена полностью — потеря @overload проходила ok=True.
Codex-5 (+находка deep-reasoner): bulk-skip (TESP-cap) писал processed_errors
  под ключом БЕЗ ::L{line}, селектор его не читал — bulk-skip был no-op.

Запуск: python tests/test_phase_codex_review_fixes.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from analysis.symbol_regression import check_symbol_regression  # noqa: E402
from core.pipeline_context import PipelineContext  # noqa: E402
from core.pipeline_engine import PipelineEngine  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402
from core.utils import process_key as _process_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_for_test_codex_fixes", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AuditManager


# --- Codex-1: файл с проваленным откатом принудительно в failed_segments ---
def test_rollback_failed_file_forced_to_failed_segments():
    AuditManager = _load_audit()
    fname = "src/app.py"
    content = "def f():\n    pass\n"
    project_dir = Path(tempfile.mkdtemp())
    working_dir = Path(tempfile.mkdtemp())
    for d in (project_dir, working_dir):
        (d / "src").mkdir(parents=True)
        # в sandbox файл ИЗМЕНЁН (откат провалился), но символы целы и
        # lint-ошибок нет — раньше такой файл уходил в passed_files
        (d / fname).write_text(
            content if d is project_dir else content.replace("pass", "return 1"),
            encoding="utf-8",
        )

    analyzer = MagicMock()
    analyzer.analyze = MagicMock(return_value=[])
    holder = {"ctx": None}
    am = AuditManager(
        project_path=project_dir, language="python", analyzer=analyzer,
        classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
        context_getter=lambda: holder["ctx"],
    )
    ctx = MagicMock()
    ctx.metadata = {
        "file_error_signatures_before": {fname: set()},
        "patch_snapshots": [],
        "_rollback_failed_files": [fname],
    }
    ctx.working_path = working_dir
    holder["ctx"] = ctx

    res = am.audit_run(ctx)
    assert res["audit_ok"] is False, (
        f"файл с проваленным откатом не может пройти аудит: {res}"
    )
    assert fname in res["failed_segments"], res
    assert fname not in res["passed_files"], res
    assert fname in res.get("rollback_failed", []), res


# --- Codex-2: additive-guard python-манифеста ---
def _dep_engine(project: Path, sandbox: Path) -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    eng.language = "python"
    eng.config = {"pipeline": {}}
    eng.reporter = None
    eng.context = PipelineContext(
        project_path=project, language="python", working_path=sandbox,
    )
    return eng


def test_dep_recovery_non_additive_manifest_not_registered():
    import unittest.mock as mock
    with tempfile.TemporaryDirectory() as d:
        project = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        project.mkdir()
        sandbox.mkdir()
        original = "requests>=2.0\nflask==1.0\n"
        (sandbox / "requirements.txt").write_text(original, encoding="utf-8")

        def _bad_recover(self, root, *, run_pip_check=False):
            # «инференс» ПЕРЕПИСАЛ манифест, удалив flask — не additive
            (Path(root) / "requirements.txt").write_text(
                "requests>=2.0\npytest>=8.0\n", encoding="utf-8",
            )
            return SimpleNamespace(has_changes=True, missing={"pytest": ("pytest", ">=8.0")},
                                   explanations=[])

        eng = _dep_engine(project, sandbox)
        with mock.patch(
            "analysis.python_dependency_inference.PythonDependencyInference.recover",
            _bad_recover,
        ):
            eng._recover_dependencies_sandbox(sandbox)

        assert eng.context.accepted_patches == [], (
            "не-additive манифест не должен регистрироваться в accept-поток"
        )
        assert (sandbox / "requirements.txt").read_text(encoding="utf-8") == original, (
            "sandbox-манифест обязан быть восстановлен из снапшота"
        )


def test_dep_recovery_additive_manifest_registered():
    import unittest.mock as mock
    with tempfile.TemporaryDirectory() as d:
        project = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        project.mkdir()
        sandbox.mkdir()
        (sandbox / "requirements.txt").write_text("requests>=2.0\n", encoding="utf-8")

        def _good_recover(self, root, *, run_pip_check=False):
            (Path(root) / "requirements.txt").write_text(
                "requests>=2.0\npytest>=8.0\n", encoding="utf-8",
            )
            return SimpleNamespace(has_changes=True, missing={"pytest": ("pytest", ">=8.0")},
                                   explanations=[])

        eng = _dep_engine(project, sandbox)
        with mock.patch(
            "analysis.python_dependency_inference.PythonDependencyInference.recover",
            _good_recover,
        ):
            eng._recover_dependencies_sandbox(sandbox)
        assert len(eng.context.accepted_patches) == 1, eng.context.accepted_patches


# --- Codex-3: regex-fallback ловит потерю @overload ---
_BROKEN_OVERLOADS_BEFORE = (
    "from typing import overload\n\n"
    "@overload\n"
    "def f(x: int) -> int: ...\n"
    "@overload\n"
    "def f(x: str) -> str: ...\n"
    "def f(x):\n"
    "    return x\n"
    "def broken(:\n"  # E999 → regex_fallback
)


def test_regex_fallback_detects_overload_loss():
    after = (
        "from typing import overload\n\n"
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "def f(x):\n"
        "    return x\n"
        "def broken(z):\n"
        "    return z\n"
    )
    r = check_symbol_regression(_BROKEN_OVERLOADS_BEFORE, after, "python")
    assert r["regex_fallback"] is True, r
    assert r["ok"] is False and r["missing_overloads"], (
        f"потеря @overload в fallback-режиме обязана детектироваться: {r}"
    )


def test_regex_fallback_overloads_kept_ok():
    after = (
        "from typing import overload\n\n"
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "@overload\n"
        "def f(x: str) -> str: ...\n"
        "def f(x):\n"
        "    return x\n"
        "def broken(z):\n"
        "    return z\n"
    )
    r = check_symbol_regression(_BROKEN_OVERLOADS_BEFORE, after, "python")
    assert r["ok"] is True, f"перегрузки сохранены — ложного флага быть не должно: {r}"


# --- Codex-5: TESP bulk-skip пишет ключ, который ЧИТАЕТ селектор ---
def test_tesp_cap_writes_process_key_with_line():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        pending = [
            {"file": "a.py", "line": 10, "code": "E501", "message": "long"},
            {"file": "a.py", "line": 20, "code": "E501", "message": "long"},
        ]
        error = {"file": "a.py", "line": 10, "code": "E501", "message": "long"}
        ctx = PipelineContext(
            project_path=work, language="python", working_path=work,
            current_errors=tuple(pending),
            metadata={"_tesp_file_counts": {"a.py": 4}},  # следующий инкремент = 5 = cap
        )
        stage = DecideStage(quality_evaluator=None, analyzer=None)
        out = stage._cap_file_if_tesp_exceeded(ctx, error)

        proc = dict(out.processed_errors)
        for p in pending:
            pkey = _process_key(p)
            assert proc.get(pkey) == 3, (
                f"bulk-skip обязан писать process_key (с ::L{{line}}), "
                f"иначе селектор его не видит: ключи={list(proc)}"
            )
            assert "::L" in pkey  # sanity: ключ действительно с линией


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
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
    print(f"\nCodex review fixes: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
