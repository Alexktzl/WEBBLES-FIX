"""
2026-06-24: WEBBLES_LLM_API_KEY_POOL даёт каждому параллельному потоку
свой LLMClient (свой api_key, свой изолированный circuit breaker) — без
этого все потоки делили ОДИН ключ, конкурентные вызовы упирались в общий
rate-limit провайдера (см. живой бенчмарк Bluetooth-Devices/dbus-fast: 14
LLM hard timeout с одним общим ключом на 4 потока).

Находка при живом прогоне (не баг бенчмарка, баг build_llm_client_pool):
конфиги без llm.providers (старый формат provider/model прямо под "llm",
или минимальные тестовые конфиги) приводили к клиентам с providers=[] —
бесполезным, но тихо "успешным" записям в пуле.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from fixers.llm_client import LLMClient, build_llm_client_pool


def _set_pool_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WEBBLES_LLM_API_KEY_POOL", raising=False)
    else:
        monkeypatch.setenv("WEBBLES_LLM_API_KEY_POOL", value)


def test_no_pool_env_returns_single_client(monkeypatch):
    _set_pool_env(monkeypatch, None)
    cfg = {"llm": {"providers": [{"provider": "deepseek", "model": "m", "api_key": "primary-key"}]}}
    primary = LLMClient(config=cfg)
    pool = build_llm_client_pool(cfg, primary_client=primary)
    assert pool == [primary]


def test_pool_env_builds_distinct_clients_with_own_keys(monkeypatch):
    _set_pool_env(monkeypatch, "key-1,key-2")
    cfg = {"llm": {"providers": [{"provider": "deepseek", "model": "m", "api_key": "primary-key"}]}}
    primary = LLMClient(config=cfg)
    pool = build_llm_client_pool(cfg, primary_client=primary)

    assert len(pool) == 3
    assert pool[0] is primary
    assert pool[0].providers[0]["api_key"] == "primary-key"
    assert pool[1].providers[0]["api_key"] == "key-1"
    assert pool[2].providers[0]["api_key"] == "key-2"
    # provider/model сохранены такими же, как у основного клиента.
    assert pool[1].providers[0]["provider"] == "deepseek"
    assert pool[1].providers[0]["model"] == "m"


def test_pool_env_with_no_providers_in_config_falls_back_to_primary_only(monkeypatch):
    """2026-06-24 (найдено живым прогоном): конфиг без llm.providers (старый
    формат/минимальный тестовый конфиг, см. test_pipeline_engine_resume_config.py)
    — нет шаблона для клонирования с другим api_key. Раньше это тихо
    создавало клиентов с providers=[] (бесполезных). Теперь пул не строится
    вовсе — возвращается только primary."""
    _set_pool_env(monkeypatch, "key-1,key-2,key-3")
    cfg = {"llm": {"timeout": 5}}  # старый/минимальный формат, без "providers"
    primary = LLMClient(config=cfg)
    pool = build_llm_client_pool(cfg, primary_client=primary)

    # Главное: НЕ строятся 3 лишних клиента (раньше — с providers=[]).
    # primary как есть (его собственные providers — не предмет этого теста).
    assert pool == [primary]
    assert len(pool) == 1


def test_pool_clients_never_have_empty_providers(monkeypatch):
    """Регрессия: ни один клиент в пуле не должен иметь providers=[] —
    такой клиент тихо "успешен" при создании, но бесполезен для реальных
    вызовов (см. docstring модуля)."""
    _set_pool_env(monkeypatch, "key-1,key-2")
    cfg = {"llm": {"providers": [{"provider": "deepseek", "model": "m", "api_key": "primary-key"}]}}
    primary = LLMClient(config=cfg)
    pool = build_llm_client_pool(cfg, primary_client=primary)
    for client in pool:
        assert client.providers, f"клиент в пуле с пустым providers: {client}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
