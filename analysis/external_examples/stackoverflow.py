"""
Stage J.4 — StackOverflowProvider (opt-in).

StackExchange API: ищет вопросы по тегу языка с error_code в заголовке, берёт
топ-2 с принятым ответом и вытаскивает первый code-блок из принятого ответа.
Сетевой провайдер — opt-in (по умолчанию НЕ в списке `from_config`). В smoke
сеть не дёргается.

API StackExchange допускает анонимные запросы (без ключа), но с низким лимитом —
поэтому кэш (J.5) обязателен, а сам провайдер включается явно.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from analysis.external_examples.provider import Example, ExampleSearchProvider

logger = logging.getLogger(__name__)

_TAG_MAP = {
    "rust": "rust", "rs": "rust",
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
}
_CODE_BLOCK_RE = re.compile(r"<code>(.*?)</code>", re.DOTALL)


class StackOverflowProvider(ExampleSearchProvider):
    name = "stackoverflow"

    SEARCH_URL = "https://api.stackexchange.com/2.3/search/advanced"
    ANSWERS_URL = "https://api.stackexchange.com/2.3/answers/{ids}"
    TIMEOUT_SEC = 8
    MAX_QUESTIONS = 2

    def __init__(self, api_key: str = ""):
        self._key = api_key or os.environ.get("STACKEXCHANGE_KEY", "")

    def available(self) -> bool:
        # Анонимный доступ разрешён, но провайдер всё равно opt-in через config.
        return True

    def search(self, error: Dict[str, Any], language: str,
               max_results: int = 3) -> List[Example]:
        code = (error.get("code") or "").strip()
        tag = _TAG_MAP.get((language or "").lower())
        if not code or not tag:
            return []

        questions = self._search_questions(code, tag)
        out: List[Example] = []
        for q in questions[:min(self.MAX_QUESTIONS, max_results)]:
            answer_id = q.get("accepted_answer_id")
            if not answer_id:
                continue
            snippet = self._fetch_answer_code(answer_id)
            if not snippet:
                continue
            out.append(Example(
                source=self.name,
                title=html.unescape(q.get("title", "") or code),
                snippet=snippet,
                url=q.get("link", ""),
                score=0.5,
            ))
        return out

    # -----------------------------------------------------------------
    def _search_questions(self, code: str, tag: str) -> List[Dict[str, Any]]:
        params = {
            "order": "desc", "sort": "votes",
            "title": code, "tagged": tag,
            "accepted": "True", "site": "stackoverflow",
        }
        if self._key:
            params["key"] = self._key
        data = self._get(self.SEARCH_URL + "?" + urllib.parse.urlencode(params))
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def _fetch_answer_code(self, answer_id: int) -> str:
        params = {"site": "stackoverflow", "filter": "withbody"}
        if self._key:
            params["key"] = self._key
        url = self.ANSWERS_URL.format(ids=answer_id) + "?" + urllib.parse.urlencode(params)
        data = self._get(url)
        items = data.get("items") if isinstance(data, dict) else None
        if not items:
            return ""
        body = items[0].get("body", "")
        m = _CODE_BLOCK_RE.search(body)
        if not m:
            return ""
        snippet = html.unescape(m.group(1)).strip()
        if len(snippet) > 1200:
            snippet = snippet[:1200].rstrip() + "\n... [truncated]"
        return snippet

    def _get(self, url: str) -> Dict[str, Any]:
        req = urllib.request.Request(url, headers={"User-Agent": "webbles-fix"})
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as resp:
                # StackExchange отдаёт gzip; urllib не распаковывает сам, но
                # API также принимает identity — полагаемся на стандартный путь.
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug("stackoverflow: запрос упал: %s", e)
            return {}
