"""
Stage L — agent orchestration smoke (L.1 + L.2 + L.3 + L.4).

Детерминированно, без LLM/сети: оркестратор гоняется со стаб-fixer'ом.

Покрывает:
* L.1 StateProjection — frontmatter round-trip, дефолты, журнал, контекст,
  устойчивость к битому/отсутствующему frontmatter;
* L.3 modes — gating can(), system_prompt, refusal;
* L.4 explanation — сборка {what,why,how} из error/EditSet/review + render;
* L.2 FixOrchestrator — 5 ошибок по одной, запись каждой, REJECT→retry→FAILED,
  NEEDS_REVIEW-маршрут, resume (пропуск done), gating (Chat → отказ, файлы не
  тронуты).

Запуск: python3 tests/test_phase_l_agent.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent import modes
from agent.state_projection import StateProjection, default_head
from agent.explanation import build_explanation, render_markdown
from agent.fix_orchestrator import (
    FixOrchestrator, FixResult, error_signature,
    ACCEPT, NEEDS_REVIEW, REJECT, FAILED,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _err(code, file="m.py", line=1, message="problem", error_class="BLOCKING"):
    return {"code": code, "file": file, "line": line, "message": message,
            "error_class": error_class}


# --- L.1 StateProjection --------------------------------------------
def test_projection_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        check("proj_absent_default", sp.load_head()["mode"] == "fix")
        sp.init_if_absent("fix")
        check("proj_file_created", sp.state_path.exists())
        head = sp.load_head()
        head["next_action"] = "E0609:src/main.rs:4"
        head["counters"]["accepted"] = 2
        sp.save_head(head)
        again = sp.load_head()
        check("proj_head_persist", again["next_action"] == "E0609:src/main.rs:4")
        check("proj_counters_persist", again["counters"]["accepted"] == 2)
        sp.append_journal("## [шаг 1] X — ACCEPT\n- **Что:** ...\n")
        check("proj_journal_persist", "[шаг 1]" in sp.load_journal())
        # голова не пострадала после дописывания прозы
        check("proj_head_after_journal", sp.load_head()["next_action"] == "E0609:src/main.rs:4")


def test_projection_robust_and_context():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        # битый frontmatter → дефолт, без исключения
        sp.dir.mkdir(parents=True, exist_ok=True)
        sp.state_path.write_text("---\n{not json}\n---\n\n# тело\n", encoding="utf-8")
        check("proj_broken_frontmatter_default", sp.load_head() == default_head())
        check("proj_broken_body_kept", "тело" in sp.load_journal())
        # контекст-файл
        sp.write_context("# Контекст\nstack: rust\n")
        check("proj_context_roundtrip", "stack: rust" in sp.read_context())


# --- L.3 modes ------------------------------------------------------
def test_modes_gating():
    check("chat_cannot_run", modes.can(modes.Mode.CHAT, modes.Capability.RUN_PIPELINE) is False)
    check("chat_can_discuss", modes.can(modes.Mode.CHAT, modes.Capability.DISCUSS) is True)
    check("fix_can_run", modes.can(modes.Mode.FIX, modes.Capability.RUN_PIPELINE) is True)
    check("fix_cannot_arch", modes.can(modes.Mode.FIX, modes.Capability.DISCUSS_ARCH) is False)
    check("create_can_create", modes.can(modes.Mode.CREATE, modes.Capability.CREATE_FILES) is True)
    check("prompt_nonempty", len(modes.system_prompt(modes.Mode.FIX)) > 0)
    check("refusal_suggests_fix", "fix" in modes.refusal(modes.Mode.CHAT, modes.Capability.RUN_PIPELINE).lower())


# --- L.4 explanation ------------------------------------------------
def test_explanation_assembly():
    err = _err("E0609", file="src/main.rs", line=4,
               message="no field `player_name` on type `Player`")
    edit = {"intent": "rename access to .name", "edits": [{"rationale": "field is name"}],
            "risks": ["no other usages"]}
    review = {"verdict": "ok", "reasons": ["fix matches struct definition"]}
    e = build_explanation(err, edit, review)
    check("expl_what_has_code", "E0609" in e["what"])
    check("expl_why_has_reason", "struct definition" in e["why"])
    check("expl_how_has_intent", "rename access" in e["how"])
    check("expl_how_has_risk", "usages" in e["how"])
    md = render_markdown(e)
    check("expl_md_three_fields",
          "**Что:**" in md and "**Почему:**" in md and "**Как:**" in md)
    # без edit_set / review — деградирует, но не падает
    e2 = build_explanation(_err("F401", message="unused import"))
    check("expl_degraded_ok", "F401" in e2["what"] and e2["how"])


# --- L.2 FixOrchestrator --------------------------------------------
class _ScriptedFixer:
    """Возвращает вердикт по коду ошибки; пишет порядок вызовов."""
    def __init__(self, verdict_by_code):
        self.verdict_by_code = verdict_by_code
        self.calls = []

    def __call__(self, error):
        self.calls.append(error["code"])
        v = self.verdict_by_code.get(error["code"], ACCEPT)
        return FixResult(verdict=v, edit_set={"intent": f"fix {error['code']}"})


def test_orchestrator_five_one_by_one():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        errs = [_err("A", line=1), _err("B", line=2), _err("C", line=3),
                _err("D", line=4), _err("E", line=5)]
        fixer = _ScriptedFixer({"A": ACCEPT, "B": NEEDS_REVIEW, "C": ACCEPT,
                                "D": REJECT, "E": ACCEPT})
        orch = FixOrchestrator(sp, fixer, mode=modes.Mode.FIX, max_attempts=2)
        summary = orch.run(errs)
        # по одной, в порядке очереди (D вызван дважды из-за retry)
        check("orch_order", fixer.calls == ["A", "B", "C", "D", "D", "E"])
        check("orch_steps", summary.steps == 5)
        check("orch_accepted", summary.accepted == 3)        # A, C, E
        check("orch_needs_review", summary.needs_review == 1)  # B
        check("orch_failed", summary.failed == 1)             # D (REJECT×2)
        # каждый шаг записан в журнал
        journal = sp.load_journal()
        check("orch_journal_5_steps", journal.count("## [шаг") == 5)
        check("orch_journal_has_explanation", journal.count("### Объяснение") == 5)
        # после прогона — простой
        head = sp.load_head()
        check("orch_idle_after", head["next_action"] is None and head["queue"] == [])
        check("orch_done_5", len(head["done"]) == 5)


def test_orchestrator_reject_retried():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        fixer = _ScriptedFixer({"BAD": REJECT})
        orch = FixOrchestrator(sp, fixer, mode=modes.Mode.FIX, max_attempts=3)
        summary = orch.run([_err("BAD")])
        check("retry_count", fixer.calls == ["BAD", "BAD", "BAD"])  # max_attempts=3
        check("retry_failed", summary.failed == 1 and summary.accepted == 0)


def test_orchestrator_resume_skips_done():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="fix")
        errs2 = [_err("A", line=1), _err("B", line=2)]
        FixOrchestrator(sp, _ScriptedFixer({"A": ACCEPT, "B": ACCEPT}),
                        mode=modes.Mode.FIX).run(errs2)
        # вторая сессия: A,B уже done → обрабатываем только C
        fixer2 = _ScriptedFixer({"C": ACCEPT})
        errs3 = [_err("A", line=1), _err("B", line=2), _err("C", line=3)]
        summary = FixOrchestrator(sp, fixer2, mode=modes.Mode.FIX).run(errs3)
        check("resume_only_new_called", fixer2.calls == ["C"])
        check("resume_steps", summary.steps == 1)
        head = sp.load_head()
        check("resume_done_total", len(head["done"]) == 3)


def test_orchestrator_mode_gated():
    with tempfile.TemporaryDirectory() as d:
        sp = StateProjection(d, name="chat")
        fixer = _ScriptedFixer({"A": ACCEPT})
        orch = FixOrchestrator(sp, fixer, mode=modes.Mode.CHAT)
        summary = orch.run([_err("A")])
        check("chat_refused", summary.refused is True)
        check("chat_no_fix_called", fixer.calls == [])
        # gating до любых записей — файл состояния не создан
        check("chat_no_state_file", not sp.state_path.exists())


if __name__ == "__main__":
    print("Stage L agent orchestration smoke:")
    test_projection_roundtrip()
    test_projection_robust_and_context()
    test_modes_gating()
    test_explanation_assembly()
    test_orchestrator_five_one_by_one()
    test_orchestrator_reject_retried()
    test_orchestrator_resume_skips_done()
    test_orchestrator_mode_gated()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage L agent: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
