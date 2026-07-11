"""
Tech debt audit 2026-06-21, находка #2 (P1): SystemHealthEvaluator никогда не
видел патчи, обработанные ParallelExecutor (core/engine/parallel.py) — тот
мини-цикл не вызывает DecideStage, поэтому last_decision не выставляется, а
health_evaluator.update() вызывался ТОЛЬКО внутри _single_run(), куда
параллельно обработанные файлы не попадают вообще.

Фикс:
1. core/pipeline_engine.py::_global_fix_loop вызывает health_evaluator.update()
   явно сразу после parallel_executor.execute(), ДО того как
   _parallel_touched_files может быть преобразован в _incremental_target_files.
2. core/system_health.py::_infer_decision распознаёт
   metadata["_parallel_touched_files"] и возвращает "PARALLEL_BATCH" вместо
   общего "UNKNOWN".
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.system_health import SystemHealthEvaluator


def _ctx_with_history(*states, metadata=None):
    ctx = PipelineContext(project_path=Path("/tmp/fake"), language="python")
    for s in states:
        ctx = ctx.add_state_to_history(s)
    if metadata:
        ctx = ctx.update(metadata=dict(ctx.metadata, **metadata))
    return ctx


def test_parallel_batch_recognized_instead_of_unknown():
    """Контекст сразу после ParallelExecutor: prev_state != DECIDING (никакого
    DECIDING вообще не было в истории), но metadata несёт маркер параллельной
    обработки — должен вернуть "PARALLEL_BATCH", не "UNKNOWN"."""
    evaluator = SystemHealthEvaluator()
    ctx = _ctx_with_history(
        State.NEXT_ERROR, State.ANALYZING,
        metadata={"_parallel_touched_files": ["a.py", "b.py"]},
    )
    decision = evaluator._infer_decision(ctx)
    assert decision == "PARALLEL_BATCH"


def test_no_marker_falls_back_to_old_behavior():
    """Без _parallel_touched_files — поведение не изменилось (last_decision/
    UNKNOWN fallback)."""
    evaluator = SystemHealthEvaluator()
    ctx = _ctx_with_history(State.NEXT_ERROR, State.ANALYZING)
    assert evaluator._infer_decision(ctx) == "UNKNOWN"

    ctx2 = _ctx_with_history(
        State.NEXT_ERROR, State.ANALYZING, metadata={"last_decision": "ACCEPT"},
    )
    assert evaluator._infer_decision(ctx2) == "ACCEPT"


def test_deciding_transition_still_takes_priority_over_parallel_marker():
    """Если контекст реально пришёл из DECIDING (последовательный путь), эта
    логика приоритетнее — даже если по какой-то причине маркер параллельной
    обработки остался в metadata от прошлого цикла (защитная очистка должна
    предотвращать это в pipeline_engine.py, но _infer_decision сам по себе
    не должен путать приоритеты)."""
    evaluator = SystemHealthEvaluator()
    ctx = _ctx_with_history(
        State.DECIDING, State.COMPLETED,
        metadata={"_parallel_touched_files": ["a.py"]},
    )
    assert evaluator._infer_decision(ctx) == "ACCEPT"


def test_update_appends_history_and_decision():
    evaluator = SystemHealthEvaluator()
    ctx = _ctx_with_history(
        State.NEXT_ERROR, State.ANALYZING,
        metadata={"_parallel_touched_files": ["a.py"]},
    )
    ctx = ctx.set_errors([{"file": "a.py", "line": 1, "code": "E1", "message": "x"}])
    evaluator.update(ctx)
    assert len(evaluator.history) == 1
    assert evaluator.decisions[-1] == "PARALLEL_BATCH"
    assert evaluator.error_counts[-1] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
