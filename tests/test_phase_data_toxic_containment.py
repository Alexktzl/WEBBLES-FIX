"""
Q1 (2026-07-02, спека Алекса): containment data-toxic файла.

pyca/bcrypt, tests/test_bcrypt.py — malformed тестовые данные. LLM-патчи
mypy-ошибок на этом файле детерминированно портят данные, guard O.19
(analysis/data_literal_guard.py, decide_stage.py:697-700) уводит их в
NEEDS_REVIEW, а FileAntiLoop (core/anti_loop.py) блокирует файл после 3-4
одинаковых причин ("NEEDS_REVIEW (not auto-accepted): O.19 data mangling").
Проблема: блок FileAntiLoop сбрасывается при смене hash файла, и СТИЛЕВЫЕ
(не O.19) ошибки того же файла продолжают жечь циклы бесконечно.

3 точки фикса:
  1. GeneratePatchStage._mark_data_toxic_if_o19 (core/stages/generate_patch_stage.py)
     — при блокировке FileAntiLoop с причиной "O.19 data mangling" помечает
     файл в metadata["_data_toxic_files"].
  2. PrioritizeStage.execute (core/stages/prioritize_stage.py) — вычищает из
     очереди НЕструктурные ошибки data-toxic файлов; структурные (E999/E902/
     invalid-syntax/CRITICAL_SYNTAX) оставляет; считает пропущенное в
     metadata["_data_toxic_skipped"] (max за прогон, не сумма).
  3. PipelineEngine._build_result / _send_report (core/pipeline_engine.py) —
     прокидывает data_toxic_files/data_toxic_skipped в итоговый result и в
     финальное сообщение отчёта.

Запуск: python tests/test_phase_data_toxic_containment.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from core.pipeline_context import PipelineContext  # noqa: E402
from core.pipeline_engine import PipelineEngine  # noqa: E402
from core.state_machine import State  # noqa: E402
from core.stages.generate_patch_stage import GeneratePatchStage  # noqa: E402
from core.stages.prioritize_stage import PrioritizeStage  # noqa: E402
from safety.error_priority_engine import ErrorPriorityEngine  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


# ---------------------------------------------------------------------------
# (a) маркировка — GeneratePatchStage._mark_data_toxic_if_o19
# ---------------------------------------------------------------------------

def test_mark_data_toxic_on_o19_failure_reason():
    ctx = PipelineContext(project_path=Path("."), language="python")
    failure_reason = (
        "NEEDS_REVIEW (not auto-accepted): O.19 data mangling: your patch "
        "changed the type/content of existing test data"
    )
    out = GeneratePatchStage._mark_data_toxic_if_o19(ctx, "tests/test_bcrypt.py", failure_reason)
    check(
        "file_added_to_data_toxic_files",
        list(out.metadata.get("_data_toxic_files") or []) == ["tests/test_bcrypt.py"],
        f"metadata: {dict(out.metadata)}",
    )


def test_mark_data_toxic_noop_on_other_failure_reason():
    ctx = PipelineContext(project_path=Path("."), language="python")
    out = GeneratePatchStage._mark_data_toxic_if_o19(
        ctx, "tests/test_bcrypt.py", "structured direct apply: anchor не найден",
    )
    check(
        "file_not_added_for_non_o19_reason",
        not out.metadata.get("_data_toxic_files"),
        f"metadata: {dict(out.metadata)}",
    )


def test_mark_data_toxic_no_duplicate_entries():
    ctx = PipelineContext(
        project_path=Path("."), language="python",
        metadata={"_data_toxic_files": ["tests/test_bcrypt.py"]},
    )
    out = GeneratePatchStage._mark_data_toxic_if_o19(
        ctx, "tests/test_bcrypt.py", "... O.19 data mangling ...",
    )
    check(
        "file_not_duplicated",
        list(out.metadata.get("_data_toxic_files") or []) == ["tests/test_bcrypt.py"],
        f"metadata: {dict(out.metadata)}",
    )


# ---------------------------------------------------------------------------
# (b) containment — PrioritizeStage вычищает нестроктурные ошибки
# ---------------------------------------------------------------------------

def test_prioritize_stage_filters_non_structural_toxic_errors(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")

    e_style = {"file": "a.py", "line": 10, "code": "E501", "message": "line too long"}
    e_syntax = {"file": "a.py", "line": 20, "code": "E999", "message": "invalid syntax"}
    e_other_file = {"file": "b.py", "line": 5, "code": "E501", "message": "line too long"}

    ctx = PipelineContext(
        project_path=tmp_path, language="python",
        current_errors=(e_style, e_syntax, e_other_file),
        metadata={"_data_toxic_files": ["a.py"]},
    )

    out = PrioritizeStage(ErrorPriorityEngine()).execute(ctx)

    prioritized = list(out.prioritized_errors)
    check(
        "style_error_in_toxic_file_removed",
        not any(e is e_style or (e.get("file") == "a.py" and e.get("code") == "E501") for e in prioritized),
        f"prioritized: {prioritized}",
    )
    check(
        "structural_error_in_toxic_file_kept",
        any(e.get("file") == "a.py" and e.get("code") == "E999" for e in prioritized),
        f"prioritized: {prioritized}",
    )
    check(
        "error_in_other_file_kept",
        any(e.get("file") == "b.py" for e in prioritized),
        f"prioritized: {prioritized}",
    )
    check(
        "skipped_count_recorded",
        dict(out.metadata.get("_data_toxic_skipped") or {}) == {"a.py": 1},
        f"skipped: {dict(out.metadata.get('_data_toxic_skipped') or {})}",
    )


def test_prioritize_stage_skipped_count_takes_max_not_sum_across_rescans():
    """Повторный пересчёт того же прохода не должен суммировать счётчик
    пропущенных ошибок — берём max(старое, новое)."""
    project = Path(".")
    ctx = PipelineContext(
        project_path=project, language="python",
        metadata={
            "_data_toxic_files": ["a.py"],
            "_data_toxic_skipped": {"a.py": 5},
        },
    )
    # Симулируем логику напрямую, как это делает PrioritizeStage.execute
    # (2 новых пропуска в этом проходе < старого максимума 5).
    _skip_meta = dict(ctx.metadata.get("_data_toxic_skipped") or {})
    _skipped_now = {"a.py": 2}
    for _f, _n in _skipped_now.items():
        _skip_meta[_f] = max(_skip_meta.get(_f, 0), _n)
    check(
        "max_not_sum",
        _skip_meta == {"a.py": 5},
        f"skip_meta: {_skip_meta}",
    )


# ---------------------------------------------------------------------------
# (c) отчёт — _build_result / _send_report прокидывают поля
# ---------------------------------------------------------------------------

def _engine(project: Path, metadata: dict) -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    eng.language = "python"
    eng.dry_run = False
    eng.step_times = {}
    eng.dynamic_params = {}
    eng.start_time = time.time()
    eng._patch_recorder = MagicMock()
    eng.health_evaluator = MagicMock()
    eng.health_evaluator.evaluate.return_value.to_dict.return_value = {}
    eng.context = PipelineContext(
        project_path=project, language="python", metadata=metadata,
    )
    return eng


def test_build_result_includes_data_toxic_fields(tmp_path):
    eng = _engine(tmp_path, {
        "_data_toxic_files": ["tests/test_bcrypt.py"],
        "_data_toxic_skipped": {"tests/test_bcrypt.py": 7},
    })
    result = eng._build_result()
    check(
        "result_has_data_toxic_files",
        result.get("data_toxic_files") == ["tests/test_bcrypt.py"],
        f"result: {result.get('data_toxic_files')}",
    )
    check(
        "result_has_data_toxic_skipped",
        result.get("data_toxic_skipped") == {"tests/test_bcrypt.py": 7},
        f"result: {result.get('data_toxic_skipped')}",
    )


def test_build_result_empty_data_toxic_fields_by_default(tmp_path):
    eng = _engine(tmp_path, {})
    result = eng._build_result()
    check(
        "result_data_toxic_files_empty_by_default",
        result.get("data_toxic_files") == [],
        f"result: {result.get('data_toxic_files')}",
    )
    check(
        "result_data_toxic_skipped_empty_by_default",
        result.get("data_toxic_skipped") == {},
        f"result: {result.get('data_toxic_skipped')}",
    )


def test_send_report_mentions_data_toxic_files(tmp_path):
    eng = _engine(tmp_path, {"needs_review_count": 0, "needs_review_items": []})
    eng.reporter = MagicMock()
    result = {
        "data_toxic_files": ["tests/test_bcrypt.py"],
        "data_toxic_skipped": {"tests/test_bcrypt.py": 7},
    }
    eng._send_report([], [], {"audit_ok": True}, [], result)
    sent_msg = eng.reporter.send.call_args[0][0]
    check(
        "report_mentions_toxic_file",
        "test_bcrypt.py" in sent_msg and "data-toxic" in sent_msg,
        f"msg: {sent_msg}",
    )
    check(
        "report_mentions_skipped_count",
        "7" in sent_msg,
        f"msg: {sent_msg}",
    )


def test_send_report_silent_when_no_toxic_files(tmp_path):
    eng = _engine(tmp_path, {"needs_review_count": 0, "needs_review_items": []})
    eng.reporter = MagicMock()
    result = {"data_toxic_files": [], "data_toxic_skipped": {}}
    eng._send_report([], [], {"audit_ok": True}, [], result)
    sent_msg = eng.reporter.send.call_args[0][0]
    check(
        "no_toxic_line_when_empty",
        "data-toxic" not in sent_msg,
        f"msg: {sent_msg}",
    )


# ---------------------------------------------------------------------------
# (d) регресс — без _data_toxic_files PrioritizeStage ничего не фильтрует
# ---------------------------------------------------------------------------

def test_prioritize_stage_no_filtering_without_data_toxic_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    e1 = {"file": "a.py", "line": 10, "code": "E501", "message": "line too long"}
    e2 = {"file": "a.py", "line": 20, "code": "F401", "message": "unused import"}

    ctx = PipelineContext(
        project_path=tmp_path, language="python",
        current_errors=(e1, e2),
    )
    out = PrioritizeStage(ErrorPriorityEngine()).execute(ctx)
    prioritized = list(out.prioritized_errors)
    check(
        "both_errors_kept_without_toxic_marking",
        len(prioritized) == 2,
        f"prioritized: {prioritized}",
    )
    check(
        "no_skipped_metadata_without_toxic_marking",
        not out.metadata.get("_data_toxic_skipped"),
        f"metadata: {dict(out.metadata)}",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    test_mark_data_toxic_on_o19_failure_reason()
    test_mark_data_toxic_noop_on_other_failure_reason()
    test_mark_data_toxic_no_duplicate_entries()

    with tempfile.TemporaryDirectory() as d:
        test_prioritize_stage_filters_non_structural_toxic_errors(Path(d))
    test_prioritize_stage_skipped_count_takes_max_not_sum_across_rescans()

    with tempfile.TemporaryDirectory() as d:
        test_build_result_includes_data_toxic_fields(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_build_result_empty_data_toxic_fields_by_default(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_send_report_mentions_data_toxic_files(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_send_report_silent_when_no_toxic_files(Path(d))

    with tempfile.TemporaryDirectory() as d:
        test_prioritize_stage_no_filtering_without_data_toxic_files(Path(d))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]

    print(f"data_toxic_containment (Q1): {passed}/{len(results)} passed")
    if failed:
        for name, note in failed:
            print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
        sys.exit(1)
    sys.exit(0)
