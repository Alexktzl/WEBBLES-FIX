"""
Stage H.2 — SemanticAuditor.

Оркестрирует семантический аудит «docstring ↔ код»:
  DocstringExtractor (H.1) → AuditCache (H.4) → InvariantGuard.check_intent_vs_code (H.3)
и превращает обнаруженные расхождения в Webbles error-dict'ы с
`code = "SEMANTIC_MISMATCH"`.

Это отдельный режим аудита (не на горячем пути исправления): код компилируется
и тесты могут проходить, но он делает не то, что обещает документация. Каскадно
graceful: нет LLM / нет docstring'ов / аудит «unknown» → просто нет находок.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from analysis.docstring_extractor import DocstringExtractor, DocItem
from analysis.invariant_guard import InvariantGuard

logger = logging.getLogger(__name__)

SEMANTIC_MISMATCH_CODE = "SEMANTIC_MISMATCH"
SEMANTIC_MISMATCH_CLASS = "SEMANTIC_MISMATCH"


class SemanticAuditor:
    """Аудит соответствия документации и кода → SEMANTIC_MISMATCH-ошибки."""

    def __init__(self, llm_client=None, cache=None,
                 guard: Optional[InvariantGuard] = None,
                 extractor: Optional[DocstringExtractor] = None):
        self.extractor = extractor or DocstringExtractor()
        # guard можно передать явно (тесты со stub-LLM); иначе строим из llm_client.
        if guard is not None:
            self.guard = guard
        elif llm_client is not None:
            self.guard = InvariantGuard(llm_client)
        else:
            self.guard = None
        self.cache = cache

    # -----------------------------------------------------------------
    def audit_source(self, source: str, language: str,
                     file: str = "") -> List[Dict[str, Any]]:
        """Аудит одного исходника. Возвращает список SEMANTIC_MISMATCH error-dict'ов."""
        items = self.extractor.extract(source, language)
        if not items:
            return []
        errors: List[Dict[str, Any]] = []
        for item in items:
            result = self._audit_item(item, language)
            if result.get("verdict") == "mismatch":
                errors.append(self._to_error(item, result, file))
        if errors:
            logger.info("SemanticAuditor: %d SEMANTIC_MISMATCH в %s",
                        len(errors), file or "<source>")
        return errors

    def audit_file(self, path, language: str, rel: str = "") -> List[Dict[str, Any]]:
        """Аудит файла на диске (удобная обёртка)."""
        p = Path(path)
        try:
            source = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.debug("SemanticAuditor: не прочитать %s: %s", p, e)
            return []
        return self.audit_source(source, language, file=rel or str(p))

    # -----------------------------------------------------------------
    def _audit_item(self, item: DocItem, language: str) -> Dict[str, Any]:
        # 1) кэш по (doc, code)
        if self.cache is not None:
            try:
                hit = self.cache.get(item.doc, item.code)
            except Exception:
                hit = None
            if hit is not None:
                return hit
        # 2) нет guard (нет LLM) — судить не можем
        if self.guard is None:
            return {"verdict": "unknown", "reason": "no guard/llm", "confidence": 0.0}
        # 3) LLM-аудит + запись в кэш
        try:
            result = self.guard.check_intent_vs_code(
                item.doc, item.code, language=language, name=item.name)
        except Exception as e:
            logger.debug("SemanticAuditor: check_intent_vs_code упал: %s", e)
            result = {"verdict": "unknown", "reason": str(e), "confidence": 0.0}
        if self.cache is not None:
            try:
                self.cache.put(item.doc, item.code, result)
            except Exception:
                pass
        return result

    @staticmethod
    def _to_error(item: DocItem, result: Dict[str, Any], file: str) -> Dict[str, Any]:
        reason = (result.get("reason") or "").strip() or "code does not match its documentation"
        return {
            "file": file or "",
            "line": int(item.line or 0),
            "column": 0,
            "message": f"{item.kind} `{item.name}`: doc/code mismatch — {reason}",
            "code": SEMANTIC_MISMATCH_CODE,
            "severity": "warning",
            "error_type": "semantic",
            "error_class": SEMANTIC_MISMATCH_CLASS,
            "confidence": float(result.get("confidence", 0.8) or 0.8),
            "base_weight": 5.0,
            # пригодится промпту/ревью: что именно обещала документация
            "documented_intent": item.doc[:500],
        }
