"""
Структурный рефакторинг (2026-06-21), согласован с пользователем после
control series decision-integrity из 10 проектов: 5 живых находок подряд
показали, что "не забыть вызвать append_decision_log из каждого нового места
принятия решения" — ненадёжный контракт. Перенесена логика на уровень,
где её невозможно обойти:

- PipelineContext.add_accepted_patch/add_rejected_patch — ЕДИНСТВЕННЫЕ точки
  добавления в accepted_patches/rejected_patches — теперь логируют
  decisions[] автоматически внутри себя. Любой будущий вызывающий код,
  который ДОБАВЛЯЕТ запись в эти списки, автоматически логируется — забыть
  невозможно, поскольку другого способа добавить запись не существует.
- NeedsReviewStage.execute() — единственный executor для State.NEEDS_REVIEW
  (см. dispatch table в PipelineEngine) и единственная точка добавления в
  needs_review_items — логирует decisions[] автоматически, читая reason из
  metadata["_needs_review_pending_reason"] (с защитным fallback "needs_review",
  если вызывающий код забыл его установить — НЕ теряет запись совсем, в
  отличие от старой архитектуры).

Эти тесты проверяют САМУ гарантию (а не конкретные bypass-пути, которые уже
покрыты test_phase_generate_patch_reject_logging.py,
test_phase_net_delta_decision_logging.py, test_phase_decide_premature_logging.py):
произвольный/гипотетический вызывающий код, который ДАЖЕ НЕ ЗНАЕТ про
append_decision_log, всё равно получает корректную запись в decisions[].
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.stages.needs_review_stage import NeedsReviewStage


def test_add_accepted_patch_always_logs_even_without_explicit_call(tmp_path):
    """Гипотетический НОВЫЙ вызывающий код, который вызывает ТОЛЬКО
    add_accepted_patch и НИКОГДА не слышал про append_decision_log —
    decisions[] всё равно получает запись."""
    ctx = PipelineContext(project_path=tmp_path, language="python")
    error = {"file": "new_stage.py", "line": 7, "code": "X1", "message": "hypothetical"}

    ctx = ctx.add_accepted_patch({"error": error, "reason": "some_future_reason"})

    assert len(ctx.accepted_patches) == 1
    decisions = ctx.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "ACCEPT"
    assert decisions[0]["reason"] == "some_future_reason"
    assert decisions[0]["file"] == "new_stage.py"


def test_add_rejected_patch_always_logs_even_without_explicit_call(tmp_path):
    ctx = PipelineContext(project_path=tmp_path, language="python")
    error = {"file": "new_stage.py", "line": 9, "code": "X2", "message": "hypothetical"}

    ctx = ctx.add_rejected_patch({"error": error, "reason": "some_future_reject_reason"})

    assert len(ctx.rejected_patches) == 1
    decisions = ctx.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "REJECT"
    assert decisions[0]["reason"] == "some_future_reject_reason"


def test_multiple_accept_reject_calls_accumulate_correctly(tmp_path):
    """Несколько вызовов подряд (как в реальном многошаговом прогоне) —
    decisions[] накапливается без потерь и без дублей."""
    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.add_accepted_patch({"error": {"file": "a.py", "line": 1, "code": "A"}, "reason": "r1"})
    ctx = ctx.add_rejected_patch({"error": {"file": "b.py", "line": 2, "code": "B"}, "reason": "r2"})
    ctx = ctx.add_accepted_patch({"error": {"file": "c.py", "line": 3, "code": "C"}, "reason": "r3"})

    decisions = ctx.metadata.get("decisions", [])
    assert len(decisions) == 3
    assert [d["decision"] for d in decisions] == ["ACCEPT", "REJECT", "ACCEPT"]
    assert len(ctx.accepted_patches) == 2
    assert len(ctx.rejected_patches) == 1


def test_needs_review_stage_logs_with_explicit_pending_reason(tmp_path):
    """Вызывающий код устанавливает _needs_review_pending_reason — лог
    использует именно эту причину."""
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "X3", "message": "m"})
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_needs_review_pending_reason": "some_future_nr_reason",
    }))

    result = NeedsReviewStage().execute(ctx)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "NEEDS_REVIEW"
    assert decisions[0]["reason"] == "some_future_nr_reason"
    # Флаг должен быть потреблён (не утекать в следующую итерацию).
    assert "_needs_review_pending_reason" not in result.metadata


def test_needs_review_stage_defensive_fallback_when_reason_not_set(tmp_path):
    """Гипотетический НОВЫЙ вызывающий код, который перешёл в
    State.NEEDS_REVIEW (или напрямую вызвал эту стадию), но ЗАБЫЛ установить
    _needs_review_pending_reason — старая архитектура теряла запись СОВСЕМ;
    новая использует защитный fallback и НЕ теряет её."""
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "X4", "message": "m"})
    # _needs_review_pending_reason намеренно НЕ установлен.

    result = NeedsReviewStage().execute(ctx)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "NEEDS_REVIEW"
    assert decisions[0]["reason"] == "needs_review"  # дефолтный fallback


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
