"""
Решение Алекса 2026-07-02, вариант «в»: dependency recovery работает только
в sandbox, манифест доставляется обычным accept-потоком.

До фикса Controller._recover_dependencies писал requirements.txt/Cargo.toml
ПРЯМО в проект пользователя ДО создания sandbox, мимо accept-цикла —
вопреки правилу «в проекте пользователя только .webbles_backups/»
(контрольная серия 07-02: «# auto-added by Webbles Fix» появился в корне
клона tenacity).

Запуск: python tests/test_phase_dep_recovery_sandbox.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext  # noqa: E402
from core.pipeline_engine import PipelineEngine  # noqa: E402


def _engine(project: Path, sandbox: Path, config=None) -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    eng.language = "python"
    eng.config = config or {"pipeline": {}}
    eng.reporter = None
    eng.context = PipelineContext(
        project_path=project, language="python", working_path=sandbox,
    )
    return eng


def _fake_recover_writes(sandbox: Path):
    """Заглушка PythonDependencyInference.recover: пишет requirements.txt
    в переданный root (как настоящий) и возвращает результат с изменениями."""
    def _recover(self, root, *, run_pip_check=False):
        (Path(root) / "requirements.txt").write_text(
            "# auto-added by Webbles Fix (Python dep-inference)\npytest>=8.0.0\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            has_changes=True,
            missing={"pytest": ("pytest", ">=8.0.0")},
            explanations=["+ pytest (import pytest)"],
        )
    return _recover


# --- 1. Recovery пишет ТОЛЬКО в sandbox; проект пользователя не тронут ---
def test_recovery_writes_sandbox_only_and_registers_accept():
    with tempfile.TemporaryDirectory() as d:
        project = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        project.mkdir()
        sandbox.mkdir()
        (project / "app.py").write_text("import pytest\n", encoding="utf-8")
        (sandbox / "app.py").write_text("import pytest\n", encoding="utf-8")

        eng = _engine(project, sandbox)
        with mock.patch(
            "analysis.python_dependency_inference.PythonDependencyInference.recover",
            _fake_recover_writes(sandbox),
        ):
            eng._recover_dependencies_sandbox(sandbox)

        assert not (project / "requirements.txt").exists(), (
            "проект пользователя не должен трогаться до доставки"
        )
        assert (sandbox / "requirements.txt").exists()
        accepted = eng.context.accepted_patches
        assert len(accepted) == 1, accepted
        assert accepted[0]["error"]["file"] == "requirements.txt"
        assert accepted[0]["reason"] == "dependency_recovery_sandbox"


# --- 2. Доставка: зарегистрированный манифест копируется accept-потоком с бэкапом ---
def test_recovered_manifest_delivered_via_accept_flow():
    with tempfile.TemporaryDirectory() as d:
        project = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        project.mkdir()
        sandbox.mkdir()
        (sandbox / "requirements.txt").write_text("pytest>=8.0.0\n", encoding="utf-8")

        eng = _engine(project, sandbox)
        eng.context = eng.context.add_accepted_patch({
            "error": {"file": "requirements.txt", "line": 0,
                      "code": "dependency_recovery", "message": "m"},
            "reason": "dependency_recovery_sandbox",
        })
        audit_result = {"passed_files": [], "failed_segments": {}}
        eng._copy_passed_files_to_original(sandbox, audit_result)

        assert (project / "requirements.txt").exists(), (
            "манифест обязан доехать до оригинала через accepted-fallback"
        )
        assert (project / "requirements.txt").read_text(encoding="utf-8") == "pytest>=8.0.0\n"


# --- 3. Флаг pipeline.dependency_recovery=False полностью выключает recovery ---
def test_recovery_disabled_by_flag():
    with tempfile.TemporaryDirectory() as d:
        project = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        project.mkdir()
        sandbox.mkdir()

        eng = _engine(project, sandbox,
                      config={"pipeline": {"dependency_recovery": False}})
        with mock.patch(
            "analysis.python_dependency_inference.PythonDependencyInference.recover",
        ) as m:
            eng._recover_dependencies_sandbox(sandbox)
        assert m.call_count == 0
        assert not (sandbox / "requirements.txt").exists()


# --- 4. Нет изменений → нет записей в accepted_patches ---
def test_no_changes_no_accept():
    with tempfile.TemporaryDirectory() as d:
        project = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        project.mkdir()
        sandbox.mkdir()

        def _recover_noop(self, root, *, run_pip_check=False):
            return SimpleNamespace(has_changes=False, missing={}, explanations=[])

        eng = _engine(project, sandbox)
        with mock.patch(
            "analysis.python_dependency_inference.PythonDependencyInference.recover",
            _recover_noop,
        ):
            eng._recover_dependencies_sandbox(sandbox)
        assert eng.context.accepted_patches == []


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
    print(f"\nDep recovery sandbox: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
