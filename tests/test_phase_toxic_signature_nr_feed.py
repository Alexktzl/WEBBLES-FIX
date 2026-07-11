"""
Q3 (2026-07-07): NR-исходы неверифицируемого mypy-класса кормят toxic-счётчик.

Контекст (дыра, найденная после Q3 2026-07-03): `_track_toxic_signature`
(core/stages/decide_stage.py) считал только честные REJECT-причины
(target_error_still_present, error_count_not_decreased). Но неверифицируемый
класс mypy-ошибок (list-item/arg-type на parametrize-таблицах test_bcrypt.py)
у DecideStage/ValidateStage регулярно заканчивается НЕ REJECT, а
NEEDS_REVIEW — с причинами net_delta_uncertain (ValidateStage, демаскированный
net-delta без подтверждения) или target_fixed_reviewer_ok_count_unchanged
(DecideStage._dispatch_after_success, когда guard откатывает предполагаемый
ACCEPT). Эти NR-исходы в счётчик не шли, и LLM продолжала жечься на классе до
конца прогона (bcrypt 20260703-121449z: 11 решений на классе, real_fix_impact=0).

Решающий эксперимент 2026-07-07: single-file mypy (тот же invocation, что
MypyAnalyzer) на пристинном tests/test_bcrypt.py НЕ воспроизводит list-item
вообще — класс неверифицируем target_recheck-ом в принципе, ни один патч
не может быть подтверждён. Порог MAX_TOXIC_SIG_REJECTS также снижен 4 → 3.

Подключены ДВЕ точки вызова: `target_fixed_reviewer_ok_count_unchanged`
внутри `DecideStage._dispatch_after_success` (core/stages/decide_stage.py) и
`net_delta_uncertain` в net-delta ветке ValidateStage
(core/stages/validate_stage.py, перед прямым вызовом
`NeedsReviewStage().execute(...)` — эта ветка минует DecideStage.execute()).
Ради второй точки `_track_toxic_signature` сделан classmethod (использует
только атрибуты класса): ValidateStage зовёт его через класс, без
инстанцирования DecideStage — тест №6 кодирует этот контракт.

Запуск: python tests/test_phase_toxic_signature_nr_feed.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from core.pipeline_context import PipelineContext  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402
from core.state_machine import State  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))


def _mk_stage():
    return DecideStage(quality_evaluator=MagicMock(), analyzer=None)


def _class_key(err):
    return f"{str(err.get('file') or '').replace(chr(92), '/')}::{err.get('code') or ''}"


_MYPY_ERROR = {
    "file": "tests/test_bcrypt.py",
    "line": 359,
    "code": "list-item",
    "message": 'List item 0 has incompatible type "int"; expected "str"',
}

_FLAKE8_ERROR = {
    "file": "tests/test_bcrypt.py",
    "line": 5,
    "code": "E501",
    "message": "line too long (100 > 79 characters)",
}


# ---------------------------------------------------------------------------
# 1. Смешанные REJECT+NR причины одного (file, code) достигают порога
# ---------------------------------------------------------------------------

def test_two_rejects_plus_one_nr_mark_signature_toxic():
    """2 честных REJECT (error_count_not_decreased) + 1 NR (net_delta_uncertain)
    по одному (file, code) → класс помечен toxic (порог MAX_TOXIC_SIG_REJECTS=3).
    """
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    reasons = [
        "error_count_not_decreased",
        "error_count_not_decreased",
        "net_delta_uncertain",
    ]
    for reason in reasons:
        ctx = stage._track_toxic_signature(ctx, _MYPY_ERROR, reason)
    sig = _class_key(_MYPY_ERROR)
    check(
        "mixed_reject_nr_marks_toxic",
        sig in list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


def test_two_nr_only_do_not_mark_toxic_below_threshold():
    """2 NR (net_delta_uncertain) без REJECT — ниже порога 3, класс НЕ toxic."""
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    for _ in range(2):
        ctx = stage._track_toxic_signature(ctx, _MYPY_ERROR, "net_delta_uncertain")
    sig = _class_key(_MYPY_ERROR)
    check(
        "two_nr_below_threshold_not_toxic",
        sig not in list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


# ---------------------------------------------------------------------------
# 2. Не-mypy код с новыми причинами — не считается
# ---------------------------------------------------------------------------

def test_flake8_code_with_nr_reasons_not_counted():
    stage = _mk_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    for reason in ["net_delta_uncertain", "target_fixed_reviewer_ok_count_unchanged", "net_delta_uncertain"]:
        ctx = stage._track_toxic_signature(ctx, _FLAKE8_ERROR, reason)
    check(
        "flake8_with_nr_reasons_never_toxic",
        not list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )
    check(
        "flake8_no_counts_recorded",
        not dict(ctx.metadata.get("_mypy_reject_sig_counts") or {}),
        f"counts: {dict(ctx.metadata.get('_mypy_reject_sig_counts') or {})}",
    )


# ---------------------------------------------------------------------------
# 3. Интеграция с _dispatch_after_success: ACCEPT-путь НЕ инкрементит счётчик
# ---------------------------------------------------------------------------

class _DecideCtx:
    """Минимальный fake-context (образец: test_phase_symbol_regression.py),
    достаточный для _dispatch_after_success без подъёма реального движка."""

    def __init__(self, metadata, error):
        self.metadata = metadata
        self.selected_error = error
        self.history_states = []
        self.accepted = []
        self.rejected = []
        self.processed = []
        self.iterations = 0
        self.working_path = None
        self.project_path = None
        self.validation_results = {}

    def update(self, metadata=None, **_kw):
        if metadata is not None:
            self.metadata = metadata
        return self

    def add_accepted_patch(self, p):
        self.accepted.append(p)
        return self

    def add_rejected_patch(self, p):
        self.rejected.append(p)
        return self

    def record_processed_error(self, sig):
        self.processed.append(sig)
        return self

    def increment_iteration(self):
        self.iterations += 1
        return self

    def add_state_to_history(self, state):
        self.history_states.append(state)
        return self

    def remove_patch_snapshot(self, file_name):
        snaps = list(self.metadata.get("patch_snapshots", []))
        self.metadata = dict(self.metadata)
        self.metadata["patch_snapshots"] = [
            s for s in snaps if s.get("file") != file_name
        ]
        return self

    def set_errors(self, errs):
        return self


def test_accept_path_does_not_feed_toxic_counter():
    """target_fixed_reviewer_ok_count_unchanged без единого guard-триггера →
    обычный ACCEPT (никакой из O.14/O.17/O.18/O.19/Q.1-3 не сработал) —
    _track_toxic_signature из ACCEPT-ветки НЕ вызывается вовсе."""
    ds = _mk_stage()
    error = dict(_MYPY_ERROR)
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "patch_snapshots": [],
        },
        error=error,
    )
    out = ds._dispatch_after_success(
        ctx, error, "sig-accept", reason="target_fixed_reviewer_ok_count_unchanged",
    )
    check("accept_path.last_decision_ACCEPT", out.metadata.get("last_decision") == "ACCEPT")
    check(
        "accept_path.no_toxic_counts",
        not dict(out.metadata.get("_mypy_reject_sig_counts") or {}),
        f"counts: {dict(out.metadata.get('_mypy_reject_sig_counts') or {})}",
    )
    check(
        "accept_path.no_toxic_signatures",
        not list(out.metadata.get("_toxic_signatures") or []),
    )


# ---------------------------------------------------------------------------
# 4. guard-NR (O.19 data mangling) НЕ считается toxic
# ---------------------------------------------------------------------------

def test_guard_nr_o19_data_mangling_not_counted():
    """decision уходит в NEEDS_REVIEW из-за O.19 (порча данных), а не из-за
    неверифицируемости класса — _track_toxic_signature вызываться не должен."""
    ds = _mk_stage()
    error = dict(_MYPY_ERROR)
    target_sig = "sig-guard-o19"
    ctx = _DecideCtx(
        metadata={
            "confidence": 0.95,
            "review": {"verdict": "ok"},
            "data_literal_mangling": {"file": error["file"], "markers": ["bytes_to_str"]},
            "patch_snapshots": [],
            # Второй NR-заход (feedback retry уже был) — чтобы сразу попасть
            # в терминальную ветку State.NEEDS_REVIEW, а не в retry-петлю.
            f"_nr_retry_{target_sig}": 1,
        },
        error=error,
    )
    out = ds._dispatch_after_success(
        ctx, error, target_sig, reason="target_fixed_reviewer_ok_count_unchanged",
    )
    check("guard_o19.last_decision_NEEDS_REVIEW", out.metadata.get("last_decision") == "NEEDS_REVIEW")
    check(
        "guard_o19.history_ends_NEEDS_REVIEW",
        out.history_states and out.history_states[-1] == State.NEEDS_REVIEW,
    )
    check(
        "guard_o19.no_toxic_counts",
        not dict(out.metadata.get("_mypy_reject_sig_counts") or {}),
        f"counts: {dict(out.metadata.get('_mypy_reject_sig_counts') or {})}",
    )
    check(
        "guard_o19.no_toxic_signatures",
        not list(out.metadata.get("_toxic_signatures") or []),
    )


# ---------------------------------------------------------------------------
# 5. Живой путь: NEEDS_REVIEW БЕЗ content-guard (снапшот не создан, C4/H3) —
#    ЭТО считается (неверифицируемость, а не порча контентом)
# ---------------------------------------------------------------------------

def test_target_fixed_nr_via_snapshot_failed_is_counted():
    """C4/H3 (снапшот не создан) — НЕ content-guard из списка исключений
    (O.14/O.17/O.18/O.19/Q.1-3), поэтому такой NR корректно кормит toxic-
    счётчик: символьные проверки не выполнялись, значит патч не подтверждён,
    и это ровно тот сигнал "неверифицируемо", который должен считаться."""
    ds = _mk_stage()
    error = dict(_MYPY_ERROR)
    meta = {
        "confidence": 0.95,
        "review": {"verdict": "ok"},
        "snapshot_failed": {"file": error["file"], "reason": "no_original_content"},
        "patch_snapshots": [],
    }
    ctx = None
    for i in range(3):
        target_sig = f"sig-snapfail-{i}"
        # Переносим накопленный счётчик (_mypy_reject_sig_counts) между
        # попытками — как это происходило бы через реальный context.metadata
        # на последовательных итерациях пайплайна; сбрасываем per-attempt
        # retry-ключ, чтобы каждый раз сразу попадать в терминальную ветку.
        meta = dict(meta)
        meta[f"_nr_retry_{target_sig}"] = 1
        ctx = _DecideCtx(metadata=meta, error=error)
        ctx = ds._dispatch_after_success(
            ctx, error, target_sig, reason="target_fixed_reviewer_ok_count_unchanged",
        )
        meta = dict(ctx.metadata)
    sig = _class_key(error)
    check(
        "snapshot_failed_reaches_needs_review",
        ctx.metadata.get("last_decision") == "NEEDS_REVIEW",
    )
    check(
        "snapshot_failed_counted_after_3",
        sig in list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


# ---------------------------------------------------------------------------
# 6. Контракт для ValidateStage: вызов классметодом БЕЗ инстанса DecideStage
# ---------------------------------------------------------------------------

def test_classmethod_call_without_instance_net_delta_uncertain():
    """net-delta ветка ValidateStage зовёт `DecideStage._track_toxic_signature`
    через КЛАСС (DecideStage требует quality_evaluator в __init__ — там его
    нет и не должно быть). Кодируем контракт: вызов без инстанса работает и
    3× net_delta_uncertain по одному (file, code) помечают класс toxic."""
    ctx = PipelineContext(project_path=Path("."), language="python")
    for _ in range(3):
        ctx = DecideStage._track_toxic_signature(ctx, _MYPY_ERROR, "net_delta_uncertain")
    sig = _class_key(_MYPY_ERROR)
    check(
        "classmethod_no_instance_marks_toxic",
        sig in list(ctx.metadata.get("_toxic_signatures") or []),
        f"toxic_signatures: {list(ctx.metadata.get('_toxic_signatures') or [])}",
    )


if __name__ == "__main__":
    test_two_rejects_plus_one_nr_mark_signature_toxic()
    test_two_nr_only_do_not_mark_toxic_below_threshold()
    test_flake8_code_with_nr_reasons_not_counted()
    test_accept_path_does_not_feed_toxic_counter()
    test_guard_nr_o19_data_mangling_not_counted()
    test_target_fixed_nr_via_snapshot_failed_is_counted()
    test_classmethod_call_without_instance_net_delta_uncertain()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"toxic_signature_nr_feed (Q3): {passed}/{len(results)} passed")
    if failed:
        for name, note in failed:
            print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
        sys.exit(1)
    sys.exit(0)
