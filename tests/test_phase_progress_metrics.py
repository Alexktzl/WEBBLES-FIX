"""
2026-06-24: расследование на RussellDash332/pytils показало, что
`baseline_remaining` (внутренняя инкрементальная модель очереди
`context.current_errors`) расходится с независимым fresh full scan того же
кода на ~1187 ошибок при всего 5 ACCEPT — то есть НЕ годится как показатель
"сколько реально исправлено".

Точка истины теперь — fresh full scan before/after (см.
`PipelineEngine.compute_progress_metrics`). Эти тесты проверяют чистую
функцию изолированно (без поднятия всего PipelineEngine).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

import types
try:  # pragma: no cover
    import tomlkit  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_engine import compute_progress_metrics


def _scan(total, errors):
    return {"total": total, "errors": errors, "breakdown": {}}


def test_independent_scan_delta_is_source_of_truth():
    before = _scan(100, [{"file": "a.py", "code": "E1"}] * 100)
    after = _scan(70, [{"file": "a.py", "code": "E1"}] * 70)
    out = compute_progress_metrics(
        scan_before=before, scan_after=after,
        accepted_patches=[], rejected_patches=[],
        needs_review_meta={}, initial_error_count=100,
        baseline_remaining=40, total_current_errors=70,
    )
    assert out["independent_scan_before"] == 100
    assert out["independent_scan_after"] == 70
    assert out["independent_scan_delta"] == 30
    # queue_delta (старая метрика) считается отдельно и МОЖЕТ не совпадать
    # с independent_scan_delta — именно в этом расхождении был смысл расследования.
    assert out["queue_delta"] == 60


def test_real_fix_impact_only_counts_touched_files():
    before = _scan(10, [
        {"file": "a.py", "code": "E1"}, {"file": "a.py", "code": "E2"},
        {"file": "b.py", "code": "E1"}, {"file": "b.py", "code": "E2"},
        {"file": "b.py", "code": "E3"},
    ])
    after = _scan(3, [
        {"file": "a.py", "code": "E1"}, {"file": "a.py", "code": "E2"},
        {"file": "b.py", "code": "E1"},
    ])
    accepted = [{"file": "b.py", "code": "E2"}]
    out = compute_progress_metrics(
        scan_before=before, scan_after=after,
        accepted_patches=accepted, rejected_patches=[],
        needs_review_meta={}, initial_error_count=10,
        baseline_remaining=3, total_current_errors=3,
    )
    # a.py не тронут ACCEPT'ом — его 2 ошибки (без изменений) не входят
    # в real_fix_impact, даже если они есть в both scan'ах.
    # b.py: было 3 ошибки, осталось 1 -> real_fix_impact = 2 (каскадный эффект:
    # ACCEPT целился в E2, но и E3 тоже пропала).
    assert out["real_fix_impact"] == 2
    assert out["independent_scan_delta"] == 7


def test_real_fix_impact_none_when_scan_before_missing():
    out = compute_progress_metrics(
        scan_before=None, scan_after=_scan(5, []),
        accepted_patches=[{"file": "a.py"}], rejected_patches=[],
        needs_review_meta={}, initial_error_count=10,
        baseline_remaining=5, total_current_errors=5,
    )
    assert out["real_fix_impact"] is None
    assert out["independent_scan_before"] is None
    assert out["independent_scan_delta"] is None
    # queue_delta не зависит от scan'ов и считается всегда.
    assert out["queue_delta"] == 5


def test_attempted_errors_count_excludes_no_patch_unprocessed():
    out = compute_progress_metrics(
        scan_before=_scan(10, []), scan_after=_scan(8, []),
        accepted_patches=[{"file": "a.py"}, {"file": "b.py"}],
        rejected_patches=[{"file": "c.py"}],
        needs_review_meta={
            "needs_review_with_patch_count": 3,
            "needs_review_no_patch_count": 15,
        },
        initial_error_count=10, baseline_remaining=2, total_current_errors=8,
    )
    # 2 ACCEPT + 1 REJECT + 3 NEEDS_REVIEW-с-патчем = 6 ошибок, до которых
    # система реально дошла с попыткой генерации. 15 каскадных ошибок без
    # единой попытки (упёрлись в project_timeout раньше) — НЕ считаются.
    assert out["attempted_errors_count"] == 6
    assert out["needs_review_with_patch"] == 3
    assert out["needs_review_no_patch_unprocessed"] == 15


def test_oscillation_cancelled_accept_excluded_from_real_fix_impact():
    """4. Взаимно отменённые ACCEPT (oscillation_cancelled=True, см.
    PipelineEngine._ban_oscillating_signatures) не считаются REAL_FIX/
    real_fix_impact — 2026-06-24, Bluetooth-Devices/dbus-fast: 2 ACCEPT на
    tests/test_marshaller.py:1344 взаимно отменили друг друга, файл
    байт-в-байт идентичен исходному, но без этого фильтра real_fix_impact
    ложно засчитал бы этот файл как "тронутый ACCEPT"."""
    before = _scan(5, [
        {"file": "a.py", "code": "E1"},  # реальный фикс
        {"file": "b.py", "code": "E2"}, {"file": "b.py", "code": "E2"},
        {"file": "b.py", "code": "E2"},
        {"file": "c.py", "code": "E3"},
    ])
    after = _scan(4, [
        {"file": "b.py", "code": "E2"}, {"file": "b.py", "code": "E2"},
        {"file": "b.py", "code": "E2"},
        {"file": "c.py", "code": "E3"},
    ])
    accepted = [
        {"file": "a.py", "line": 1, "code": "E1"},  # настоящий ACCEPT
        {"file": "b.py", "line": 2, "code": "E2", "oscillation_cancelled": True},
        {"file": "b.py", "line": 2, "code": "E2", "oscillation_cancelled": True},
    ]
    out = compute_progress_metrics(
        scan_before=before, scan_after=after,
        accepted_patches=accepted, rejected_patches=[],
        needs_review_meta={"accept_cancelled_items": [
            {"file": "b.py", "line": 2, "code": "E2", "net_change_bytes": 0},
        ]},
        initial_error_count=5, baseline_remaining=4, total_current_errors=4,
    )
    # b.py НЕ входит в touched_files (оба его ACCEPT отменены) — real_fix_impact
    # считает только a.py (1 ошибка реально пропала), а не b.py (0 пропало) +
    # a.py = всё равно 1, но КЛЮЧЕВОЕ: если бы b.py ложно попал в touched_files,
    # before_touched/after_touched по b.py были бы равны (3==3), не испортив
    # число — поэтому проверяем явно через отдельный сценарий ниже, где b.py
    # единственный touched file и его "вклад" должен быть 0, не больше.
    assert out["real_fix_impact"] == 1
    assert out["accept_cancelled_count"] == 2
    assert out["accept_cancelled_items"] == [
        {"file": "b.py", "line": 2, "code": "E2", "net_change_bytes": 0},
    ]


def test_oscillation_cancelled_only_accept_gives_zero_real_fix_impact():
    """Сценарий, где ЕДИНСТВЕННЫЙ ACCEPT в прогоне — отменённая осцилляция:
    real_fix_impact должен быть 0 (или None, если совсем нет реальных ACCEPT),
    а НЕ положительным числом из-за случайного совпадения total-ошибок."""
    before = _scan(3, [
        {"file": "b.py", "code": "E2"}, {"file": "b.py", "code": "E2"},
        {"file": "b.py", "code": "E2"},
    ])
    after = _scan(3, [
        {"file": "b.py", "code": "E2"}, {"file": "b.py", "code": "E2"},
        {"file": "b.py", "code": "E2"},
    ])
    accepted = [
        {"file": "b.py", "line": 2, "code": "E2", "oscillation_cancelled": True},
        {"file": "b.py", "line": 2, "code": "E2", "oscillation_cancelled": True},
    ]
    out = compute_progress_metrics(
        scan_before=before, scan_after=after,
        accepted_patches=accepted, rejected_patches=[],
        needs_review_meta={}, initial_error_count=3,
        baseline_remaining=3, total_current_errors=3,
    )
    # Без реальных (не-cancelled) ACCEPT touched_files пуст -> real_fix_impact = 0.
    assert out["real_fix_impact"] == 0
    assert out["accept_cancelled_count"] == 2


def test_defaults_to_zero_without_needs_review_meta():
    out = compute_progress_metrics(
        scan_before=_scan(1, []), scan_after=_scan(1, []),
        accepted_patches=[], rejected_patches=[],
        needs_review_meta={}, initial_error_count=1,
        baseline_remaining=1, total_current_errors=1,
    )
    assert out["needs_review_with_patch"] == 0
    assert out["needs_review_no_patch_unprocessed"] == 0
    assert out["attempted_errors_count"] == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
