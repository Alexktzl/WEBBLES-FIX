"""
Stage L.2 — адаптер к пайплайну (`agent/pipeline_fixer.py`), детерминированно.

Проверяет перевод `context.metadata` → FixResult и интеграцию адаптера с
FixOrchestrator на фейковом `run_single` (без реального пайплайна/LLM).

Запуск: python3 tests/test_phase_l_adapter.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent import modes
from agent.state_projection import StateProjection
from agent.fix_orchestrator import FixOrchestrator, ACCEPT, NEEDS_REVIEW, REJECT, FAILED
from agent.pipeline_fixer import result_from_context, PipelineFixer

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class _Ctx:
    """Минимальный фейк PipelineContext — только metadata."""
    def __init__(self, metadata):
        self.metadata = metadata


def _err(code, file="m.py", line=1):
    return {"code": code, "file": file, "line": line, "message": "x", "error_class": "BLOCKING"}


# --- result_from_context --------------------------------------------
def test_result_mapping():
    ctx = _Ctx({"last_decision": "ACCEPT",
                "structured_edit": {"intent": "fix it", "edits": [], "risks": []},
                "review": {"verdict": "ok", "reasons": ["looks right"]}})
    r = result_from_context(ctx)
    check("map_accept", r.verdict == ACCEPT)
    check("map_editset", r.edit_set and r.edit_set["intent"] == "fix it")
    check("map_review", r.review and r.review["verdict"] == "ok")

    check("map_needs_review", result_from_context(_Ctx({"last_decision": "NEEDS_REVIEW"})).verdict == NEEDS_REVIEW)
    check("map_reject", result_from_context(_Ctx({"last_decision": "REJECT"})).verdict == REJECT)
    # нет решения → консервативно REJECT
    check("map_missing_reject", result_from_context(_Ctx({})).verdict == REJECT)
    check("map_none_ctx_reject", result_from_context(None).verdict == REJECT)
    # мусорный verdict → REJECT + detail
    bad = result_from_context(_Ctx({"last_decision": "WAT"}))
    check("map_garbage_reject", bad.verdict == REJECT and "no decision" in bad.detail)
    # review неверного типа → None, без падения
    check("map_bad_review_none", result_from_context(_Ctx({"last_decision": "ACCEPT", "review": "x"})).review is None)


# --- PipelineFixer (callable) + изоляция сбоя -----------------------
def test_pipeline_fixer_callable():
    pf = PipelineFixer(lambda e: _Ctx({"last_decision": "ACCEPT"}))
    check("pf_accept", pf(_err("A")).verdict == ACCEPT)

    def _boom(e):
        raise RuntimeError("pipeline exploded")
    pf_boom = PipelineFixer(_boom)
    res = pf_boom(_err("B"))
    check("pf_exception_reject", res.verdict == REJECT and "failed" in res.detail)


# --- интеграция адаптера с оркестратором ----------------------------
def test_adapter_drives_orchestrator():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        # run_single по коду ошибки возвращает разные вердикты через metadata
        verdicts = {"A": "ACCEPT", "B": "NEEDS_REVIEW", "C": "REJECT"}

        def run_single(error):
            return _Ctx({"last_decision": verdicts[error["code"]],
                         "structured_edit": {"intent": f"fix {error['code']}"}})

        fixer = PipelineFixer(run_single)
        orch = FixOrchestrator(sp, fixer, mode=modes.Mode.FIX, max_attempts=2)
        summary = orch.run([_err("A"), _err("B"), _err("C")])
        check("adapter_accepted", summary.accepted == 1)
        check("adapter_needs_review", summary.needs_review == 1)
        check("adapter_failed", summary.failed == 1)   # C: REJECT×2 → FAILED
        journal = sp.load_journal()
        check("adapter_journal_3", journal.count("## [шаг") == 3)
        check("adapter_intent_in_journal", "fix A" in journal)


if __name__ == "__main__":
    print("Stage L.2 pipeline-adapter smoke:")
    test_result_mapping()
    test_pipeline_fixer_callable()
    test_adapter_drives_orchestrator()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage L adapter: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
