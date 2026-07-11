"""
Инструмент веб-поиска для Webbles Fix.
Поддерживает SerpAPI, Google Custom Search и заглушку для тестов.
Выполняет поиск и возвращает отформатированные результаты.
"""

import logging
import os
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class WebSearchTool:
    """Выполняет поиск в интернете и фильтрует результаты."""

    def __init__(
        self,
        max_results: int = 3,
        min_relevance: float = 0.5,
        max_snippet_length: int = 500,
        required_keywords: Optional[List[str]] = None,
        config: Optional[Dict] = None,
    ):
        self.max_results = max_results
        self.min_relevance = min_relevance
        self.max_snippet_length = max_snippet_length
        self.required_keywords = required_keywords or []
        self.config = config or {}

    def search(self, query: str) -> str:
        """
        Возвращает строку с отфильтрованными результатами поиска.
        Пытается использовать SerpAPI, Google Custom Search или заглушку.
        """
        logger.info(f"Веб-поиск: {query}")

        # 1. Пробуем SerpAPI
        api_key = self.config.get("serpapi_key") or os.environ.get("SERPAPI_API_KEY")
        if api_key:
            results = self._search_serpapi(query, api_key)
            if results:
                return self._format_results(results)

        # 2. Пробуем Google Custom Search
        api_key = self.config.get("google_api_key") or os.environ.get("GOOGLE_API_KEY")
        cx = self.config.get("google_cx") or os.environ.get("GOOGLE_CX")
        if api_key and cx:
            results = self._search_google(query, api_key, cx)
            if results:
                return self._format_results(results)

        # 3. Заглушка (для тестов)
        return self._fallback_stub(query)

    def _search_serpapi(self, query: str, api_key: str) -> List[Dict]:
        """Поиск через SerpAPI."""
        try:
            params = {
                "q": query,
                "api_key": api_key,
                "num": self.max_results,
            }
            resp = requests.get("https://serpapi.com/search", params=params, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"SerpAPI вернул {resp.status_code}")
                return []
            data = resp.json()
            return self._filter_results(data.get("organic_results", []))
        except Exception as e:
            logger.warning(f"Ошибка SerpAPI: {e}")
            return []

    def _search_google(self, query: str, api_key: str, cx: str) -> List[Dict]:
        """Поиск через Google Custom Search API."""
        try:
            params = {
                "key": api_key,
                "cx": cx,
                "q": query,
                "num": self.max_results,
            }
            resp = requests.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"Google CSE вернул {resp.status_code}")
                return []
            data = resp.json()
            return self._filter_results(data.get("items", []))
        except Exception as e:
            logger.warning(f"Ошибка Google CSE: {e}")
            return []

    def _fallback_stub(self, query: str) -> str:
        """Заглушка для тестов, когда API недоступен."""
        logger.info("Используется заглушка веб-поиска (API не настроен)")
        return (
            f"[Web search stub] No real API configured.\n"
            f"Query: {query}\n"
            f"Tip: set SERPAPI_API_KEY or GOOGLE_API_KEY+GOOGLE_CX in config.\n"
        )

    def _filter_results(self, results: List[dict]) -> List[dict]:
        """Фильтрует результаты по ключевым словам, длине и релевантности."""
        filtered = []
        for result in results:
            title = result.get("title", "")
            snippet = result.get("snippet", "") or result.get("description", "")
            content = f"{title}. {snippet}"

            # Ключевые слова
            if self.required_keywords:
                if not any(kw.lower() in content.lower() for kw in self.required_keywords):
                    continue

            # Обрезка длины
            if len(content) > self.max_snippet_length:
                content = content[:self.max_snippet_length] + "..."

            # Релевантность (если есть)
            relevance = result.get("relevance", 1.0)
            if relevance < self.min_relevance:
                continue

            filtered.append({
                "title": title,
                "snippet": content,
                "url": result.get("link", "") or result.get("url", ""),
            })

        return filtered[:self.max_results]

    def _format_results(self, results: List[dict]) -> str:
        """Форматирует результаты в строку."""
        if not results:
            return "No relevant results found."

        formatted = []
        for i, result in enumerate(results, 1):
            formatted.append(f"{i}. {result['title']}\n   {result['snippet']}\n   URL: {result['url']}")

        return "\n\n".join(formatted)