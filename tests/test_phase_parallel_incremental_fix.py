"""
Фикс архитектурной недостижимости incremental_analysis при parallel_workers > 1.

Баг (до фикса): ParallelExecutor.execute() напрямую вызывает GeneratePatchStage/
ApplyPatchStage в потоках на каждый файл, минуя ValidateStage/DecideStage целиком.
После merge `merged_ctx = local_dict` брал current_state ПОСЛЕДНЕГО завершившегося
воркера (VALIDATING/NEXT_ERROR/что угодно — зависит от того, что произошло в его
локальном мини-цикле), затирая ANALYZING, который _global_fix_loop выставлял ПЕРЕД
вызовом execute(). _single_run() в следующей итерации стартовал с этого чужого
состояния и пропускал AnalyzeStage целиком — даже если файлы реально изменились
на диске. Контрольные серии 13/14 (pygenda, webex_bot) показали 0 вхождений
"инкрементальный re-scan" во всех прогонах с parallel_workers=2 — фича была
architecturally недостижима, не просто неэффективна.

Фикс: ParallelExecutor отслеживает touched_files (через реальный сигнал успеха —
переход ApplyPatchStage в State.VALIDATING, а не мёртвый last_decision=="ACCEPT",
который никогда не выставлялся — DecideStage этот мини-цикл не вызывает). После
merge current_state форсированно ставится в ANALYZING, touched_files кладётся в
metadata["_parallel_touched_files"]. core/pipeline_engine.py читает этот список
и (если incremental_analysis включён) превращает его в
metadata["_incremental_target_files"] для AnalyzeStage._execute_incremental,
который теперь принимает список файлов (не только один — для последовательного
ACCEPT-пути) одним combined-вызовом анализатора.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.analyze_stage import AnalyzeStage
from analyzers.python_analyzer import PythonAnalyzer


# ---------------------------------------------------------------------------
# Уровень 1: ParallelExecutor — current_state форсированно ANALYZING после merge
# ---------------------------------------------------------------------------

def _make_base_ctx_dict(file_errors=None):
    if file_errors is None:
        file_errors = [
            {"file": "file_a.py", "line": 1, "code": "F401", "message": "unused import"},
            {"file": "file_b.py", "line": 2, "code": "F401", "message": "unused import"},
        ]
    ctx = PipelineContext(project_path=Path("/tmp/fake_project"), language="python", config={
        "pipeline": {"parallel_workers": 2, "incremental_analysis": True},
    })
    ctx = ctx.add_state_to_history(State.ANALYZING)
    ctx = ctx.update(current_errors=tuple(file_errors))
    return ctx.to_dict()


def _make_executor(shared):
    from core.engine.parallel import ParallelExecutor
    lock = threading.Lock()

    def get_ctx():
        return PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())

    def set_ctx(new_ctx):
        shared["ctx_dict"] = new_ctx.to_dict()

    return ParallelExecutor(
        llm_client=MagicMock(), memory=MagicMock(), patch_engine=MagicMock(),
        state_lock=lock, get_context=get_ctx, set_context=set_ctx,
    )


def test_current_state_forced_to_analyzing_when_worker_succeeds(tmp_path):
    """Воркер успешно применяет патч (ApplyPatchStage -> VALIDATING) — раньше
    это состояние "выигрывало" merge и current_state становился VALIDATING,
    а не ANALYZING."""
    (tmp_path / "file_a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "file_b.py").write_text("y = 2\n", encoding="utf-8")

    file_errors = [
        {"file": "file_a.py", "line": 1, "code": "F401", "message": "unused import"},
        {"file": "file_b.py", "line": 2, "code": "F401", "message": "unused import"},
    ]
    shared = {"ctx_dict": _make_base_ctx_dict(file_errors)}
    executor = _make_executor(shared)

    def fake_gen_execute(self_stage, context):
        context = context.set_patch("--- a\n+++ b\n")
        return context.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply_execute(self_stage, context):
        # Симулируем успешное применение патча — единственный реальный сигнал
        # успеха в этом мини-цикле (last_decision никогда не выставляется).
        return context.add_state_to_history(State.VALIDATING)

    config = {"pipeline": {"parallel_workers": 2}}
    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_execute), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply_execute):
        executor.execute(config, tmp_path)

    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert final_ctx.current_state == State.ANALYZING


def test_current_state_forced_to_analyzing_when_nothing_applied():
    """Контроль: даже если НИ ОДИН воркер не применил патч — current_state
    всё равно должен быть ANALYZING (не падать в исключение/None)."""
    shared = {"ctx_dict": _make_base_ctx_dict()}
    executor = _make_executor(shared)

    def fake_gen_execute(self_stage, context):
        return context.add_state_to_history(State.NEXT_ERROR)

    config = {"pipeline": {"parallel_workers": 2}}
    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_execute):
        executor.execute(config, Path("/tmp/fake_project"))

    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert final_ctx.current_state == State.ANALYZING


def test_touched_files_tracked_only_for_successfully_applied_files(tmp_path):
    """metadata["_parallel_touched_files"] должен содержать ТОЛЬКО файлы, где
    ApplyPatchStage реально перешёл в VALIDATING — не все файлы с ошибками."""
    (tmp_path / "file_a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "file_b.py").write_text("y = 2\n", encoding="utf-8")

    file_errors = [
        {"file": "file_a.py", "line": 1, "code": "F401", "message": "unused import"},
        {"file": "file_b.py", "line": 2, "code": "F401", "message": "unused import"},
    ]
    shared = {"ctx_dict": _make_base_ctx_dict(file_errors)}
    executor = _make_executor(shared)

    def fake_gen_execute(self_stage, context):
        if context.selected_error.get("file") == "file_a.py":
            context = context.set_patch("--- a\n+++ b\n")
            return context.add_state_to_history(State.APPLYING_PATCH)
        return context.add_state_to_history(State.NEXT_ERROR)  # file_b: нет патча

    def fake_apply_execute(self_stage, context):
        return context.add_state_to_history(State.VALIDATING)

    config = {"pipeline": {"parallel_workers": 2}}
    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_execute), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply_execute):
        executor.execute(config, tmp_path)

    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert final_ctx.metadata.get("_parallel_touched_files") == ["file_a.py"]


def test_no_touched_files_key_when_nothing_applied():
    shared = {"ctx_dict": _make_base_ctx_dict()}
    executor = _make_executor(shared)

    def fake_gen_execute(self_stage, context):
        return context.add_state_to_history(State.NEXT_ERROR)

    config = {"pipeline": {"parallel_workers": 2}}
    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_execute):
        executor.execute(config, Path("/tmp/fake_project"))

    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert "_parallel_touched_files" not in final_ctx.metadata


# ---------------------------------------------------------------------------
# Уровень 2: AnalyzeStage._execute_incremental с НЕСКОЛЬКИМИ файлами
# ---------------------------------------------------------------------------

def test_execute_incremental_handles_multiple_files(tmp_path):
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")  # F401
    (tmp_path / "b.py").write_text("import sys\n", encoding="utf-8")  # F401
    (tmp_path / "untouched.py").write_text("def f(:\n", encoding="utf-8")  # invalid, не должен сканироваться

    stage = AnalyzeStage(analyzer=PythonAnalyzer())
    config = {"pipeline": {
        "incremental_analysis": True, "security_scan": False, "use_mypy": False,
        "use_ruff": False, "use_bandit": False, "cascade_collapse": False,
    }, "tools": {"semgrep": {"enabled": False}}}
    ctx = PipelineContext(project_path=tmp_path, language="python", config=config, working_path=tmp_path)
    ctx = ctx.set_errors([
        {"file": "untouched.py", "line": 1, "code": "F401", "message": "stale"},
    ])
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_incremental_target_files": ["a.py", "b.py"],
    }))

    result = stage.execute(ctx)

    files = {e["file"] for e in result.current_errors}
    assert "a.py" in files and "b.py" in files
    # untouched.py не трогали (его старая запись осталась как была)
    untouched = [e for e in result.current_errors if e["file"] == "untouched.py"]
    assert len(untouched) == 1 and untouched[0]["code"] == "F401"
    assert "_incremental_target_files" not in result.metadata


def test_execute_incremental_combines_singular_and_plural_targets(tmp_path):
    """Singular (_incremental_target_file, последовательный ACCEPT) и plural
    (_incremental_target_files, parallel merge) могут оба быть установлены —
    execute() должен объединить их в один re-scan."""
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import sys\n", encoding="utf-8")

    stage = AnalyzeStage(analyzer=PythonAnalyzer())
    config = {"pipeline": {
        "incremental_analysis": True, "security_scan": False, "use_mypy": False,
        "use_ruff": False, "use_bandit": False, "cascade_collapse": False,
    }, "tools": {"semgrep": {"enabled": False}}}
    ctx = PipelineContext(project_path=tmp_path, language="python", config=config, working_path=tmp_path)
    ctx = ctx.set_errors([])
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_incremental_target_file": "a.py",
        "_incremental_target_files": ["b.py"],
    }))

    result = stage.execute(ctx)
    files = {e["file"] for e in result.current_errors}
    assert "a.py" in files and "b.py" in files


# ---------------------------------------------------------------------------
# Уровень 3: интеграционный сквозной тест — ParallelExecutor -> AnalyzeStage
# ---------------------------------------------------------------------------

def test_end_to_end_parallel_then_incremental_analyze_not_planning(tmp_path):
    """Воспроизводит ИМЕННО баг из контрольной серии 13/14: после параллельной
    обработки с реальным успешным патчем, следующий вызов AnalyzeStage должен
    пойти по ИНКРЕМЕНТАЛЬНОМУ пути (не пропуститься, не уйти в полный скан),
    используя СПИСОК файлов из ParallelExecutor — а не "перескочить сразу в
    PLANNING", как было до фикса."""
    (tmp_path / "file_a.py").write_text("import os\n", encoding="utf-8")  # F401 после "патча"
    (tmp_path / "file_b.py").write_text("import sys\n", encoding="utf-8")

    file_errors = [
        {"file": "file_a.py", "line": 1, "code": "E225", "message": "stale, должно исчезнуть"},
        {"file": "file_b.py", "line": 1, "code": "E225", "message": "stale, должно исчезнуть"},
    ]
    shared = {"ctx_dict": _make_base_ctx_dict(file_errors)}

    from core.engine.parallel import ParallelExecutor
    lock = threading.Lock()

    def get_ctx():
        return PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())

    def set_ctx(new_ctx):
        shared["ctx_dict"] = new_ctx.to_dict()

    executor = ParallelExecutor(
        llm_client=MagicMock(), memory=MagicMock(), patch_engine=MagicMock(),
        state_lock=lock, get_context=get_ctx, set_context=set_ctx,
    )

    def fake_gen_execute(self_stage, context):
        context = context.set_patch("--- a\n+++ b\n")
        return context.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply_execute(self_stage, context):
        return context.add_state_to_history(State.VALIDATING)

    config = {"pipeline": {"parallel_workers": 2, "incremental_analysis": True}}
    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_execute), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply_execute):
        executor.execute(config, tmp_path)

    # Воспроизводим то, что делает _global_fix_loop сразу после execute():
    ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert ctx.current_state == State.ANALYZING, "до фикса здесь было VALIDATING — баг"
    touched = ctx.metadata.get("_parallel_touched_files")
    assert touched == ["file_a.py", "file_b.py"]

    pim = dict(ctx.metadata)
    pim["_incremental_target_files"] = list(touched)
    pim.pop("_parallel_touched_files", None)
    ctx = ctx.update(metadata=pim, config=config, working_path=tmp_path)

    # Теперь _single_run() вызвал бы AnalyzeStage.execute() как первый шаг —
    # воспроизводим это напрямую.
    analyze_stage = AnalyzeStage(analyzer=PythonAnalyzer())
    result = analyze_stage.execute(ctx)

    files = {e["file"] for e in result.current_errors}
    assert "file_a.py" in files and "file_b.py" in files
    stale = [e for e in result.current_errors if e.get("message") == "stale, должно исчезнуть"]
    assert stale == [], "инкрементальный re-scan должен заменить устаревшие записи свежими"
    assert result.current_state == State.CLASSIFY, "не PLANNING и не что-то другое — ровно как полный путь"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
