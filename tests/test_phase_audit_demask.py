"""
Regress: финал-аудит правильно различает «демаскирование» и regression.

Контекст: после фикса импорта (`use guess::Guess` → `use crate::guess::Guess`)
компилятор разблокирует type-check и обнажает E0609 (`no field 'value'`) —
ошибки, которые раньше были замаскированы. Это НЕ регрессия. Старый аудит
триггерил откат на любую new_sig; новая логика откатывает только если общее
число ошибок выросло ИЛИ есть CRITICAL_SYNTAX.

Зеркалирует правку в `decide_stage._classify_decision`.

Запуск: python3 tests/test_phase_audit_demask.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_audit():
    """Импортируем AuditManager напрямую, минуя core.engine.__init__."""
    spec = importlib.util.spec_from_file_location(
        "audit_for_test", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AuditManager


def _mk_context(file_sigs_before):
    """Минимальный мок-контекст: metadata.file_error_signatures_before + working_path."""
    ctx = MagicMock()
    ctx.metadata = {"file_error_signatures_before": file_sigs_before,
                    "patch_snapshots": []}
    ctx.working_path = None  # будет fallback на project_path
    return ctx


def _mk_audit(project_dir, after_errors):
    """Создаёт AuditManager с мокаными зависимостями. analyzer.analyze возвращает
    переданный список after-ошибок."""
    AuditManager = _load_audit()
    analyzer = MagicMock()
    analyzer.analyze = MagicMock(return_value=after_errors)
    ctx_holder = {"ctx": None}

    def getter():
        return ctx_holder["ctx"]

    am = AuditManager(
        project_path=project_dir,
        language="rust",
        analyzer=analyzer,
        classifier=MagicMock(),
        compiler=MagicMock(),
        config={"pipeline": {}},
        context_getter=getter,
    )
    return am, ctx_holder


def _err(file, code, msg, error_class=""):
    return {"file": file, "code": code, "message": msg,
            "error_class": error_class, "severity": "error", "error_type": "compile"}


def _setup_project_with_file(fname="src/game.rs"):
    """Создаёт временный проект с пустым целевым файлом, чтобы audit_path/fname.exists()."""
    d = Path(tempfile.mkdtemp())
    (d / fname).parent.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text("dummy\n", encoding="utf-8")
    return d


# --- 1. Демаскирование: 4 → 4 ошибки, состав изменился, нет CRITICAL_SYNTAX ---
def test_demask_passes():
    """До: 4 импорт-ошибки. После: 4 field-ошибки. Аудит должен пропустить."""
    fname = "src/game.rs"
    d = _setup_project_with_file(fname)
    before = [
        _err(fname, "E0432", "unresolved import player"),
        _err(fname, "E0432", "unresolved import guess"),
        _err(fname, "", "edition note for player"),
        _err(fname, "", "edition note for guess"),
    ]
    after = [
        _err(fname, "E0609", "no field value on type Guess"),
        _err(fname, "E0609", "no field value on type Guess at 18"),
        _err(fname, "E0432", "unresolved import player"),  # один остался
        _err(fname, "", "edition note for player"),
    ]
    # Намеренно сделаем 4 уникальных before и 4 уникальных after
    am, holder = _mk_audit(d, after)
    holder["ctx"] = _mk_context({fname: before})
    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is True, f"ждали audit_ok=True (демаскирование), получили {res}"
    assert fname in res["passed_files"], f"ждали {fname} в passed_files, получили {res}"


# --- 2. Реальная regression: счётчик вырос ---
def test_count_grew_fails():
    """До: 2 ошибки. После: 5 ошибок (вырос счётчик). Аудит должен failed."""
    fname = "src/game.rs"
    d = _setup_project_with_file(fname)
    before = [
        _err(fname, "E0432", "unresolved import guess"),
        _err(fname, "E0432", "unresolved import player"),
    ]
    after = [
        _err(fname, "E0609", "no field value"),
        _err(fname, "E0609", "no field value at 18"),
        _err(fname, "E0308", "type mismatch"),
        _err(fname, "E0432", "unresolved import player"),
        _err(fname, "E0001", "extra unrelated"),
    ]
    am, holder = _mk_audit(d, after)
    holder["ctx"] = _mk_context({fname: before})
    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is False, "ждали audit_ok=False (счётчик вырос)"
    assert fname in res["failed_segments"], "ждали fname в failed_segments"


# --- 3. CRITICAL_SYNTAX в new — failed даже если счётчик не вырос ---
def test_critical_syntax_in_new_fails():
    """До: 4 импорт-ошибки. После: 4, но появилась CRITICAL_SYNTAX → failed."""
    fname = "src/game.rs"
    d = _setup_project_with_file(fname)
    before = [
        _err(fname, "E0432", "unresolved import a"),
        _err(fname, "E0432", "unresolved import b"),
        _err(fname, "E0432", "unresolved import c"),
        _err(fname, "E0432", "unresolved import d"),
    ]
    after = [
        _err(fname, "E0001", "expected one of comma", error_class="CRITICAL_SYNTAX"),
        _err(fname, "E0609", "no field value"),
        _err(fname, "E0609", "no field x"),
        _err(fname, "E0609", "no field y"),
    ]
    am, holder = _mk_audit(d, after)
    holder["ctx"] = _mk_context({fname: before})
    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is False, "ждали audit_ok=False (есть CRITICAL_SYNTAX)"
    assert fname in res["failed_segments"], "CRITICAL_SYNTAX в new → файл должен быть в failed_segments"


# --- 4. Чистый файл (нет new_sigs) — passed ---
def test_no_new_passes():
    fname = "src/lib.rs"
    d = _setup_project_with_file(fname)
    before = [_err(fname, "W0001", "warn x")]
    after = [_err(fname, "W0001", "warn x")]  # тот же набор
    am, holder = _mk_audit(d, after)
    holder["ctx"] = _mk_context({fname: before})
    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is True
    assert fname in res["passed_files"]


if __name__ == "__main__":
    tests = [
        ("demask_passes", test_demask_passes),
        ("count_grew_fails", test_count_grew_fails),
        ("critical_syntax_in_new_fails", test_critical_syntax_in_new_fails),
        ("no_new_passes", test_no_new_passes),
    ]
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
    print()
    print(f"Audit demask regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
