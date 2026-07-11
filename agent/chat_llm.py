"""
Тонкий HTTP-клиент для чат-LLM (OpenAI-совместимый chat completions).

Использует только stdlib (`urllib`) — это исключает зависимость чата от
тяжёлого `openai`/`anthropic` SDK. Формат совместим с DeepSeek, OpenAI и
большинством хостов.

Поддерживает function/tool calling. Возвращает ВСЁ ассистент-сообщение
(`{content, tool_calls?}`), а не только текст — `ChatSession` сам решает, есть
ли там запрос на tool и нужно ли крутить цикл.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ChatLLMError(RuntimeError):
    """Любая ошибка обращения к чат-LLM (сеть, HTTP, парсинг)."""


class HTTPChatLLM:
    """OpenAI-совместимый клиент через urllib. Один POST на ответ."""

    def __init__(self, *, api_key: str, base_url: str, model: str,
                 timeout: float = 60.0):
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or "deepseek-chat"
        self.timeout = float(timeout)

    def chat(self, messages: List[Dict[str, Any]],
             *, tools: Optional[List[Dict[str, Any]]] = None,
             temperature: float = 0.3,
             max_tokens: int = 1500) -> Dict[str, Any]:
        """Один запрос. Возвращает полное assistant-сообщение из choices[0]."""
        if not self.base_url:
            raise ChatLLMError("base_url не задан")
        url = self.base_url + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        # SOCKS/ALL_PROXY ломают urllib — изолируем как в LLMClient.
        old_env = {}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            if k in os.environ:
                old_env[k] = os.environ.pop(k)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise ChatLLMError(f"HTTP {e.code}: {text or e.reason}") from e
        except urllib.error.URLError as e:
            raise ChatLLMError(f"сеть: {e.reason}") from e
        finally:
            os.environ.update(old_env)

        try:
            data = json.loads(raw)
        except Exception as e:
            raise ChatLLMError(f"невалидный JSON-ответ: {e}") from e
        try:
            msg = data["choices"][0]["message"]
        except Exception as e:
            raise ChatLLMError(f"неожиданная схема ответа: {raw[:200]}") from e
        # Нормализуем: content всегда str (возможно пустой).
        if not isinstance(msg.get("content"), str):
            msg["content"] = "" if msg.get("content") is None else str(msg.get("content"))
        return msg


# Тип «как угодно дёрнуть LLM»: для production — HTTPChatLLM.chat,
# для тестов — любой `lambda messages, tools=None: {"content": "..."}`.
ChatCallable = Callable[..., Dict[str, Any]]


def make_default_chat(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> ChatCallable:
    """Удобная фабрика клиента из ENV.

    Приоритет выбора бэкенда:
      1. Явные параметры base_url / api_key / model.
      2. WEBBLES_CHAT_BASE_URL / WEBBLES_CHAT_API_KEY / WEBBLES_CHAT_MODEL
         (чат-специфичный ключ).
      3. WEBBLES_LOCAL_LLM_URL — если задан и WEBBLES_CHAT_BASE_URL не задан,
         LocalLLMProvider(profile="chat") обслуживает чат через локальный сервер.
      4. Дефолт: DeepSeek cloud.
    """
    # Локальный LLM как бэкенд чата (если задан, приоритет ниже chat-specific ENV)
    local_url = os.environ.get("WEBBLES_LOCAL_LLM_URL")
    if local_url and not base_url and not os.environ.get("WEBBLES_CHAT_BASE_URL"):
        return make_local_chat(
            base_url=local_url,
            model=model or os.environ.get("WEBBLES_LOCAL_LLM_MODEL"),
            api_key=api_key or os.environ.get("WEBBLES_LOCAL_LLM_KEY", ""),
        )

    api_key = api_key or os.environ.get("WEBBLES_CHAT_API_KEY", "")
    base_url = base_url or os.environ.get("WEBBLES_CHAT_BASE_URL") \
        or "https://api.deepseek.com/v1"
    model = model or os.environ.get("WEBBLES_CHAT_MODEL") or "deepseek-chat"
    client = HTTPChatLLM(api_key=api_key, base_url=base_url, model=model)

    def _call(messages: List[Dict[str, Any]],
              tools: Optional[List[Dict[str, Any]]] = None,
              **kw) -> Dict[str, Any]:
        return client.chat(messages, tools=tools, **kw)

    return _call


def make_local_chat(
    *,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 60.0,
) -> ChatCallable:
    """Фабрика ChatCallable через LocalLLMProvider(profile='chat').

    ENV (читаются только если параметр не задан):
      WEBBLES_LOCAL_LLM_URL   — base_url
      WEBBLES_LOCAL_LLM_MODEL — model
      WEBBLES_LOCAL_LLM_KEY   — api_key
    """
    from fixers.local_llm_provider import make_local_provider
    provider = make_local_provider(
        profile="chat",
        base_url=base_url or os.environ.get("WEBBLES_LOCAL_LLM_URL"),
        model=model or os.environ.get("WEBBLES_LOCAL_LLM_MODEL"),
        api_key=api_key or os.environ.get("WEBBLES_LOCAL_LLM_KEY", ""),
        timeout=timeout,
    )
    return provider.as_chat_callable()
