"""
Control series 2026-06-21, находка #1 (потеря decisions[] для REJECT/
NEEDS_REVIEW): три net-delta пути в ValidateStage.execute() (cached per-file
cap, финальный rollback после исчерпанной IMP-E retry-попытки, needs_review)
переходят в NEXT_ERROR/NeedsReviewStage НАПРЯМУЮ, минуя DecideStage —
единственное место, которое раньше вызывало append_decision_log. В реальных
прогонах (control series, 4 из 20 проектов) это давало `decisions: []` в
RunStatistics при ненулевых `rejected_files_count`/`needs_review_count`.

Фикс: append_decision_log вынесен в analysis/run_statistics.py (общая точка,
DecideStage._append_decision_log теперь тонкая обёртка над ней), и добавлены
явные вызовы в трёх net-delta веток validate_stage.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.stages.validate_stage import ValidateStage
from core.stages.net_delta_check import NetDeltaClassification


def _make_stage():
    compiler = MagicMock()
    linter = MagicMock()
    security = MagicMock()
    compiler.run.return_value = (True, [])
    linter.run.return_value = (True, [])
    security.run.return_value = (True, [])
    from analyzers.python_analyzer import PythonAnalyzer
    return ValidateStage(compiler=compiler, linter=linter, security=security,
                         analyzer=PythonAnalyzer(), degradation=MagicMock())


def _base_ctx(tmp_path, target_file="a.py"):
    config = {"pipeline": {"use_mypy": False, "use_bandit": False, "run_tests": False,
                           "logic_guard": False}}
    ctx = PipelineContext(project_path=tmp_path, language="python", config=config,
                          working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": target_file, "line": 1, "code": "E501",
                                  "message": "x", "error_class": "CLEANUP"})
    ctx = ctx.update(generated_patch="--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n")
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_pre_patch_content": {target_file: "original content\n"},
    }))
    return ctx


def _rollback_classification():
    return NetDeltaClassification(
        regression=[{"file": "a.py", "line": 1, "code": "E999"}],
        unmasked=[], uncertain=[], changed_lines={1},
    )


def _needs_review_classification():
    return NetDeltaClassification(
        regression=[], unmasked=[],
        uncertain=[{"file": "b.py", "line": 50, "code": "E501"}],
        changed_lines={1},
    )


def test_net_delta_rollback_logs_reject(tmp_path):
    """Финальный (второй+) rollback-исход — должен логироваться как REJECT."""
    (tmp_path / "a.py").write_text("y = 1\n", encoding="utf-8")
    stage = _make_stage()
    ctx = _base_ctx(tmp_path)
    # Симулируем "уже была одна retry-попытка для этой ошибки" — иначе
    # сработает IMP-E retry-ветка (GENERATING_PATCH), не финальный REJECT.
    from core.pipeline_stage import PipelineStage as _PS
    sig = _PS._static_signature(ctx.selected_error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_net_delta_error_retries": {sig: 1},
    }))

    with patch("core.stages.net_delta_check.classify_net_delta",
              return_value=_rollback_classification()):
        result = stage.execute(ctx)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "REJECT"
    assert decisions[0]["reason"] == "net_delta_regression"
    # Живая серия 10 проектов (Anime0t4ku/mister-companion) нашла decisions[]
    # без соответствующего rejected_patches — "Отклонено:" в выводе
    # недосчитывало net-delta rollback. add_rejected_patch теперь тоже вызван.
    assert len(result.rejected_patches) == 1
    assert result.rejected_patches[0]["reason"] == "net_delta_regression"


def test_net_delta_per_file_cap_logs_reject(tmp_path):
    """Cached per-file cap (файл уже заблокирован прошлым rollback) —
    тоже REJECT-исход, тоже должен логироваться."""
    (tmp_path / "a.py").write_text("y = 1\n", encoding="utf-8")
    stage = _make_stage()
    ctx = _base_ctx(tmp_path)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_net_delta_capped_files": ["a.py"],
    }))

    with patch("core.stages.net_delta_check.classify_net_delta",
              return_value=_rollback_classification()):
        result = stage.execute(ctx)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "REJECT"
    assert decisions[0]["reason"] == "net_delta_per_file_cap"
    assert len(result.rejected_patches) == 1
    assert result.rejected_patches[0]["reason"] == "net_delta_per_file_cap"


def test_net_delta_needs_review_logs_needs_review(tmp_path):
    """NET_DELTA uncertain → NeedsReviewStage напрямую — должен логироваться
    как NEEDS_REVIEW, не пропадать."""
    (tmp_path / "a.py").write_text("y = 1\n", encoding="utf-8")
    stage = _make_stage()
    ctx = _base_ctx(tmp_path)

    with patch("core.stages.net_delta_check.classify_net_delta",
              return_value=_needs_review_classification()):
        result = stage.execute(ctx)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "NEEDS_REVIEW"
    assert decisions[0]["reason"] == "net_delta_uncertain"


def test_net_delta_first_retry_does_not_log_yet(tmp_path):
    """Первая (IMP-E retry) попытка → GENERATING_PATCH, НЕ финальный исход —
    decisions[] должен остаться пуст (логировать рано, решение не финальное)."""
    (tmp_path / "a.py").write_text("y = 1\n", encoding="utf-8")
    stage = _make_stage()
    ctx = _base_ctx(tmp_path)  # нет _net_delta_error_retries → первая попытка

    with patch("core.stages.net_delta_check.classify_net_delta",
              return_value=_rollback_classification()):
        result = stage.execute(ctx)

    assert result.metadata.get("decisions", []) == []
    from core.state_machine import State
    assert result.current_state == State.GENERATING_PATCH


def test_append_decision_log_shared_function_direct():
    """Прямой тест общей функции (вынесена из DecideStage в
    analysis/run_statistics.py, находка #1)."""
    from analysis.run_statistics import append_decision_log
    meta = {}
    append_decision_log(meta, {"file": "x.py", "line": 1, "code": "E1"}, "REJECT", "test_reason")
    assert len(meta["decisions"]) == 1
    assert meta["decisions"][0]["decision"] == "REJECT"
    assert meta["decisions"][0]["reason"] == "test_reason"
    assert meta["decisions"][0]["file"] == "x.py"


def test_decide_stage_append_decision_log_delegates_to_shared_function():
    """DecideStage._append_decision_log остаётся тонкой обёрткой (backward
    compat для ~13 существующих call sites внутри decide_stage.py)."""
    from core.stages.decide_stage import DecideStage
    meta = {}
    DecideStage._append_decision_log(meta, {"file": "y.py", "line": 2, "code": "E2"},
                                     "ACCEPT", "ok")
    assert len(meta["decisions"]) == 1
    assert meta["decisions"][0]["decision"] == "ACCEPT"


def test_safe_int_handles_non_numeric_line_without_dropping_record():
    """Живая ревалидация 2026-06-21: ведущая гипотеза для одной потерянной
    decision-записи — нечисловое поле `line` поднимало ValueError внутри
    decision_from_metadata, которое глушилось без следа. _safe_int не должен
    позволить ОДНОМУ битому полю выбросить всю запись."""
    from analysis.run_statistics import append_decision_log, _safe_int

    assert _safe_int("abc", default=0) == 0
    assert _safe_int("12", default=0) == 12
    assert _safe_int(None, default=0) == 0
    assert _safe_int(3.7, default=0) == 3

    meta = {}
    append_decision_log(meta, {"file": "x.py", "line": "not-a-number", "code": "E1"},
                        "REJECT", "test")
    # Запись НЕ потеряна — line просто упал в default, остальные поля целы.
    assert len(meta.get("decisions", [])) == 1
    assert meta["decisions"][0]["line"] == 0
    assert meta["decisions"][0]["file"] == "x.py"


def test_append_decision_log_logs_on_genuine_failure(caplog):
    """except Exception теперь логирует на WARNING, а не глушит молча
    (находка #1 живой ревалидации: тихое проглатывание маскирует рецидив)."""
    import logging
    from unittest.mock import patch
    from analysis.run_statistics import append_decision_log

    meta = {}
    with caplog.at_level(logging.WARNING, logger="analysis.run_statistics"):
        with patch("analysis.run_statistics.decision_from_metadata",
                  side_effect=RuntimeError("boom")):
            append_decision_log(meta, {"file": "x.py", "line": 1}, "REJECT", "test")

    assert meta.get("decisions", []) == []  # запись действительно потеряна...
    assert any("потеряна" in r.message for r in caplog.records)  # ...но НЕ молча


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
