"""
Tech debt audit 2026-06-21, находка #1 (P0): _skip_legacy_validation_checks
требовала use_mypy AND use_bandit ОДНОВРЕМЕННО для пропуска легаси-валидаторов
(CompilerChecks/LintChecks/SecurityChecks). В реальном конфиге (use_bandit=False)
условие никогда не выполнялось — на каждую попытку патча (accept И reject) гонялся
ВТОРОЙ полный mypy-скан (через CompilerChecks._run_mypy, дублируя MypyAnalyzer
в AnalyzeStage) + полный flake8 (дублируя PythonAnalyzer.analyze() ниже в той же
стадии) + bandit при use_bandit=True.

Фикс: три независимых skip-условия вместо одной AND-связки:
  - lint   — пропускается ВСЕГДА для Python (flake8 безусловно дублирует analyzer)
  - compile — пропускается только если use_mypy=True (тогда MypyAnalyzer уже покрыл)
  - security — пропускается только если use_bandit=True (тогда BanditAnalyzer покрыл)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.stages.validate_stage import ValidateStage


def _ctx(use_mypy=False, use_bandit=False, language="python"):
    config = {"pipeline": {"use_mypy": use_mypy, "use_bandit": use_bandit}}
    return PipelineContext(project_path=Path("/tmp/fake"), language=language, config=config)


# ---------------------------------------------------------------------------
# Уровень 1: три независимых статических метода
# ---------------------------------------------------------------------------

def test_lint_always_skipped_for_python_regardless_of_flags():
    assert ValidateStage._skip_legacy_lint_check(_ctx(use_mypy=False, use_bandit=False)) is True
    assert ValidateStage._skip_legacy_lint_check(_ctx(use_mypy=True, use_bandit=True)) is True


def test_lint_not_skipped_for_non_python():
    assert ValidateStage._skip_legacy_lint_check(_ctx(language="rust")) is False


def test_compile_skip_tracks_use_mypy_only():
    assert ValidateStage._skip_legacy_compile_check(_ctx(use_mypy=True, use_bandit=False)) is True
    assert ValidateStage._skip_legacy_compile_check(_ctx(use_mypy=False, use_bandit=True)) is False
    assert ValidateStage._skip_legacy_compile_check(_ctx(use_mypy=False, use_bandit=False)) is False


def test_security_skip_tracks_use_bandit_only():
    assert ValidateStage._skip_legacy_security_check(_ctx(use_mypy=False, use_bandit=True)) is True
    assert ValidateStage._skip_legacy_security_check(_ctx(use_mypy=True, use_bandit=False)) is False
    assert ValidateStage._skip_legacy_security_check(_ctx(use_mypy=False, use_bandit=False)) is False


# ---------------------------------------------------------------------------
# Уровень 2: интеграция — реальный продуктивный конфиг (use_mypy=True,
# use_bandit=False) больше не вызывает дублирующий легаси-mypy/flake8, но
# СОХРАНЯЕТ единственную bandit-подобную проверку (security), раз она не
# покрыта отдельно.
# ---------------------------------------------------------------------------

def _make_stage():
    compiler = MagicMock()
    linter = MagicMock()
    security = MagicMock()
    compiler.run.return_value = (True, [])
    linter.run.return_value = (True, [])
    security.run.return_value = (True, [])
    analyzer = MagicMock()
    analyzer.analyze.return_value = []
    stage = ValidateStage(compiler=compiler, linter=linter, security=security,
                          analyzer=analyzer, degradation=MagicMock())
    return stage, compiler, linter, security


def test_production_config_skips_compile_and_lint_but_runs_security(tmp_path):
    """use_mypy=True, use_bandit=False (реальный конфиг скилла) — главный
    регрессионный сценарий находки #1."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    stage, compiler, linter, security = _make_stage()

    ctx = _ctx(use_mypy=True, use_bandit=False)
    ctx = ctx.update(working_path=tmp_path, config={
        "pipeline": {"use_mypy": True, "use_bandit": False, "run_tests": False, "logic_guard": False},
    })
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "E225", "message": "x"})
    ctx = ctx.update(generated_patch="--- a/a.py\n+++ b/a.py\n")

    stage.execute(ctx)

    compiler.run.assert_not_called()
    linter.run.assert_not_called()
    security.run.assert_called_once()


def test_default_config_no_analyzers_enabled_runs_all_legacy_checks_except_lint(tmp_path):
    """use_mypy=False, use_bandit=False — ни один отдельный анализатор не включён,
    поэтому compile/security ДОЛЖНЫ выполняться (единственная защита), lint всё
    равно пропускается (безусловно дублирует analyzer.analyze())."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    stage, compiler, linter, security = _make_stage()

    ctx = _ctx(use_mypy=False, use_bandit=False)
    ctx = ctx.update(working_path=tmp_path, config={
        "pipeline": {"use_mypy": False, "use_bandit": False, "run_tests": False, "logic_guard": False},
    })
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "E225", "message": "x"})
    ctx = ctx.update(generated_patch="--- a/a.py\n+++ b/a.py\n")

    stage.execute(ctx)

    compiler.run.assert_called_once()
    security.run.assert_called_once()
    linter.run.assert_not_called()


def test_both_analyzers_enabled_skips_all_three_legacy_checks(tmp_path):
    """Контроль обратной совместимости: старое поведение (use_mypy=True AND
    use_bandit=True → пропустить все три) должно сохраниться."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    stage, compiler, linter, security = _make_stage()

    ctx = _ctx(use_mypy=True, use_bandit=True)
    ctx = ctx.update(working_path=tmp_path, config={
        "pipeline": {"use_mypy": True, "use_bandit": True, "run_tests": False, "logic_guard": False},
    })
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "E225", "message": "x"})
    ctx = ctx.update(generated_patch="--- a/a.py\n+++ b/a.py\n")

    stage.execute(ctx)

    compiler.run.assert_not_called()
    linter.run.assert_not_called()
    security.run.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
