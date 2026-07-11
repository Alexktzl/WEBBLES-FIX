"""
empty_response диагностика (2026-06-22), точечные фиксы для локальной LLM:

1. Pre-flight health-check — если локальный сервер недоступен, узнаём об
   этом за секунды (а не после серии retry с exponential backoff на каждую
   ошибку всего прогона) и взводим circuit breaker заранее.
2. Guard по max_input_tokens — обрезаем входной промпт ДО отправки, если он
   превышает context_window, вместо того чтобы сервер молча обрезал контекст
   или вернул пустой ответ (категория "empty").
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fixers.llm_client import LLMClient
from fixers.local_llm_provider import LocalLLMProvider


# ---------------------------------------------------------------------------
# Guard по max_input_tokens
# ---------------------------------------------------------------------------

def _provider(context_window=1000, max_tokens=100):
    return LocalLLMProvider(
        base_url="http://localhost:8080/v1", model="x", profile="patch",
        context_window=context_window, max_tokens_override=max_tokens,
    )


def test_enforce_input_budget_no_truncation_when_within_budget():
    p = _provider(context_window=10_000, max_tokens=100)
    sys_msg, user = p._enforce_input_budget("system", "short prompt")
    assert sys_msg == "system"
    assert user == "short prompt"


def test_enforce_input_budget_truncates_when_over_budget():
    p = _provider(context_window=200, max_tokens=10)
    # max_input_chars будет маленьким — длинный user_prompt должен обрезаться.
    long_prompt = "x" * 5000
    sys_msg, user = p._enforce_input_budget("sys", long_prompt)
    assert len(user) < len(long_prompt)
    assert len("sys") + len(user) <= p.max_input_chars


def test_enforce_input_budget_keeps_system_message_intact():
    p = _provider(context_window=200, max_tokens=10)
    long_prompt = "x" * 5000
    sys_msg, _ = p._enforce_input_budget("system unchanged", long_prompt)
    assert sys_msg == "system unchanged"


def test_enforce_input_budget_pathological_tiny_budget():
    """Даже при бюджете меньше длины system_message — не должно падать,
    возвращает пустой/максимально урезанный user_prompt."""
    p = _provider(context_window=1, max_tokens=0)
    sys_msg, user = p._enforce_input_budget("a very long system message here", "user text")
    assert isinstance(user, str)
    assert len(user) >= 0


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

def test_health_check_returns_false_on_unreachable_server():
    p = LocalLLMProvider(base_url="http://localhost:1", model="x", profile="patch")
    assert p.health_check(timeout=1.0) is False


def test_health_check_returns_true_on_success(monkeypatch):
    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _FakeResp(),
    )
    p = LocalLLMProvider(base_url="http://localhost:8080/v1", model="x", profile="patch")
    assert p.health_check(timeout=1.0) is True


# ---------------------------------------------------------------------------
# Pre-flight в LLMClient.__init__
# ---------------------------------------------------------------------------

def test_preflight_trips_circuit_breaker_when_local_server_down():
    cfg = {
        "llm": {
            "providers": [{"provider": "local", "model": "x", "base_url": "http://localhost:1"}],
            "unresponsive_threshold": 3,
        }
    }
    client = LLMClient(config=cfg)
    assert client.is_unresponsive() is True
    assert client.consecutive_hard_timeouts == 3


def test_preflight_does_not_trip_when_server_up(monkeypatch):
    monkeypatch.setattr(
        "fixers.local_llm_provider.LocalLLMProvider.health_check",
        lambda self, timeout=5.0: True,
    )
    cfg = {
        "llm": {
            "providers": [{"provider": "local", "model": "x", "base_url": "http://localhost:8080/v1"}],
            "unresponsive_threshold": 3,
        }
    }
    client = LLMClient(config=cfg)
    assert client.is_unresponsive() is False
    assert client.consecutive_hard_timeouts == 0


def test_preflight_skipped_for_remote_providers():
    """openai/deepseek/anthropic — не пингуются (см. docstring
    _preflight_health_check), не должно быть сетевого вызова/исключения."""
    cfg = {
        "llm": {
            "providers": [{"provider": "deepseek", "model": "x", "api_key": "k"}],
        }
    }
    client = LLMClient(config=cfg)
    assert client.is_unresponsive() is False


def test_preflight_ignores_unused_local_provider_further_in_list():
    """2026-06-24: реально используется только providers[0] (см.
    generate_structured_fix/_call_llm — все берут self.providers[0]).
    Раньше _preflight_health_check проверял ВСЕ записи списка — если
    providers[0] был исправным облачным провайдером, а providers[1] —
    неиспользуемый "local" с мёртвым сервером, health-check ОШИБОЧНО
    взводил общий circuit breaker, и реально используемый providers[0]
    fail-fast'ился без единой попытки."""
    cfg = {
        "llm": {
            "providers": [
                {"provider": "deepseek", "model": "x", "api_key": "k"},
                {"provider": "local", "model": "y", "base_url": "http://localhost:1"},
            ],
            "unresponsive_threshold": 3,
        }
    }
    client = LLMClient(config=cfg)
    assert client.is_unresponsive() is False
    assert client.consecutive_hard_timeouts == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
