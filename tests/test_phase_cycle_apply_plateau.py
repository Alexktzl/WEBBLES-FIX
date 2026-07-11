"""
Плато применённых патчей внутри одного цикла (2026-07-08, loguru №7).

Инцидент: _single_run завершается только на ACCEPT — проект, где патчи
систематически применяются-и-умирают (~50 BLOCKING-генераций за цикл,
каждая Apply→full-scan→rollback ~15-90s), крутил ПЕРВЫЙ глобальный цикл
до project_timeout: cycles_run=0, 1873s впустую, AdaptiveCycleController
не получал ни одной межцикловой точки для своего plateau-детекта.

Инвариант: после MAX_APPLIED_FAILURES_PER_CYCLE применённых патчей без
единого ACCEPT цикл мягко завершается (State.COMPLETED) — но НЕ посреди
разрешения патча (уважает _PATCH_RESOLUTION_STATES, как circuit-open).
ACCEPT до порога завершает цикл штатно, плато не объявляется.

Запуск: python tests/test_phase_cycle_apply_plateau.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from core.pipeline_engine import PipelineEngine  # noqa: E402
from core.state_machine import State  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


class _Ctx:
    """Минимальный контекст: скриптованная последовательность состояний.
    Каждый вызов стадии двигает по сценарию GENERATING→APPLYING→NEXT→..."""

    def __init__(self, script):
        self.script = list(script)
        self.current_state = self.script.pop(0)
        self.metadata = {}
        self.generated_patch = "--- a/x\n+++ b/x\n"
        self.selected_error = {"file": "x.py", "line": 1, "code": "E999"}
        self.project_path = Path(".")
        self.final_states = []

    def advance(self):
        if self.script:
            self.current_state = self.script.pop(0)
        return self

    def add_state_to_history(self, state):
        self.current_state = state
        self.final_states.append(state)
        return self

    def update(self, **kw):
        if "metadata" in kw and kw["metadata"] is not None:
            self.metadata = kw["metadata"]
        return self

    def notify(self, *_a, **_k):
        return None


def _engine_with_script(script, accept_at=None):
    eng = PipelineEngine.__new__(PipelineEngine)
    ctx = _Ctx(script)
    eng.context = ctx
    eng.project_path = Path(".")
    eng.dry_run = True
    eng.step_times = {}
    eng.dynamic_params = {}
    eng.circuit_breaker = MagicMock(is_open=MagicMock(return_value=False))
    eng.anti_loop = MagicMock(should_break=MagicMock(return_value=False))
    eng.health_evaluator = MagicMock()
    eng.stability_governor = MagicMock(
        get_params=MagicMock(return_value={"max_iterations": 10}),
        step=MagicMock(return_value={}),
    )
    eng._save_state = lambda: None
    eng._emit_after_stage = lambda *_: None
    eng._build_result = lambda status_override=None: {"status": status_override}
    eng._error_signature = lambda e: "sig"

    applied_counter = {"n": 0}

    class _Stage:
        def execute(self, context):
            if accept_at is not None and context.current_state == State.APPLYING_PATCH:
                applied_counter["n"] += 1
                if applied_counter["n"] == accept_at:
                    m = dict(context.metadata)
                    from core.contract import MetadataKeys
                    m[MetadataKeys.LAST_DECISION] = "ACCEPT"
                    context.metadata = m
            return context.advance()

    eng.stages = {s: _Stage() for s in State}
    return eng, ctx


def _burn_script(n_patches):
    """n_patches раз: GENERATING → APPLYING → NEXT_ERROR (патч умер)."""
    seq = []
    for _ in range(n_patches):
        seq += [State.GENERATING_PATCH, State.APPLYING_PATCH, State.NEXT_ERROR]
    seq += [State.GENERATING_PATCH] * 5  # хвост, до которого дойти не должны
    return seq


def test_plateau_breaks_after_threshold():
    """10 применённых-и-умерших патчей → цикл мягко завершён COMPLETED,
    хвост очереди в этом цикле не обрабатывается."""
    eng, ctx = _engine_with_script(_burn_script(15))
    res = eng._single_run()
    check("plateau_completed", ctx.final_states and ctx.final_states[-1] == State.COMPLETED,
          f"final: {ctx.final_states}")
    check("script_tail_untouched", len(ctx.script) > 0, "цикл дожевал всё — плато не сработало")


def test_no_plateau_below_threshold():
    """9 умерших патчей — ниже порога, цикл дорабатывает сценарий без
    насильственного COMPLETED от плато."""
    eng, ctx = _engine_with_script(
        [State.GENERATING_PATCH, State.APPLYING_PATCH, State.NEXT_ERROR] * 9 + [State.COMPLETED]
    )
    eng._single_run()
    # дошли до скриптового COMPLETED, плато-ветка не добавляла его сама
    check("no_forced_plateau", ctx.final_states == [], f"final: {ctx.final_states}")


def test_accept_before_threshold_exits_normally():
    """ACCEPT на 3-м применении завершает цикл штатным брейком (не плато)."""
    eng, ctx = _engine_with_script(_burn_script(15), accept_at=3)
    res = eng._single_run()
    check("accept_exit_no_plateau_state", ctx.final_states == [], f"final: {ctx.final_states}")


if __name__ == "__main__":
    test_plateau_breaks_after_threshold()
    test_no_plateau_below_threshold()
    test_accept_before_threshold_exits_normally()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"cycle_apply_plateau: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
