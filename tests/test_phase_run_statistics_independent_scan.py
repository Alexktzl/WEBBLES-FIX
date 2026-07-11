"""
bcrypt-прогон 2026-07-03 (statistic/bcrypt/run_20260703-121449z.md): заголовок
отчёта показал «Ошибок до 24, после 58 (-141.7%)», хотя честный независимый
скан дал 24 -> 22 и unsafe_accept=0. Причина:
`record_final(final_error_count=len(context.current_errors))` — это длина
ВНУТРЕННЕЙ инкрементальной очереди движка, которая на bcrypt распухла от
де-маскированных mypy-ошибок (dependency recovery поставил pytest в sandbox).
Отчёт строил «Итог» от очереди, а не от независимого скана.

Эти тесты проверяют ИНВАРИАНТ (не реализацию):
  1) когда независимый скан задан и расходится с очередью — секция «Итог» в
     markdown показывает числа СКАНА (с процентом от before-скана), а
     очередь движка выводится отдельной диагностической строкой;
  2) когда скан недоступен (None) — markdown идентичен старому поведению
     (числа очереди), диагностической строки про очередь нет — честный
     fallback, ничего не подделываем;
  3) aggregate() отдаёт independent_scan_before/after/delta корректно и
     None-безопасно (частичные данные -> delta тоже None, не 0 — иначе
     выглядело бы как «ничего не изменилось»).

Стиль — по образцу tests/test_phase_progress_metrics.py (чистые assert,
без подъёма PipelineEngine).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.run_statistics import RunStatistics


def _make_stats(final_error_count=58, independent_scan_before=None,
                 independent_scan_after=None, initial_errors=24):
    s = RunStatistics()
    s.start("/proj", "python")
    s.record_initial_errors([{"code": "X"}] * initial_errors)
    s.record_final(
        final_error_count=final_error_count,
        accepted_patches=[], rejected_patches=[], needs_review_items=[],
        audit_result={}, iterations=1, rollbacks=0,
        independent_scan_before=independent_scan_before,
        independent_scan_after=independent_scan_after,
    )
    return s


# ---------------------------------------------------------------------
# 1. Скан задан и расходится с очередью (воспроизводит bcrypt-инцидент).
# ---------------------------------------------------------------------

def test_markdown_summary_uses_independent_scan_when_available():
    # Воспроизводит bcrypt 2026-07-03: очередь движка "до 24, после 58"
    # (демаскированные mypy-ошибки распухли счётчик), независимый скан
    # честно даёт "24 -> 22".
    s = _make_stats(
        final_error_count=58, initial_errors=24,
        independent_scan_before=24, independent_scan_after=22,
    )
    agg = s.aggregate()
    assert agg["independent_scan_before"] == 24
    assert agg["independent_scan_after"] == 22
    assert agg["independent_scan_delta"] == 2

    md = s.to_markdown()
    itog = md.split("## Итог", 1)[1]
    # "Итог" построен от скана (24 -> 22), НЕ от раздутой очереди (24 -> 58).
    assert "**24**" in itog
    assert "**22**" in itog
    assert "после: **58**" not in itog
    # очередь движка вынесена отдельной диагностической строкой.
    assert "Очередь движка" in itog
    assert "58" in itog  # число очереди всё ещё присутствует, но не как "Итог"


def test_markdown_scan_percentage_relative_to_scan_before_not_queue():
    # Процент в "Итоге" должен считаться от before-скана (10 -> 8 = 20%),
    # а не от очереди (10 -> 1 дало бы 90%, что было бы неверным bcrypt-
    # паттерном "-141.7%").
    s = _make_stats(
        final_error_count=1, initial_errors=10,
        independent_scan_before=10, independent_scan_after=8,
    )
    md = s.to_markdown()
    itog = md.split("## Итог", 1)[1].split("##", 1)[0]
    assert "20.0%" in itog


# ---------------------------------------------------------------------
# 2. Скан недоступен (None) -> прежнее поведение, без подделки чисел.
# ---------------------------------------------------------------------

def test_markdown_falls_back_to_queue_when_scan_unavailable():
    s = _make_stats(
        final_error_count=58, initial_errors=24,
        independent_scan_before=None, independent_scan_after=None,
    )
    agg = s.aggregate()
    assert agg["independent_scan_before"] is None
    assert agg["independent_scan_after"] is None
    assert agg["independent_scan_delta"] is None

    md = s.to_markdown()
    itog = md.split("## Итог", 1)[1].split("##", 1)[0]
    # Старое поведение: "Итог" от очереди (24 -> 58), без diagnostic-строки.
    assert "**24**" in itog
    assert "после: **58**" in itog
    assert "Очередь движка" not in itog


def test_markdown_falls_back_when_only_one_scan_side_present():
    # Частичные данные (например, scan_after не удалось посчитать) — тоже
    # честный fallback, не полусостояние.
    s = _make_stats(
        final_error_count=5, initial_errors=10,
        independent_scan_before=10, independent_scan_after=None,
    )
    agg = s.aggregate()
    assert agg["independent_scan_delta"] is None
    md = s.to_markdown()
    itog = md.split("## Итог", 1)[1].split("##", 1)[0]
    assert "Очередь движка" not in itog


# ---------------------------------------------------------------------
# 3. aggregate() None-безопасность delta.
# ---------------------------------------------------------------------

def test_aggregate_independent_scan_delta_none_safe():
    s = _make_stats(independent_scan_before=None, independent_scan_after=5)
    agg = s.aggregate()
    assert agg["independent_scan_before"] is None
    assert agg["independent_scan_after"] == 5
    assert agg["independent_scan_delta"] is None


def test_aggregate_independent_scan_delta_computed():
    s = _make_stats(independent_scan_before=24, independent_scan_after=22)
    agg = s.aggregate()
    assert agg["independent_scan_delta"] == 2


def test_record_final_default_is_none_no_crash():
    """record_final без independent_scan_* (старые вызовы / другие
    прогоны) не должен падать — None остаётся None, markdown как раньше."""
    s = RunStatistics()
    s.start("/proj", "python")
    s.record_initial_errors([{"code": "X"}] * 3)
    s.record_final(
        final_error_count=1,
        accepted_patches=[], rejected_patches=[], needs_review_items=[],
        audit_result={}, iterations=0, rollbacks=0,
    )
    agg = s.aggregate()
    assert agg["independent_scan_before"] is None
    assert agg["independent_scan_after"] is None
    assert agg["independent_scan_delta"] is None
    md = s.to_markdown()
    assert "Очередь движка" not in md
