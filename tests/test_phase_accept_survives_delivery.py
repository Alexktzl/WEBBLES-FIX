"""
Regress (2026-07-02, deep-reasoner, pyca/bcrypt): принятый (ACCEPT) патч не
имеет права молча исчезнуть из доставки. Если финальный аудит ПОЛНОСТЬЮ
откатывает файл (failed_segments c end==0), его ACCEPT-патчи обязаны быть
помечены audit_reverted и исключены из «живого» ACCEPT-учёта / real_fix_impact —
иначе прогон отчитывается «принято N», хотя файл байт-в-байт равен оригиналу
(verify_accepts: verdict=UNCHANGED_FILE).

Инцидент: prod-прогон bcrypt 18:01–18:30. 5 ACCEPT на tests/test_bcrypt.py
заменили байт-строки на строки и расплющили parametrize-кейс; before_sigs для
Python — flake8-only, поэтому новый косметический E131 увеличил число уникальных
flake8-сигнатур файла 1→2 → audit_run: count_grew → полный откат. Патчи остались
«принятыми» → ACCEPT + UNCHANGED_FILE.

Инвариант: каждый патч из accepted_patches либо доживает до доставленного файла,
либо ЯВНО помечен (oscillation_cancelled / audit_reverted). «Тихо отсутствует» —
запрещено.

Запуск: python tests/test_phase_accept_survives_delivery.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_engine import PipelineEngine, compute_progress_metrics  # noqa: E402


class _Ctx:
    """Минимальный CoW-контекст: .metadata + .update(**kw) -> self (для теста)."""

    def __init__(self):
        self.metadata = {}

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        return self


def _bare_engine() -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.context = _Ctx()
    return eng


# --- 1. Полный откат файла аудитом → все его ACCEPT помечены audit_reverted ---
def test_full_rollback_marks_accepts_reverted():
    eng = _bare_engine()
    accepted = [
        {"file": "tests\\test_bcrypt.py", "line": 359, "code": "list-item"},
        {"file": "tests\\test_bcrypt.py", "line": 368, "code": "list-item"},
        {"file": "noxfile.py", "line": 1, "code": "import-not-found"},
    ]
    audit_result = {
        "passed_files": ["noxfile.py"],
        # end==0 → полный откат tests/test_bcrypt.py
        "failed_segments": {"tests\\test_bcrypt.py": [(1, 0, "")]},
    }
    eng._mark_audit_reverted_accepts(accepted, audit_result)

    reverted = [p for p in accepted if p.get("audit_reverted")]
    assert len(reverted) == 2, f"оба патча test_bcrypt.py должны быть reverted: {accepted}"
    assert all(p["file"].endswith("test_bcrypt.py") for p in reverted)
    # noxfile.py доставлен (passed) — не reverted
    assert not accepted[2].get("audit_reverted"), "доставленный файл не помечается"
    assert eng.context.metadata.get("accept_reverted_count") == 2
    items = eng.context.metadata.get("accept_reverted_items", [])
    assert {i["line"] for i in items} == {359, 368}, items


# --- 2. Частичный откат (end != 0) НЕ помечает ACCEPT reverted ---
def test_partial_rollback_does_not_mark_reverted():
    eng = _bare_engine()
    accepted = [{"file": "a.py", "line": 10, "code": "E1"}]
    audit_result = {"passed_files": [], "failed_segments": {"a.py": [(2, 3, "seg")]}}
    eng._mark_audit_reverted_accepts(accepted, audit_result)
    assert not accepted[0].get("audit_reverted"), (
        "частичный откат может сохранить выжившие сегменты — огульно помечать нельзя"
    )
    assert eng.context.metadata.get("accept_reverted_count", 0) == 0


# --- 3. Чистый аудит (нет failed_segments) → ничего не помечается ---
def test_clean_audit_no_marks():
    eng = _bare_engine()
    accepted = [{"file": "a.py", "line": 10, "code": "E1"}]
    eng._mark_audit_reverted_accepts(accepted, {"passed_files": ["a.py"], "failed_segments": {}})
    assert not accepted[0].get("audit_reverted")


# --- 4. Регистронезависимое/слэш-сравнение путей (Windows accepted vs failed) ---
def test_path_key_normalization():
    eng = _bare_engine()
    accepted = [{"file": "Tests/Test_BCrypt.py", "line": 1, "code": "list-item"}]
    audit_result = {"failed_segments": {"tests\\test_bcrypt.py": [(1, 0, "")]}}
    eng._mark_audit_reverted_accepts(accepted, audit_result)
    # На Windows/macOS (normcase лоуэркейсит) путь совпадает и помечается;
    # на POSIX регистр значим — тогда не помечается. Проверяем через сам ключ.
    same = PipelineEngine._norm_deliver_key("Tests/Test_BCrypt.py") == \
        PipelineEngine._norm_deliver_key("tests\\test_bcrypt.py")
    assert bool(accepted[0].get("audit_reverted")) == same


# --- 5. compute_progress_metrics исключает audit_reverted из real_fix_impact ---
def test_reverted_excluded_from_real_fix_impact():
    # scan_before/after — тот же файл-ключ. Патч помечен audit_reverted →
    # его файл НЕ должен попасть в touched_files, real_fix_impact = 0.
    scan_before = {"total": 3, "errors": [
        {"file": "tests/test_bcrypt.py"}, {"file": "tests/test_bcrypt.py"},
        {"file": "tests/test_bcrypt.py"},
    ]}
    scan_after = {"total": 3, "errors": [
        {"file": "tests/test_bcrypt.py"}, {"file": "tests/test_bcrypt.py"},
        {"file": "tests/test_bcrypt.py"},
    ]}
    accepted = [{"file": "tests/test_bcrypt.py", "line": 359, "code": "list-item",
                 "audit_reverted": True}]
    out = compute_progress_metrics(
        scan_before=scan_before, scan_after=scan_after,
        accepted_patches=accepted, rejected_patches=[],
        needs_review_meta={"accept_reverted_count": 1,
                           "accept_reverted_items": [{"file": "tests/test_bcrypt.py",
                                                      "line": 359, "code": "list-item"}]},
        initial_error_count=3, baseline_remaining=3, total_current_errors=3,
    )
    # touched_files пуст (единственный ACCEPT reverted) → real_fix_impact = 0
    assert out["real_fix_impact"] == 0, out
    assert out["accept_reverted_count"] == 1, out
    assert len(out["accept_reverted_items"]) == 1, out


# --- 6. Живой ACCEPT (не reverted) продолжает считаться в real_fix_impact ---
def test_live_accept_still_counts():
    scan_before = {"total": 2, "errors": [
        {"file": "good.py"}, {"file": "good.py"},
    ]}
    scan_after = {"total": 0, "errors": []}
    accepted = [{"file": "good.py", "line": 1, "code": "E1"}]
    out = compute_progress_metrics(
        scan_before=scan_before, scan_after=scan_after,
        accepted_patches=accepted, rejected_patches=[],
        needs_review_meta={}, initial_error_count=2, baseline_remaining=0,
        total_current_errors=0,
    )
    assert out["real_fix_impact"] == 2, out
    assert out["accept_reverted_count"] == 0, out


# --- 7. Инвариант доставки: reverted-файл НЕ уезжает в оригинал ---
def test_reverted_file_not_delivered_end_to_end():
    """Полная связка: полный откат + пометка reverted + доставка. Оригинал
    reverted-файла не меняется, а помеченный патч исключён из живого учёта."""
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        proj.mkdir(parents=True)
        sandbox.mkdir(parents=True)
        (proj / "t.py").write_text("ORIGINAL\n", encoding="utf-8")
        (sandbox / "t.py").write_text("ORIGINAL\n", encoding="utf-8")  # restore уже вернул

        eng = _bare_engine()
        eng.project_path = proj.resolve()
        # _copy читает self.context.accepted_patches (nested-shape)
        eng.context.accepted_patches = [{"error": {"file": "t.py", "code": "list-item"}}]

        accepted = [{"file": "t.py", "line": 5, "code": "list-item"}]
        audit_result = {"passed_files": [], "failed_segments": {"t.py": [(1, 0, "")]}}

        eng._mark_audit_reverted_accepts(accepted, audit_result)
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={"t.py": True})

        assert accepted[0].get("audit_reverted") is True
        assert (proj / "t.py").read_text(encoding="utf-8") == "ORIGINAL\n", (
            "полностью откачённый файл не должен доставляться"
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
    print(f"\naccept survives delivery: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
