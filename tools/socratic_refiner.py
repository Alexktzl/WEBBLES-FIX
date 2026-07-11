"""
Адаптер Socratic Refiner для Webbles Fix.
Добавляет шаг рассуждения (Socratic questioning) перед генерацией патча.
"""

import json
import logging
import re
from typing import Dict, Any, Optional
from tools.base_tool import BaseTool

logger = logging.getLogger(__name__)


class SocraticRefiner(BaseTool):
    """Задаёт LLM уточняющие вопросы и строит цепочку рассуждений."""

    def __init__(self, llm_client=None, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.llm_client = llm_client  # ссылка на основной LLMClient

    def is_available(self) -> bool:
        """Доступен, если передан LLM-клиент."""
        return self.llm_client is not None

    def run(self, error: Dict[str, Any], context: str, language: str) -> Optional[str]:
        """
        Выполняет Socratic questioning и возвращает обогащённый промпт.
        """
        if not self.llm_client:
            return None

        # Шаг 1: Попросить LLM объяснить суть ошибки
        explanation_prompt = (
            f"You are a {language} expert. Explain the following error in one sentence: "
            f"{error.get('message')}"
        )
        explanation = self.llm_client._call_llm(explanation_prompt)

        # Шаг 2: Попросить LLM предложить гипотезу исправления
        hypothesis_prompt = (
            f"Given the error '{error.get('message')}' in file {error.get('file')} at line {error.get('line')}, "
            f"propose a minimal fix hypothesis (one sentence). Do NOT write code, only describe the change."
        )
        hypothesis = self.llm_client._call_llm(hypothesis_prompt)

        # Шаг 3: Попросить LLM проверить гипотезу (найти риски)
        verification_prompt = (
            f"Hypothesis: {hypothesis}\n"
            f"Explain why this fix is safe and will not introduce new bugs. "
            f"If there are risks, list them."
        )
        verification = self.llm_client._call_llm(verification_prompt)

        # Собираем обогащённый контекст из трёх ответов LLM
        parts = []
        if explanation:
            parts.append(f"## Суть ошибки\n{str(explanation).strip()}")
        if hypothesis:
            parts.append(f"## Гипотеза исправления\n{str(hypothesis).strip()}")
        if verification:
            parts.append(f"## Анализ рисков\n{str(verification).strip()}")

        if not parts:
            return None

        return "# Socratic-анализ ошибки\n" + "\n\n".join(parts)

    # ------------------------------------------------------------------
    # E999 Semantic Recovery (2026-06-21): LLM-судья «сохранён ли смысл».
    # ------------------------------------------------------------------

    _VERDICT_RE = re.compile(r'\{.*\}', re.DOTALL)

    def compare_logic(self, old_content: str, new_content: str,
                      file_name: str = "") -> Dict[str, Any]:
        """Сравнивает СЛОМАННЫЙ файл (old) с реконструированным (new) —
        сохранён ли смысл/назначение, не «нарисовали ли кота вместо Моны
        Лизы». Деterministic-часть (имена/сигнатуры) делает отдельно
        signatures_match в fixers/semantic_recovery.py — здесь только
        семантика, которую AST не видит.

        Возвращает {"verdict": "ok"|"wrong"|"uncertain", "reasons": [...]}.
        При недоступности LLM или непарсимом ответе — "uncertain" (консервативный
        fallback: НЕ ACCEPT по умолчанию, требует ручного решения)."""
        if not self.llm_client:
            return {"verdict": "uncertain", "reasons": ["LLM client unavailable"]}

        prompt = (
            f"A Python file ({file_name or 'unknown'}) had cascading syntax errors and was "
            f"completely reconstructed by another LLM to be syntactically valid. Class names, "
            f"function names, and signatures were verified to match automatically — your job is "
            f"to judge whether the INTERNAL LOGIC and PURPOSE of the code was preserved, not just "
            f"the names.\n\n"
            f"Original (broken, syntax errors present) file:\n```\n{old_content}\n```\n\n"
            f"Reconstructed (now valid) file:\n```\n{new_content}\n```\n\n"
            f"Did the reconstruction preserve the apparent intent and logic of the original code "
            f"(allowing for the syntax fix itself and minor internal restructuring)? Or does it "
            f"look like the logic was substantially changed, simplified away, or replaced with "
            f"something different (e.g. stub/placeholder logic instead of the real implementation)?\n\n"
            f"Respond with ONLY a JSON object: "
            f'{{"verdict": "ok"|"wrong"|"uncertain", "reasons": ["short reason", ...]}}'
        )
        try:
            raw = self.llm_client._call_llm(prompt)
        except Exception as e:
            logger.warning("compare_logic: LLM вызов упал: %s", e)
            return {"verdict": "uncertain", "reasons": [f"LLM call failed: {e}"]}

        if not raw:
            return {"verdict": "uncertain", "reasons": ["empty LLM response"]}

        m = self._VERDICT_RE.search(raw)
        if not m:
            return {"verdict": "uncertain", "reasons": ["no parseable verdict in LLM response"]}
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return {"verdict": "uncertain", "reasons": ["malformed JSON verdict"]}

        verdict = str(parsed.get("verdict", "")).strip().lower()
        if verdict not in ("ok", "wrong", "uncertain"):
            verdict = "uncertain"
        reasons = parsed.get("reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        return {"verdict": verdict, "reasons": [str(r) for r in reasons]}