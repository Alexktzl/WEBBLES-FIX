"""
Control series 2026-06-21, находка #1 (продолжение, репро живьём на
ad-ha/mg-saic-ha): DecideStage логировал решение (append_decision_log) ДО
проверки retry-механизмов (TESP feedback retry, NR feedback retry) —
безусловно, в самом начале _dispatch_after_success/execute(). Если на ПЕРВОЙ
попытке срабатывает retry (не финальный исход — возврат в GENERATING_PATCH),
а до ВТОРОЙ попытки прогон не доходит (например, project_timeout срезал
сразу после retry) — decisions[] получал "призрачную" запись без
соответствующего add_rejected_patch/add_accepted_patch (decisions[]=1,
rejected_patches=0 на живом прогоне).

Фикс: логирование перенесено к месту, где решение действительно финальное —
на retry-попытке decisions[] остаётся пуст, на финальной попытке (или когда
retry-механизм вообще не применим к этой ошибке) — ровно одна запись.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.decide_stage import DecideStage
from analyzers.python_analyzer import PythonAnalyzer


def _make_stage():
    return DecideStage(quality_evaluator=MagicMock(evaluate=MagicMock(return_value=(1.0, []))),
                       analyzer=PythonAnalyzer())


def _ctx_with_target_still_present(tmp_path, before=5, after=3):
    """target error НЕ исчез, но другие ошибки в файле исчезли (after<before) —
    ведёт к reason='target_error_still_present' (TESP), а не
    'error_count_not_decreased'."""
    error = {"file": "a.py", "line": 1, "code": "E501", "message": "x", "error_class": "CLEANUP"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(current_errors=(error,))  # target ещё присутствует
    ctx = ctx.set_validation_results({"error_count_before": before, "error_count_after": after})
    return ctx, error


def test_tesp_first_retry_does_not_log_decision(tmp_path):
    """Первая TESP-попытка → GENERATING_PATCH (retry, не финал) — decisions[]
    должен остаться пуст. Этот путь живёт в execute() (target_still_present
    fall-through), не в _dispatch_after_success."""
    stage = _make_stage()
    ctx, error = _ctx_with_target_still_present(tmp_path)

    result = stage.execute(ctx)

    assert result.current_state == State.GENERATING_PATCH
    assert result.metadata.get("decisions", []) == []


def test_tesp_second_attempt_logs_exactly_one_decision(tmp_path):
    """Вторая TESP-попытка (retry уже был) → финальный REJECT — ровно одна
    запись в decisions[], rejected_patches тоже ровно одна."""
    stage = _make_stage()
    ctx, error = _ctx_with_target_still_present(tmp_path)
    target_sig = stage._error_signature(error)
    _tesp_key = f"_tesp_retry_{target_sig}"
    ctx = ctx.update(metadata=dict(ctx.metadata, **{_tesp_key: 1}))

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "REJECT"
    assert decisions[0]["reason"] == "target_error_still_present"
    assert len(result.rejected_patches) == 1


def test_nr_first_retry_does_not_log_decision(tmp_path):
    """Первая NR-попытка (verdict=wrong) → GENERATING_PATCH (retry, не
    финал) — decisions[] должен остаться пуст."""
    stage = _make_stage()
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "x", "error_class": "CLEANUP"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{"review": {"verdict": "wrong", "reasons": []}}))
    target_sig = stage._error_signature(error)

    result = stage._dispatch_after_success(ctx, error, target_sig, reason="x")

    assert result.current_state == State.GENERATING_PATCH
    assert result.metadata.get("decisions", []) == []


def test_nr_second_attempt_logs_exactly_one_decision(tmp_path):
    """Вторая NR-попытка (retry уже был) → финальный NEEDS_REVIEW — ровно
    одна запись. После структурного рефакторинга (2026-06-21) логирование
    происходит внутри NeedsReviewStage.execute() (единственная точка добавления
    в needs_review_items), а DecideStage только устанавливает
    metadata["_needs_review_pending_reason"] перед переходом в State.
    NEEDS_REVIEW — поэтому тест дополняет цепочку этим вызовом, как сделал бы
    state-machine dispatcher в реальном прогоне."""
    stage = _make_stage()
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "x", "error_class": "CLEANUP"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    target_sig = stage._error_signature(error)
    _nr_key = f"_nr_retry_{target_sig}"
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "review": {"verdict": "wrong", "reasons": []}, _nr_key: 1,
    }))

    result = stage._dispatch_after_success(ctx, error, target_sig, reason="x")
    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "x"
    assert result.metadata.get("decisions", []) == []  # ещё не залогировано

    from core.stages.needs_review_stage import NeedsReviewStage
    result = NeedsReviewStage().execute(result)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "NEEDS_REVIEW"
    assert decisions[0]["reason"] == "x"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
