"""
LocalLLMProvider — регрессия профилей, JSON-fallback, конфиг-интеграция.

Покрывает:
  1. Профили: defaults (json_mode, temperature, max_tokens)
  2. Невалидный профиль → ValueError
  3. _extract_first_json: чистый JSON, JSON в ```-фенсах, JSON с текстом вокруг,
     вложенные объекты, массивы, отсутствие JSON, пустая строка
  4. as_pipeline_provider_config(): структура dict, provider="local"
  5. as_chat_callable(): возвращает callable с правильной сигнатурой
  6. LLMClient._call_provider_for_json: роутинг "local" не падает
  7. LLMClient._call_provider_with_prompt: роутинг "local" не падает
  8. LLMClient: существующие провайдеры (openai/deepseek/anthropic/ollama)
     не задеты — счётчик роутинга совпадает с ожидаемым
  9. make_local_provider(): параметры переопределяют ENV
  10. make_local_chat(): возвращает callable
  11. make_default_chat(): WEBBLES_LOCAL_LLM_URL переключает на local
  12. make_default_chat(): WEBBLES_CHAT_BASE_URL имеет приоритет над local
  13. LocalLLMProvider.complete_json: fallback extraction используется
  14. LocalLLMProvider.complete_json: None если сервер упал
  15. LocalLLMProvider._post_chat: добавляет response_format только для json_mode

Запуск: python tests/test_phase_local_llm.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# Заглушки тяжёлых зависимостей, которые тянут openai/anthropic SDK
for _stub in ("openai", "anthropic", "httpx"):
    if _stub not in sys.modules:
        import types as _t
        sys.modules[_stub] = _t.ModuleType(_stub)

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types as _t
    sys.modules["tomlkit"] = _t.ModuleType("tomlkit")

from fixers.local_llm_provider import (
    LOCAL_LLM_CONTEXT_WINDOW,
    PROFILE_DEFAULTS,
    VALID_PROFILES,
    LocalLLMProvider,
    _CHARS_PER_TOKEN,
    _CTK_THINKING_OFF,
    _CTK_THINKING_ON,
    _NO_THINKING_SYSTEM_PREFIX,
    _PROFILE_SYS_OVERHEAD,
    _extract_content,
    _extract_first_json,
    make_local_provider,
)
from agent.chat_llm import make_default_chat, make_local_chat

results: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK " if ok else "FAIL"
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  [{mark}] {name}{suffix}")
    results.append((name, ok))


# ---------------------------------------------------------------------------
# 1–2. Профили
# ---------------------------------------------------------------------------

def test_profiles_defaults():
    for profile in ("patch", "review", "chat", "architect"):
        d = PROFILE_DEFAULTS[profile]
        expected_json = profile in ("patch", "review")
        check(
            f"profile_{profile}_json_mode",
            d["json_mode"] == expected_json,
            f"got json_mode={d['json_mode']}",
        )
        check(f"profile_{profile}_has_system", bool(d.get("system")))
        check(f"profile_{profile}_has_temperature", isinstance(d.get("temperature"), float))
        check(f"profile_{profile}_has_max_tokens", isinstance(d.get("max_tokens"), int))


def test_profile_invalid():
    try:
        LocalLLMProvider(base_url="http://localhost:8080/v1", model="x", profile="unknown")
        check("invalid_profile_raises", False, "no exception raised")
    except ValueError:
        check("invalid_profile_raises", True)


def test_profile_overrides():
    p = LocalLLMProvider(
        base_url="http://localhost:1234/v1",
        model="llama",
        profile="patch",
        json_mode_override=False,
        temperature_override=0.9,
        max_tokens_override=512,
    )
    check("override_json_mode", p._json_mode is False)
    check("override_temperature", p._temperature == 0.9)
    check("override_max_tokens", p._max_tokens == 512)


# ---------------------------------------------------------------------------
# 3. _extract_first_json
# ---------------------------------------------------------------------------

def test_extract_json_clean():
    obj = {"intent": "fix", "edits": []}
    check("extract_clean_object", _extract_first_json(json.dumps(obj)) is not None)
    arr = [1, 2, 3]
    check("extract_clean_array", _extract_first_json(json.dumps(arr)) is not None)


def test_extract_json_fenced():
    text = '```json\n{"a": 1}\n```'
    got = _extract_first_json(text)
    check("extract_fenced_json", got is not None and json.loads(got)["a"] == 1)

    text2 = '```\n{"b": 2}\n```'
    got2 = _extract_first_json(text2)
    check("extract_fenced_plain", got2 is not None)


def test_extract_json_surrounded():
    text = 'Here is the result:\n{"verdict": "ok", "reasons": []}\nEnd.'
    got = _extract_first_json(text)
    check(
        "extract_surrounded",
        got is not None and json.loads(got)["verdict"] == "ok",
        repr(got),
    )


def test_extract_json_nested():
    text = 'prefix {"outer": {"inner": [1, 2]}} suffix'
    got = _extract_first_json(text)
    check(
        "extract_nested",
        got is not None and json.loads(got)["outer"]["inner"] == [1, 2],
    )


def test_extract_json_no_json():
    check("extract_no_json", _extract_first_json("no json here at all") is None)
    check("extract_empty", _extract_first_json("") is None)
    check("extract_none_str", _extract_first_json(None) is None)  # type: ignore[arg-type]


def test_extract_json_broken():
    check("extract_broken", _extract_first_json('{"a": missing_quote}') is None)


def test_extract_json_first_wins():
    text = '{"first": 1} and {"second": 2}'
    got = _extract_first_json(text)
    check(
        "extract_first_wins",
        got is not None and json.loads(got).get("first") == 1,
    )


# ---------------------------------------------------------------------------
# 4. as_pipeline_provider_config
# ---------------------------------------------------------------------------

def test_pipeline_config():
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="mistral-7b",
        profile="patch",
    )
    cfg = p.as_pipeline_provider_config()
    check("pipeline_cfg_provider_local", cfg["provider"] == "local")
    check("pipeline_cfg_model", cfg["model"] == "mistral-7b")
    check("pipeline_cfg_base_url", cfg["base_url"] == "http://localhost:8080/v1")
    check("pipeline_cfg_profile", cfg["profile"] == "patch")
    check("pipeline_cfg_has_max_tokens", isinstance(cfg.get("max_tokens"), int))
    check("pipeline_cfg_name_contains_profile", "patch" in cfg.get("name", ""))


def test_pipeline_config_review():
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="qwen",
        profile="review",
    )
    cfg = p.as_pipeline_provider_config()
    check("pipeline_cfg_review_profile", cfg["profile"] == "review")
    check("pipeline_cfg_review_max_tokens", cfg["max_tokens"] == PROFILE_DEFAULTS["review"]["max_tokens"])


# ---------------------------------------------------------------------------
# 5. as_chat_callable
# ---------------------------------------------------------------------------

def test_chat_callable_signature():
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="chat",
    )
    fn = p.as_chat_callable()
    check("chat_callable_is_callable", callable(fn))
    import inspect
    sig = inspect.signature(fn)
    # должны принимать messages и tools как позиционный/keyword
    check("chat_callable_accepts_messages", "messages" in sig.parameters)
    check("chat_callable_accepts_tools", "tools" in sig.parameters)


# ---------------------------------------------------------------------------
# 6–7. LLMClient роутинг "local"
# ---------------------------------------------------------------------------

def test_llmclient_local_json_routing():
    from fixers.llm_client import LLMClient
    client = LLMClient.__new__(LLMClient)
    client.per_request_timeout = 30

    called = []

    def fake_call_local_json(config, sys_msg, user_prompt):
        called.append(("json", config["provider"]))
        return '{"intent":"x","edits":[],"confidence":0.9,"risks":[]}'

    client._call_local_json = fake_call_local_json
    config = {
        "provider": "local",
        "model": "llama",
        "base_url": "http://localhost:8080/v1",
        "profile": "patch",
    }
    result, err = client._call_provider_for_json(config, "sys", "user")
    check("local_json_routing_called", len(called) == 1 and called[0][0] == "json")
    check("local_json_routing_no_err", err is None)
    check("local_json_routing_result", result is not None)


def test_llmclient_local_text_routing():
    from fixers.llm_client import LLMClient
    client = LLMClient.__new__(LLMClient)
    client.per_request_timeout = 30

    called = []

    def fake_call_local_text(config, prompt):
        called.append(("text", config["provider"]))
        return "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new"

    client._call_local_text = fake_call_local_text
    config = {
        "provider": "local",
        "model": "llama",
        "base_url": "http://localhost:8080/v1",
        "profile": "patch",
    }
    result, err = client._call_provider_with_prompt(config, "fix this")
    check("local_text_routing_called", len(called) == 1 and called[0][0] == "text")
    check("local_text_routing_no_err", err is None)
    check("local_text_routing_result", result is not None)


# ---------------------------------------------------------------------------
# 8. Существующие провайдеры не задеты
# ---------------------------------------------------------------------------

def test_existing_providers_unaffected():
    from fixers.llm_client import LLMClient
    client = LLMClient.__new__(LLMClient)
    client.per_request_timeout = 30

    routed = []

    for name, method in (
        ("_call_openai_json", "openai"),
        ("_call_deepseek_json", "deepseek"),
        ("_call_anthropic_json", "anthropic"),
        ("_call_ollama_json", "ollama"),
    ):
        def _fake(cfg, s, u, _n=name):
            routed.append(_n)
            return "{}"
        setattr(client, name, _fake)

    for provider_name in ("openai", "deepseek", "anthropic", "ollama"):
        routed.clear()
        client._call_provider_for_json(
            {"provider": provider_name, "model": "x"}, "s", "u"
        )
        check(
            f"existing_provider_{provider_name}_routed",
            len(routed) == 1,
            f"routed={routed}",
        )


def test_unknown_provider_returns_error():
    from fixers.llm_client import LLMClient
    client = LLMClient.__new__(LLMClient)
    client.per_request_timeout = 30
    result, err = client._call_provider_for_json(
        {"provider": "fictional_provider_xyz", "model": "x"}, "s", "u"
    )
    check("unknown_provider_returns_none", result is None)
    check("unknown_provider_has_err", err is not None)


# ---------------------------------------------------------------------------
# 9. make_local_provider
# ---------------------------------------------------------------------------

def test_make_local_provider_explicit():
    p = make_local_provider(
        profile="review",
        base_url="http://localhost:1234/v1",
        model="phi3",
        api_key="sk-test",
        timeout=30.0,
    )
    check("make_provider_profile", p.profile == "review")
    check("make_provider_base_url", p.base_url == "http://localhost:1234/v1")
    check("make_provider_model", p.model == "phi3")
    check("make_provider_api_key", p.api_key == "sk-test")
    check("make_provider_timeout", p.timeout == 30.0)


def test_make_local_provider_from_env():
    import os
    old = {}
    for k, v in (
        ("WEBBLES_LOCAL_LLM_URL", "http://localhost:9999/v1"),
        ("WEBBLES_LOCAL_LLM_MODEL", "gemma-2"),
        ("WEBBLES_LOCAL_LLM_KEY", "sk-env"),
    ):
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        p = make_local_provider(profile="chat")
        check("make_provider_env_url", p.base_url == "http://localhost:9999/v1")
        check("make_provider_env_model", p.model == "gemma-2")
        check("make_provider_env_key", p.api_key == "sk-env")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 10. make_local_chat
# ---------------------------------------------------------------------------

def test_make_local_chat():
    fn = make_local_chat(
        base_url="http://localhost:8080/v1",
        model="llama",
    )
    check("make_local_chat_callable", callable(fn))


# ---------------------------------------------------------------------------
# 11–12. make_default_chat ENV routing
# ---------------------------------------------------------------------------

def test_make_default_chat_local_override():
    import os
    old_local = os.environ.get("WEBBLES_LOCAL_LLM_URL")
    old_chat = os.environ.get("WEBBLES_CHAT_BASE_URL")
    os.environ["WEBBLES_LOCAL_LLM_URL"] = "http://localhost:8080/v1"
    os.environ.pop("WEBBLES_CHAT_BASE_URL", None)
    try:
        fn = make_default_chat()
        check("default_chat_local_callable", callable(fn))
        # провайдер должен быть LocalLLMProvider
        import inspect
        closure_vars = []
        if hasattr(fn, "__closure__") and fn.__closure__:
            for cell in fn.__closure__:
                try:
                    closure_vars.append(cell.cell_contents)
                except ValueError:
                    pass
        found_local = any(
            isinstance(v, LocalLLMProvider) for v in closure_vars
        )
        check("default_chat_local_provider_used", found_local)
    finally:
        if old_local is None:
            os.environ.pop("WEBBLES_LOCAL_LLM_URL", None)
        else:
            os.environ["WEBBLES_LOCAL_LLM_URL"] = old_local
        if old_chat is not None:
            os.environ["WEBBLES_CHAT_BASE_URL"] = old_chat


def test_make_default_chat_chat_url_priority():
    """WEBBLES_CHAT_BASE_URL должен иметь приоритет над WEBBLES_LOCAL_LLM_URL."""
    import os
    saved = {}
    for k in ("WEBBLES_LOCAL_LLM_URL", "WEBBLES_CHAT_BASE_URL", "WEBBLES_CHAT_API_KEY"):
        saved[k] = os.environ.get(k)
    os.environ["WEBBLES_LOCAL_LLM_URL"] = "http://localhost:8080/v1"
    os.environ["WEBBLES_CHAT_BASE_URL"] = "https://api.deepseek.com/v1"
    os.environ.pop("WEBBLES_CHAT_API_KEY", None)
    try:
        fn = make_default_chat()
        # Должен быть HTTPChatLLM (не LocalLLMProvider), т.к. CHAT_BASE_URL задан
        import inspect
        closure_vars = []
        if hasattr(fn, "__closure__") and fn.__closure__:
            for cell in fn.__closure__:
                try:
                    closure_vars.append(cell.cell_contents)
                except ValueError:
                    pass
        found_local = any(isinstance(v, LocalLLMProvider) for v in closure_vars)
        check("chat_url_priority_not_local", not found_local)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 13–14. complete_json с mock _do_request
# ---------------------------------------------------------------------------

def test_complete_json_extracts_wrapped():
    """complete_json должен извлечь JSON из ответа с лишним текстом."""
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="patch",
    )
    raw_response = json.dumps({
        "choices": [{
            "message": {
                "content": 'Sure! Here:\n```json\n{"intent":"fix","edits":[],"confidence":0.9,"risks":[]}\n```\nDone.'
            }
        }]
    })
    with patch.object(p, "_do_request", return_value=raw_response):
        result = p.complete_json("sys", "user")
    check("complete_json_wrapped_extracts", result is not None)
    if result:
        parsed = json.loads(result)
        check("complete_json_wrapped_intent", parsed.get("intent") == "fix")


def test_complete_json_server_down():
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="patch",
    )
    with patch.object(p, "_do_request", return_value=None):
        result = p.complete_json("sys", "user")
    check("complete_json_server_down_none", result is None)


def test_complete_text_server_down():
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="patch",
    )
    with patch.object(p, "_do_request", return_value=None):
        result = p.complete_text("prompt")
    check("complete_text_server_down_none", result is None)


# ---------------------------------------------------------------------------
# 15. _post_chat добавляет response_format только для json_mode
# ---------------------------------------------------------------------------

def test_post_chat_json_mode_adds_response_format():
    p_patch = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="patch",
    )
    captured = {}

    def fake_request(path, payload):
        captured.update(payload)
        return json.dumps({
            "choices": [{"message": {"content": "{}"}}]
        })

    with patch.object(p_patch, "_do_request", side_effect=fake_request):
        p_patch._post_chat(system_message="s", user_prompt="u", force_json=True)
    check(
        "post_chat_patch_has_response_format",
        captured.get("response_format") == {"type": "json_object"},
    )


def test_post_chat_text_mode_no_response_format():
    p_chat = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="chat",
    )
    captured = {}

    def fake_request(path, payload):
        captured.update(payload)
        return json.dumps({
            "choices": [{"message": {"content": "Hello!"}}]
        })

    with patch.object(p_chat, "_do_request", side_effect=fake_request):
        p_chat._post_chat(system_message="s", user_prompt="u", force_json=True)
    check(
        "post_chat_chat_no_response_format",
        "response_format" not in captured,
    )


def test_post_chat_architect_no_response_format():
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="architect",
    )
    captured = {}

    def fake_request(path, payload):
        captured.update(payload)
        return json.dumps({
            "choices": [{"message": {"content": "Here is the plan..."}}]
        })

    with patch.object(p, "_do_request", side_effect=fake_request):
        p._post_chat(system_message="s", user_prompt="u", force_json=False)
    check(
        "post_chat_architect_no_response_format",
        "response_format" not in captured,
    )


# ---------------------------------------------------------------------------
# _extract_content: явная фильтрация reasoning_content / __verbose
# ---------------------------------------------------------------------------

def test_extract_content_plain():
    msg = {"role": "assistant", "content": "hello"}
    check("extract_content_plain", _extract_content(msg) == "hello")


def test_extract_content_ignores_reasoning():
    msg = {
        "role": "assistant",
        "content": '{"intent":"fix"}',
        "reasoning_content": "<think>lots of reasoning</think>",
    }
    result = _extract_content(msg)
    check("extract_content_ignores_reasoning", result == '{"intent":"fix"}')
    check("extract_content_no_reasoning_bleed", "think" not in result)


def test_extract_content_ignores_verbose():
    msg = {
        "role": "assistant",
        "content": "plain text answer",
        "__verbose": {"tokens": 99, "latency": 1.2},
    }
    result = _extract_content(msg)
    check("extract_content_ignores_verbose", result == "plain text answer")


def test_extract_content_none_content():
    msg = {"role": "assistant", "content": None}
    check("extract_content_none_is_empty_str", _extract_content(msg) == "")


def test_extract_content_int_content():
    msg = {"role": "assistant", "content": 42}
    check("extract_content_coerces_to_str", _extract_content(msg) == "42")


def test_extract_content_only_reasoning():
    """Если content отсутствует/None при наличии reasoning — возвращаем ""."""
    msg = {"role": "assistant", "content": None, "reasoning_content": "deep thoughts"}
    check("extract_content_only_reasoning_empty", _extract_content(msg) == "")


# ---------------------------------------------------------------------------
# disable_thinking — профили, API-параметр, системный префикс
# ---------------------------------------------------------------------------

def test_disable_thinking_defaults():
    for profile, expected in (("patch", True), ("review", True),
                               ("chat", False), ("architect", False)):
        p = LocalLLMProvider(base_url="http://x/v1", model="m", profile=profile)
        check(
            f"disable_thinking_default_{profile}",
            p._disable_thinking == expected,
            f"got {p._disable_thinking}",
        )


def test_patch_max_tokens_8000():
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="patch")
    check("patch_max_tokens_8000", p._max_tokens == 8000)


def test_review_max_tokens_8000():
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="review")
    check("review_max_tokens_8000", p._max_tokens == 8000)


def test_thinking_disabled_adds_api_param():
    """patch: _post_chat должен добавить chat_template_kwargs={enable_thinking:False}."""
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="patch")
    captured: list = []

    def fake_request(path, payload):
        captured.append(dict(payload))
        return json.dumps({"choices": [{"finish_reason": "stop",
                                        "message": {"content": "{}"}}]})

    with patch.object(p, "_do_request", side_effect=fake_request):
        p._post_chat(system_message="sys", user_prompt="user", force_json=True)

    check("thinking_api_param_present", len(captured) == 1)
    check(
        "thinking_ctk_value",
        captured[0].get("chat_template_kwargs") == _CTK_THINKING_OFF,
        f"got {captured[0].get('chat_template_kwargs')}",
    )
    check("thinking_no_reasoning_format_leak", "reasoning_format" not in captured[0])
    check("thinking_old_param_absent", "thinking" not in captured[0])


def test_thinking_disabled_prefixes_system():
    """patch: системное сообщение должно начинаться с _NO_THINKING_SYSTEM_PREFIX."""
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="patch")
    captured: list = []

    def fake_request(path, payload):
        captured.append(payload["messages"][0]["content"])
        return json.dumps({"choices": [{"finish_reason": "stop",
                                        "message": {"content": "{}"}}]})

    with patch.object(p, "_do_request", side_effect=fake_request):
        p._post_chat(system_message="original system", user_prompt="u", force_json=True)

    check("thinking_prefix_present", len(captured) == 1)
    sys_content = captured[0]
    check(
        "thinking_prefix_starts_correct",
        sys_content.startswith(_NO_THINKING_SYSTEM_PREFIX),
        f"got: {sys_content[:80]}",
    )
    check("thinking_prefix_keeps_original", "original system" in sys_content)


def test_thinking_enabled_no_api_param():
    """chat: payload НЕ должен содержать поле thinking; chat_template_kwargs={enable_thinking:True}."""
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="chat")
    captured: list = []

    def fake_request(path, payload):
        captured.append(dict(payload))
        return json.dumps({"choices": [{"finish_reason": "stop",
                                        "message": {"content": "hello"}}]})

    with patch.object(p, "_do_request", side_effect=fake_request):
        p._post_chat(system_message="sys", user_prompt="user", force_json=False)

    check("chat_no_old_thinking_param", "thinking" not in captured[0])
    check(
        "chat_ctk_thinking_on",
        captured[0].get("chat_template_kwargs") == _CTK_THINKING_ON,
        f"got {captured[0].get('chat_template_kwargs')}",
    )
    check("chat_no_reasoning_format_override", "reasoning_format" not in captured[0])


def test_thinking_override():
    """disable_thinking_override=False → patch получает enable_thinking=True, без префикса."""
    p = LocalLLMProvider(
        base_url="http://x/v1", model="m", profile="patch",
        disable_thinking_override=False,
    )
    captured: list = []

    def fake_request(path, payload):
        captured.append(dict(payload))
        return json.dumps({"choices": [{"finish_reason": "stop",
                                        "message": {"content": "{}"}}]})

    with patch.object(p, "_do_request", side_effect=fake_request):
        p._post_chat(system_message="sys", user_prompt="user", force_json=True)

    check("override_false_no_old_thinking_param", "thinking" not in captured[0])
    check(
        "override_false_ctk_thinking_on",
        captured[0].get("chat_template_kwargs") == _CTK_THINKING_ON,
        f"got {captured[0].get('chat_template_kwargs')}",
    )
    sys_content = captured[0]["messages"][0]["content"]
    check("override_false_no_prefix", not sys_content.startswith(_NO_THINKING_SYSTEM_PREFIX))


def test_chat_method_thinking_control():
    """chat() с profile=patch добавляет chat_template_kwargs={enable_thinking:False} в payload."""
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="patch")
    captured: list = []

    def fake_request(path, payload):
        captured.append(dict(payload))
        return json.dumps({"choices": [{"finish_reason": "stop",
                                        "message": {"content": "{}"}}]})

    with patch.object(p, "_do_request", side_effect=fake_request):
        p.chat([
            {"role": "system", "content": "You are a fixer."},
            {"role": "user", "content": "Fix it."},
        ])

    check(
        "chat_patch_ctk_thinking_off",
        captured[0].get("chat_template_kwargs") == _CTK_THINKING_OFF,
        f"got {captured[0].get('chat_template_kwargs')}",
    )
    check("chat_patch_no_old_thinking_param", "thinking" not in captured[0])
    sys_msg = captured[0]["messages"][0]["content"]
    check("chat_patch_sys_prefix", sys_msg.startswith(_NO_THINKING_SYSTEM_PREFIX))


# ---------------------------------------------------------------------------
# context_window / prompt budget
# ---------------------------------------------------------------------------

def test_context_window_defaults():
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="patch")
    check("ctx_window_default_value", p.context_window == LOCAL_LLM_CONTEXT_WINDOW)
    check("ctx_window_is_248k", LOCAL_LLM_CONTEXT_WINDOW == 248_000)


def test_context_window_budget():
    for profile in ("patch", "review", "chat", "architect"):
        p = LocalLLMProvider(base_url="http://x/v1", model="m", profile=profile)
        overhead = _PROFILE_SYS_OVERHEAD[profile]
        expected_tokens = max(1, 248_000 - p._max_tokens - overhead)
        check(f"ctx_budget_tokens_{profile}", p.max_input_tokens == expected_tokens)
        expected_chars = int(expected_tokens * _CHARS_PER_TOKEN)
        check(f"ctx_budget_chars_{profile}", p.max_input_chars == expected_chars)
        # Sanity: chars должен быть >> разумного файла
        check(f"ctx_budget_chars_{profile}_large", p.max_input_chars > 500_000)


def test_context_window_in_pipeline_config():
    p = LocalLLMProvider(base_url="http://x/v1", model="m", profile="patch")
    cfg = p.as_pipeline_provider_config()
    check("pipeline_cfg_has_context_window", "context_window" in cfg)
    check("pipeline_cfg_ctx_window_value", cfg["context_window"] == 248_000)


def test_context_window_override():
    p = LocalLLMProvider(
        base_url="http://x/v1", model="m", profile="chat", context_window=32_000
    )
    check("ctx_override_stored", p.context_window == 32_000)
    check("ctx_override_budget_smaller", p.max_input_tokens < 32_000)
    cfg = p.as_pipeline_provider_config()
    check("ctx_override_in_config", cfg["context_window"] == 32_000)


def test_llmclient_large_ctx_no_trim():
    """При context_window>=100k generate_structured_fix передаёт весь файл."""
    from fixers.llm_client import LLMClient, _trim_around_line
    client = LLMClient.__new__(LLMClient)
    client.per_request_timeout = 30
    client.max_retries = 1
    client.providers = [{
        "provider": "local",
        "model": "m",
        "base_url": "http://x/v1",
        "profile": "patch",
        "max_tokens": 1024,
        "context_window": 248_000,
    }]

    captured_prompt: list = []

    def fake_json(provider, sys_msg, user_prompt):
        captured_prompt.append(user_prompt)
        # Валидный EditSet JSON
        return ('{"intent":"fix","edits":[{"file":"f.py","anchor":{"line":1,'
                '"match":"x = 1"},"kind":"replace","new":"x = 2","rationale":"test"}],'
                '"confidence":0.9,"risks":[]}'), None

    client._call_for_json_with_retries = fake_json

    big_file = "\n".join(f"line_{i} = {i}" for i in range(1, 501))
    client.generate_structured_fix(
        {"file": "f.py", "line": 250, "code": "E501", "message": "too long"},
        {"f.py": big_file},
        "python",
    )
    check("large_ctx_captures_prompt", len(captured_prompt) == 1)
    # Файл в промпте должен содержать строки из начала (line_1) и конца (line_499)
    check(
        "large_ctx_has_full_file",
        "line_1" in captured_prompt[0] and "line_499" in captured_prompt[0],
    )


def test_llmclient_small_ctx_trims():
    """При context_window<100k generate_structured_fix режет файл (radius=60)."""
    from fixers.llm_client import LLMClient
    client = LLMClient.__new__(LLMClient)
    client.per_request_timeout = 30
    client.max_retries = 1
    client.providers = [{
        "provider": "openai",
        "model": "gpt-4",
        "api_key": "x",
        "base_url": None,
        "max_tokens": 8000,
        # нет context_window → дефолт 0 → <100k → режем
    }]

    captured_prompt: list = []

    def fake_json(provider, sys_msg, user_prompt):
        captured_prompt.append(user_prompt)
        return ('{"intent":"fix","edits":[{"file":"f.py","anchor":{"line":1,'
                '"match":"line_1"},"kind":"replace","new":"x","rationale":"t"}],'
                '"confidence":0.9,"risks":[]}'), None

    client._call_for_json_with_retries = fake_json

    big_file = "\n".join(f"line_{i} = {i}" for i in range(1, 501))
    client.generate_structured_fix(
        {"file": "f.py", "line": 250, "code": "E501", "message": "too long"},
        {"f.py": big_file},
        "python",
    )
    check("small_ctx_captures_prompt", len(captured_prompt) == 1)
    # line_1 должна быть обрезана (вне окна ±60 от строки 250)
    check(
        "small_ctx_trimmed_head",
        "line_1 " not in captured_prompt[0],
    )


def test_chat_method_filters_reasoning():
    """chat() должен вернуть msg с content=str, reasoning_content убран из content."""
    p = LocalLLMProvider(
        base_url="http://localhost:8080/v1",
        model="llama",
        profile="chat",
    )
    raw_response = json.dumps({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Here is the answer",
                "reasoning_content": "<think>some reasoning</think>",
                "__verbose": {"x": 1},
            }
        }]
    })
    with patch.object(p, "_do_request", return_value=raw_response):
        result = p.chat([{"role": "user", "content": "hi"}])
    check("chat_result_content_clean", result["content"] == "Here is the answer")
    check("chat_result_no_think_bleed", "think" not in result["content"])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("LocalLLMProvider regression:")
    test_profiles_defaults()
    test_profile_invalid()
    test_profile_overrides()
    test_extract_json_clean()
    test_extract_json_fenced()
    test_extract_json_surrounded()
    test_extract_json_nested()
    test_extract_json_no_json()
    test_extract_json_broken()
    test_extract_json_first_wins()
    test_pipeline_config()
    test_pipeline_config_review()
    test_chat_callable_signature()
    test_llmclient_local_json_routing()
    test_llmclient_local_text_routing()
    test_existing_providers_unaffected()
    test_unknown_provider_returns_error()
    test_make_local_provider_explicit()
    test_make_local_provider_from_env()
    test_make_local_chat()
    test_make_default_chat_local_override()
    test_make_default_chat_chat_url_priority()
    test_complete_json_extracts_wrapped()
    test_complete_json_server_down()
    test_complete_text_server_down()
    test_post_chat_json_mode_adds_response_format()
    test_post_chat_text_mode_no_response_format()
    test_post_chat_architect_no_response_format()
    test_extract_content_plain()
    test_extract_content_ignores_reasoning()
    test_extract_content_ignores_verbose()
    test_extract_content_none_content()
    test_extract_content_int_content()
    test_extract_content_only_reasoning()
    test_chat_method_filters_reasoning()
    test_disable_thinking_defaults()
    test_patch_max_tokens_8000()
    test_review_max_tokens_8000()
    test_thinking_disabled_adds_api_param()
    test_thinking_disabled_prefixes_system()
    test_thinking_enabled_no_api_param()
    test_thinking_override()
    test_chat_method_thinking_control()
    test_context_window_defaults()
    test_context_window_budget()
    test_context_window_in_pipeline_config()
    test_context_window_override()
    test_llmclient_large_ctx_no_trim()
    test_llmclient_small_ctx_trims()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nLocalLLMProvider regression: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
