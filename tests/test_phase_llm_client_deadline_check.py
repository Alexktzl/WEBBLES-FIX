"""
2026-06-24: PROJECT_DEADLINE раньше проверялся только МЕЖДУ вызовами
llm_client (GeneratePatchStage._deadline_exceeded перед каждой итерацией
СВОИХ retry-петель), но не ВНУТРИ одного вызова _call_with_retries/
_call_for_json_with_retries — их собственная exponential-backoff петля (до
max_retries попыток по hard_limit=per_request_timeout*1.5 секунд + 2/4/8с
пауз между) могла перерасходовать бюджет на десятки-сотни секунд за ОДИН
вызов, даже если дедлайн уже прошёл к началу второй попытки (control
series 12, investdaytip/html_export.py — тот же класс находки, но для
ВНЕШНЕЙ петли стадии, не для retry внутри самого LLMClient).
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixers.llm_client import LLMClient


def _client():
    cfg = {"llm": {"providers": [{"provider": "deepseek", "model": "m", "api_key": "k"}],
                    "timeout": 60, "max_retries": 3}}
    return LLMClient(config=cfg)


def test_no_deadline_set_does_not_change_behavior():
    """project_deadline=None по умолчанию — старое поведение не меняется."""
    client = _client()
    assert client.project_deadline is None
    assert client._deadline_exceeded() is False


def test_deadline_already_passed_fails_fast_without_any_attempt():
    """Если дедлайн уже прошёл ДО первого вызова — ни одной попытки, без
    единого реального HTTP-вызова."""
    client = _client()
    client.project_deadline = time.monotonic() - 1.0  # уже в прошлом

    calls = {"n": 0}

    def fake_provider_call(self_or_provider, prompt):
        calls["n"] += 1
        return ("should not be called", None)

    with patch.object(LLMClient, "_call_provider_with_prompt", fake_provider_call):
        response, err = client._call_with_retries({"provider": "deepseek"}, "prompt")

    assert response is None
    assert err == "deadline_exceeded"
    assert calls["n"] == 0


def test_deadline_passes_between_attempts_stops_retry_loop():
    """Дедлайн истекает ПОСЛЕ первой неудачной попытки — retry-петля
    должна остановиться, не дожидаясь max_retries попыток."""
    client = _client()
    client.max_retries = 5  # без фикса было бы до 5 попыток

    call_count = {"n": 0}

    def fake_provider_call(self_instance, provider, prompt):
        call_count["n"] += 1
        # после первой неудачной попытки дедлайн "истекает"
        client.project_deadline = time.monotonic() - 0.01
        return (None, "transient_error")

    with patch.object(LLMClient, "_call_provider_with_prompt", fake_provider_call), \
         patch("fixers.llm_client.time.sleep", lambda s: None):  # не спим в тесте
        response, err = client._call_with_retries({"provider": "deepseek"}, "prompt")

    assert response is None
    assert err == "deadline_exceeded"
    assert call_count["n"] == 1, "должна была сделать ровно 1 попытку, не все 5"


def test_json_retry_loop_also_respects_deadline():
    """Тот же фикс для _call_for_json_with_retries (JSON-режим, использует
    generate_structured_fix)."""
    client = _client()
    client.project_deadline = time.monotonic() - 1.0

    calls = {"n": 0}

    def fake_json_call(self_or_provider, system_message, user_prompt):
        calls["n"] += 1
        return ("should not be called", None)

    with patch.object(LLMClient, "_call_provider_for_json", fake_json_call):
        response, err = client._call_for_json_with_retries(
            {"provider": "deepseek"}, "system", "user prompt",
        )

    assert response is None
    assert err == "deadline_exceeded"
    assert calls["n"] == 0


def test_generate_patch_stage_propagates_deadline_to_llm_client(tmp_path):
    """GeneratePatchStage.execute() выставляет llm_client.project_deadline
    из context.metadata[PROJECT_DEADLINE] — единственная точка входа для
    всех вызовов LLM из этой стадии."""
    from core.contract import MetadataKeys
    from core.pipeline_context import PipelineContext
    from core.stages.generate_patch_stage import GeneratePatchStage

    client = _client()
    stage = GeneratePatchStage.__new__(GeneratePatchStage)
    stage.llm_client = client
    stage._segments_cache = {}
    stage._cache_key = None

    deadline = time.monotonic() + 123.0
    # error_class уже задан -> пропускает self.classifier.classify (стаб
    # стадии не имеет classifier). is_unfixable -> True заставляет execute()
    # вернуться сразу ПОСЛЕ выставления project_deadline, не уходя в
    # реальную генерацию патча, которую здесь незачем стабить целиком.
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "m", "error_class": "BLOCKING"}
    from core.utils import error_signature
    sig = error_signature(error)
    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{MetadataKeys.PROJECT_DEADLINE: deadline}))
    ctx = ctx.update(unfixable_attempts={sig: ctx.MAX_UNFIXABLE_ATTEMPTS})

    GeneratePatchStage.execute(stage, ctx)

    assert client.project_deadline == deadline


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
