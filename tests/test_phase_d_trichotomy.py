"""
Stage F.3 regress — Phase D (Reviewer / NEEDS_REVIEW / trichotomy / AI).

Гарантирует инварианты Stage D:
- D.2: `default_confidence_for` даёт ожидаемые числа на ключевых источниках.
- D.4: `_classify_decision` корректно реализует трёхвариантную логику.
- D.6: `ai_authored_detector.score` различает чистый/AI-style код.
- D.4 (новая ветка): «импорт-фикс открыл латентные ошибки» — target устранена,
  счётчик не упал, reviewer in {ok, skipped} + conf>=0.85 → ACCEPT через диспетчер.

Запуск: python3 tests/test_phase_d_trichotomy.py
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_decide_stage():
    spec = importlib.util.spec_from_file_location(
        "decide_stage_for_test", str(ROOT / "core" / "stages" / "decide_stage.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DecideStage


def test_default_confidence_for_rule_based():
    from core.contract import default_confidence_for
    assert default_confidence_for("quote_heuristic") == 0.95
    assert default_confidence_for("brace_heuristic") == 0.95
    assert default_confidence_for("splice_recovery") == 0.95


def test_default_confidence_for_legacy_llm_family():
    from core.contract import default_confidence_for
    assert default_confidence_for("llm") == 0.6
    assert default_confidence_for("llm_critical") == 0.6
    assert default_confidence_for("llm_blocking_multi") == 0.6
    assert default_confidence_for("llm_blocking_segment") == 0.6


def test_default_confidence_for_unknown():
    from core.contract import default_confidence_for, DEFAULT_CONFIDENCE_UNKNOWN
    assert default_confidence_for("") == DEFAULT_CONFIDENCE_UNKNOWN
    assert default_confidence_for(None) == DEFAULT_CONFIDENCE_UNKNOWN
    assert default_confidence_for("never_heard_of") == DEFAULT_CONFIDENCE_UNKNOWN


def _make_classify_ctx(conf, verdict=None):
    from core.pipeline_context import PipelineContext
    from core.contract import MetadataKeys
    meta = {MetadataKeys.CONFIDENCE: conf}
    if verdict is not None:
        meta["review"] = {"verdict": verdict}
    return PipelineContext(
        project_path=Path("/tmp"), language="rust",
        metadata=meta, selected_error={"file": "x"},
    )


def test_classify_trichotomy_boundaries():
    DecideStage = _load_decide_stage()
    stage = DecideStage(quality_evaluator=MagicMock())
    assert stage._classify_decision(_make_classify_ctx(0.95, "ok")) == "ACCEPT"
    assert stage._classify_decision(_make_classify_ctx(0.85, "ok")) == "ACCEPT"
    assert stage._classify_decision(_make_classify_ctx(0.70, "ok")) == "NEEDS_REVIEW"
    assert stage._classify_decision(_make_classify_ctx(0.50, "ok")) == "NEEDS_REVIEW"
    assert stage._classify_decision(_make_classify_ctx(0.49, "ok")) == "REJECT"
    assert stage._classify_decision(_make_classify_ctx(0.30, "ok")) == "REJECT"


def test_classify_verdict_overrides():
    DecideStage = _load_decide_stage()
    stage = DecideStage(quality_evaluator=MagicMock())
    assert stage._classify_decision(_make_classify_ctx(0.99, "noisy")) == "NEEDS_REVIEW"
    assert stage._classify_decision(_make_classify_ctx(0.99, "wrong")) == "REJECT"
    assert stage._classify_decision(_make_classify_ctx(0.30, "wrong")) == "REJECT"


def _make_unmask_ctx(verdict, conf, target_still_present):
    from core.pipeline_context import PipelineContext
    from core.contract import MetadataKeys
    target = {"file": "src/game.rs", "line": 2, "column": 5,
              "code": "E0432", "message": "unresolved import `guess`"}
    meta = {MetadataKeys.CONFIDENCE: conf,
            MetadataKeys.PATCH_SOURCE: "structured_llm",
            "review": {"verdict": verdict, "confidence_adjustment": 0.0}}
    current_errors = [target] if target_still_present else [
        {"file": "src/game.rs", "line": 15, "code": "E0609",
         "message": "no field `value` on type `Guess`"}
    ]
    return PipelineContext(
        project_path=Path("/tmp"), language="rust",
        metadata=meta, selected_error=target,
        current_errors=current_errors,
        validation_results={"error_count_before": 1, "error_count_after": 8,
                            "project_error_count_before": 1, "project_error_count_after": 8,
                            "current_errors_before": [target]},
    )


def test_decide_import_unmask_accept():
    DecideStage = _load_decide_stage()
    from core.state_machine import State
    stage = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(verdict="ok", conf=1.0, target_still_present=False)
    out = stage.execute(ctx)
    ld = out.metadata.get("last_decision")
    assert ld in ("ACCEPT", "NEEDS_REVIEW"), f"ждали ACCEPT/NEEDS_REVIEW, получили {ld!r}"
    assert out.current_state != State.DECIDING
    accepted = list(out.accepted_patches or [])
    assert any("target_fixed_reviewer_ok_count_unchanged" in (p.get("reason", "") or "")
               for p in accepted) or ld == "NEEDS_REVIEW", \
        "ожидали ACCEPT по новой ветке или NEEDS_REVIEW"


def test_decide_import_unmask_reviewer_wrong_rejects():
    DecideStage = _load_decide_stage()
    stage = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(verdict="wrong", conf=1.0, target_still_present=False)
    out = stage.execute(ctx)
    rejected = list(out.rejected_patches or [])
    assert out.metadata.get("last_decision") == "REJECT" or any(
        (p.get("reason") or "").startswith(("reviewer_verdict_wrong", "error_count_not_decreased"))
        for p in rejected), "ждали REJECT при verdict=wrong"


def test_decide_import_unmask_low_conf_rejects():
    DecideStage = _load_decide_stage()
    stage = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(verdict="ok", conf=0.70, target_still_present=False)
    out = stage.execute(ctx)
    assert any(p.get("reason") == "error_count_not_decreased"
               for p in (out.rejected_patches or [])), \
        "ждали REJECT с причиной error_count_not_decreased"


def test_decide_import_unmask_skipped_accepts():
    DecideStage = _load_decide_stage()
    from core.state_machine import State
    stage = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(verdict="skipped", conf=0.90, target_still_present=False)
    out = stage.execute(ctx)
    ld = out.metadata.get("last_decision")
    assert ld in ("ACCEPT", "NEEDS_REVIEW"), \
        f"ждали ACCEPT/NEEDS_REVIEW при verdict=skipped (high-trust), получили {ld!r}"
    assert out.current_state != State.DECIDING


def test_decide_import_unmask_unavailable_rejects():
    DecideStage = _load_decide_stage()
    stage = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(verdict="unavailable", conf=1.0, target_still_present=False)
    out = stage.execute(ctx)
    assert any(p.get("reason") == "error_count_not_decreased"
               for p in (out.rejected_patches or [])), \
        "ждали REJECT при verdict=unavailable в ветке unmask"


def test_ai_authored_detector_clean_vs_ai():
    from analysis.ai_authored_detector import score
    clean = "fn main() { println!(\"hi\"); }"
    assert score(clean) < 0.3, f"clean code score {score(clean)} should be < 0.3"
    ai_style = "\n".join(
        ["# TODO: implement this properly later"] * 5
        + ["def process_user_data_from_database_endpoint_v" + str(i) + "():"
           "\n    # FIXME: add validation\n    return {\"status\": \"ok\"}"
           for i in range(1, 6)]
    )
    assert score(ai_style) >= 0.5, f"AI-style score {score(ai_style)} should be ≥ 0.5"


if __name__ == "__main__":
    tests = [
        ("default_confidence_for_rule_based", test_default_confidence_for_rule_based),
        ("default_confidence_for_legacy_llm_family", test_default_confidence_for_legacy_llm_family),
        ("default_confidence_for_unknown", test_default_confidence_for_unknown),
        ("classify_trichotomy_boundaries", test_classify_trichotomy_boundaries),
        ("classify_verdict_overrides", test_classify_verdict_overrides),
        ("decide_import_unmask_accept", test_decide_import_unmask_accept),
        ("decide_import_unmask_reviewer_wrong_rejects", test_decide_import_unmask_reviewer_wrong_rejects),
        ("decide_import_unmask_low_conf_rejects", test_decide_import_unmask_low_conf_rejects),
        ("decide_import_unmask_skipped_accepts", test_decide_import_unmask_skipped_accepts),
        ("decide_import_unmask_unavailable_rejects", test_decide_import_unmask_unavailable_rejects),
        ("ai_authored_detector_clean_vs_ai", test_ai_authored_detector_clean_vs_ai),
    ]
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
    print()
    print(f"Phase D regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
