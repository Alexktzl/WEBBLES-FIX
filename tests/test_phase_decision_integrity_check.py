"""
Control series 2026-06-21, расширенное логирование decisions[] — встроенная
сверка PipelineEngine._check_decision_integrity. Цель: каждый прогон должен
САМ обнаруживать расхождение между granular decisions[] и независимыми
счётчиками (accepted_patches/rejected_patches/needs_review_items), логируя
его на WARNING (виден в стандартном выводе run_agent.py без специальной
настройки логирования) и сохраняя диагностику в notes/metadata, вместо того
чтобы расхождение оставалось незамеченным, как было найдено в control series
из 20 проектов 2026-06-21.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_engine import PipelineEngine
from analysis.run_statistics import RunStatistics, DecisionRecord


def _fake_self():
    # M5 (аудит 2026-07-01): продовый код теперь пишет _decision_integrity
    # через context.update(metadata=...) (copy-on-write), а не прямой
    # мутацией — заглушка воспроизводит этот контракт.
    ctx = SimpleNamespace(metadata={})

    def _update(**kw):
        for k, v in kw.items():
            setattr(ctx, k, v)
        return ctx

    ctx.update = _update
    return SimpleNamespace(context=ctx)


def _stats_with_decisions(*decisions):
    s = RunStatistics()
    for dec in decisions:
        s.decisions.append(DecisionRecord(
            file="x.py", line=1, code="E1", error_class="CLEANUP", message="m",
            decision=dec, reason="r", patch_source="", confidence=None,
            review_verdict="", symbol_regression=False, ts_iso="2026-06-21T00:00:00Z",
        ))
    return s


def test_integrity_ok_when_counts_match():
    fake_self = _fake_self()
    stats = _stats_with_decisions("ACCEPT", "REJECT", "NEEDS_REVIEW")
    PipelineEngine._check_decision_integrity(
        fake_self, stats,
        accepted_patches=[{"error": {}}],
        rejected_patches=[{"error": {}, "reason": "error_count_not_decreased"}],
        needs_review_items=[{"error_sig": "x"}],
        decisions_merge_dropped=0,
    )
    integrity = fake_self.context.metadata["_decision_integrity"]
    assert integrity["ok"] is True
    assert integrity["issues"] == []


def test_integrity_detects_missing_needs_review_entry():
    """Воспроизводит ровно ту находку, что обнаружена в живой ревалидации
    (AdsMCP): net_delta_uncertain_count=1 / needs_review_items=1, но
    decisions[] не содержит соответствующей NEEDS_REVIEW записи."""
    fake_self = _fake_self()
    stats = _stats_with_decisions("ACCEPT")  # NEEDS_REVIEW отсутствует в decisions[]
    PipelineEngine._check_decision_integrity(
        fake_self, stats,
        accepted_patches=[{"error": {}}],
        rejected_patches=[],
        needs_review_items=[{"error_sig": "x"}],  # но реально была 1
        decisions_merge_dropped=0,
    )
    integrity = fake_self.context.metadata["_decision_integrity"]
    assert integrity["ok"] is False
    assert any("NEEDS_REVIEW" in issue for issue in integrity["issues"])


def test_integrity_explains_reject_gap_via_invalid_context():
    """REJECT gap, полностью объяснённый известным invalid_context-bypass
    (GeneratePatchStage), НЕ должен флагироваться как неожиданная находка."""
    fake_self = _fake_self()
    stats = _stats_with_decisions("ACCEPT")  # 0 REJECT в decisions[]
    PipelineEngine._check_decision_integrity(
        fake_self, stats,
        accepted_patches=[{"error": {}}],
        rejected_patches=[
            {"error": {}, "reason": "invalid_context"},
            {"error": {}, "reason": "invalid_context"},
        ],
        needs_review_items=[],
        decisions_merge_dropped=0,
    )
    integrity = fake_self.context.metadata["_decision_integrity"]
    assert integrity["ok"] is True
    assert integrity["reject_gap_explained_by_invalid_context"] == 2


def test_integrity_flags_unexplained_reject_gap():
    """REJECT gap, НЕ объяснённый известным bypass'ом — должен флагироваться."""
    fake_self = _fake_self()
    stats = _stats_with_decisions("ACCEPT")  # 0 REJECT в decisions[]
    PipelineEngine._check_decision_integrity(
        fake_self, stats,
        accepted_patches=[{"error": {}}],
        rejected_patches=[{"error": {}, "reason": "error_count_not_decreased"}],
        needs_review_items=[],
        decisions_merge_dropped=0,
    )
    integrity = fake_self.context.metadata["_decision_integrity"]
    assert integrity["ok"] is False
    assert any("REJECT" in issue for issue in integrity["issues"])


def test_integrity_flags_merge_drops():
    fake_self = _fake_self()
    stats = _stats_with_decisions()
    PipelineEngine._check_decision_integrity(
        fake_self, stats,
        accepted_patches=[], rejected_patches=[], needs_review_items=[],
        decisions_merge_dropped=2,
    )
    integrity = fake_self.context.metadata["_decision_integrity"]
    assert integrity["ok"] is False
    assert integrity["decisions_merge_dropped"] == 2


def test_integrity_never_raises_on_malformed_input():
    """Сам integrity-check не должен ронять прогон — это диагностика, а не
    критический путь."""
    fake_self = _fake_self()
    PipelineEngine._check_decision_integrity(
        fake_self, None, accepted_patches=None, rejected_patches=None,
        needs_review_items=None, decisions_merge_dropped=0,
    )  # не должно поднять исключение


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
