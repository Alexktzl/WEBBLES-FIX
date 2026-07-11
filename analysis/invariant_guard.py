"""
Модуль защиты инвариантов для Webbles Fix.
Решает проблему "локальные улучшения ≠ глобальная оптимальность".
Внедряет проверку бизнес-логики и архитектурных ограничений.
Добавлен LRU-кэш для извлечения намерений, семантическое сравнение намерений,
улучшенная обработка ошибок LLM.
"""

import json
import logging
import re
from functools import lru_cache
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Специальная константа, возвращаемая при недоступности LLM
_LLM_ERROR_SENTINEL = "LLM_ERROR"


class InvariantGuard:
    """
    Анализатор инвариантов конкретного сегмента кода.
    Извлекает «смысл» (намерение) кода и проверяет,
    что патч не нарушает этот смысл.
    """

    MAX_CACHE_SIZE = 128

    def __init__(self, llm_client):
        self.llm_client = llm_client
        # Кэшируем реальную реализацию
        self._extract_intent_impl = lru_cache(maxsize=self.MAX_CACHE_SIZE)(
            self._extract_intent_uncached
        )

    def _extract_intent_uncached(self, segment_text: str) -> Optional[str]:
        """
        Извлекает намерение сегмента через LLM.
        Возвращает строку с намерением, пустую строку при невозможности извлечения,
        или _LLM_ERROR_SENTINEL при ошибке вызова LLM.
        """
        if not segment_text.strip():
            return ""

        prompt = f"""Analyze the following code segment and describe its PURPOSE in ONE clear sentence.
Focus on WHAT it does, not HOW.  Do not mention implementation details.
Answer ONLY with the sentence, nothing else.

CODE SEGMENT:
{segment_text}
"""
        try:
            response = self.llm_client._call_llm(prompt)
            if response is None:
                logger.warning("LLM не вернул ответ при извлечении намерения")
                return _LLM_ERROR_SENTINEL
            cleaned = response.strip().strip('"\'').strip()
            if not cleaned:
                logger.warning("Пустой ответ LLM при извлечении намерения")
                return ""
            logger.info(f"Извлечено намерение: {cleaned[:100]}")
            return cleaned
        except TimeoutError as e:
            logger.warning(f"Таймаут LLM при извлечении намерения: {e}")
            return _LLM_ERROR_SENTINEL
        except Exception as e:
            logger.warning(f"Ошибка вызова LLM при извлечении намерения: {e}")
            return _LLM_ERROR_SENTINEL

    def _extract_intent(self, segment_text: str) -> Optional[str]:
        """Публичный метод, использующий кэш."""
        return self._extract_intent_impl(segment_text)

    def _intents_equivalent(self, intent1: str, intent2: str) -> bool:
        """
        Проверяет эквивалентность двух описаний намерения.
        Использует быстрое сравнение или запрос к LLM при необходимости.
        """
        if intent1 == _LLM_ERROR_SENTINEL or intent2 == _LLM_ERROR_SENTINEL:
            # Если одно из намерений не удалось извлечь, не можем судить
            logger.warning("Не удалось сравнить намерения из-за ошибки LLM")
            return False  # Считаем неэквивалентными для безопасности

        # Быстрая проверка: точное совпадение или высокая похожесть
        if intent1.strip() == intent2.strip():
            return True
        import difflib
        if difflib.SequenceMatcher(None, intent1, intent2).ratio() > 0.8:
            return True

        # Семантическое сравнение через LLM
        prompt = f"""Are the following two descriptions of code purpose EQUIVALENT?
Answer YES or NO only.

Description 1: "{intent1}"
Description 2: "{intent2}"
"""
        try:
            response = self.llm_client._call_llm(prompt)
            if response is None:
                logger.warning("LLM не ответил при сравнении намерений, считаем неэквивалентными")
                return False
            answer = response.strip().upper()
            return answer == "YES"
        except Exception as e:
            logger.warning(f"Ошибка LLM при сравнении намерений: {e}, считаем неэквивалентными")
            return False

    def enrich_prompt(self, error: Dict[str, Any], file_content: str,
                      segment_text: str, base_prompt: str) -> str:
        """
        Расширяет промпт для LLM, добавляя в него «смысл» сегмента.
        Если извлечение намерения провалилось, возвращает исходный промпт.
        """
        intent = self._extract_intent(segment_text)
        if not intent or intent == _LLM_ERROR_SENTINEL:
            logger.warning("Не удалось извлечь намерение сегмента, используется исходный промпт")
            return base_prompt

        enriched = f"""CRITICAL: The following code segment has a specific purpose.
DO NOT CHANGE its behavior or expected output.

SEGMENT PURPOSE:
{intent}

============================================================
ORIGINAL INSTRUCTIONS:
{base_prompt}
============================================================

STRICT RULES:
1. Fix ONLY the specified error
2. PRESERVE the segment purpose exactly
3. DO NOT alter business logic
4. DO NOT change function signatures or return types
5. The fix MUST NOT change what this code does, only HOW it does it
"""
        logger.debug(f"Промпт расширен намерением: {intent[:100]}...")
        return enriched

    def check_intent_vs_code(self, doc: str, code: str,
                             language: str = "", name: str = "") -> Dict[str, Any]:
        """Stage H.3 — сверяет ЗАДОКУМЕНТИРОВАННОЕ намерение (docstring /
        doc-комментарий) с тем, что код реально делает.

        Возвращает dict:
          verdict:    "ok" | "mismatch" | "unknown"
          reason:     краткое объяснение (для SEMANTIC_MISMATCH-промпта)
          confidence: 0.0–1.0

        Любая проблема с LLM → verdict="unknown" (НЕ флагуем — отсутствие
        сигнала не равно нарушению; ложные срабатывания дороже пропусков).
        """
        doc = (doc or "").strip()
        code = (code or "").strip()
        if not doc or not code:
            return {"verdict": "unknown", "reason": "empty doc or code", "confidence": 0.0}

        header = f"`{name}`" if name else "the item"
        lang = language or "code"
        prompt = f"""You are a strict code reviewer checking whether {lang} code does
what its documentation promises.

DOCUMENTATION (the contract):
\"\"\"{doc}\"\"\"

CODE:
{code}

Does the CODE actually implement the behaviour described in the DOCUMENTATION
for {header}? Ignore style, naming and minor wording. Flag a mismatch ONLY when
the code clearly does something different from, or contradictory to, the
documented behaviour (wrong operation, wrong return, missing documented effect).

Answer with ONLY a JSON object:
{{"verdict": "ok" | "mismatch", "reason": "<one short sentence>"}}"""
        try:
            response = self.llm_client._call_llm(prompt)
        except Exception as e:
            logger.warning("check_intent_vs_code: LLM упал: %s", e)
            return {"verdict": "unknown", "reason": f"llm error: {e}", "confidence": 0.0}
        if not response:
            return {"verdict": "unknown", "reason": "no llm response", "confidence": 0.0}
        return self._parse_audit_response(response)

    @staticmethod
    def _parse_audit_response(response: str) -> Dict[str, Any]:
        """Лениво парсит ответ аудита: сначала JSON, затем ключевые слова."""
        text = response.strip()
        verdict, reason = "", ""
        # 1) попытка JSON (возможно внутри ```-блока)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                verdict = str(obj.get("verdict", "")).strip().lower()
                reason = str(obj.get("reason", "")).strip()
            except Exception:
                pass
        # 2) фоллбэк по ключевым словам
        if verdict not in ("ok", "mismatch"):
            low = text.lower()
            if "mismatch" in low:
                verdict = "mismatch"
            elif "ok" in low or '"ok"' in low:
                verdict = "ok"
            else:
                return {"verdict": "unknown", "reason": "unparseable response", "confidence": 0.0}
        if not reason:
            reason = text[:200]
        confidence = 0.8 if verdict in ("ok", "mismatch") else 0.0
        return {"verdict": verdict, "reason": reason[:300], "confidence": confidence}

    def verify_patch(self, original_segment: str, patched_segment: str,
                     error: Dict[str, Any]) -> Dict[str, Any]:
        """
        Проверяет, что патч не нарушил «смысл» кода.
        """
        original_intent = self._extract_intent(original_segment)
        if not original_intent or original_intent == _LLM_ERROR_SENTINEL:
            logger.warning("Не удалось извлечь исходное намерение, патч принимается по умолчанию")
            return {"accepted": True}

        new_intent = self._extract_intent(patched_segment)
        if not new_intent or new_intent == _LLM_ERROR_SENTINEL:
            logger.warning("Не удалось извлечь новое намерение, патч принимается по умолчанию")
            return {"accepted": True}

        if self._intents_equivalent(original_intent, new_intent):
            return {"accepted": True}
        else:
            return {
                "accepted": False,
                "reason": (
                    f"Patch violated business logic invariant.\n"
                    f"Original intent: '{original_intent}'\n"
                    f"New intent: '{new_intent}'"
                )
            }
