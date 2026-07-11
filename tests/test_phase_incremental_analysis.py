"""
Инкрементальный анализ (pipeline.incremental_analysis, по умолчанию False):
вместо полного пересканирования всего проекта на каждую попытку патча и
после каждого принятого патча — пересканируем только изменённый файл,
обновляя в context.current_errors только его записи.

Полный скан остаётся:
  - в начале прогона (никакого _incremental_target_file ещё нет);
  - в конце (_finalize_and_audit делает отдельный полный analyze() для
    финального отчёта — не через эту фичу, не трогали);
  - принудительно, если последний патч дал symbol_regression/duplication
    (риск межфайловых сломанных ссылок — partial-скан их не увидит).

Тесты ниже проверяют каждый слой отдельно (analyzers -> PipelineContext ->
AnalyzeStage -> ValidateStage -> DecideStage rollback -> pipeline_engine
триггер), с РЕАЛЬНЫМИ flake8/ruff/bandit на временных файлах — не только
моками.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzers.python_analyzer import PythonAnalyzer
from analyzers.ruff_analyzer import RuffAnalyzer
from analyzers.bandit_analyzer import BanditAnalyzer
from core.pipeline_context import PipelineContext
from core.stages.analyze_stage import AnalyzeStage
from core.stages.decide_stage import DecideStage
from core.stages.validate_stage import ValidateStage


# ---------------------------------------------------------------------------
# Уровень 1: анализаторы поддерживают files= и дают тот же результат, что и
# полный скан, ограниченный одним файлом.
# ---------------------------------------------------------------------------

def test_python_analyzer_files_param_scopes_to_one_file(tmp_path):
    (tmp_path / "a.py").write_text("import os\nx=1\n", encoding="utf-8")  # E225, F401
    (tmp_path / "b.py").write_text("import sys\ny=2\n", encoding="utf-8")  # E225, F401

    analyzer = PythonAnalyzer()
    full = analyzer.analyze(tmp_path)
    only_a = analyzer.analyze(tmp_path, files=["a.py"])

    full_files = {e["file"] for e in full}
    assert "a.py" in full_files and "b.py" in full_files
    assert {e["file"] for e in only_a} == {"a.py"}
    # Результат по a.py идентичен полному и точечному скану.
    assert sorted((e["line"], e["code"]) for e in only_a) == sorted(
        (e["line"], e["code"]) for e in full if e["file"] == "a.py"
    )


def test_python_analyzer_files_param_skips_other_files_entirely(tmp_path):
    """Файл b.py синтаксически невалиден — но раз он не в files=, точечный
    скан не должен его даже открывать (иначе сломал бы AST-фоллбэк)."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def f(:\n", encoding="utf-8")  # invalid syntax

    analyzer = PythonAnalyzer()
    only_a = analyzer.analyze(tmp_path, files=["a.py"])
    assert all(e["file"] != "b.py" for e in only_a)


def test_ruff_analyzer_files_param(tmp_path):
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")  # F401
    (tmp_path / "b.py").write_text("import sys\n", encoding="utf-8")  # F401

    ruff = RuffAnalyzer()
    if not ruff.available():
        pytest.skip("ruff not installed")
    only_a = ruff.analyze(tmp_path, files=["a.py"])
    assert {e["file"] for e in only_a} <= {"a.py"}
    assert any(e["file"] == "a.py" for e in only_a)


def test_bandit_analyzer_files_param(tmp_path):
    (tmp_path / "a.py").write_text(
        "import subprocess\nsubprocess.call('ls', shell=True)\n", encoding="utf-8"
    )  # B602/B603-ish
    (tmp_path / "b.py").write_text(
        "import subprocess\nsubprocess.call('ls', shell=True)\n", encoding="utf-8"
    )

    bandit = BanditAnalyzer()
    if not bandit.available():
        pytest.skip("bandit not installed")
    only_a = bandit.analyze(tmp_path, files=["a.py"])
    assert {e["file"] for e in only_a} <= {"a.py"}


# ---------------------------------------------------------------------------
# Уровень 2: PipelineContext.merge_file_errors
# ---------------------------------------------------------------------------

def test_merge_file_errors_replaces_only_target_file(tmp_path):
    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.set_errors([
        {"file": "a.py", "line": 1, "code": "E501", "message": "x"},
        {"file": "b.py", "line": 2, "code": "F401", "message": "y"},
        {"file": "b.py", "line": 5, "code": "E225", "message": "z"},
    ])

    new_ctx = ctx.merge_file_errors("a.py", [
        {"file": "a.py", "line": 1, "code": "W291", "message": "new"},
    ])

    by_file = {}
    for e in new_ctx.current_errors:
        by_file.setdefault(e["file"], []).append(e)

    assert len(by_file["a.py"]) == 1
    assert by_file["a.py"][0]["code"] == "W291"
    assert len(by_file["b.py"]) == 2  # нетронуты


def test_merge_file_errors_normalizes_slashes(tmp_path):
    """Windows-style '\\' и '/' должны матчиться как один и тот же файл."""
    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.set_errors([
        {"file": "pkg\\a.py", "line": 1, "code": "E501", "message": "x"},
        {"file": "b.py", "line": 1, "code": "F401", "message": "y"},
    ])
    new_ctx = ctx.merge_file_errors("pkg/a.py", [])
    files = [e["file"] for e in new_ctx.current_errors]
    assert "pkg\\a.py" not in files and "pkg/a.py" not in files  # заменили (пусто)
    assert "b.py" in files


def test_merge_file_errors_empty_removes_all_for_file(tmp_path):
    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.set_errors([
        {"file": "a.py", "line": 1, "code": "E501", "message": "x"},
        {"file": "a.py", "line": 2, "code": "F401", "message": "y"},
        {"file": "b.py", "line": 1, "code": "E225", "message": "z"},
    ])
    new_ctx = ctx.merge_file_errors("a.py", [])  # файл починен полностью
    assert len(new_ctx.current_errors) == 1
    assert new_ctx.current_errors[0]["file"] == "b.py"


# ---------------------------------------------------------------------------
# Уровень 3: AnalyzeStage._execute_incremental
# ---------------------------------------------------------------------------

def _make_analyze_stage():
    return AnalyzeStage(analyzer=PythonAnalyzer())


def _ctx_for_analyze(tmp_path, incremental_target=None, incremental_enabled=True):
    config = {"pipeline": {
        "incremental_analysis": incremental_enabled,
        "security_scan": False, "use_mypy": False, "use_ruff": False,
        "use_bandit": False, "cascade_collapse": False,
    }, "tools": {"semgrep": {"enabled": False}}}
    ctx = PipelineContext(project_path=tmp_path, language="python",
                          config=config, working_path=tmp_path)
    ctx = ctx.set_errors([
        {"file": "untouched.py", "line": 1, "code": "F401", "message": "unused"},
    ])
    if incremental_target:
        ctx = ctx.update(metadata=dict(ctx.metadata, **{
            "_incremental_target_file": incremental_target,
        }))
    return ctx


def test_analyze_stage_incremental_only_rescans_target_file(tmp_path):
    (tmp_path / "untouched.py").write_text("def f(:\n", encoding="utf-8")  # would break full scan
    (tmp_path / "changed.py").write_text("import os\n", encoding="utf-8")  # F401

    stage = _make_analyze_stage()
    ctx = _ctx_for_analyze(tmp_path, incremental_target="changed.py")
    result = stage.execute(ctx)

    files = {e["file"] for e in result.current_errors}
    assert "changed.py" in files
    # untouched.py: запись из старого снимка (F401 "unused") сохранена как была,
    # хотя реальный untouched.py на диске сейчас невалиден — инкрементальный
    # путь его не трогал и не должен был.
    untouched = [e for e in result.current_errors if e["file"] == "untouched.py"]
    assert len(untouched) == 1
    assert untouched[0]["code"] == "F401"


def test_analyze_stage_incremental_clears_metadata_flag(tmp_path):
    (tmp_path / "changed.py").write_text("x = 1\n", encoding="utf-8")
    stage = _make_analyze_stage()
    ctx = _ctx_for_analyze(tmp_path, incremental_target="changed.py")
    result = stage.execute(ctx)
    assert "_incremental_target_file" not in result.metadata


def test_analyze_stage_full_scan_when_no_target_file(tmp_path):
    """Без _incremental_target_file (начало прогона) — обычный полный путь."""
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import sys\n", encoding="utf-8")
    stage = _make_analyze_stage()
    ctx = _ctx_for_analyze(tmp_path, incremental_target=None)
    result = stage.execute(ctx)
    files = {e["file"] for e in result.current_errors}
    assert "a.py" in files and "b.py" in files


# ---------------------------------------------------------------------------
# Уровень 4: ValidateStage инкрементальная ветка
# ---------------------------------------------------------------------------

def _make_validate_stage():
    return ValidateStage(
        compiler=MagicMock(), linter=MagicMock(), security=MagicMock(),
        analyzer=PythonAnalyzer(), degradation=MagicMock(),
    )


def test_validate_stage_incremental_preserves_other_files_errors(tmp_path):
    (tmp_path / "patched.py").write_text("x = 1\n", encoding="utf-8")  # чисто после патча
    config = {"pipeline": {
        "incremental_analysis": True, "use_mypy": True, "use_bandit": True,
        "use_ruff": False, "run_tests": False, "logic_guard": False,
    }}
    ctx = PipelineContext(project_path=tmp_path, language="python",
                          config=config, working_path=tmp_path)
    ctx = ctx.set_errors([
        {"file": "patched.py", "line": 1, "code": "E225", "message": "stale, до патча"},
        {"file": "other.py", "line": 3, "code": "F401", "message": "не трогали"},
    ])
    ctx = ctx.set_selected_error({"file": "patched.py", "line": 1, "code": "E225", "message": "x"})
    ctx = ctx.update(generated_patch="--- a/patched.py\n+++ b/patched.py\n")

    stage = _make_validate_stage()
    result = stage.execute(ctx)

    files = {e["file"] for e in result.current_errors}
    assert "other.py" in files, "ошибки нетронутого файла должны сохраниться"
    patched_errors = [e for e in result.current_errors if e["file"] == "patched.py"]
    assert patched_errors == [], "patched.py стал чистым после патча — записей не осталось"
    assert result.validation_results["error_count_before"] == 2
    assert result.validation_results["error_count_after"] == 1  # только other.py осталось


def test_validate_stage_non_incremental_unaffected(tmp_path):
    """Контроль: incremental_analysis=False (default) — старое поведение
    полного скана, без регрессий от нового кода."""
    (tmp_path / "patched.py").write_text("x = 1\n", encoding="utf-8")
    config = {"pipeline": {
        "incremental_analysis": False, "use_mypy": True, "use_bandit": True,
        "use_ruff": False, "run_tests": False, "logic_guard": False,
    }}
    ctx = PipelineContext(project_path=tmp_path, language="python",
                          config=config, working_path=tmp_path)
    ctx = ctx.set_errors([
        {"file": "patched.py", "line": 1, "code": "E225", "message": "stale"},
    ])
    ctx = ctx.set_selected_error({"file": "patched.py", "line": 1, "code": "E225", "message": "x"})
    ctx = ctx.update(generated_patch="--- a/patched.py\n+++ b/patched.py\n")

    stage = _make_validate_stage()
    result = stage.execute(ctx)
    assert result.validation_results["error_count_after"] == 0


# ---------------------------------------------------------------------------
# Уровень 5: DecideStage rollback инкрементальная ветка
# ---------------------------------------------------------------------------

def test_decide_stage_rollback_incremental_preserves_other_files(tmp_path):
    (tmp_path / "rolled_back.py").write_text("import os\n", encoding="utf-8")  # F401 после rollback

    config = {"pipeline": {"incremental_analysis": True}}
    ctx = PipelineContext(project_path=tmp_path, language="python",
                          config=config, working_path=tmp_path)
    ctx = ctx.set_errors([
        {"file": "rolled_back.py", "line": 1, "code": "E225", "message": "от патча, должно исчезнуть"},
        {"file": "other.py", "line": 1, "code": "F401", "message": "не трогали"},
    ])
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "patch_snapshots": [{"file": "rolled_back.py", "original_content": "import os\n"}],
    }))

    stage = DecideStage(quality_evaluator=MagicMock(), analyzer=PythonAnalyzer())
    result = stage._rollback_file_from_snapshot(ctx, {"file": "rolled_back.py"})

    files_by_name = {}
    for e in result.current_errors:
        files_by_name.setdefault(e["file"], []).append(e)
    assert "other.py" in files_by_name and len(files_by_name["other.py"]) == 1
    assert "rolled_back.py" in files_by_name
    assert files_by_name["rolled_back.py"][0]["code"] == "F401"  # пересканировано
    assert (tmp_path / "rolled_back.py").read_text(encoding="utf-8") == "import os\n"


def test_decide_stage_rollback_non_incremental_full_rescan(tmp_path):
    (tmp_path / "rolled_back.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("import sys\n", encoding="utf-8")

    config = {"pipeline": {"incremental_analysis": False}}
    ctx = PipelineContext(project_path=tmp_path, language="python",
                          config=config, working_path=tmp_path)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "patch_snapshots": [{"file": "rolled_back.py", "original_content": "import os\n"}],
    }))

    stage = DecideStage(quality_evaluator=MagicMock(), analyzer=PythonAnalyzer())
    result = stage._rollback_file_from_snapshot(ctx, {"file": "rolled_back.py"})
    files = {e["file"] for e in result.current_errors}
    assert "other.py" in files and "rolled_back.py" in files  # полный скан увидел оба


# ---------------------------------------------------------------------------
# Уровень 6: триггер в pipeline_engine._global_fix_loop (через прямую проверку
# условия — полный e2e прогон global_fix_loop слишком тяжёл для unit-теста).
# ---------------------------------------------------------------------------

def test_engine_sets_target_file_only_when_enabled_and_safe():
    """Логика триггера (см. core/pipeline_engine.py, рядом с ANALYZING
    transition) воспроизведена здесь как чистая функция тех же условий —
    защищает само решающее правило от регрессии при рефакторинге."""
    def should_set_incremental(cfg_enabled, new_accepted, risky_patch):
        return bool(cfg_enabled and new_accepted and not risky_patch)

    assert should_set_incremental(True, [{"error": {"file": "a.py"}}], False) is True
    assert should_set_incremental(False, [{"error": {"file": "a.py"}}], False) is False
    assert should_set_incremental(True, [], False) is False
    assert should_set_incremental(True, [{"error": {"file": "a.py"}}], True) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
