"""
2026-06-24 (control series на 10 проектах, найдено на pluggy/jsonschema/
tenacity — все три застряли с "180s exceeded (HTTP timeout=120s)" и
"попытка X/3" вместо настроенных llm.timeout=60/max_retries=2):

`Controller.run_pipeline` строил `LLMClient(config.get("llm", {}))` —
передавал ТОЛЬКО распакованный под-словарь "llm" как ВЕСЬ config.
Внутри `LLMClient.__init__` код читает `self.config.get("llm", {}).get(
"timeout", 120)` — ищет ключ "llm" ВНУТРИ уже распакованного словаря
"llm", не находит, и ВСЕГДА возвращает дефолты (120s/3 retries/
unresponsive_threshold=5), независимо от настроек webles_config.json.
`_load_providers()` не задет только благодаря отдельному
defensive-fallback на `self.config.get("providers", [])` — для timeout/
max_retries/unresponsive_threshold такого fallback'а нет.

ВЕСЬ продакшен-путь (run_agent.py → run_fix_agent → Controller.run_pipeline)
игнорировал llm.timeout/llm.max_retries с момента появления этих настроек.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.controller import Controller


def test_run_pipeline_passes_full_config_to_llm_client(tmp_path, monkeypatch):
    """LLMClient должен получить ПОЛНЫЙ config (с вложенным "llm"), не
    распакованный config["llm"] — иначе timeout/max_retries/
    unresponsive_threshold молча откатываются на дефолты 120/3/5."""
    captured = {}

    class _FakeLLMClient:
        def __init__(self, config, language_provider=None):
            captured["config"] = config
        language_provider = None

    monkeypatch.setattr("fixers.llm_client.LLMClient", _FakeLLMClient)

    controller = Controller.__new__(Controller)
    controller.reporter = MagicMock()
    controller.pipeline_engine = None
    controller.pipeline_running = False
    controller._recover_dependencies = MagicMock()
    controller._extract_secrets = MagicMock()

    config = {
        "llm": {
            "providers": [{"provider": "deepseek", "model": "m", "api_key": "k"}],
            "timeout": 60,
            "max_retries": 2,
        },
        "pipeline": {},
        "dry_run": True,
    }

    with patch("core.pipeline_engine.PipelineEngine") as fake_engine_cls:
        fake_engine = MagicMock()
        fake_engine.run.return_value = {"status": "COMPLETED"}
        fake_engine_cls.return_value = fake_engine
        controller.run_pipeline(tmp_path, "python", config)

    # Ключевая проверка: config, переданный в LLMClient, содержит ВЛОЖЕННЫЙ
    # "llm" ключ (т.е. это ПОЛНЫЙ config, а не его распакованный под-словарь).
    assert "llm" in captured["config"], (
        "LLMClient получил config без вложенного ключа 'llm' — значит ему "
        "передали распакованный config['llm'] вместо полного config, и "
        "timeout/max_retries/unresponsive_threshold молча откатятся на "
        "дефолты (120/3/5) независимо от настроек webles_config.json"
    )
    assert captured["config"]["llm"]["timeout"] == 60
    assert captured["config"]["llm"]["max_retries"] == 2


def test_llm_client_reads_correct_timeout_from_full_config():
    """Прямая проверка через настоящий LLMClient (не мок) — воспроизводит
    ровно баг из расследования: per_request_timeout/max_retries должны
    совпадать с config, а не откатываться на 120/3."""
    from fixers.llm_client import LLMClient

    config = {
        "llm": {
            "providers": [{"provider": "deepseek", "model": "m", "api_key": "k"}],
            "timeout": 60,
            "max_retries": 2,
            "unresponsive_threshold": 5,
        },
    }
    client = LLMClient(config)
    assert client.per_request_timeout == 60
    assert client.max_retries == 2

    # Контроль: воспроизводим СТАРЫЙ (баговый) вызов — должен давать дефолты,
    # подтверждая, что мы тестируем именно то расхождение, которое было
    # причиной находки.
    broken_client = LLMClient(config.get("llm", {}))
    assert broken_client.per_request_timeout == 120
    assert broken_client.max_retries == 3
