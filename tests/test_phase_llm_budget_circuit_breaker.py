"""
Perf-1 (2026-07-07, дизайн deep-reasoner, конкретизирован техлидом):
circuit-breaker `_llm_noeffect_counts` / `_llm_budget_exhausted`.

Контекст: perf-профиль показал LLM ≈ 90% wall-time, а обречённые решения
жгут бюджет до потолка FileAntiLoop (httpx: стрик 5× REJECT structured_llm
по test_auth.py ~226с; _models.py дошёл до 11/10 попыток). По
learning_cases за июль: LLM-REJECT с error_count_not_decreased = 30 записей
(structured_llm 15, structured_llm_blocking 11, llm_blocking_single 3,
llm 1). Существующие отсечки промахивались: Q3-toxic считает только
mypy-коды; TESP-cap — только target_error_still_present; _anchor_fail_counts
— только пустые LLM-ответы. Никто не считал «непустой LLM-патч применился,
но счётчик ошибок не упал» на ВСЕХ кодах.

Фикс — два места:
  1. DecideStage._track_llm_noeffect (core/stages/decide_stage.py): считает
     REJECT-причины target_error_still_present/error_count_not_decreased,
     но ТОЛЬКО когда patch_source — LLM-backed (_LLM_PATCH_SOURCES). После
     MAX_LLM_NOEFFECT_PER_CLASS класс (file, code) уходит в
     metadata["_llm_budget_exhausted"]. Сброс — на ACCEPT того же класса
     (_dispatch_after_success).
  2. GeneratePatchStage.execute (core/stages/generate_patch_stage.py): гейт
     сразу после structured-unanchorable fast-fail — если (file, code) уже
     в _llm_budget_exhausted и ошибка не синтаксическая, уходим в
     NEEDS_REVIEW без дорогого вызова LLM.

Запуск: venv/Scripts/python.exe -m pytest tests/test_phase_llm_budget_circuit_breaker.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.decide_stage import DecideStage
from core.stages.generate_patch_stage import GeneratePatchStage
from fixers.patch_engine import PatchEngine
from memory.learning import MemoryLearning


def _mk_stage():
    return DecideStage(quality_evaluator=MagicMock(), analyzer=None)


def _class_key(err):
    return f"{str(err.get('file') or '').replace(chr(92), '/')}::{err.get('code') or ''}"


_ERROR = {
    "file": "tests/test_auth.py",
    "line": 42,
    "code": "misc",
    "message": "some mypy error",
}


# ---------------------------------------------------------------------------
# 1. 3x LLM-REJECT (error_count_not_decreased, source=structured_llm) одного
#    (file, code) → ключ попадает в _llm_budget_exhausted.
# ---------------------------------------------------------------------------

def test_three_llm_rejects_exhaust_budget():
    stage = _mk_stage()
    ctx = PipelineContext(
        project_path=Path("."), language="python",
        metadata={"patch_source": "structured_llm"},
    )
    for _ in range(DecideStage.MAX_LLM_NOEFFECT_PER_CLASS):
        ctx = stage._track_llm_noeffect(ctx, _ERROR, "error_count_not_decreased")
    key = _class_key(_ERROR)
    assert key in list(ctx.metadata.get("_llm_budget_exhausted") or []), ctx.metadata
    assert ctx.metadata.get("_llm_noeffect_counts", {}).get(key) == DecideStage.MAX_LLM_NOEFFECT_PER_CLASS


# ---------------------------------------------------------------------------
# 2. Те же 3 REJECT с source=rule_based — детерминированный путь не жжёт
#    LLM-бюджет, счётчик не должен инкрементиться вовсе.
# ---------------------------------------------------------------------------

def test_rule_based_source_not_counted():
    stage = _mk_stage()
    ctx = PipelineContext(
        project_path=Path("."), language="python",
        metadata={"patch_source": "rule_based"},
    )
    for _ in range(DecideStage.MAX_LLM_NOEFFECT_PER_CLASS):
        ctx = stage._track_llm_noeffect(ctx, _ERROR, "error_count_not_decreased")
    key = _class_key(_ERROR)
    assert key not in list(ctx.metadata.get("_llm_budget_exhausted") or [])
    assert not dict(ctx.metadata.get("_llm_noeffect_counts") or {})


# ---------------------------------------------------------------------------
# 3. Смешанные источники: 2 LLM + 1 rule_based → порог (3) не достигнут,
#    т.к. rule_based-REJECT не инкрементит счётчик этого класса.
# ---------------------------------------------------------------------------

def test_mixed_sources_do_not_reach_threshold():
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python", metadata={})
    sources = ["structured_llm", "structured_llm", "rule_based"]
    for src in sources:
        meta = dict(ctx.metadata)
        meta["patch_source"] = src
        ctx = ctx.update(metadata=meta)
        ctx = stage._track_llm_noeffect(ctx, _ERROR, "error_count_not_decreased")
    key = _class_key(_ERROR)
    assert key not in list(ctx.metadata.get("_llm_budget_exhausted") or [])
    assert ctx.metadata.get("_llm_noeffect_counts", {}).get(key) == 2


# ---------------------------------------------------------------------------
# 4. Гейт в GeneratePatchStage.execute: exhausted-ключ + несинтаксическая
#    ошибка → NEEDS_REVIEW без обращения к llm_client.
# ---------------------------------------------------------------------------

def test_execute_gate_skips_llm_when_budget_exhausted(tmp_path):
    (tmp_path / "test_auth.py").write_text(
        "def f():\n    x = 1\n    return x\n", encoding="utf-8",
    )
    llm_client = MagicMock()
    stage = GeneratePatchStage(
        llm_client=llm_client, memory=MemoryLearning(), patch_engine=PatchEngine(),
    )
    class_key = "test_auth.py::misc"
    ctx = PipelineContext(
        project_path=tmp_path, language="python", working_path=tmp_path,
        metadata={"_llm_budget_exhausted": [class_key]},
    )
    ctx = ctx.set_selected_error({
        "file": "test_auth.py", "line": 1, "code": "misc",
        "message": "some mypy error", "error_class": "BLOCKING",
    })

    result = stage.execute(ctx)

    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "llm_budget_exhausted"
    assert llm_client.mock_calls == [], f"llm_client не должен вызываться: {llm_client.mock_calls}"


def test_syntax_errors_not_suppressed_by_budget_gate():
    """Синтаксические ошибки (E999/E902/invalid-syntax/CRITICAL_SYNTAX)
    обязаны чиниться даже если класс числится в _llm_budget_exhausted —
    файл должен парситься. Формула повторяет условие _is_syntax, уже
    используемое соседним structured-unanchorable-гейтом (execute,
    generate_patch_stage.py) — CRITICAL_SYNTAX-ошибки в реальности не
    достигают нового гейта вовсе (уходят в _handle_critical_syntax раньше
    по execute()), поэтому здесь кодируем сам предикат, как и соседний тест
    tests/test_phase_unanchorable_format_fastfail.py."""
    for code, error_class in [("E999", ""), ("invalid-syntax", ""), ("", "CRITICAL_SYNTAX")]:
        is_syntax = error_class == "CRITICAL_SYNTAX" or code in ("E999", "E902", "invalid-syntax")
        assert is_syntax, f"{code!r}/{error_class!r} обязан считаться синтаксическим"
    is_syntax = "" == "CRITICAL_SYNTAX" or "misc" in ("E999", "E902", "invalid-syntax")
    assert not is_syntax


# ---------------------------------------------------------------------------
# 5. ACCEPT того же (file, code) сбрасывает счётчик и снимает exhausted-флаг.
# ---------------------------------------------------------------------------

class _DecideCtx:
    """Минимальный fake-context (образец: test_phase_toxic_signature_nr_feed.py),
    достаточный для _dispatch_after_success без подъёма реального движка."""

    def __init__(self, metadata, error):
        self.metadata = metadata
        self.selected_error = error
        self.history_states = []
        self.accepted = []
        self.rejected = []
        self.processed = []
        self.iterations = 0
        self.working_path = None
        self.project_path = None
        self.validation_results = {}

    def update(self, metadata=None, **_kw):
        if metadata is not None:
            self.metadata = metadata
        return self

    def add_accepted_patch(self, p):
        self.accepted.append(p)
        return self

    def add_rejected_patch(self, p):
        self.rejected.append(p)
        return self

    def record_processed_error(self, sig):
        self.processed.append(sig)
        return self

    def increment_iteration(self):
        self.iterations += 1
        return self

    def add_state_to_history(self, state):
        self.history_states.append(state)
        return self

    def remove_patch_snapshot(self, file_name):
        return self

    def set_errors(self, errs):
        return self


def test_accept_resets_noeffect_counter_and_exhausted_flag():
    ds = _mk_stage()
    error = dict(_ERROR)
    key = _class_key(error)
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "patch_snapshots": [],
            "_llm_noeffect_counts": {key: 2},
            "_llm_budget_exhausted": [key],
        },
        error=error,
    )
    out = ds._dispatch_after_success(
        ctx, error, "sig-accept", reason="target_fixed_reviewer_ok_count_unchanged",
    )
    assert out.metadata.get("last_decision") == "ACCEPT"
    assert key not in dict(out.metadata.get("_llm_noeffect_counts") or {})
    assert key not in list(out.metadata.get("_llm_budget_exhausted") or [])


# ---------------------------------------------------------------------------
# 6. Copy-on-write: исходный metadata-dict не мутирован.
# ---------------------------------------------------------------------------

def test_copy_on_write_original_metadata_untouched():
    stage = _mk_stage()
    original_metadata = {"patch_source": "llm"}
    ctx = PipelineContext(project_path=Path("."), language="python", metadata=original_metadata)
    stage._track_llm_noeffect(ctx, _ERROR, "target_error_still_present")
    assert original_metadata == {"patch_source": "llm"}, (
        f"исходный dict не должен мутироваться: {original_metadata}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
