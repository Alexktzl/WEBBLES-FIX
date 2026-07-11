"""
Q2 (2026-07-02, замер №3 pyca/bcrypt): skip NR-feedback-retry, когда
ЕДИНСТВЕННАЯ причина NEEDS_REVIEW — O.19 data-literal mangling.

Данные deep-reasoner по логу замера: 100% O.19-ретраев воспроизвели ту же
порчу (0 честных фиксов) — фидбек «чини аннотацию, не данные» логически
неисполним на malformed-parametrize блоке; FileAntiLoop добивал файл после
3-4 одинаковых причин, retry-петли съели project_timeout при 0
LLM-таймаутов. Retry при O.19+другой триггер СОХРАНЯЕТСЯ (другая причина
может быть исправима).

Запуск: python tests/test_phase_toxic_file_containment.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402
from core.state_machine import State  # noqa: E402

_ERR = {"file": "tests/test_bcrypt.py", "line": 359, "code": "list-item",
        "message": 'List item 0 has incompatible type "int"; expected "str"'}


def _ctx(work: Path, metadata: dict) -> PipelineContext:
    return PipelineContext(
        project_path=work, language="python", working_path=work,
        selected_error=dict(_ERR),
        validation_results=MappingProxyType({
            "error_count_before": 60, "error_count_after": 60,
            "current_errors_before": [],
        }),
        metadata=metadata,
    )


def _dispatch(ctx: PipelineContext) -> PipelineContext:
    stage = DecideStage(quality_evaluator=None, analyzer=None)
    return stage._dispatch_after_success(
        ctx, dict(_ERR), stage._error_signature(_ERR), reason="test",
    )


# --- 1. O.19 — единственная причина → сразу NEEDS_REVIEW, БЕЗ retry ---
def test_o19_sole_cause_skips_retry():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "tests").mkdir()
        (work / _ERR["file"]).write_text("x = 1\n", encoding="utf-8")
        ctx = _ctx(work, {
            "data_literal_mangling": {"file": _ERR["file"],
                                      "markers": ["numeric_literal_demoted"]},
            "confidence": 1.0,
            "review": {"verdict": "ok"},
            "patch_snapshots": [],
            "_pre_patch_content": {_ERR["file"]: "x = 1\n"},
        })
        out = _dispatch(ctx)
        assert out.current_state == State.NEEDS_REVIEW, (
            f"O.19-sole-cause обязан идти сразу в NEEDS_REVIEW, "
            f"получили {out.current_state} (retry сжёг бы LLM-вызов впустую)"
        )


# --- 2. O.19 + reviewer=noisy → retry СОХРАНЯЕТСЯ (другая причина исправима) ---
def test_o19_with_other_trigger_keeps_retry():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "tests").mkdir()
        (work / _ERR["file"]).write_text("x = 1\n", encoding="utf-8")
        ctx = _ctx(work, {
            "data_literal_mangling": {"file": _ERR["file"],
                                      "markers": ["numeric_literal_demoted"]},
            "confidence": 1.0,
            "review": {"verdict": "noisy", "reasons": ["patch looks noisy"]},
            "patch_snapshots": [],
            "_pre_patch_content": {_ERR["file"]: "x = 1\n"},
        })
        out = _dispatch(ctx)
        assert out.current_state == State.GENERATING_PATCH, (
            f"при O.19 вместе с другим триггером первый retry должен "
            f"сохраниться: {out.current_state}"
        )


# --- 3. NR по другому guard-у (O.18, без O.19) → retry как раньше ---
def test_plain_nr_retry_unchanged():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "tests").mkdir()
        (work / _ERR["file"]).write_text("x = 1\n", encoding="utf-8")
        ctx = _ctx(work, {
            "type_erosion": {"file": _ERR["file"], "markers": ["any_type"]},
            "confidence": 1.0,
            "review": {"verdict": "ok"},
            "patch_snapshots": [],
            "_pre_patch_content": {_ERR["file"]: "x = 1\n"},
        })
        out = _dispatch(ctx)
        assert out.current_state == State.GENERATING_PATCH, (
            f"NR-retry по не-O.19 причине (O.18) не должен регрессировать: {out.current_state}"
        )


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
    print(f"\nToxic file containment (Q2): {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
