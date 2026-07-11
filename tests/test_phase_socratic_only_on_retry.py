"""
2026-06-25: SocraticRefiner.safe_run раньше вызывался БЕЗУСЛОВНО на КАЖДОЙ
попытке генерации патча (включая первую) — 3 доп. LLM-вызова (explanation/
hypothesis/verification) сверх основной генерации, без конфиг-флага для
отключения. По решению пользователя сужено до retry-попыток: на первой
попытке (failure_reason пуст) socratic-обогащение пропускается.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.stages.generate_patch_stage import GeneratePatchStage


def _stage_with_socratic_spy(tmp_path):
    from fixers.patch_engine import PatchEngine

    llm_client = MagicMock()
    llm_client.generate_fix.return_value = (None, "empty_response")
    llm_client.generate_structured_fix.return_value = (None, "empty_response")

    stage = GeneratePatchStage(
        llm_client=llm_client, memory=MagicMock(), patch_engine=PatchEngine(),
    )
    stage.socratic.safe_run = MagicMock(return_value="# Socratic-анализ\n...")
    return stage


def test_first_attempt_skips_socratic_enrichment(tmp_path):
    """failure_reason пуст (первая попытка) — socratic.safe_run НЕ вызывается."""
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    stage = _stage_with_socratic_spy(tmp_path)
    error = {"file": "mod.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    # LAST_PATCH_FAILURE отсутствует -> failure_reason == ""

    stage.execute(ctx)

    stage.socratic.safe_run.assert_not_called()


def test_retry_attempt_with_failure_reason_calls_socratic_enrichment(tmp_path):
    """failure_reason непустой (повторная попытка после неудачи) —
    socratic.safe_run ВЫЗЫВАЕТСЯ (та же ценность рассуждения, что раньше,
    но только когда она реально нужна)."""
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    stage = _stage_with_socratic_spy(tmp_path)
    error = {"file": "mod.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        MetadataKeys.LAST_PATCH_FAILURE: "NET_DELTA regression: +1 новых ошибок",
    }))

    stage.execute(ctx)

    stage.socratic.safe_run.assert_called_once()
