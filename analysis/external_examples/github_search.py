"""
Stage J.3 — GitHubCodeSearchProvider (opt-in, требует GITHUB_TOKEN).

Ищет в коде на GitHub места, где упоминается error_code на нужном языке —
это даёт LLM реальные прецеденты из открытых репозиториев. Сетевой провайдер,
поэтому строго opt-in: без `GITHUB_TOKEN` в окружении `available()` → False и
провайдер молча skip-ается. В smoke-тестах сеть НЕ дёргается.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from analysis.external_examples.provider import Example, ExampleSearchProvider

logger = logging.getLogger(__name__)

_LANG_MAP = {
    "rust": "Rust", "rs": "Rust",
    "python": "Python", "py": "Python",
    "javascript": "JavaScript", "js": "JavaScript",
    "typescript": "TypeScript", "ts": "TypeScript",
}


class GitHubCodeSearchProvider(ExampleSearchProvider):
    name = "github"

    API_URL = "https://api.github.com/search/code"
    TIMEOUT_SEC = 8

    def __init__(self, token: str = ""):
        # Токен можно передать явно (тесты), иначе берём из окружения.
        self._token = token or os.environ.get("GITHUB_TOKEN", "")

    def available(self) -> bool:
        return bool(self._token)

    def search(self, error: Dict[str, Any], language: str,
               max_results: int = 3) -> List[Example]:
        code = (error.get("code") or "").strip()
        if not code:
            return []
        gh_lang = _LANG_MAP.get((language or "").lower())
        query = f'"{code}"'
        if gh_lang:
            query += f" language:{gh_lang}"

        items = self._query(query, max_results)
        out: List[Example] = []
        for it in items[:max_results]:
            repo = (it.get("repository") or {}).get("full_name", "")
            path = it.get("path", "")
            url = it.get("html_url", "")
            title = f"{repo}/{path}".strip("/") or code
            out.append(Example(
                source=self.name,
                title=title,
                # GitHub Code Search не отдаёт тело файла в этом endpoint —
                # сниппетом служит указание где искать; LLM получает ссылку.
                snippet=f"See usage of {code} in {title}",
                url=url,
                score=0.6,
            ))
        return out

    # -----------------------------------------------------------------
    def _query(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        params = urllib.parse.urlencode({
            "q": query,
            "per_page": max(1, min(max_results, 10)),
        })
        req = urllib.request.Request(
            f"{self.API_URL}?{params}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github.text-match+json",
                "User-Agent": "webbles-fix",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug("github_search: запрос упал: %s", e)
            return []
        items = data.get("items")
        return items if isinstance(items, list) else []
