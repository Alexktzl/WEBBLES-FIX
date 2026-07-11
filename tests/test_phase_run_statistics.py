"""
RunStatistics — статистика прогона в `<project>/statistic/`.

Пользователь после прогона должен получить готовые отчёты для
демонстрации: машиночитаемый JSON + читаемый Markdown + append-only
JSONL для трендов. Этот suite проверяет:

  1) Чистый класс `RunStatistics` собирает метрики из методов
     start / record_initial_errors / record_decision / record_final.
  2) `aggregate()` правильно считает delta ошибок, распределение
     по decision/source/verdict, помечает symbol_regression.
  3) `write_to_disk(path)` создаёт папку `statistic/` и 4 файла:
     run_<ts>.json, run_<ts>.md, summary.md, runs.jsonl.
  4) `runs.jsonl` append-only (повторный прогон не затирает).
  5) `decision_from_metadata` корректно вытаскивает поля из
     error + context.metadata (review/symbol_regression/confidence).
  6) Edge: пустой прогон → write_to_disk пишет нули, не падает.

Запуск: python3 tests/test_phase_run_statistics.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.run_statistics import (
    RunStatistics, DecisionRecord, decision_from_metadata,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------------
# 1. start / record_initial_errors
# ---------------------------------------------------------------------

def test_start_and_initial():
    s = RunStatistics()
    s.start("/proj", "python")
    s.record_initial_errors([
        {"file": "a.py", "line": 1, "code": "E0602", "error_class": "BLOCKING"},
        {"file": "a.py", "line": 2, "code": "E0602", "error_class": "BLOCKING"},
        {"file": "b.py", "line": 1, "code": "F401",  "error_class": "CLEANUP"},
    ])
    check("start: project_path", s.project_path == "/proj")
    check("start: language", s.language == "python")
    check("start: ts started", bool(s.started_at_iso))
    check("initial: count == 3", s.initial_error_count == 3)
    check("initial: by_class.BLOCKING == 2",
          s.initial_errors_by_class.get("BLOCKING") == 2)
    check("initial: by_class.CLEANUP == 1",
          s.initial_errors_by_class.get("CLEANUP") == 1)
    check("initial: by_code.E0602 == 2",
          s.initial_errors_by_code.get("E0602") == 2)


# ---------------------------------------------------------------------
# 2. record_decision / aggregate
# ---------------------------------------------------------------------

def test_aggregate_counts():
    s = RunStatistics()
    s.start("/p", "python")
    s.record_initial_errors([{"code": "X"}] * 5)
    s.record_decision(DecisionRecord(
        file="a.py", line=1, code="X", decision="ACCEPT",
        patch_source="rule_based", confidence=0.95,
        review_verdict="skipped"))
    s.record_decision(DecisionRecord(
        file="b.py", line=1, code="X", decision="ACCEPT",
        patch_source="structured_llm", confidence=0.88,
        review_verdict="ok"))
    s.record_decision(DecisionRecord(
        file="c.py", line=1, code="X", decision="NEEDS_REVIEW",
        patch_source="structured_llm", confidence=0.7,
        review_verdict="noisy"))
    s.record_decision(DecisionRecord(
        file="d.py", line=1, code="X", decision="REJECT",
        reason="error_count_not_decreased"))
    s.record_decision(DecisionRecord(
        file="e.py", line=1, code="X", decision="NEEDS_REVIEW",
        patch_source="structured_llm",
        symbol_regression=True))
    s.record_final(
        final_error_count=2,
        accepted_patches=[
            {"error": {"file": "a.py"}}, {"error": {"file": "b.py"}}
        ],
        rejected_patches=[{"error": {"file": "d.py"}}],
        needs_review_items=[{"file": "c.py", "line": 1, "error_code": "X"},
                            {"file": "e.py", "line": 1, "error_code": "X"}],
        audit_result={"passed_files": ["a.py", "b.py"],
                      "failed_segments": {}},
        iterations=4, rollbacks=2,
    )
    agg = s.aggregate()
    check("agg: initial 5", agg["initial_errors"] == 5)
    check("agg: final 2", agg["final_errors"] == 2)
    check("agg: delta 3", agg["errors_fixed_delta"] == 3)
    check("agg: pct 60", agg["errors_fixed_pct"] == 60.0)
    check("agg: decisions 5", agg["decisions_total"] == 5)
    check("agg: ACCEPT 2",
          agg["decisions_by_outcome"].get("ACCEPT") == 2)
    check("agg: NEEDS_REVIEW 2",
          agg["decisions_by_outcome"].get("NEEDS_REVIEW") == 2)
    check("agg: REJECT 1",
          agg["decisions_by_outcome"].get("REJECT") == 1)
    check("agg: source rule_based 1",
          agg["patches_by_source"].get("rule_based") == 1)
    check("agg: source structured_llm 3",
          agg["patches_by_source"].get("structured_llm") == 3)
    check("agg: review noisy 1",
          agg["review_by_verdict"].get("noisy") == 1)
    check("agg: symbol_regression 1",
          agg["symbol_regression_count"] == 1)
    check("agg: accepted_files 2",
          agg["accepted_files_count"] == 2)
    check("agg: needs_review 2",
          agg["needs_review_count"] == 2)
    check("agg: iterations 4", agg["iterations"] == 4)
    check("agg: rollbacks 2", agg["rollbacks"] == 2)


# ---------------------------------------------------------------------
# 3. write_to_disk
# ---------------------------------------------------------------------

def test_write_to_disk_creates_files():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        # statistics_dir живёт в системном webbles_fix/statistic/<basename(d)>,
        # НЕ в d — без явной чистки тест оставляет мусор в реальном репо
        # (см. project_tech_debt_backlog в памяти, пункт 8, 2026-06-20).
        try:
            s = RunStatistics()
            s.start(str(proj), "python")
            s.record_initial_errors([{"code": "E"}])
            s.record_decision(DecisionRecord(
                file="x.py", line=1, code="E", decision="ACCEPT",
                patch_source="rule_based", confidence=0.95))
            s.record_final(
                final_error_count=0,
                accepted_patches=[{"error": {"file": "x.py"}}],
                rejected_patches=[], needs_review_items=[],
                audit_result={"passed_files": ["x.py"]}, iterations=1, rollbacks=0,
            )
            paths = s.write_to_disk(str(proj))
            check("write: paths returned", isinstance(paths, dict) and paths)
            # P0.3: statistic/ теперь живёт в папке webbles_fix-системы,
            # а не в ремонтируемом проекте.
            stat_dir = RunStatistics.statistics_dir(proj)
            check("write: dir created", stat_dir.is_dir())
            # summary.md + run_<ts>.{json,md} + runs.jsonl
            files = sorted(p.name for p in stat_dir.iterdir())
            check("write: summary.md exists",
                  "summary.md" in files)
            check("write: runs.jsonl exists",
                  "runs.jsonl" in files)
            check("write: run_*.md exists",
                  any(f.startswith("run_") and f.endswith(".md") for f in files))
            check("write: run_*.json exists",
                  any(f.startswith("run_") and f.endswith(".json") for f in files))
            # summary.md содержит итог.
            summary = (stat_dir / "summary.md").read_text(encoding="utf-8")
            check("write: summary mentions ACCEPT",
                  "ACCEPT" in summary)
            check("write: summary mentions fixed pct",
                  "100.0%" in summary or "0%" in summary or "%" in summary)
            # run.json валидный.
            run_json_path = next(p for p in stat_dir.iterdir()
                                 if p.name.startswith("run_") and p.suffix == ".json")
            data = json.loads(run_json_path.read_text(encoding="utf-8"))
            check("write: json has aggregate", "aggregate" in data)
            check("write: json has decisions",
                  len(data.get("decisions", [])) == 1)
        finally:
            shutil.rmtree(RunStatistics.statistics_dir(proj), ignore_errors=True)


def test_runs_jsonl_is_append_only():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        try:
            for i in range(3):
                s = RunStatistics()
                s.start(str(proj), "python")
                s.record_initial_errors([{"code": "X"}] * (i + 1))
                s.record_final(
                    final_error_count=i,
                    accepted_patches=[], rejected_patches=[],
                    needs_review_items=[], audit_result={},
                    iterations=1, rollbacks=0,
                )
                s.write_to_disk(str(proj))
            # P0.3: jsonl лежит в системной папке, не в проекте.
            jsonl = (RunStatistics.statistics_dir(proj) / "runs.jsonl").read_text(encoding="utf-8")
            lines = [ln for ln in jsonl.splitlines() if ln.strip()]
            check("jsonl: 3 lines after 3 runs", len(lines) == 3)
            # Каждая строка — JSON.
            ok_all = True
            for ln in lines:
                try:
                    json.loads(ln)
                except Exception:
                    ok_all = False
                    break
            check("jsonl: every line parseable", ok_all)
        finally:
            shutil.rmtree(RunStatistics.statistics_dir(proj), ignore_errors=True)


# ---------------------------------------------------------------------
# 4. decision_from_metadata
# ---------------------------------------------------------------------

def test_decision_from_metadata_basic():
    err = {"file": "auth.py", "line": 10, "code": "E0432",
           "error_class": "BLOCKING", "message": "unresolved import"}
    md = {
        "patch_source": "structured_llm",
        "confidence": 0.87,
        "review": {"verdict": "ok"},
        "symbol_regression": None,
    }
    rec = decision_from_metadata(err, md, "ACCEPT", "imports fixed")
    check("dfm: file",   rec.file == "auth.py")
    check("dfm: line",   rec.line == 10)
    check("dfm: code",   rec.code == "E0432")
    check("dfm: class",  rec.error_class == "BLOCKING")
    check("dfm: decision", rec.decision == "ACCEPT")
    check("dfm: reason", rec.reason == "imports fixed")
    check("dfm: source", rec.patch_source == "structured_llm")
    check("dfm: conf",   abs((rec.confidence or 0) - 0.87) < 1e-6)
    check("dfm: review", rec.review_verdict == "ok")
    check("dfm: sym_reg False", rec.symbol_regression is False)


def test_decision_from_metadata_with_symbol_regression():
    err = {"file": "auth.py", "line": 3, "code": "E0001",
           "error_class": "BLOCKING", "message": "x"}
    md = {
        "confidence": 0.95,
        "symbol_regression": {
            "file": "auth.py",
            "missing_defs": ["hash_password"],
            "missing_classes": [],
        },
    }
    rec = decision_from_metadata(err, md, "NEEDS_REVIEW", "lost defs")
    check("dfm(sym): sym_reg True", rec.symbol_regression is True)


def test_decision_from_metadata_empty():
    """Пустой error/metadata не должны падать."""
    rec = decision_from_metadata({}, {}, "REJECT", "")
    check("dfm(empty): file ''",   rec.file == "")
    check("dfm(empty): line 0",    rec.line == 0)
    check("dfm(empty): code ''",   rec.code == "")
    check("dfm(empty): conf None", rec.confidence is None)
    check("dfm(empty): decision REJECT", rec.decision == "REJECT")


# ---------------------------------------------------------------------
# 5. edge: пустой прогон
# ---------------------------------------------------------------------

def test_empty_run_writes_safely():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        try:
            s = RunStatistics()
            s.start(str(proj), "rust")
            # никаких record_initial_errors / record_decision
            s.record_final(
                final_error_count=0,
                accepted_patches=None, rejected_patches=None,
                needs_review_items=None, audit_result=None,
                iterations=0, rollbacks=0,
            )
            paths = s.write_to_disk(str(proj))
            check("empty: write returned paths", bool(paths))
            agg = s.aggregate()
            check("empty: delta 0", agg["errors_fixed_delta"] == 0)
            check("empty: pct 0",   agg["errors_fixed_pct"] == 0.0)
            check("empty: decisions 0", agg["decisions_total"] == 0)
            check("empty: needs_review 0", agg["needs_review_count"] == 0)
        finally:
            shutil.rmtree(RunStatistics.statistics_dir(proj), ignore_errors=True)


# ---------------------------------------------------------------------
# 6. Markdown содержит ключевые секции
# ---------------------------------------------------------------------

def test_markdown_has_sections():
    s = RunStatistics()
    s.start("/p", "python")
    s.record_initial_errors([
        {"code": "E0602", "error_class": "BLOCKING"},
        {"code": "F401",  "error_class": "CLEANUP"},
    ])
    s.record_decision(DecisionRecord(
        file="a.py", line=1, code="E0602", decision="ACCEPT",
        patch_source="rule_based", confidence=0.95, review_verdict="skipped"))
    s.record_final(
        final_error_count=1,
        accepted_patches=[{"error": {"file": "a.py"}}],
        rejected_patches=[], needs_review_items=[],
        audit_result={"passed_files": ["a.py"]},
        iterations=1, rollbacks=0,
    )
    md = s.to_markdown()
    for section in (
        "# Webbles Fix",
        "## Итог",
        "Ошибки на старте",
        "Топ-10 кодов",
        "Источники патчей",
        "Журнал решений",
        "ACCEPT",
        "rule_based",
    ):
        check(f"md: section '{section}' present", section in md)


if __name__ == "__main__":
    print("RunStatistics:")
    test_start_and_initial()
    test_aggregate_counts()
    test_write_to_disk_creates_files()
    test_runs_jsonl_is_append_only()
    test_decision_from_metadata_basic()
    test_decision_from_metadata_with_symbol_regression()
    test_decision_from_metadata_empty()
    test_empty_run_writes_safely()
    test_markdown_has_sections()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nRunStatistics: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
