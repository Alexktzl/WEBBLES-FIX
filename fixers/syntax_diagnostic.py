"""
Модуль точной синтаксической диагностики с кэшированием рецептов.
Использует LLM для поиска места незакрытой кавычки, скобки или unknown prefix.
Кэш рецептов хранится в .webbles_syntax_memory.json.
При наличии InvariantGuard обогащает промпт информацией о намерении кода.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MEMORY_FILE = ".webbles_syntax_memory.json"


class SyntaxDiagnostic:
    """Диагностирует синтаксические ошибки и запоминает успешные решения."""

    def __init__(self, llm_client=None, project_path: Optional[Path] = None,
                 invariant_guard=None, segmenter=None):
        self.llm_client = llm_client
        self.memory_path = Path(project_path) / MEMORY_FILE if project_path else None
        self.invariant_guard = invariant_guard
        self.segmenter = segmenter
        self.memory: Dict[str, Dict[str, Any]] = {}
        self._loaded = False
        self._load_memory()

    # ------------------------------------------------------------------
    # Загрузка / сохранение кэша
    # ------------------------------------------------------------------
    def _load_memory(self):
        if self._loaded:
            return
        self._loaded = True
        if self.memory_path and self.memory_path.exists():
            try:
                with open(self.memory_path, "r", encoding="utf-8") as f:
                    self.memory = json.load(f)
                logger.info("Загружено %d рецептов из %s", len(self.memory), self.memory_path)
            except Exception as e:
                logger.warning("Не удалось загрузить память синтаксиса: %s", e)

    def _save_memory(self):
        if not self.memory_path:
            return
        try:
            tmp_path = self.memory_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, indent=2, ensure_ascii=False)
            tmp_path.replace(self.memory_path)
            logger.debug("Память синтаксиса сохранена (%d записей)", len(self.memory))
        except Exception as e:
            logger.warning("Не удалось сохранить память синтаксиса: %s", e)

    # ------------------------------------------------------------------
    # Ключ для кэширования
    # ------------------------------------------------------------------
    @staticmethod
    def _make_key(error: Dict[str, Any], line_content: str) -> str:
        code = error.get("code", "")
        msg = error.get("message", "")
        snippet = line_content[:80] if line_content else ""
        return f"{code}::{snippet}"

    # ------------------------------------------------------------------
    # Извлечение контекста и намерения
    # ------------------------------------------------------------------
    def _get_context(self, file_content: str, line_num: int, radius: int = 30) -> str:
        lines = file_content.splitlines()
        total = len(lines)
        start = max(0, line_num - 1 - radius)
        end = min(total, line_num + radius)
        return "\n".join(lines[start:end])

    def _get_intent_for_line(self, file_content: str, line_num: int) -> Optional[str]:
        if not self.invariant_guard or not self.segmenter:
            return None
        try:
            segments = self.segmenter.segment_file(file_content, max_segment_lines=10000)
            for seg in segments:
                if seg["start_line"] <= line_num <= seg["end_line"]:
                    intent = self.invariant_guard._extract_intent(seg["text"])
                    if intent and intent != "_LLM_ERROR_SENTINEL":
                        logger.debug("Извлечено намерение для строк %d-%d", seg["start_line"], seg["end_line"])
                        return f"CODE PURPOSE: {intent}"
            return None
        except Exception as e:
            logger.debug("Не удалось извлечь намерение: %s", e)
            return None

    # ------------------------------------------------------------------
    # Публичные методы диагностики
    # ------------------------------------------------------------------
    def find_missing_quote(self, error: Dict[str, Any], file_content: str,
                           line_content: str, line_num: int) -> Optional[Dict[str, Any]]:
        """Ищет место для закрывающей кавычки (сначала в кэше, потом LLM)."""
        key = self._make_key(error, line_content)
        if key in self.memory:
            logger.info("Найден рецепт в кэше для %s", key)
            return self.memory[key]

        if not self.llm_client:
            return None

        context = self._get_context(file_content, line_num, radius=30)
        intent = self._get_intent_for_line(file_content, line_num)

        prompt = f"""You are a Rust syntax expert. A file has an unterminated double quote string.
The error is reported near line {line_num}. Below is the surrounding code.

{intent if intent else ""}

Find the exact line and column where a closing double quote '"' is missing.
Return ONLY a JSON object: {{"line": <line_number>, "insert_after_col": <column_number>}}
- line: the line containing the opening quote (1-indexed).
- insert_after_col: the character position AFTER which to insert the closing quote (0 = before first char).
  If the quote should be at the end of the line, use the line length.
Do NOT include any other text.

Code:
{context}
"""
        result = self._query_llm(prompt)
        if result and isinstance(result.get("line"), int) and result["line"] > 0:
            self.memory[key] = result
            self._save_memory()
            logger.info("Сохранён новый рецепт для %s: %s", key, result)
        return result

    def find_missing_brace(self, error: Dict[str, Any], file_content: str,
                           line_content: str, line_num: int) -> Optional[Dict[str, Any]]:
        """Ищет место для закрывающей фигурной скобки (сначала в кэше, потом LLM)."""
        key = self._make_key(error, line_content)
        if key in self.memory:
            logger.info("Найден рецепт в кэше для %s", key)
            return self.memory[key]

        if not self.llm_client:
            return None

        context = self._get_context(file_content, line_num, radius=50)
        intent = self._get_intent_for_line(file_content, line_num)

        prompt = f"""You are a Rust syntax expert. The file has mismatched braces.
Error reported near line {line_num}. Below is the surrounding code.

{intent if intent else ""}

Find the exact line number AFTER which a closing brace '}}' is missing.
Return ONLY a JSON object: {{"line": <line_number>, "indent": <indent_spaces>}}
- line: line number AFTER which to insert '}}' (1-indexed).
- indent: number of spaces for indentation.
If you cannot determine, return {{"line": 0, "indent": 0}}.
Do NOT include any other text.

Code:
{context}
"""
        result = self._query_llm(prompt)
        if result and isinstance(result.get("line"), int) and result["line"] > 0:
            self.memory[key] = result
            self._save_memory()
            logger.info("Сохранён новый рецепт для %s: %s", key, result)
        return result

    def find_unknown_prefix_fix(self, error: Dict[str, Any], file_content: str,
                                line_content: str, line_num: int) -> Optional[Dict[str, Any]]:
        """
        Ищет точное место для вставки пробела при ошибке unknown prefix.
        Возвращает словарь с ключом "insert_before_col" — позиция, перед которой нужно вставить пробел.
        """
        key = self._make_key(error, line_content)
        if key in self.memory:
            logger.info("Найден рецепт в кэше для %s", key)
            return self.memory[key]

        if not self.llm_client:
            return None

        context = self._get_context(file_content, line_num, radius=5)
        intent = self._get_intent_for_line(file_content, line_num)

        prompt = f"""You are a Rust syntax expert. The following line has an 'unknown prefix' error.
This means an identifier is immediately followed by a double quote without a space.
The compiler suggests inserting whitespace.

Return ONLY a JSON object: {{"insert_before_col": <column_number>}}
- insert_before_col: the column BEFORE which a space should be inserted (0-indexed).
  For example, if the line is `println!("text"reached")`, the space should go before the 'r' of 'reached',
  so insert_before_col should be the index of 'r'.
Do NOT include any other text.

Line with error (line {line_num}):
{line_content}

Surrounding context:
{context}
"""
        result = self._query_llm(prompt)
        if result and isinstance(result.get("insert_before_col"), int):
            self.memory[key] = result
            self._save_memory()
            logger.info("Сохранён новый рецепт для %s: %s", key, result)
        return result

    def _query_llm(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Отправляет промпт LLM и извлекает JSON из ответа."""
        try:
            response = self.llm_client._call_llm(prompt)
            if not response:
                return None
            # Найдём первую валидную JSON-структуру
            json_match = re.search(r'\{[^{}]*\}', response)
            if json_match:
                data = json.loads(json_match.group())
                return data
            logger.warning("LLM не вернул JSON: %s", response[:200])
        except Exception as e:
            logger.error("Ошибка запроса к LLM: %s", e)
        return None