"""
PatchRecorder — структурированная запись событий патчинга.

Пишет в два JSONL-файла:
  1. <runtime_dir>/patch_events.jsonl — per-project детальные события
  2. runtime/learning_cases.jsonl    — глобальный накопитель кейсов

Оба формата стабильны, машиночитаемы и пригодны для:
  - статистики и анализа закономерностей по большим сериям
  - аудита качества патчей по источнику (rule_based, ruff, llm, …)
  - построения отчётов и дашбордов
  - обучения/дообучения моделей

Schema version: 1.2
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.4"

# Предельный размер строковых фрагментов кода (символов) — не хранить большие бинарные блоки
_MAX_CODE_CHARS = 2000
_MAX_PATCH_CHARS = 3000


class PatchRecorder:
    """Thread-safe JSONL-накопитель patch-событий (per-project + глобальный)."""

    def __init__(
        self,
        output_path: Path,
        *,
        learning_cases_path: Optional[Path] = None,
        language: str = "",
        llm_model: str = "",
        prompt_profile: str = "",
        project_id: str = "",
    ) -> None:
        self._path = output_path
        self._learning_path = learning_cases_path
        self._lock = threading.Lock()
        self._language = language
        self._llm_model = llm_model
        self._prompt_profile = prompt_profile
        self._project_id = project_id

        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._learning_path:
            self._learning_path.parent.mkdir(parents=True, exist_ok=True)

        # Заголовок схемы при создании нового per-project файла
        if not self._path.exists():
            self._write_to(self._path, {
                "_type": "schema",
                "schema_version": SCHEMA_VERSION,
                "fields": [
                    "ts", "project_id", "error", "patch",
                    "validation", "decision", "context", "learning",
                ],
            })

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        project_id: str,
        error: Dict[str, Any],
        patch_source: str,
        patch_intent: str,
        patch_confidence: float,
        error_count_before: Optional[int],
        error_count_after: Optional[int],
        target_fixed: bool,
        new_errors_introduced: int,
        regression: bool,
        verdict: str,
        reason: str,
        rollback: bool,
        iteration: int,
        global_cycle: int,
        total_errors_at_decision: int,
        duration_s: Optional[float] = None,
        # Поля для learning_cases.jsonl
        original_context: str = "",
        patch_candidate: str = "",
        tests_passed: Optional[bool] = None,
        # v1.3: расширенные поля для chain-reconstruction
        attempt_number: int = 0,
        new_error_codes: Optional[list] = None,
        patch_applied: bool = True,
        # v1.4: полная цепочка этапов обработки
        stages: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = _now_iso()
        net_delta: Optional[int] = None
        if error_count_before is not None and error_count_after is not None:
            net_delta = error_count_after - error_count_before
        runtime_ms = int(duration_s * 1000) if duration_s is not None else None

        # ── 1. Per-project patch_events.jsonl ──────────────────────────
        patch_event: Dict[str, Any] = {
            "_type": "patch_event",
            "schema_version": SCHEMA_VERSION,
            "ts": ts,
            "project_id": project_id,

            "error": {
                "code": error.get("code") or "",
                "category": error.get("error_class") or error.get("error_type") or "",
                "file": error.get("file") or "",
                "line": int(error.get("line") or 0),
                "col": int(error.get("column") or 0),
                "message": (error.get("message") or "")[:200],
                "severity": error.get("severity") or "",
                "signature": _sig(error),
            },

            "patch": {
                "source": patch_source,
                "intent": (patch_intent or "")[:200],
                "confidence": round(float(patch_confidence), 4),
            },

            "validation": {
                "error_count_before": error_count_before,
                "error_count_after": error_count_after,
                "net_delta": net_delta,
                "target_fixed": target_fixed,
                "new_errors_introduced": new_errors_introduced,
                "regression": regression,
            },

            "decision": {
                "verdict": verdict,
                "reason": reason,
                "rollback": rollback,
            },

            "context": {
                "iteration": iteration,
                "global_cycle": global_cycle,
                "total_errors_at_decision": total_errors_at_decision,
            },
        }
        if runtime_ms is not None:
            patch_event["timing"] = {"runtime_ms": runtime_ms}
        if extra:
            patch_event["extra"] = extra

        self._write_to(self._path, patch_event)

        # ── 2. Глобальный runtime/learning_cases.jsonl ─────────────────
        if self._learning_path:
            lc: Dict[str, Any] = {
                # Мета-идентификаторы (самодостаточность кейса)
                "schema_version": SCHEMA_VERSION,
                "timestamp": ts,
                "project_id": project_id,
                "language": self._language,

                # Исходная проблема
                "error_signature": _sig(error),
                "error_code": error.get("code") or "",
                "error_message": (error.get("message") or "")[:300],
                "error_category": error.get("error_class") or error.get("error_type") or "",
                "file_path": error.get("file") or "",
                "error_line": int(error.get("line") or 0),
                "original_context": (original_context or "")[:_MAX_CODE_CHARS],

                # Действия системы
                "patch_source": patch_source,
                "patch_intent": (patch_intent or "")[:200],
                "patch_applied": patch_applied,
                "patch_candidate": (patch_candidate or "")[:_MAX_PATCH_CHARS],
                "attempt_number": attempt_number,

                # Результат проверки
                "validation_result": {
                    "error_count_before": error_count_before,
                    "error_count_after": error_count_after,
                    "target_removed": target_fixed,
                    "net_delta": net_delta,
                    "new_errors_count": new_errors_introduced,
                    "new_error_codes": (new_error_codes or [])[:20],
                    "regression": regression,
                },
                "target_removed": target_fixed,
                "net_delta": net_delta,
                "new_errors": new_errors_introduced,
                "new_error_codes": (new_error_codes or [])[:20],
                "tests_passed": tests_passed,
                "rollback_used": rollback,

                # Итоговое решение и причина
                "decision": verdict,
                "decision_reason": reason,

                # Контекст позиции в пайплайне
                "iteration": iteration,
                "global_cycle": global_cycle,
                "total_errors_at_decision": total_errors_at_decision,

                # Параметры системы
                "confidence": round(float(patch_confidence), 4),
                "runtime_ms": runtime_ms,
                "llm_model": self._llm_model,
                "prompt_profile": self._prompt_profile,

                # v1.4: полная цепочка этапов обработки
                # Каждый этап — самодостаточная запись для аудита и сравнения стратегий
                "stages": stages or {
                    "error": {
                        "code": error.get("code") or "",
                        "class": error.get("error_class") or "",
                        "type": error.get("error_type") or "",
                        "severity": error.get("severity") or "",
                        "file": error.get("file") or "",
                        "line": int(error.get("line") or 0),
                    },
                    "generate": {
                        "method": patch_source,
                        "intent": (patch_intent or "")[:200],
                        "confidence": round(float(patch_confidence), 4),
                    },
                    "apply": {
                        "success": patch_applied,
                        "patch_applied": patch_applied,
                    },
                    "validate": {
                        "before": error_count_before,
                        "after": error_count_after,
                        "target_removed": target_fixed,
                        "new_errors": new_errors_introduced,
                        "new_error_codes": (new_error_codes or [])[:10],
                        "regression": regression,
                        "net_delta": net_delta,
                    },
                    "decide": {
                        "verdict": verdict,
                        "reason": reason,
                        "rollback": rollback,
                        "confidence": round(float(patch_confidence), 4),
                    },
                },
            }
            self._write_to(self._learning_path, lc)

    def close(self) -> None:
        self._write_to(self._path, {"_type": "session_end", "ts": _now_iso()})

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _write_to(self, path: Path, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception as e:
                logger.debug("PatchRecorder: не удалось записать в %s: %s", path, e)


# ------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + \
           f".{int((time.time() % 1) * 1000):03d}000"


def _sig(error: Dict[str, Any]) -> str:
    f = error.get("file") or ""
    l = error.get("line") or 0
    c = error.get("code") or error.get("message") or "?"
    return f"{f}::{l}::{c}"
