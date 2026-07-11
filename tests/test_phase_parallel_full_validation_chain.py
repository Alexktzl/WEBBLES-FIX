"""
2026-06-24: ParallelExecutor (parallel_workers>1) раньше обрывал
обработку ошибки сразу после ApplyPatchStage — ValidateStage/ReviewStage/
DecideStage никогда не вызывались. Патчи писались на диск без
net_delta_check/symbol_regression/type_erosion_guard и без формального
ACCEPT/REJECT решения (см. находку в docstring ParallelExecutor.__init__,
runtime/_bench_deepseek/* benchmark расследование throughput).

Эти тесты проверяют, что _resolve_patch реально прогоняет цепочку стадий
разрешения (не просто доверяет ApplyPatchStage), и что её результат
(ACCEPT/REJECT/NEEDS_REVIEW) корректно долетает до итогового merge.

ParallelExecutor.execute() параллелит только если ошибки лежат в >=2
РАЗНЫХ файлах (см. "Только один файл с ошибками – параллелить нечего") —
поэтому каждый тест добавляет тривиальный b.py-файл без патча (отдельный
поток для него просто проходит no-op), а интересующая логика — на a.py.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

import pytest

from core.engine.parallel import ParallelExecutor
from core.pipeline_context import PipelineContext
from core.state_machine import State


def _base_ctx_dict(file_errors=None):
    if file_errors is None:
        file_errors = [
            {"file": "a.py", "line": 1, "code": "E1", "message": "m", "error_class": "BLOCKING"},
            {"file": "b.py", "line": 1, "code": "E9", "message": "trivial", "error_class": "BLOCKING"},
        ]
    ctx = PipelineContext(project_path=Path("/tmp/fake_project"), language="python", config={})
    ctx = ctx.add_state_to_history(State.GENERATING_PATCH)
    ctx = ctx.update(current_errors=tuple(file_errors))
    return ctx.to_dict()


def _executor(shared):
    lock = threading.Lock()

    def get_ctx():
        return PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())

    def set_ctx(new_ctx):
        shared["ctx_dict"] = new_ctx.to_dict()

    return ParallelExecutor(
        llm_client=MagicMock(),
        memory=MagicMock(),
        patch_engine=MagicMock(),
        state_lock=lock,
        get_context=get_ctx,
        set_context=set_ctx,
        compiler=MagicMock(),
        linter=MagicMock(),
        security=MagicMock(),
        quality_evaluator=MagicMock(),
        recorder=MagicMock(),
    )


def _setup_two_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")


def _is_target_file(context, target="a.py"):
    err = context.selected_error or {}
    return err.get("file") == target


def test_validate_review_decide_all_called_on_accept_path(tmp_path):
    """Патч доходит до VALIDATING — цепочка должна реально прогнать
    ValidateStage→ReviewStage→DecideStage, а не остановиться сразу после
    ApplyPatchStage. DecideStage в этом сценарии принимает патч (ACCEPT)."""
    _setup_two_files(tmp_path)
    shared = {"ctx_dict": _base_ctx_dict()}
    executor = _executor(shared)

    calls = {"gen": 0, "apply": 0, "validate": 0, "review": 0, "decide": 0}

    def fake_gen(self_stage, context):
        if not _is_target_file(context):
            return context.add_state_to_history(State.NEXT_ERROR)
        calls["gen"] += 1
        ctx = context.set_patch("--- a/a.py\n+++ b/a.py\n@@\n-x = 1\n+x = 2\n")
        return ctx.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply(self_stage, context):
        calls["apply"] += 1
        return context.add_state_to_history(State.VALIDATING)

    def fake_validate(self_stage, context):
        calls["validate"] += 1
        return context.add_state_to_history(State.REVIEWING)

    def fake_review(self_stage, context):
        calls["review"] += 1
        return context.add_state_to_history(State.DECIDING)

    def fake_decide(self_stage, context):
        calls["decide"] += 1
        context = context.add_accepted_patch({
            "error": {"file": "a.py", "line": 1, "code": "E1"}, "reason": "test_accept",
        })
        return context.add_state_to_history(State.NEXT_ERROR)

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply), \
         patch("core.stages.validate_stage.ValidateStage.execute", fake_validate), \
         patch("core.stages.review_stage.ReviewStage.execute", fake_review), \
         patch("core.stages.decide_stage.DecideStage.execute", fake_decide):
        executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    assert calls == {"gen": 1, "apply": 1, "validate": 1, "review": 1, "decide": 1}
    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert len(final_ctx.accepted_patches) == 1
    assert final_ctx.accepted_patches[0]["reason"] == "test_accept"


def test_rollback_stage_called_and_reject_recorded_not_silently_kept(tmp_path):
    """DecideStage решает REJECT и переходит в ROLLING_BACK — RollbackStage
    должен реально вызваться (откатить файл), а итог попасть в
    rejected_patches. Раньше параллельный мини-цикл такого пути не знал
    вовсе — патч молча оставался на диске."""
    _setup_two_files(tmp_path)
    shared = {"ctx_dict": _base_ctx_dict()}
    executor = _executor(shared)

    calls = {"rollback": 0}

    def fake_gen(self_stage, context):
        if not _is_target_file(context):
            return context.add_state_to_history(State.NEXT_ERROR)
        ctx = context.set_patch("--- a/a.py\n+++ b/a.py\n@@\n-x = 1\n+x = 2\n")
        return ctx.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply(self_stage, context):
        return context.add_state_to_history(State.VALIDATING)

    def fake_validate(self_stage, context):
        return context.add_state_to_history(State.REVIEWING)

    def fake_review(self_stage, context):
        return context.add_state_to_history(State.DECIDING)

    def fake_decide(self_stage, context):
        context = context.add_rejected_patch({
            "error": {"file": "a.py", "line": 1, "code": "E1"}, "reason": "test_reject",
        })
        return context.add_state_to_history(State.ROLLING_BACK)

    def fake_rollback(self_stage, context):
        calls["rollback"] += 1
        return context.add_state_to_history(State.NEXT_ERROR)

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply), \
         patch("core.stages.validate_stage.ValidateStage.execute", fake_validate), \
         patch("core.stages.review_stage.ReviewStage.execute", fake_review), \
         patch("core.stages.decide_stage.DecideStage.execute", fake_decide), \
         patch("core.stages.rollback_stage.RollbackStage.execute", fake_rollback):
        executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    assert calls["rollback"] == 1
    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert len(final_ctx.rejected_patches) == 1
    assert len(final_ctx.accepted_patches) == 0


def test_needs_review_path_invoked_not_silently_accepted(tmp_path):
    """DecideStage уходит в NEEDS_REVIEW (неуверенное решение) — патч
    должен попасть в очередь ручного просмотра, а не быть тихо принятым."""
    _setup_two_files(tmp_path)
    shared = {"ctx_dict": _base_ctx_dict()}
    executor = _executor(shared)
    calls = {"needs_review": 0}

    def fake_gen(self_stage, context):
        if not _is_target_file(context):
            return context.add_state_to_history(State.NEXT_ERROR)
        ctx = context.set_patch("patch")
        return ctx.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply(self_stage, context):
        return context.add_state_to_history(State.VALIDATING)

    def fake_validate(self_stage, context):
        return context.add_state_to_history(State.REVIEWING)

    def fake_review(self_stage, context):
        return context.add_state_to_history(State.DECIDING)

    def fake_decide(self_stage, context):
        return context.add_state_to_history(State.NEEDS_REVIEW)

    def fake_needs_review(self_stage, context):
        calls["needs_review"] += 1
        return context.add_state_to_history(State.NEXT_ERROR)

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply), \
         patch("core.stages.validate_stage.ValidateStage.execute", fake_validate), \
         patch("core.stages.review_stage.ReviewStage.execute", fake_review), \
         patch("core.stages.decide_stage.DecideStage.execute", fake_decide), \
         patch("core.stages.needs_review_stage.NeedsReviewStage.execute", fake_needs_review):
        executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    assert calls["needs_review"] == 1


def test_resolution_steps_bounded_against_infinite_loop(tmp_path):
    """Защитный потолок _MAX_RESOLUTION_STEPS не даёт потоку зависнуть
    навечно, если стадия патологически возвращает одно и то же состояние."""
    _setup_two_files(tmp_path)
    shared = {"ctx_dict": _base_ctx_dict()}
    executor = _executor(shared)
    calls = {"validate": 0}

    def fake_gen(self_stage, context):
        if not _is_target_file(context):
            return context.add_state_to_history(State.NEXT_ERROR)
        ctx = context.set_patch("patch")
        return ctx.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply(self_stage, context):
        return context.add_state_to_history(State.VALIDATING)

    def fake_validate_loops_forever(self_stage, context):
        calls["validate"] += 1
        return context.add_state_to_history(State.VALIDATING)  # никогда не продвигается

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply), \
         patch("core.stages.validate_stage.ValidateStage.execute", fake_validate_loops_forever):
        executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    from core.engine.parallel import _MAX_RESOLUTION_STEPS
    assert calls["validate"] == _MAX_RESOLUTION_STEPS


def test_retry_loop_regenerates_and_reapplies_on_generating_patch_request(tmp_path):
    """2026-06-24 (вторая итерация фикса): ValidateStage может запросить
    повторную генерацию (net_delta regression — откатывает файл и просит
    GENERATING_PATCH). Раньше поток на этом просто останавливался и
    оставлял ошибку недоразрешённой. Теперь gen_stage/app_stage реально
    вызываются ЗАНОВО, и второй проход доходит до ACCEPT."""
    _setup_two_files(tmp_path)
    shared = {"ctx_dict": _base_ctx_dict()}
    executor = _executor(shared)
    calls = {"gen": 0, "apply": 0, "validate": 0, "decide": 0}

    def fake_gen(self_stage, context):
        if not _is_target_file(context):
            return context.add_state_to_history(State.NEXT_ERROR)
        calls["gen"] += 1
        ctx = context.set_patch(f"patch-attempt-{calls['gen']}")
        return ctx.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply(self_stage, context):
        calls["apply"] += 1
        return context.add_state_to_history(State.VALIDATING)

    def fake_validate(self_stage, context):
        calls["validate"] += 1
        if calls["validate"] == 1:
            # Первая попытка: net_delta regression -> retry (откат уже
            # выполнен "внутри" ValidateStage в реальном коде).
            return context.add_state_to_history(State.GENERATING_PATCH)
        return context.add_state_to_history(State.DECIDING)

    def fake_decide(self_stage, context):
        calls["decide"] += 1
        context = context.add_accepted_patch({
            "error": {"file": "a.py", "line": 1, "code": "E1"}, "reason": "accepted_on_retry",
        })
        return context.add_state_to_history(State.NEXT_ERROR)

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply), \
         patch("core.stages.validate_stage.ValidateStage.execute", fake_validate), \
         patch("core.stages.review_stage.ReviewStage.execute",
               lambda self_stage, context: context.add_state_to_history(State.DECIDING)), \
         patch("core.stages.decide_stage.DecideStage.execute", fake_decide):
        executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    assert calls["gen"] == 2 and calls["apply"] == 2 and calls["validate"] == 2 and calls["decide"] == 1
    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    assert len(final_ctx.accepted_patches) == 1
    assert final_ctx.accepted_patches[0]["reason"] == "accepted_on_retry"


def test_circuit_breaker_open_stops_retry_without_calling_gen_again(tmp_path):
    """Если circuit_breaker.is_open() — retry-попытка НЕ предпринимается
    (gen_stage не вызывается повторно); ошибка просто остаётся
    недоразрешённой для этого прохода, а не уходит в бесконечный retry."""
    _setup_two_files(tmp_path)
    shared = {"ctx_dict": _base_ctx_dict()}
    executor = _executor(shared)
    executor.circuit_breaker = MagicMock(is_open=MagicMock(return_value=True))
    calls = {"gen": 0}

    def fake_gen(self_stage, context):
        if not _is_target_file(context):
            return context.add_state_to_history(State.NEXT_ERROR)
        calls["gen"] += 1
        ctx = context.set_patch("patch")
        return ctx.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply(self_stage, context):
        return context.add_state_to_history(State.VALIDATING)

    def fake_validate(self_stage, context):
        return context.add_state_to_history(State.GENERATING_PATCH)

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply), \
         patch("core.stages.validate_stage.ValidateStage.execute", fake_validate):
        executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    # gen_stage вызывается 1 раз (первая попытка) — повторного retry-вызова
    # после открытого circuit breaker быть не должно.
    assert calls["gen"] == 1
    executor.circuit_breaker.is_open.assert_called()


def test_anti_loop_should_break_stops_retry(tmp_path):
    """Аналогично circuit breaker — anti_loop.should_break()==True
    останавливает retry-попытку без бесконечного цикла."""
    _setup_two_files(tmp_path)
    shared = {"ctx_dict": _base_ctx_dict()}
    executor = _executor(shared)
    executor.anti_loop = MagicMock(should_break=MagicMock(return_value=True))
    calls = {"gen": 0}

    def fake_gen(self_stage, context):
        if not _is_target_file(context):
            return context.add_state_to_history(State.NEXT_ERROR)
        calls["gen"] += 1
        ctx = context.set_patch("patch")
        return ctx.add_state_to_history(State.APPLYING_PATCH)

    def fake_apply(self_stage, context):
        return context.add_state_to_history(State.VALIDATING)

    def fake_validate(self_stage, context):
        return context.add_state_to_history(State.GENERATING_PATCH)

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen), \
         patch("core.stages.apply_patch_stage.ApplyPatchStage.execute", fake_apply), \
         patch("core.stages.validate_stage.ValidateStage.execute", fake_validate):
        executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    assert calls["gen"] == 1
    executor.anti_loop.should_break.assert_called()


def test_resolution_states_consistent_with_sequential_engine():
    """2026-06-24 (антидрейф): `_RESOLUTION_STAGE_STATES` в parallel.py — это
    отдельный набор от `PipelineEngine._PATCH_RESOLUTION_STATES`, которым
    руководствуется последовательный `_single_run`. Полностью объединять их
    рискованно (там есть FINAL_RESOLVE/CLEANUP_ANALYSIS — состояния
    ПРОЕКТНОГО уровня, не относящиеся к разрешению одной ошибки в потоке),
    но если когда-нибудь в _single_run добавят НОВОЕ per-error состояние
    разрешения (VALIDATING/REVIEWING/DECIDING/ROLLING_BACK/NEEDS_REVIEW) и
    забудут зеркально добавить его сюда — этот тест должен упасть, а не
    молча разойтись."""
    from core.engine.parallel import _RESOLUTION_STAGE_STATES
    from core.pipeline_engine import PipelineEngine

    _PROJECT_LEVEL_ONLY = {State.FINAL_RESOLVE, State.CLEANUP_ANALYSIS}
    _per_error_canonical = PipelineEngine._PATCH_RESOLUTION_STATES - _PROJECT_LEVEL_ONLY
    assert _RESOLUTION_STAGE_STATES == _per_error_canonical, (
        "parallel.py._RESOLUTION_STAGE_STATES разошёлся с "
        "PipelineEngine._PATCH_RESOLUTION_STATES (за вычетом project-level "
        "состояний) — обновите оба места синхронно"
    )


def test_llm_client_pool_round_robins_distinct_clients_per_file(tmp_path):
    """2026-06-24: WEBBLES_LLM_API_KEY_POOL даёт каждому потоку СВОЙ
    LLMClient (свой api_key/circuit breaker), а не один общий на все
    потоки. Три файла + пул из 2 клиентов -> клиенты переиспользуются
    round-robin (0,1,0), но КАЖДЫЙ файл получает клиент именно из пула,
    не self.llm_client напрямую."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("z = 1\n", encoding="utf-8")
    file_errors = [
        {"file": "a.py", "line": 1, "code": "E1", "message": "m", "error_class": "BLOCKING"},
        {"file": "b.py", "line": 1, "code": "E2", "message": "m", "error_class": "BLOCKING"},
        {"file": "c.py", "line": 1, "code": "E3", "message": "m", "error_class": "BLOCKING"},
    ]
    shared = {"ctx_dict": _base_ctx_dict(file_errors=file_errors)}
    lock = threading.Lock()

    def get_ctx():
        return PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())

    def set_ctx(new_ctx):
        shared["ctx_dict"] = new_ctx.to_dict()

    client_a = MagicMock(name="client_pool_0")
    client_b = MagicMock(name="client_pool_1")
    executor = ParallelExecutor(
        llm_client=client_a,
        memory=MagicMock(),
        patch_engine=MagicMock(),
        state_lock=lock,
        get_context=get_ctx,
        set_context=set_ctx,
        compiler=MagicMock(),
        linter=MagicMock(),
        security=MagicMock(),
        quality_evaluator=MagicMock(),
        recorder=MagicMock(),
        llm_client_pool=[client_a, client_b],
    )

    # ThreadPoolExecutor может переиспользовать один и тот же worker-поток
    # для нескольких файлов подряд — ключ по id(thread) затирал бы более
    # раннюю запись. Собираем ВСЕ вызовы списком под локом.
    seen_clients_lock = threading.Lock()
    seen_clients = []

    def fake_gen_init(self, llm_client, memory, patch_engine):
        with seen_clients_lock:
            seen_clients.append(llm_client)
        self._llm_client = llm_client

    def fake_gen_execute(self_stage, context):
        return context.add_state_to_history(State.NEXT_ERROR)

    with patch("core.engine.parallel.GeneratePatchStage.__init__", fake_gen_init), \
         patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_execute):
        executor.execute({"pipeline": {"parallel_workers": 3}}, tmp_path)

    # round-robin по индексу файла (0,1,2 % 2 пула) -> [client_a, client_b, client_a]
    assert len(seen_clients) == 3
    assert seen_clients.count(client_a) == 2
    assert seen_clients.count(client_b) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
