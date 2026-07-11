"""
Regress H4.3 (PROJECT_AUDIT_REPORT 2026-07-01): при merge результатов
ParallelExecutor metadata ПЕРВОГО завершившегося воркера побеждала целиком —
decisions[] и needs_review_items остальных воркеров терялись (decision
integrity гарантированно расходился, NR-патчи исчезали из отчёта).

Фикс: та же дельта-схема (по baseline-длине), что и для
accepted/rejected/unfixable.

Запуск: python tests/test_phase_parallel_merge_decisions.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.engine.parallel import ParallelExecutor  # noqa: E402
from core.pipeline_context import PipelineContext  # noqa: E402


class _FakeGenStage:
    """Генератор-заглушка: пишет decision + NR-item в metadata и НЕ создаёт
    патч (мини-цикл разрешения пропускается — тестируем только merge)."""

    def __init__(self, *a, **k):
        pass

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        err = ctx.selected_error or {}
        m = dict(ctx.metadata)
        m["decisions"] = list(m.get("decisions", [])) + [{
            "file": err.get("file", "?"), "line": err.get("line", 0),
            "code": err.get("code", ""), "decision": "NEEDS_REVIEW",
            "reason": "fake_worker_decision",
        }]
        m["needs_review_items"] = list(m.get("needs_review_items", [])) + [{
            "file": err.get("file", "?"), "error_code": err.get("code", ""),
        }]
        return ctx.update(metadata=m).set_patch(None)


def test_decisions_of_all_workers_survive_merge():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("x = 1\n", encoding="utf-8")
        (work / "b.py").write_text("y = 2\n", encoding="utf-8")

        baseline_decision = {"file": "old.py", "line": 1, "code": "X",
                             "decision": "ACCEPT", "reason": "baseline"}
        ctx = PipelineContext(
            project_path=work, language="python",
            config={"pipeline": {"parallel_workers": 2}},
            metadata={"decisions": [baseline_decision], "needs_review_items": []},
            current_errors=(
                {"file": "a.py", "line": 1, "code": "E501", "message": "m1"},
                {"file": "b.py", "line": 1, "code": "E501", "message": "m2"},
            ),
            working_path=work,
        )
        holder = {"ctx": ctx}

        executor = ParallelExecutor(
            llm_client=mock.MagicMock(), memory=None,
            patch_engine=mock.MagicMock(),
            state_lock=__import__("threading").Lock(),
            get_context=lambda: holder["ctx"],
            set_context=lambda c: holder.update(ctx=c),
        )

        with mock.patch("core.engine.parallel.GeneratePatchStage", _FakeGenStage):
            executor.execute({"pipeline": {"parallel_workers": 2}}, work)

        merged = holder["ctx"]
        decisions = list(merged.metadata.get("decisions", []))
        reasons = [dd.get("reason") for dd in decisions]
        files = {dd.get("file") for dd in decisions if dd.get("reason") == "fake_worker_decision"}

        assert reasons.count("baseline") == 1, (
            f"baseline-decision должен войти РОВНО один раз: {decisions}"
        )
        assert files == {"a.py", "b.py"}, (
            f"H4: decisions обоих воркеров обязаны выжить в merge: {decisions}"
        )
        nr_items = list(merged.metadata.get("needs_review_items", []))
        assert {it.get("file") for it in nr_items} == {"a.py", "b.py"}, nr_items
        assert merged.metadata.get("needs_review_count") == 2, merged.metadata.get("needs_review_count")


if __name__ == "__main__":
    failed = 0
    try:
        test_decisions_of_all_workers_survive_merge()
        print("  [OK ] decisions_of_all_workers_survive_merge")
    except AssertionError as e:
        print(f"  [FAIL] decisions_of_all_workers_survive_merge: {e}")
        failed += 1
    except Exception as e:
        print(f"  [FAIL] decisions_of_all_workers_survive_merge: {type(e).__name__}: {e}")
        failed += 1
    print(f"\nParallel merge decisions: {1 - failed}/1 pass")
    sys.exit(0 if failed == 0 else 1)
