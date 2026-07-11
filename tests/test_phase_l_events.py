"""
Stage L.5 — событийный протокол (`agent/events.py`) + эмиссия из оркестратора.

Детерминированно, без сети/UI: подписываем CollectingSink на реальный прогон
FixOrchestrator со стаб-fixer'ом и проверяем последовательность событий и их
JSON-сериализацию.

Запуск: python3 tests/test_phase_l_events.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent import modes
from agent.events import Event, EventType, EventBus, CollectingSink, as_emitter
from agent.state_projection import StateProjection
from agent.fix_orchestrator import FixOrchestrator, FixResult, ACCEPT, NEEDS_REVIEW, REJECT

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _err(code, file="m.py", line=1):
    return {"code": code, "file": file, "line": line, "message": "x", "error_class": "BLOCKING"}


# --- event model -----------------------------------------------------
def test_event_model():
    e = Event(EventType.VERDICT, {"sig": "A", "verdict": "ACCEPT"})
    d = e.to_dict()
    check("event_to_dict_type", d["type"] == "verdict")
    check("event_to_dict_data", d["data"]["verdict"] == "ACCEPT")
    check("event_json", json.loads(json.dumps(d))["type"] == "verdict")


def test_eventbus_isolation():
    bus = EventBus()
    seen = []
    def boom(_e):
        raise RuntimeError("listener crash")
    bus.subscribe(boom)
    bus.subscribe(lambda e: seen.append(e.type))
    bus.emit(Event(EventType.RUN_STARTED, {}))
    check("bus_survives_bad_listener", seen == [EventType.RUN_STARTED])


def test_as_emitter():
    check("as_emitter_none_noop", callable(as_emitter(None)))
    bus = EventBus(); got = []
    bus.subscribe(lambda e: got.append(e))
    em = as_emitter(bus)            # EventBus → .emit
    em(Event(EventType.RUN_FINISHED, {}))
    check("as_emitter_bus", len(got) == 1)


# --- emission from orchestrator -------------------------------------
class _ScriptedFixer:
    def __init__(self, by_code):
        self.by_code = by_code
    def __call__(self, error):
        return FixResult(verdict=self.by_code.get(error["code"], ACCEPT),
                         edit_set={"intent": f"fix {error['code']}"})


def test_orchestrator_emits_stream():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        sink = CollectingSink()
        orch = FixOrchestrator(sp, _ScriptedFixer({"A": ACCEPT, "B": NEEDS_REVIEW}),
                               mode=modes.Mode.FIX, emit=sink)
        orch.run([_err("A"), _err("B")])
        types = sink.types
        check("emit_starts_run", types[0] == "run_started")
        check("emit_ends_run", types[-1] == "run_finished")
        check("emit_two_dequeues", types.count("error_dequeued") == 2)
        check("emit_two_verdicts", types.count("verdict") == 2)
        check("emit_patch_proposed", types.count("patch_proposed") == 2)
        check("emit_applied_once", types.count("applied") == 1)        # A
        check("emit_needs_review_once", types.count("needs_review") == 1)  # B
        check("emit_explanation_each", types.count("explanation") == 2)
        # run_finished несёт сводку
        fin = sink.of(EventType.RUN_FINISHED)[0]
        check("emit_finished_counts",
              fin.data["accepted"] == 1 and fin.data["needs_review"] == 1)
        # explanation несёт Что/Почему/Как
        expl = sink.of(EventType.EXPLANATION)[0]
        check("emit_explanation_fields",
              "what" in expl.data and "why" in expl.data and "how" in expl.data)


def test_no_emitter_backward_compatible():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        # без emit — поведение прежнее, не падает
        summary = FixOrchestrator(sp, _ScriptedFixer({"A": ACCEPT}),
                                  mode=modes.Mode.FIX).run([_err("A")])
        check("no_emit_ok", summary.accepted == 1)


if __name__ == "__main__":
    print("Stage L.5 events smoke:")
    test_event_model()
    test_eventbus_isolation()
    test_as_emitter()
    test_orchestrator_emits_stream()
    test_no_emitter_backward_compatible()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage L events: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
