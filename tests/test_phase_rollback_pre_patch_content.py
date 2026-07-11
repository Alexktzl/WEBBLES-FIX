"""
Regress C4/C5/C6 (PROJECT_AUDIT_REPORT 2026-07-01): единый откат по
`metadata["_pre_patch_content"]`.

До фикса:
  * C4 — при отсутствии снапшота откат ТИХО пропускался (REJECT записывался
    с rollback=True, файл оставался патченным); fallback
    `original_content = patched_content` в _save_patch_snapshot превращал
    откат в запись патченного содержимого обратно;
  * C5 — многофайловый патч откатывался только для error["file"], побочные
    файлы оставались с непринятыми правками;
  * C6 — _pre_patch_content не очищался после разрешения патча: net-delta
    откат СЛЕДУЮЩЕЙ ошибки мог записать stale-карту и стереть принятую работу.

Запуск: python tests/test_phase_rollback_pre_patch_content.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402
from core.stages.validate_stage import ValidateStage  # noqa: E402


def _ctx(work: Path, metadata: dict, error: dict) -> PipelineContext:
    return PipelineContext(
        project_path=work, language="python", config={"pipeline": {}},
        metadata=metadata, selected_error=error, working_path=work,
    )


def _decide() -> DecideStage:
    return DecideStage(quality_evaluator=None, analyzer=None)


# --- 1. C5: многофайловый патч — откатываются ВСЕ файлы из карты ---
def test_multifile_rollback_restores_all_files():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("PATCHED_A", encoding="utf-8")
        (work / "b.py").write_text("PATCHED_B", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E999", "message": "x"}
        meta = {"_pre_patch_content": {"a.py": "ORIG_A", "b.py": "ORIG_B"},
                "patch_snapshots": []}
        ctx = _ctx(work, meta, error)

        ctx = _decide()._rollback_file_from_snapshot(ctx, error)

        assert (work / "a.py").read_text(encoding="utf-8") == "ORIG_A"
        assert (work / "b.py").read_text(encoding="utf-8") == "ORIG_B", (
            "C5: побочный файл многофайлового патча обязан откатиться"
        )
        # C6: карта потреблена
        assert "_pre_patch_content" not in ctx.metadata


# --- 1b. Конверсия-2 (2026-07-02): rollback сбрасывает stale baseline-счётчики ---
def test_rollback_resets_validation_baselines():
    """`_primary_error_count_before` ставится в Validate ПОСЛЕ применения патча;
    после отката диск возвращается к состоянию с бОльшим числом ошибок, и на
    retry той же ошибки before оказывался занижен ровно на число исправленного
    прошлой попыткой — честный фикс получал REJECT error_count_not_decreased
    (learning_cases 06-25..07-02: E704/tenacity/pluggy, 51→51 на attempt>=2)."""
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("PATCHED_A", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E704", "message": "x"}
        meta = {"_pre_patch_content": {"a.py": "ORIG_A"},
                "_primary_error_count_before": 51,
                "_ruff_count_before": 3,
                "patch_snapshots": []}
        ctx = _ctx(work, meta, error)

        ctx = _decide()._rollback_file_from_snapshot(ctx, error)

        assert "_primary_error_count_before" not in ctx.metadata, (
            "stale baseline после отката должен сбрасываться"
        )
        assert "_ruff_count_before" not in ctx.metadata


# --- 2. C4: нет ни карты, ни снапшота → файл в _rollback_failed_files ---
def test_no_sources_marks_rollback_failed():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("PATCHED_A", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E501", "message": "x"}
        ctx = _ctx(work, {"patch_snapshots": []}, error)

        ctx = _decide()._rollback_file_from_snapshot(ctx, error)

        assert "a.py" in (ctx.metadata.get("_rollback_failed_files") or []), (
            "C4: невозможный откат обязан быть зафиксирован, а не пропущен молча"
        )


# --- 3. Снапшот как вторичный источник (карты нет) по-прежнему работает ---
def test_snapshot_fallback_still_works():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("PATCHED_A", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E501", "message": "x"}
        meta = {"patch_snapshots": [
            {"file": "a.py", "original_content": "ORIG_A", "patched_content": "PATCHED_A"},
        ]}
        ctx = _ctx(work, meta, error)

        ctx = _decide()._rollback_file_from_snapshot(ctx, error)
        assert (work / "a.py").read_text(encoding="utf-8") == "ORIG_A"


# --- 4. Пустой оригинал (0 байт) — валидный откат, а не skip ---
def test_empty_original_is_restored():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("PATCHED_A", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E501", "message": "x"}
        meta = {"_pre_patch_content": {"a.py": ""}, "patch_snapshots": []}
        ctx = _ctx(work, meta, error)

        ctx = _decide()._rollback_file_from_snapshot(ctx, error)
        assert (work / "a.py").read_text(encoding="utf-8") == "", (
            "исходно пустой файл обязан откатиться к пустому состоянию"
        )


# --- 5. C4: _save_patch_snapshot без pre-patch источника → snapshot_failed,
#     снапшот с original==patched НЕ создаётся ---
def test_snapshot_without_source_sets_snapshot_failed():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("PATCHED_A", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E501", "message": "x"}
        ctx = _ctx(work, {"patch_snapshots": []}, error)

        vs = ValidateStage(None, None, None, None, None)
        ctx = vs._save_patch_snapshot(ctx)

        sf = ctx.metadata.get("snapshot_failed")
        assert isinstance(sf, dict) and sf.get("file") == "a.py", (
            f"ждали snapshot_failed, получили metadata={dict(ctx.metadata)}"
        )
        snaps = ctx.metadata.get("patch_snapshots") or []
        assert not any(
            s.get("original_content") == s.get("patched_content") for s in snaps
        ), "лживый снапшот (original==patched) не должен создаваться"


# --- 6. H3/C4: DecideStage не даёт ACCEPT при snapshot_failed ---
def test_dispatch_blocks_accept_on_snapshot_failed():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("x = 1\n", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E501", "message": "x"}
        meta = {
            "snapshot_failed": {"file": "a.py", "reason": "no_pre_patch_content"},
            "confidence": 0.99,
            "review": {"verdict": "ok"},
            "patch_snapshots": [],
            # NR feedback retry уже израсходован — чтобы уйти в NEEDS_REVIEW,
            # а не в повторную генерацию.
            "_nr_retry_a.py::E501::x": 1,
        }
        ctx = _ctx(work, meta, error)
        stage = _decide()
        target_sig = stage._error_signature(error)

        out = stage._dispatch_after_success(ctx, error, target_sig, reason="test")
        assert out.metadata.get("last_decision") != "ACCEPT", (
            "патч без снапшота (snapshot_failed) не может быть принят"
        )


# --- 7. C6: ACCEPT очищает _pre_patch_content ---
def test_accept_clears_pre_patch_map():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "a.py").write_text("x = 1\n", encoding="utf-8")
        error = {"file": "a.py", "line": 1, "code": "E501", "message": "x"}
        meta = {
            "_pre_patch_content": {"a.py": "OLD"},
            "confidence": 0.99,
            "review": {"verdict": "ok"},
            "patch_snapshots": [],
        }
        ctx = _ctx(work, meta, error)
        stage = _decide()
        target_sig = stage._error_signature(error)

        out = stage._dispatch_after_success(ctx, error, target_sig, reason="test")
        assert out.metadata.get("last_decision") == "ACCEPT", dict(out.metadata)
        assert "_pre_patch_content" not in out.metadata, (
            "C6: карта принятого патча обязана быть очищена — иначе net-delta "
            "откат следующей ошибки может стереть принятую работу"
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
    print(f"\nRollback pre_patch_content: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
