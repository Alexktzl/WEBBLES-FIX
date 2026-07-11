"""
Stage L.2 — точка сборки live-агента (`agent/run_fix.py`), детерминированно.

Реальный движок/сеть/LLM НЕ запускаются: подставляем FakeController, который
возвращает канонический result-dict. Проверяем gating, проекцию результата и
событийный поток.

Запуск: python3 tests/test_phase_l_runfix.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent import modes
from agent.events import CollectingSink, EventType
from agent.state_projection import StateProjection
from agent.run_fix import run_fix_agent, _project_result

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


_RESULT = {
    "status": "COMPLETED",
    "accepted_patches": 3,
    "rejected_patches": 1,
    "needs_review_count": 2,
    "needs_review_items": [
        {"file": "src/a.py", "line": 10, "error_code": "sql_injection"},
        {"file": "src/b.js", "line": 4, "error_code": "xss"},
    ],
    "remaining_errors": [{"file": "src/c.rs", "line": 7, "code": "E0382", "message": "moved"}],
    "initial_error_count": 6,
    "final_error_count": 1,
}


class _FakeController:
    def __init__(self, result):
        self.result = result
        self.calls = []
    def load_config(self):
        return {"pipeline": {}}
    def run_pipeline(self, project_path, language, config):
        self.calls.append((str(project_path), language))
        return self.result


# --- _project_result -------------------------------------------------
def test_project_result():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        sp.init_if_absent("fix")
        sink = CollectingSink()
        _project_result(sp, _RESULT, sink)
        types = sink.types
        check("pr_needs_review_events", types.count("needs_review") == 2)
        check("pr_verdict_events", types.count("verdict") == 2)
        check("pr_dequeued_events", types.count("error_dequeued") == 2)
        check("pr_run_finished_last", types[-1] == "run_finished")
        fin = sink.of(EventType.RUN_FINISHED)[0].data
        check("pr_finished_accepted", fin["accepted"] == 3)
        check("pr_finished_needs_review", fin["needs_review"] == 2)
        check("pr_finished_failed", fin["failed"] == 1)   # remaining
        head = sp.load_head()
        check("pr_head_counters",
              head["counters"]["accepted"] == 3 and head["counters"]["needs_review"] == 2)
        check("pr_head_last_verdict", head["last_verdict"] == "COMPLETED")
        journal = sp.load_journal()
        check("pr_journal_summary", "Прогон движка" in journal and "sql_injection" in journal)


# --- run_fix_agent: gating + wiring ---------------------------------
def test_run_fix_gating():
    with tempfile.TemporaryDirectory() as d:
        ctrl = _FakeController(_RESULT)
        out = run_fix_agent(d, "python", mode=modes.Mode.CHAT, controller=ctrl)
        check("gate_refused", out.get("refused") is True)
        check("gate_no_pipeline_call", ctrl.calls == [])
        # в Chat-режиме файл состояния не создаём (gating до записи)
        check("gate_no_state", not (Path(d) / ".webbles" / "fix_state.md").exists())


def test_run_fix_runs_and_projects():
    # changes_root=d держит chat/changes.jsonl + file_snapshots внутри temp-папки
    # теста — без этого run_fix_agent пишет в настоящий chat/ репозитория
    # (см. project_tech_debt_backlog в памяти, пункт 8, 2026-06-19/20).
    from core.pipeline_engine import PipelineEngine
    import shutil
    with tempfile.TemporaryDirectory() as d:
        try:
            ctrl = _FakeController(_RESULT)
            sink = CollectingSink()
            out = run_fix_agent(d, "python", mode=modes.Mode.FIX, controller=ctrl,
                                emit=sink, changes_root=Path(d))
            check("run_called_once", len(ctrl.calls) == 1 and ctrl.calls[0][1] == "python")
            check("run_returns_result", out["status"] == "COMPLETED")
            check("run_started_first", sink.types[0] == "run_started")
            check("run_finished_last", sink.types[-1] == "run_finished")
            sp = StateProjection(PipelineEngine._project_runtime_dir(d), name="fix")
            check("run_state_written", "Прогон движка" in sp.load_journal())
        finally:
            # _project_runtime_dir живёт в системном runtime/ (не в d) по
            # дизайну (см. P0.6 в run_fix.py) — чистим вручную, иначе тест
            # оставляет runtime/<basename(d)>/ в реальном репозитории.
            shutil.rmtree(PipelineEngine._project_runtime_dir(d), ignore_errors=True)


if __name__ == "__main__":
    print("Stage L.2 run_fix assembly smoke:")
    test_project_result()
    test_run_fix_gating()
    test_run_fix_runs_and_projects()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage L run_fix: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
