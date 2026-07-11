"""
Конверсия-4/5 (2026-07-02): дедуп повторных решений.

К-4: осцилляционный бан логировался в decisions[] как NEEDS_REVIEW БЕЗ
создания NR-item — decision-integrity сверка гарантированно расходилась
(контрольная серия 07-02, httpx: decisions=13 vs items=12). Теперь —
отдельный тип OSCILLATION_BAN.

К-5: одна и та же ошибка, повторно дошедшая до NR (retry в следующем
цикле), добавляла ДУБЛИРУЮЩУЮ запись в needs_review_items и раздувала
счётчики (httpx: F821 encoding на одной строке — 3 идентичные записи).
Теперь запись обновляется на месте.

Запуск: python tests/test_phase_conversion_dedup.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext  # noqa: E402
from core.pipeline_engine import PipelineEngine  # noqa: E402
from core.stages.needs_review_stage import NeedsReviewStage  # noqa: E402


# --- 1. К-4: осцилляционный бан не пишет NEEDS_REVIEW в decisions[] ---
def test_oscillation_ban_logged_as_own_decision_type():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        sandbox = Path(d) / "sandbox"
        sandbox.mkdir()
        (sandbox / "a.py").write_text("x = 1\n", encoding="utf-8")

        eng = PipelineEngine.__new__(PipelineEngine)
        eng.project_path = repo.resolve()
        eng.context = PipelineContext(project_path=repo, language="python")

        accepted = [{"file": "a.py", "line": 1, "code": "E1", "message": "m"}]
        eng._ban_oscillating_signatures({"a.py::1::E1"}, sandbox, accepted)

        decisions = list(eng.context.metadata.get("decisions", []))
        assert decisions, "решение о бане обязано попасть в decisions[]"
        kinds = {dd.get("decision") for dd in decisions}
        assert "NEEDS_REVIEW" not in kinds, (
            f"К-4: бан без NR-item не должен считаться NEEDS_REVIEW: {decisions}"
        )
        assert "OSCILLATION_BAN" in kinds, decisions


# --- 2. К-5: повторный NR той же ошибки не дублирует запись и счётчики ---
def test_needs_review_dedup_same_error():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        error = {"file": "httpx/_models.py", "line": 173, "code": "F821",
                 "message": "undefined name 'encoding'"}
        ctx = PipelineContext(
            project_path=work, language="python", working_path=work,
            selected_error=dict(error),
            metadata={"_needs_review_pending_reason": "no_llm_codes"},
        )
        stage = NeedsReviewStage()
        ctx = stage.execute(ctx)

        # Второй заход той же ошибкой (retry следующего цикла)
        ctx = ctx.set_selected_error(dict(error))
        _m = dict(ctx.metadata)
        _m["_needs_review_pending_reason"] = "no_llm_codes"
        ctx = ctx.update(metadata=_m)
        ctx = stage.execute(ctx)

        items = list(ctx.metadata.get("needs_review_items", []))
        assert len(items) == 1, f"дубль в очереди: {items}"
        assert ctx.metadata.get("needs_review_count") == 1, ctx.metadata.get("needs_review_count")
        nr_decisions = [dd for dd in ctx.metadata.get("decisions", [])
                        if dd.get("decision") == "NEEDS_REVIEW"]
        assert len(nr_decisions) == 1, (
            f"decision-integrity: NR-decisions должно быть ровно как items: {nr_decisions}"
        )


# --- 3. К-5: РАЗНЫЕ строки с одинаковым кодом+сообщением — отдельные записи ---
def test_needs_review_different_lines_not_deduped():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        stage = NeedsReviewStage()
        ctx = PipelineContext(
            project_path=work, language="python", working_path=work,
            selected_error={"file": "m.py", "line": 173, "code": "F821",
                            "message": "undefined name 'encoding'"},
            metadata={"_needs_review_pending_reason": "no_llm_codes"},
        )
        ctx = stage.execute(ctx)
        ctx = ctx.set_selected_error({"file": "m.py", "line": 617, "code": "F821",
                                      "message": "undefined name 'encoding'"})
        _m = dict(ctx.metadata)
        _m["_needs_review_pending_reason"] = "no_llm_codes"
        ctx = ctx.update(metadata=_m)
        ctx = stage.execute(ctx)

        items = list(ctx.metadata.get("needs_review_items", []))
        assert len(items) == 2, (
            f"разные строки — разные NR-записи (это разные места кода): {items}"
        )
        assert ctx.metadata.get("needs_review_count") == 2


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nConversion dedup: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
