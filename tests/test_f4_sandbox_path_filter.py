"""
Regression test F4: sandbox paths (webbles_isolated_* / .webles_sandbox)
must NOT appear in the error list produced by AnalyzeStage's semgrep handling.

Root cause: _prepare_environment() used to copy .webles_sandbox into the temp
dir, and semgrep returned absolute paths for findings there. These ended up in
the NR queue as phantom errors pointing to non-existent temp files.

Fix: (1) copytree now excludes .webles_sandbox; (2) AnalyzeStage normalizes
absolute semgrep paths to relative and filters any residual sandbox markers.
"""

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(path: str, check_id: str = "python.lang.security.exec-detected",
                  line: int = 10) -> dict:
    return {
        "path": path,
        "start": {"line": line, "col": 1},
        "extra": {"message": "exec() usage detected", "severity": "warning"},
        "check_id": check_id,
    }


def _build_analyze_stage():
    """Создаёт AnalyzeStage с замоканными зависимостями."""
    from core.stages.analyze_stage import AnalyzeStage
    analyzer = MagicMock()
    analyzer.analyze.return_value = []
    stage = AnalyzeStage(analyzer)
    # Отключаем все optional scanners
    stage.security_scanner = MagicMock()
    stage.security_scanner.scan_project.return_value = []
    stage.test_runner = MagicMock()
    stage.test_runner.run_tests.return_value = None
    return stage


def _make_context(work_dir: Path, config: dict = None):
    ctx = MagicMock()
    ctx.working_path = work_dir
    ctx.project_path = work_dir
    ctx.language = "python"
    ctx.metadata = {
        "_current_global_cycle": 1,
        "tools": {},
    }
    ctx.config = config or {"tools": {"semgrep": {"enabled": True}}}
    ctx.current_errors = []
    ctx.add_state_to_history = MagicMock(return_value=ctx)
    ctx.set_errors = MagicMock(return_value=ctx)
    ctx.update = MagicMock(return_value=ctx)
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSemgrepPathFilter:
    """Тесты нормализации и фильтрации sandbox-путей в semgrep-находках."""

    def test_relative_path_passes_through(self, tmp_path):
        """Относительный путь semgrep остаётся без изменений."""
        stage = _build_analyze_stage()
        findings = [_make_finding("boltons/funcutils.py")]

        with patch.object(stage, "_get_semgrep_errors", return_value=findings), \
             patch.object(stage, "_security_scan_enabled", return_value=False), \
             patch.object(stage, "_pip_audit_enabled", return_value=False), \
             patch.object(stage, "_use_mypy_enabled", return_value=False), \
             patch.object(stage, "_semantic_audit_enabled", return_value=False):
            ctx = _make_context(tmp_path)
            stage.execute(ctx)

        set_errors_calls = ctx.set_errors.call_args_list
        if set_errors_calls:
            errors = set_errors_calls[-1][0][0]
        else:
            errors = ctx.update.call_args_list
            # fallback: inspect analyzer.analyze return or context update
            errors = []

        # Проверяем через analyzer.analyze output + semgrep merge
        # Лучше проверить напрямую через mock call
        ctx.set_errors.assert_called()
        all_errs = ctx.set_errors.call_args[0][0]
        paths = [e["file"] for e in all_errs]
        assert "boltons/funcutils.py" in paths

    def test_absolute_path_inside_workdir_normalized(self, tmp_path):
        """Абсолютный путь внутри work_dir нормализуется в относительный."""
        stage = _build_analyze_stage()
        abs_path = str(tmp_path / "boltons" / "funcutils.py")
        findings = [_make_finding(abs_path)]

        with patch.object(stage, "_get_semgrep_errors", return_value=findings), \
             patch.object(stage, "_security_scan_enabled", return_value=False), \
             patch.object(stage, "_pip_audit_enabled", return_value=False), \
             patch.object(stage, "_use_mypy_enabled", return_value=False), \
             patch.object(stage, "_semantic_audit_enabled", return_value=False):
            ctx = _make_context(tmp_path)
            stage.execute(ctx)

        all_errs = ctx.set_errors.call_args[0][0]
        paths = [e["file"] for e in all_errs]
        # Абсолютный путь внутри tmp_path → должен стать относительным
        assert any("funcutils.py" in p and "webbles_isolated" not in p for p in paths), \
            f"Ожидался нормализованный относительный путь, получено: {paths}"

    def test_sandbox_absolute_path_filtered(self, tmp_path):
        """Абсолютный путь из sandbox (webbles_isolated_*) должен быть отфильтрован."""
        stage = _build_analyze_stage()
        sandbox_path = r"C:\Users\user\AppData\Local\Temp\webbles_isolated_abc123\.webles_sandbox\base\boltons\funcutils.py"
        findings = [_make_finding(sandbox_path)]

        with patch.object(stage, "_get_semgrep_errors", return_value=findings), \
             patch.object(stage, "_security_scan_enabled", return_value=False), \
             patch.object(stage, "_pip_audit_enabled", return_value=False), \
             patch.object(stage, "_use_mypy_enabled", return_value=False), \
             patch.object(stage, "_semantic_audit_enabled", return_value=False):
            ctx = _make_context(tmp_path)
            stage.execute(ctx)

        all_errs = ctx.set_errors.call_args[0][0]
        paths = [e["file"] for e in all_errs]
        assert not any("webbles_isolated" in p for p in paths), \
            f"Sandbox-путь не должен попасть в errors: {paths}"
        assert not any(".webles_sandbox" in p for p in paths), \
            f"Sandbox-путь не должен попасть в errors: {paths}"

    def test_webles_sandbox_relative_path_filtered(self, tmp_path):
        """Относительный путь содержащий .webles_sandbox тоже фильтруется."""
        stage = _build_analyze_stage()
        findings = [_make_finding(".webles_sandbox/base/boltons/funcutils.py")]

        with patch.object(stage, "_get_semgrep_errors", return_value=findings), \
             patch.object(stage, "_security_scan_enabled", return_value=False), \
             patch.object(stage, "_pip_audit_enabled", return_value=False), \
             patch.object(stage, "_use_mypy_enabled", return_value=False), \
             patch.object(stage, "_semantic_audit_enabled", return_value=False):
            ctx = _make_context(tmp_path)
            stage.execute(ctx)

        all_errs = ctx.set_errors.call_args[0][0]
        paths = [e["file"] for e in all_errs]
        assert not any(".webles_sandbox" in p for p in paths), \
            f"Sandbox-путь не должен попасть в errors: {paths}"

    def test_mixed_findings_only_real_pass(self, tmp_path):
        """Из смешанных находок (реальные + sandbox) в errors попадают только реальные."""
        stage = _build_analyze_stage()
        sandbox_path = r"C:\Temp\webbles_isolated_xyz\.webles_sandbox\base\requests\auth.py"
        findings = [
            _make_finding("requests/auth.py", line=10),           # реальный
            _make_finding(sandbox_path, line=10),                  # sandbox — фильтровать
            _make_finding("requests/sessions.py", line=20),        # реальный
        ]

        with patch.object(stage, "_get_semgrep_errors", return_value=findings), \
             patch.object(stage, "_security_scan_enabled", return_value=False), \
             patch.object(stage, "_pip_audit_enabled", return_value=False), \
             patch.object(stage, "_use_mypy_enabled", return_value=False), \
             patch.object(stage, "_semantic_audit_enabled", return_value=False):
            ctx = _make_context(tmp_path)
            stage.execute(ctx)

        all_errs = ctx.set_errors.call_args[0][0]
        paths = [e["file"] for e in all_errs]
        assert "requests/auth.py" in paths
        assert "requests/sessions.py" in paths
        assert not any("webbles_isolated" in p for p in paths), \
            f"Sandbox-путь не должен попасть: {paths}"
        # 2 реальных + 0 sandbox
        semgrep_errs = [e for e in all_errs if e.get("error_type") == "lint"]
        assert len(semgrep_errs) == 2
