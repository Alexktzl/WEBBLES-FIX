"""
2026-06-24 (RussellDash332/pytils, прогон через DeepSeek): из 19 записей в
needs_review/ у 17 были пустые "patch"/"intent", но непустое "risks" с
текстом, явно относящимся к СОВЕРШЕННО ДРУГОЙ, ранее обработанной ошибке
(например, F821 "undefined name 'bisect_left'" с risks про "_OLD_POW").

Причина: NextErrorStage очищал structured_edit/intent при переходе к
следующей ошибке ("P1: review leakage", см. комментарий в коде), но забыл
risks/confidence/patch_source — те же поля выставляются ТОЛЬКО при успешном
structured_llm-ответе (generate_patch_stage.py) и никогда не сбрасывались,
если следующая ошибка не доходила до успешной генерации.
"""

from pathlib import Path

from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.stages.next_error_stage import NextErrorStage


def test_next_error_stage_clears_risks_confidence_patch_source(tmp_path):
    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "risks": ["May hide actual type issues if _OLD_POW is not exactly the built-in pow"],
        "confidence": 0.8,
        MetadataKeys.PATCH_SOURCE: "structured_llm_blocking",
        "intent": "some leftover intent",
        "structured_edit": {"foo": "bar"},
    }))
    result = NextErrorStage().execute(ctx)
    assert "risks" not in result.metadata
    assert "confidence" not in result.metadata
    assert MetadataKeys.PATCH_SOURCE not in result.metadata
    assert "intent" not in result.metadata
    assert "structured_edit" not in result.metadata
