"""
Regression-тест: ParallelExecutor не должен дублировать baseline-списки
при слиянии результатов worker-потоков.

Баг (до фикса): каждый worker стартует с полным клоном base_ctx_dict;
ни GeneratePatchStage, ни ApplyPatchStage не вызывают add_accepted_patch,
поэтому local_ctx.accepted_patches = baseline у каждого воркера. Старый
merge-код конкатенировал списки без дедупликации → N_workers × len(baseline).

После фикса: merge берёт одну копию baseline и добавляет только genuine-дельту
каждого воркера (записи за пределами _n_base_accepted / _n_base_rejected /
_n_base_unfixable).
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_base_ctx_dict(n_baseline_accepted: int = 3, file_errors: list = None):
    """Минимальный context dict с n baseline accepted_patches и ошибками по файлам."""
    from core.pipeline_context import PipelineContext
    from core.state_machine import State

    if file_errors is None:
        file_errors = [
            {"file": "file_a.py", "line": 1, "code": "E501", "message": "too long", "error_class": "CLEANUP"},
            {"file": "file_b.py", "line": 2, "code": "E501", "message": "too long", "error_class": "CLEANUP"},
        ]

    ctx = PipelineContext(
        project_path=Path("/tmp/fake_project"),
        language="python",
        config={},
    )
    ctx = ctx.add_state_to_history(State.GENERATING_PATCH)

    for i in range(n_baseline_accepted):
        ctx = ctx.add_accepted_patch({
            "error": {"file": f"old{i}.py", "line": i, "code": "E001"},
            "reason": "baseline",
        })

    ctx = ctx.update(current_errors=tuple(file_errors))
    return ctx.to_dict()


def _make_executor(shared):
    """Создаёт ParallelExecutor с минимальными моками и разделяемым state."""
    from core.engine.parallel import ParallelExecutor

    lock = threading.Lock()

    def get_ctx():
        from core.pipeline_context import PipelineContext
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
    )


# ---------------------------------------------------------------------------
# Test 1: workers возвращают unmodified baseline → merged = baseline × 1
# ---------------------------------------------------------------------------

def test_no_baseline_duplication_when_workers_add_nothing():
    """После фикса: N воркеров без патчей → merged.accepted_patches == baseline."""
    from core.state_machine import State

    shared = {"ctx_dict": _make_base_ctx_dict(n_baseline_accepted=3)}
    baseline_count = len(shared["ctx_dict"].get("accepted_patches", []))
    check("setup_baseline_3", baseline_count == 3)

    executor = _make_executor(shared)

    def fake_gen_execute(self_stage, context):
        # Без патча → process_file сделает continue (не вызовет ApplyPatchStage)
        return context.add_state_to_history(State.NEXT_ERROR)

    config = {"pipeline": {"parallel_workers": 2}}

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_execute):
        executor.execute(config, Path("/tmp/fake_project"))

    # Проверяем итоговый контекст
    from core.pipeline_context import PipelineContext
    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    final_count = len(final_ctx.accepted_patches)

    check(
        "no_duplication_count",
        final_count == baseline_count,
        f"expected {baseline_count}, got {final_count}",
    )
    check(
        "accepted_patches_content_identical",
        shared["ctx_dict"].get("accepted_patches") == _make_base_ctx_dict(
            n_baseline_accepted=3
        ).get("accepted_patches"),
        "content should match pre-fork baseline exactly",
    )


# ---------------------------------------------------------------------------
# Test 2: workers реально добавляют патч → дельта учтена один раз, не K×
# ---------------------------------------------------------------------------

def test_genuine_additions_counted_once_not_multiplied():
    """
    Если воркер добавляет 1 новый accepted_patch (genuine-дельта),
    финальный count = baseline + 1 per worker, а не (baseline + 1) × N_workers.
    """
    from core.state_machine import State

    file_errors = [
        {"file": "file_a.py", "line": 1, "code": "E501", "message": "long", "error_class": "CLEANUP"},
        {"file": "file_b.py", "line": 2, "code": "E501", "message": "long", "error_class": "CLEANUP"},
    ]
    shared = {"ctx_dict": _make_base_ctx_dict(n_baseline_accepted=2, file_errors=file_errors)}
    baseline_count = len(shared["ctx_dict"].get("accepted_patches", []))
    check("setup_baseline_2", baseline_count == 2)

    executor = _make_executor(shared)

    call_count = [0]

    def fake_gen_with_add(self_stage, context):
        call_count[0] += 1
        # Имитируем: воркер действительно принял один патч
        context = context.add_accepted_patch({
            "error": {"file": "new.py", "line": 1, "code": "E501"},
            "reason": "worker_fix",
        })
        # Возвращаем без generated_patch → process_file сделает continue
        return context.add_state_to_history(State.NEXT_ERROR)

    config = {"pipeline": {"parallel_workers": 2}}

    with patch("core.stages.generate_patch_stage.GeneratePatchStage.execute", fake_gen_with_add):
        executor.execute(config, Path("/tmp/fake_project"))

    from core.pipeline_context import PipelineContext
    final_ctx = PipelineContext.from_dict(shared["ctx_dict"], memory=MagicMock())
    final_count = len(final_ctx.accepted_patches)

    # Ожидаем: baseline + по одному genuine-добавлению от каждого воркера
    expected = baseline_count + call_count[0]
    check(
        "genuine_delta_counted_once",
        final_count == expected,
        f"expected {expected} (baseline={baseline_count} + calls={call_count[0]}), got {final_count}",
    )

    # Явно убеждаемся, что нет N×baseline дублирования
    n_workers = len(file_errors)  # 2 файла → 2 воркера
    duplicated_count = baseline_count * n_workers + call_count[0]
    check(
        "not_multiplied_by_workers",
        final_count != duplicated_count or baseline_count == 0,
        f"got {final_count}, duplicated would be {duplicated_count}",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

test_no_baseline_duplication_when_workers_add_nothing()
test_genuine_additions_counted_once_not_multiplied()

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, note) for n, ok, note in results if not ok]

print(f"parallel_no_baseline_dup: {passed}/{len(results)} passed")
if failed:
    for name, note in failed:
        print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
    raise SystemExit(1)
