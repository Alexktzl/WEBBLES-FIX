"""
Regression-тест: PipelineEngine._single_run() при срабатывании inner-deadline
guard должен проставлять project_timeout_triggered=True и
stop_reason_override='project_timeout' в context.metadata, чтобы _build_result
честно сообщал о таймауте, а не о «голом» FAILED.

До фикса: inner-guard (pipeline_engine.py, строки 484-487) только добавлял
State.FAILED в state_history и делал break, не трогая metadata.
_project_timeout_fired выставлялся только в outer _global_fix_loop (строки
699-706), которая при этом сценарии никогда не доходила до своей проверки
(inner-guard уже вернул FAILED → outer-loop делал break на строке 782).
Итог: project_timeout_triggered=False и stop_reason='running' — некорректно.

После фикса: inner-guard выставляет metadata["project_timeout_triggered"]=True
и metadata.setdefault("stop_reason_override","project_timeout") до перехода в
State.FAILED, поэтому _build_result корректно читает эти значения из metadata.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


# ---------------------------------------------------------------------------
# Minimal duck-typed "engine" object for _single_run
# ---------------------------------------------------------------------------

def _make_minimal_engine(initial_state=None):
    """
    Создаёт объект, достаточный для вызова PipelineEngine._single_run()
    через duck-typing.  Реальный __init__ PipelineEngine не нужен.
    """
    from core.pipeline_context import PipelineContext
    from core.pipeline_engine import PipelineEngine
    from core.state_machine import State

    if initial_state is None:
        initial_state = State.GENERATING_PATCH

    ctx = PipelineContext(
        project_path=Path("/tmp/test_timeout"),
        language="python",
        config={},
    )
    ctx = ctx.add_state_to_history(initial_state)

    class _FakeEngine:
        _PATCH_RESOLUTION_STATES = PipelineEngine._PATCH_RESOLUTION_STATES

        def __init__(self):
            self.context = ctx
            self.project_path = Path("/tmp/test_timeout")
            self.circuit_breaker = MagicMock()
            self.circuit_breaker.is_open.return_value = False
            self._patch_recorder = MagicMock()
            self.health_evaluator = MagicMock()
            self.health_evaluator.evaluate.return_value.to_dict.return_value = {}
            self.start_time = time.time()
            self.step_times = {}
            self.dynamic_params = {}
            self.dry_run = False
            self.language = "python"
            self.stability_governor = MagicMock()
            self.stability_governor.get_params.return_value = {"max_iterations": 10}
            self.stability_governor.step.return_value = {}

        def _save_state(self):
            pass

        def _emit_after_stage(self, state):
            pass

        def _build_result(self, error=None, status_override=None):
            # Минимальный результат — только поля, нужные для проверок в тестах.
            meta = dict(self.context.metadata)
            stop_reason = meta.get("stop_reason_override", "")
            return {
                "status": status_override or self.context.current_state.name,
                "project_timeout_triggered": bool(meta.get("project_timeout_triggered", False)),
                "stop_reason": stop_reason,
            }

    return _FakeEngine()


# ---------------------------------------------------------------------------
# Test 1: inner-deadline → project_timeout_triggered=True в metadata и result
# ---------------------------------------------------------------------------

def test_inner_deadline_sets_timeout_metadata():
    """
    _single_run с уже истёкшим deadline → metadata["project_timeout_triggered"]=True
    и _build_result()["project_timeout_triggered"]=True.
    """
    from core.pipeline_engine import PipelineEngine

    eng = _make_minimal_engine()

    # Deadline уже в прошлом → inner-guard сработает на первом шаге
    expired_deadline = time.monotonic() - 1.0

    result = PipelineEngine._single_run(eng, _deadline=expired_deadline)

    check(
        "metadata_project_timeout_triggered",
        eng.context.metadata.get("project_timeout_triggered") is True,
        f"metadata: {dict(eng.context.metadata)}",
    )
    check(
        "metadata_stop_reason_override",
        eng.context.metadata.get("stop_reason_override") == "project_timeout",
        f"stop_reason_override: {eng.context.metadata.get('stop_reason_override')}",
    )
    check(
        "result_project_timeout_triggered",
        result.get("project_timeout_triggered") is True,
        f"result ptt: {result.get('project_timeout_triggered')}",
    )
    check(
        "result_stop_reason",
        result.get("stop_reason") == "project_timeout",
        f"result stop_reason: {result.get('stop_reason')}",
    )
    check(
        "result_status_is_failed",
        result.get("status") == "FAILED",
        f"result status: {result.get('status')}",
    )


# ---------------------------------------------------------------------------
# Test 2: без deadline → project_timeout_triggered остаётся False
# ---------------------------------------------------------------------------

def test_no_deadline_no_timeout_flag():
    """
    Если _deadline=None, inner-guard не срабатывает и project_timeout_triggered
    не выставляется (контекст остаётся в non-FAILED состоянии без FAILED, т.к.
    нет стадий для выполнения — _single_run завершится когда current_state ∈
    {COMPLETED, FAILED, CIRCUIT_OPEN}).

    Для этого теста нам достаточно убедиться, что при инициализации в
    COMPLETED состоянии result['project_timeout_triggered'] == False.
    """
    from core.pipeline_engine import PipelineEngine
    from core.state_machine import State

    eng = _make_minimal_engine(initial_state=State.COMPLETED)
    # Без deadline
    result = PipelineEngine._single_run(eng, _deadline=None)

    check(
        "no_timeout_flag_without_deadline",
        result.get("project_timeout_triggered") is False,
        f"project_timeout_triggered: {result.get('project_timeout_triggered')}",
    )
    check(
        "stop_reason_not_project_timeout",
        result.get("stop_reason") != "project_timeout",
        f"stop_reason: {result.get('stop_reason')}",
    )


# ---------------------------------------------------------------------------
# Test 3: existing stop_reason_override не перезаписывается если уже установлен
# ---------------------------------------------------------------------------

def test_inner_deadline_does_not_overwrite_existing_stop_reason():
    """
    setdefault гарантирует: если stop_reason_override уже выставлен (например,
    circuit_breaker), inner-guard его не затирает.
    """
    from core.pipeline_engine import PipelineEngine
    from core.pipeline_context import PipelineContext
    from core.state_machine import State

    eng = _make_minimal_engine()
    # Предустанавливаем другой stop_reason_override
    existing_meta = dict(eng.context.metadata)
    existing_meta["stop_reason_override"] = "circuit_breaker"
    eng.context = eng.context.update(metadata=existing_meta)

    expired_deadline = time.monotonic() - 1.0
    result = PipelineEngine._single_run(eng, _deadline=expired_deadline)

    # project_timeout_triggered должен быть True
    check(
        "ptt_set_even_with_existing_stop_reason",
        result.get("project_timeout_triggered") is True,
        f"ptt: {result.get('project_timeout_triggered')}",
    )
    # stop_reason_override не должен быть перезаписан
    check(
        "existing_stop_reason_preserved",
        eng.context.metadata.get("stop_reason_override") == "circuit_breaker",
        f"stop_reason_override: {eng.context.metadata.get('stop_reason_override')}",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

test_inner_deadline_sets_timeout_metadata()
test_no_deadline_no_timeout_flag()
test_inner_deadline_does_not_overwrite_existing_stop_reason()

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, note) for n, ok, note in results if not ok]

print(f"inner_deadline_timeout_flag: {passed}/{len(results)} passed")
if failed:
    for name, note in failed:
        print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
    raise SystemExit(1)
