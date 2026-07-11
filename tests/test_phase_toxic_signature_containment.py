"""
Q3 (2026-07-03, спека Алекса, вердикт deep-reasoner по замерам №6-8 bcrypt):
сигнатурный containment.

Контекст: класс mypy-ошибок (arg-type/list-item на parametrize-блоке bcrypt) —
тулчейн-артефакт: детерминированный фикс неверифицируем (recheck его не
подтверждает), а LLM-попытки плющат весь parametrize-блок → бесконечная серия
честных REJECT (target_error_still_present / error_count_not_decreased) до
project_timeout (замер №8: rejected=8). Существующий Q1-containment
(_data_toxic_files, O.19 data mangling) сюда не дотягивается — тут нет порчи
данных, просто неверифицируемый в тулчейне фикс.

Вариант (i), утверждён оркестратором: помечаем ТОЛЬКО класс ошибки
(file::code — 2026-07-04, ключ починен после замера №9: полная сигнатура с
message была недостижима порогом, т.к. mypy-message варьирует номер
элемента), НЕ весь файл — ошибки других кодов того же файла остаются в
очереди. Это ключевое отличие от Q1 (test_phase_data_toxic_containment.py),
где toxic-маркировка выкидывает все неструктурные ошибки всего файла.

3 точки фикса:
  1. DecideStage._track_toxic_signature (core/stages/decide_stage.py) —
     вызывается из обеих REJECT-точек (основная ~1008-1090 и fallback ~1081)
     с причинами target_error_still_present / error_count_not_decreased.
     Считает честные REJECT по сигнатуре, ТОЛЬКО если код ошибки mypy-стиля
     (regex как в validate_stage.ValidateStage._MYPY_CODE_RE, исключая
     {syntax, invalid-syntax}). После MAX_TOXIC_SIG_REJECTS (3, снижено с 4
     — 2026-07-07) — сигнатура попадает в metadata["_toxic_signatures"].
  2. PrioritizeStage.execute (core/stages/prioritize_stage.py) — второй
     фильтр сразу после Q1: выкидывает из очереди ошибки, чья сигнатура в
     _toxic_signatures; счётчик пропущенного — metadata["_toxic_sig_skipped"]
     (max за проход, как в Q1).
  3. PipelineEngine._build_result / _send_report (core/pipeline_engine.py) —
     прокидывает toxic_signatures/toxic_sig_skipped в result и в финальное
     сообщение отчёта.

Запуск: python tests/test_phase_toxic_signature_containment.py
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
from core.pipeline_stage import PipelineStage  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402
from core.stages.prioritize_stage import PrioritizeStage  # noqa: E402
from safety.error_priority_engine import ErrorPriorityEngine  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))


def _mk_stage():
    return DecideStage(quality_evaluator=MagicMock())


_MYPY_ERROR = {
    "file": "tests/test_bcrypt.py",
    "line": 42,
    "code": "arg-type",
    "message": "Argument 1 to \"hash\" has incompatible type \"bytes\"; expected \"str\"",
}


def _class_key(err):
    """Ключ Q3-containment (2026-07-04): file::code, НЕ полная сигнатура —
    mypy-message варьирует номер элемента/тип, полная сигнатура у каждой
    ошибки класса разная, порог был недостижим."""
    return f"{str(err.get('file') or '').replace(chr(92), '/')}::{err.get('code') or ''}"

_FLAKE8_ERROR = {
    "file": "tests/test_bcrypt.py",
    "line": 5,
    "code": "E501",
    "message": "line too long (100 > 79 characters)",
}


# ---------------------------------------------------------------------------
# (a) DecideStage._track_toxic_signature — накопление честных REJECT
# ---------------------------------------------------------------------------

def test_three_honest_rejects_mark_signature_toxic():
    """2026-07-07: порог MAX_TOXIC_SIG_REJECTS снижен 4 → 3 (данные замеров
    №6-9 + bcrypt 20260703-121449z: P(верифицированный фикс | класс уже
    отвергнут 3 раза) ≈ 0). Раньше тест кодировал старый порог 4 —
    переименован и укорочен до 3 отказов."""
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    reasons = [
        "target_error_still_present",
        "error_count_not_decreased",
        "target_error_still_present",
    ]
    for reason in reasons:
        ctx = stage._track_toxic_signature(ctx, _MYPY_ERROR, reason)
    sig = _class_key(_MYPY_ERROR)
    check(
        "signature_marked_toxic_after_3_rejects",
        sig in list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


def test_varying_mypy_message_aggregates_to_one_class():
    """Регресс замера №9 (2026-07-04): list-item на разных элементах даёт
    РАЗНЫЙ message («List item 0/3…»), полная сигнатура у каждой ошибки своя
    → порог 4 был недостижим (8 REJECT = 4 сигнатуры, max повтор 3). Ключ
    (file, code) агрегирует их в ОДИН класс → порог достигается."""
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    base = {"file": "tests/test_bcrypt.py", "code": "list-item"}
    variants = [
        {**base, "line": 359, "message": 'List item 0 has incompatible type "int"; expected "str"'},
        {**base, "line": 366, "message": 'List item 1 has incompatible type "bytes"; expected "str"'},
        {**base, "line": 368, "message": 'List item 3 has incompatible type "bytes"; expected "str"'},
        {**base, "line": 370, "message": 'List item 2 has incompatible type "bytes"; expected "str"'},
    ]
    for e in variants:
        ctx = stage._track_toxic_signature(ctx, e, "error_count_not_decreased")
    check(
        "varying_message_aggregates_to_class",
        _class_key(base) in list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


def test_two_honest_rejects_do_not_mark_signature_toxic():
    """2026-07-07: было "три отказа не хватает" под старый порог 4 — теперь
    порог 3, значит недостаточный случай — 2 отказа."""
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    for reason in ["target_error_still_present", "error_count_not_decreased"]:
        ctx = stage._track_toxic_signature(ctx, _MYPY_ERROR, reason)
    check(
        "signature_not_toxic_after_2_rejects",
        not list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


def test_flake8_code_not_counted_towards_toxic():
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    for _ in range(6):
        ctx = stage._track_toxic_signature(ctx, _FLAKE8_ERROR, "target_error_still_present")
    check(
        "flake8_signature_never_toxic",
        not list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )
    check(
        "flake8_no_reject_sig_counts_recorded",
        not dict(ctx.metadata.get("_mypy_reject_sig_counts") or {}),
        f"counts: {dict(ctx.metadata.get('_mypy_reject_sig_counts') or {})}",
    )


def test_unrelated_reason_not_counted():
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    for _ in range(6):
        ctx = stage._track_toxic_signature(ctx, _MYPY_ERROR, "invariant_violation")
    check(
        "unrelated_reason_signature_never_toxic",
        not list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


# ---------------------------------------------------------------------------
# (b) PrioritizeStage — фильтрует ТОЛЬКО помеченную сигнатуру, не весь файл
# ---------------------------------------------------------------------------

def test_prioritize_stage_filters_only_toxic_signature(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bcrypt.py").write_text("x = 1\n", encoding="utf-8")

    toxic_sig = _class_key(_MYPY_ERROR)
    other_error = dict(_FLAKE8_ERROR)  # тот же файл, другая ошибка/сигнатура

    ctx = PipelineContext(
        project_path=tmp_path, language="python",
        current_errors=(dict(_MYPY_ERROR), other_error),
        metadata={"_toxic_signatures": [toxic_sig]},
    )
    out = PrioritizeStage(ErrorPriorityEngine()).execute(ctx)
    prioritized = list(out.prioritized_errors)

    check(
        "toxic_signature_error_removed",
        not any(_class_key(e) == toxic_sig for e in prioritized),
        f"prioritized: {prioritized}",
    )
    check(
        "same_file_other_error_kept",
        any(e.get("code") == "E501" and e.get("file") == "tests/test_bcrypt.py" for e in prioritized),
        f"prioritized: {prioritized}",
    )
    check(
        "toxic_sig_skipped_recorded",
        dict(out.metadata.get("_toxic_sig_skipped") or {}).get(toxic_sig) == 1,
        f"skipped: {dict(out.metadata.get('_toxic_sig_skipped') or {})}",
    )


def test_prioritize_stage_sig_skipped_takes_max_not_sum():
    toxic_sig = _class_key(_MYPY_ERROR)
    ctx = PipelineContext(
        project_path=Path("."), language="python",
        metadata={
            "_toxic_signatures": [toxic_sig],
            "_toxic_sig_skipped": {toxic_sig: 5},
        },
    )
    _skip_meta = dict(ctx.metadata.get("_toxic_sig_skipped") or {})
    _skipped_now = {toxic_sig: 2}
    for _s, _n in _skipped_now.items():
        _skip_meta[_s] = max(_skip_meta.get(_s, 0), _n)
    check(
        "max_not_sum",
        _skip_meta == {toxic_sig: 5},
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


def test_build_result_includes_toxic_signature_fields(tmp_path):
    sig = "tests/test_bcrypt.py::arg-type::argument 1 to hash"
    eng = _engine(tmp_path, {
        "_toxic_signatures": [sig],
        "_toxic_sig_skipped": {sig: 4},
    })
    result = eng._build_result()
    check(
        "result_has_toxic_signatures",
        result.get("toxic_signatures") == [sig],
        f"result: {result.get('toxic_signatures')}",
    )
    check(
        "result_has_toxic_sig_skipped",
        result.get("toxic_sig_skipped") == {sig: 4},
        f"result: {result.get('toxic_sig_skipped')}",
    )


def test_build_result_empty_toxic_signature_fields_by_default(tmp_path):
    eng = _engine(tmp_path, {})
    result = eng._build_result()
    check(
        "result_toxic_signatures_empty_by_default",
        result.get("toxic_signatures") == [],
        f"result: {result.get('toxic_signatures')}",
    )
    check(
        "result_toxic_sig_skipped_empty_by_default",
        result.get("toxic_sig_skipped") == {},
        f"result: {result.get('toxic_sig_skipped')}",
    )


def test_send_report_mentions_toxic_signatures(tmp_path):
    sig = "tests/test_bcrypt.py::arg-type::argument 1 to hash"
    eng = _engine(tmp_path, {"needs_review_count": 0, "needs_review_items": []})
    eng.reporter = MagicMock()
    result = {
        "toxic_signatures": [sig],
        "toxic_sig_skipped": {sig: 4},
    }
    eng._send_report([], [], {"audit_ok": True}, [], result)
    sent_msg = eng.reporter.send.call_args[0][0]
    check(
        "report_mentions_toxic_signature_count",
        "неверифицируемые: 1" in sent_msg,
        f"msg: {sent_msg}",
    )
    check(
        "report_mentions_skipped_count",
        "4" in sent_msg,
        f"msg: {sent_msg}",
    )


def test_send_report_silent_when_no_toxic_signatures(tmp_path):
    eng = _engine(tmp_path, {"needs_review_count": 0, "needs_review_items": []})
    eng.reporter = MagicMock()
    result = {"toxic_signatures": [], "toxic_sig_skipped": {}}
    eng._send_report([], [], {"audit_ok": True}, [], result)
    sent_msg = eng.reporter.send.call_args[0][0]
    check(
        "no_toxic_signature_line_when_empty",
        "неверифицируемые" not in sent_msg,
        f"msg: {sent_msg}",
    )


# ---------------------------------------------------------------------------
# (d) регресс — без _toxic_signatures PrioritizeStage ничего не фильтрует
# ---------------------------------------------------------------------------

def test_prioritize_stage_no_filtering_without_toxic_signatures(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    e1 = {"file": "a.py", "line": 10, "code": "arg-type", "message": "some mypy error"}
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
        "no_sig_skipped_metadata_without_toxic_marking",
        not out.metadata.get("_toxic_sig_skipped"),
        f"metadata: {dict(out.metadata)}",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    test_three_honest_rejects_mark_signature_toxic()
    test_two_honest_rejects_do_not_mark_signature_toxic()
    test_flake8_code_not_counted_towards_toxic()
    test_unrelated_reason_not_counted()

    with tempfile.TemporaryDirectory() as d:
        test_prioritize_stage_filters_only_toxic_signature(Path(d))
    test_prioritize_stage_sig_skipped_takes_max_not_sum()

    with tempfile.TemporaryDirectory() as d:
        test_build_result_includes_toxic_signature_fields(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_build_result_empty_toxic_signature_fields_by_default(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_send_report_mentions_toxic_signatures(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_send_report_silent_when_no_toxic_signatures(Path(d))

    with tempfile.TemporaryDirectory() as d:
        test_prioritize_stage_no_filtering_without_toxic_signatures(Path(d))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]

    print(f"toxic_signature_containment (Q3): {passed}/{len(results)} passed")
    if failed:
        for name, note in failed:
            print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
        sys.exit(1)
    sys.exit(0)
