"""
Stage L.5+ — пер-ошибочный live-стриминг событий из движка.

Покрытие:
1) `core.engine.event_emission.emit_after_stage` — хелпер изолированно:
   error_dequeued (один раз на sig), patch_proposed (только после
   GENERATING_PATCH с патчем), verdict + applied/needs_review/failed
   (только после DECIDING с last_decision).
2) `agent.run_fix.run_fix_agent` — сквозная wiring через FakeController:
   а) новая сигнатура с `event_emitter` → стримит live, _project_result НЕ
      повторяет per-item события;
   б) старая сигнатура без `event_emitter` → fallback на агрегатный путь.

Запуск: python3 tests/test_phase_l_live_stream.py
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from core.engine.event_emission import emit_after_stage
from agent import modes
from agent.events import CollectingSink, EventType
from agent.run_fix import run_fix_agent

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ----------------------------------------------------------------------
# 1. Хелпер emit_after_stage изолированно
# ----------------------------------------------------------------------
def _ctx(error=None, metadata=None, generated_patch=None):
    return SimpleNamespace(
        selected_error=error,
        metadata=metadata or {},
        generated_patch=generated_patch,
    )


def test_emit_helper_no_error():
    """Если selected_error нет — никаких событий."""
    seen = []
    sent = set()
    emit_after_stage(lambda t, d: seen.append((t, d)),
                     "GENERATING_PATCH", _ctx(),
                     error_signature_fn=lambda e: "?", emitted_sigs=sent)
    check("helper_no_error_silent", seen == [])


def test_emit_helper_none_emitter():
    """emitter=None — функция не должна падать и ничего не делать."""
    sent = set()
    emit_after_stage(None, "DECIDING",
                     _ctx(error={"file": "a.py", "line": 1, "code": "X"},
                          metadata={"last_decision": "ACCEPT"}),
                     error_signature_fn=lambda e: "s1", emitted_sigs=sent)
    check("helper_none_emitter_noop", sent == set())


def test_emit_helper_dequeued_dedup():
    """error_dequeued эмитится один раз на сигнатуру."""
    seen = []
    sent = set()
    err = {"file": "a.py", "line": 5, "code": "F401", "message": "unused import"}
    ctx = _ctx(error=err)
    emit_after_stage(lambda t, d: seen.append((t, d)),
                     "ANALYZING", ctx,
                     error_signature_fn=lambda e: "sigA", emitted_sigs=sent)
    emit_after_stage(lambda t, d: seen.append((t, d)),
                     "PRIORITIZING", ctx,
                     error_signature_fn=lambda e: "sigA", emitted_sigs=sent)
    types = [t for t, _ in seen]
    check("helper_dequeued_once", types.count("error_dequeued") == 1)
    payload = [d for t, d in seen if t == "error_dequeued"][0]
    check("helper_dequeued_payload",
          payload["file"] == "a.py" and payload["line"] == 5 and
          payload["code"] == "F401" and "unused" in payload["message"])


def test_emit_helper_patch_proposed():
    """patch_proposed только после GENERATING_PATCH с непустым generated_patch."""
    seen = []
    sent = set()
    err = {"file": "a.py", "line": 5, "code": "F401"}
    # без generated_patch — нет patch_proposed
    emit_after_stage(lambda t, d: seen.append((t, d)),
                     "GENERATING_PATCH",
                     _ctx(error=err, generated_patch=None),
                     error_signature_fn=lambda e: "s1", emitted_sigs=sent)
    types = [t for t, _ in seen]
    check("helper_no_patch_no_proposed", "patch_proposed" not in types)

    # с generated_patch + structured_edit.intent → есть payload.intent
    seen.clear()
    sent.clear()
    emit_after_stage(lambda t, d: seen.append((t, d)),
                     "GENERATING_PATCH",
                     _ctx(error=err,
                          generated_patch="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
                          metadata={"structured_edit": {"intent": "fix import"},
                                    "patch_source": "structured_llm"}),
                     error_signature_fn=lambda e: "s1", emitted_sigs=sent)
    proposed = [d for t, d in seen if t == "patch_proposed"]
    check("helper_patch_proposed_emitted", len(proposed) == 1)
    check("helper_patch_proposed_intent", proposed[0]["intent"] == "fix import")
    check("helper_patch_proposed_source", proposed[0]["patch_source"] == "structured_llm")


def test_emit_helper_verdicts():
    """DECIDING → verdict + applied/needs_review/failed по last_decision."""
    cases = [
        ("ACCEPT", "applied"),
        ("NEEDS_REVIEW", "needs_review"),
        ("REJECT", "failed"),
    ]
    for decision, terminal in cases:
        seen = []
        sent = set()
        err = {"file": "a.py", "line": 1, "code": "X"}
        emit_after_stage(
            lambda t, d: seen.append((t, d)),
            "DECIDING",
            _ctx(error=err, metadata={
                "last_decision": decision,
                "confidence": 0.87,
                "review": {"verdict": "ok"},
            }),
            error_signature_fn=lambda e: f"s-{decision}",
            emitted_sigs=sent,
        )
        types = [t for t, _ in seen]
        check(f"helper_{decision}_verdict_emitted", "verdict" in types)
        check(f"helper_{decision}_terminal_{terminal}", terminal in types)
        v = [d for t, d in seen if t == "verdict"][0]
        check(f"helper_{decision}_payload_carries_verdict", v["verdict"] == decision)
        check(f"helper_{decision}_payload_carries_conf", abs(v["confidence"] - 0.87) < 1e-6)


def test_emit_helper_deciding_without_last_decision():
    """DECIDING без last_decision не эмитит verdict."""
    seen = []
    sent = set()
    err = {"file": "a.py", "line": 1, "code": "X"}
    emit_after_stage(lambda t, d: seen.append((t, d)),
                     "DECIDING", _ctx(error=err, metadata={}),
                     error_signature_fn=lambda e: "s2", emitted_sigs=sent)
    types = [t for t, _ in seen]
    check("helper_deciding_silent_verdict", "verdict" not in types)
    # error_dequeued всё равно отправлен — есть selected_error
    check("helper_deciding_still_dequeues", "error_dequeued" in types)


def test_emit_helper_swallows_emitter_errors():
    """Эмиттер кинул исключение — не валим вызывающего."""
    def broken(t, d):
        raise RuntimeError("boom")
    sent = set()
    err = {"file": "a.py", "line": 1, "code": "X"}
    # Не должно бросить наружу.
    emit_after_stage(broken, "DECIDING",
                     _ctx(error=err, metadata={"last_decision": "ACCEPT"}),
                     error_signature_fn=lambda e: "s3", emitted_sigs=sent)
    check("helper_swallow_emitter_exc", True)


# ----------------------------------------------------------------------
# 2. Сквозная wiring через run_fix_agent + FakeController
# ----------------------------------------------------------------------
_FAKE_RESULT = {
    "status": "COMPLETED",
    "accepted_patches": 1,
    "rejected_patches": 0,
    "needs_review_count": 1,
    "needs_review_items": [{"file": "src/a.py", "line": 9, "error_code": "sql_injection"}],
    "remaining_errors": [],
    "initial_error_count": 2,
    "final_error_count": 1,
}


class _LiveController:
    """Принимает event_emitter и сам шлёт пер-ошибочные события."""
    def __init__(self, result):
        self.result = result
        self.calls = []
    def load_config(self):
        return {"pipeline": {}}
    def run_pipeline(self, project_path, language, config, *, event_emitter=None):
        self.calls.append((str(project_path), language, event_emitter is not None))
        if event_emitter:
            # имитируем эмиссию: одна ошибка → patch_proposed → verdict → applied
            base = {"file": "src/a.py", "line": 9, "code": "sql_injection"}
            event_emitter("error_dequeued", base)
            event_emitter("patch_proposed", dict(base, intent="use parametrized query",
                                                  patch_source="structured_llm"))
            event_emitter("verdict", dict(base, verdict="ACCEPT", confidence=0.9, reviewer="ok"))
            event_emitter("applied", base)
        return self.result


class _LegacyController:
    """Старая сигнатура без event_emitter — kwarg вызовет TypeError."""
    def __init__(self, result):
        self.result = result
        self.calls = []
    def load_config(self):
        return {"pipeline": {}}
    def run_pipeline(self, project_path, language, config):
        self.calls.append((str(project_path), language))
        return self.result


def _cleanup_project_runtime(d):
    """run_fix_agent пишет state в системный runtime/<basename(d)>/, не в d —
    без явной чистки тест оставляет мусор в реальном репозитории (см.
    project_tech_debt_backlog в памяти, пункт 8)."""
    import shutil
    from core.pipeline_engine import PipelineEngine
    shutil.rmtree(PipelineEngine._project_runtime_dir(d), ignore_errors=True)


def test_run_fix_live_streaming():
    """Новая сигнатура: события идут live, _project_result НЕ дублирует per-item."""
    with tempfile.TemporaryDirectory() as d:
        try:
            ctrl = _LiveController(_FAKE_RESULT)
            sink = CollectingSink()
            run_fix_agent(d, "python", mode=modes.Mode.FIX, controller=ctrl,
                          emit=sink, changes_root=Path(d))
            types = sink.types
            check("live_run_called_with_emitter", ctrl.calls and ctrl.calls[0][2] is True)
            check("live_run_started_first", types[0] == "run_started")
            check("live_dequeued_once", types.count("error_dequeued") == 1)
            check("live_patch_proposed_once", types.count("patch_proposed") == 1)
            check("live_verdict_once", types.count("verdict") == 1)
            check("live_applied_once", types.count("applied") == 1)
            # _project_result НЕ должен повторить needs_review/error_dequeued
            # для нашего одного nr_item — он был передан live.
            check("live_no_extra_needs_review_event",
                  types.count("needs_review") == 0)
            check("live_finished_last", types[-1] == "run_finished")
        finally:
            _cleanup_project_runtime(d)


def test_run_fix_legacy_fallback():
    """Старая сигнатура: TypeError → fallback на агрегатный путь с per-item."""
    with tempfile.TemporaryDirectory() as d:
        try:
            ctrl = _LegacyController(_FAKE_RESULT)
            sink = CollectingSink()
            run_fix_agent(d, "python", mode=modes.Mode.FIX, controller=ctrl,
                          emit=sink, changes_root=Path(d))
            types = sink.types
            check("legacy_two_calls",
                  len(ctrl.calls) == 1)  # один успешный legacy-вызов
            # _project_result повторит per-item, потому что live не было
            check("legacy_needs_review_event",
                  types.count("needs_review") == 1)
            check("legacy_dequeued_event",
                  types.count("error_dequeued") == 1)
        finally:
            _cleanup_project_runtime(d)


if __name__ == "__main__":
    print("Stage L.5+ live-streaming smoke:")
    test_emit_helper_no_error()
    test_emit_helper_none_emitter()
    test_emit_helper_dequeued_dedup()
    test_emit_helper_patch_proposed()
    test_emit_helper_verdicts()
    test_emit_helper_deciding_without_last_decision()
    test_emit_helper_swallows_emitter_errors()
    test_run_fix_live_streaming()
    test_run_fix_legacy_fallback()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage L live-stream: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
