"""
2026-06-24: при разборе needs_review-очереди на RussellDash332/pytils 15 из
19 записей оказались каскадными ошибками (например, F821), до которых LLM
не дошла вовсе — пустой patch/intent. NeedsReviewStage теперь различает
такие записи (has_patch=False) от записей с реальной попыткой фикса
(has_patch=True), и считает их отдельными счётчиками в metadata
(needs_review_with_patch_count / needs_review_no_patch_count) — источник
для PipelineEngine.compute_progress_metrics() (attempted_errors_count).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pipeline_context import PipelineContext
from core.stages.needs_review_stage import NeedsReviewStage


def test_no_patch_when_generated_patch_and_structured_edit_empty(tmp_path):
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 7, "code": "F821", "message": "undefined name 'x'"})
    ctx = ctx.set_patch(None)

    result = NeedsReviewStage().execute(ctx)

    assert result.metadata.get("needs_review_no_patch_count") == 1
    assert result.metadata.get("needs_review_with_patch_count", 0) == 0
    items = result.metadata.get("needs_review_items", [])
    assert items[-1]["has_patch"] is False


def test_has_patch_when_generated_patch_present(tmp_path):
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "b.py", "line": 3, "code": "assignment", "message": "m"})
    ctx = ctx.set_patch("--- a/b.py\n+++ b/b.py\n@@\n-x = 1\n+x: int = 1\n")

    result = NeedsReviewStage().execute(ctx)

    assert result.metadata.get("needs_review_with_patch_count") == 1
    assert result.metadata.get("needs_review_no_patch_count", 0) == 0
    items = result.metadata.get("needs_review_items", [])
    assert items[-1]["has_patch"] is True


def test_has_patch_when_only_structured_edit_present_no_raw_patch(tmp_path):
    """structured_edit без raw patch-строки (типичный путь structured_llm) —
    всё равно считается has_patch=True."""
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "c.py", "line": 5, "code": "call-overload", "message": "m"})
    ctx = ctx.set_patch(None)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "structured_edit": {"intent": "fix", "edits": []},
    }))

    result = NeedsReviewStage().execute(ctx)

    assert result.metadata.get("needs_review_with_patch_count") == 1
    items = result.metadata.get("needs_review_items", [])
    assert items[-1]["has_patch"] is True


def test_with_patch_and_no_patch_counts_accumulate_independently(tmp_path):
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)

    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "F821", "message": "m"})
    ctx = ctx.set_patch(None)
    ctx = NeedsReviewStage().execute(ctx)

    ctx = ctx.set_selected_error({"file": "b.py", "line": 2, "code": "assignment", "message": "m"})
    ctx = ctx.set_patch("some patch")
    ctx = NeedsReviewStage().execute(ctx)

    ctx = ctx.set_selected_error({"file": "c.py", "line": 3, "code": "F821", "message": "m"})
    ctx = ctx.set_patch(None)
    ctx = NeedsReviewStage().execute(ctx)

    assert ctx.metadata.get("needs_review_no_patch_count") == 2
    assert ctx.metadata.get("needs_review_with_patch_count") == 1
    assert ctx.metadata.get("needs_review_count") == 3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
