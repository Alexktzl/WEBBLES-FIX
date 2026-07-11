"""
Regress C1 (PROJECT_AUDIT_REPORT 2026-07-01): финальный аудит сравнивал
множества РАЗНЫХ типов — before_sigs как объекты ErrorSignature (от
get_initial_signatures_provider), after_sigs как кортежи с другой
нормализацией. `after - before` никогда ничего не вычитал: все текущие
ошибки файла считались «новыми», lost_sigs — весь baseline.

Последствия до фикса:
  * патч, заменивший одну ошибку другой при том же счётчике, проходил как
    «демаскирование» (реальная регрессия — passed);
  * при выросшем счётчике _find_problem_segments получал в new_sigs ВСЕ
    ошибки файла и перекатывал валидные сегменты.

Фикс: обе стороны приводятся к ErrorSignature.to_tuple() одной функцией
(_extract_error_signatures / _normalize_before_sigs).

Запуск: python tests/test_phase_audit_signature_types.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.error_intelligence.error_signature import ErrorSignature  # noqa: E402


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_for_test_sigtypes", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AuditManager


def _mk_context(file_sigs_before, working_path):
    ctx = MagicMock()
    ctx.metadata = {"file_error_signatures_before": file_sigs_before,
                    "patch_snapshots": []}
    ctx.working_path = working_path
    return ctx


def _mk_audit(project_dir, after_errors):
    AuditManager = _load_audit()
    analyzer = MagicMock()
    analyzer.analyze = MagicMock(return_value=after_errors)
    holder = {"ctx": None}
    am = AuditManager(
        project_path=project_dir, language="python", analyzer=analyzer,
        classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
        context_getter=lambda: holder["ctx"],
    )
    return am, holder


def _write(d: Path, fname: str, content: str) -> Path:
    (d / fname).parent.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(content, encoding="utf-8")
    return d


_CONTENT = "def foo():\n    return 1\n"


def _err(fname, line, code, msg):
    return {"file": fname, "line": line, "code": code, "message": msg}


# --- 1. Та же ошибка осталась (provider-объекты ErrorSignature) → passed ---
def test_same_error_with_provider_objects_passes():
    fname = "src/app.py"
    err = _err(fname, 3, "E501", "line too long (120 > 79 characters)")
    before = {ErrorSignature.from_error(err)}  # ровно как отдаёт sig provider

    project_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)

    am, holder = _mk_audit(project_dir, after_errors=[err])
    holder["ctx"] = _mk_context({fname: before}, working_dir)

    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is True, (
        f"pre-existing ошибка не должна считаться новой (C1 type mismatch), получили {res}"
    )
    assert fname in res["passed_files"]


# --- 2. Ошибка заменилась ДРУГОЙ при том же счётчике → failed (не «демаскирование») ---
def test_error_replaced_same_count_fails():
    fname = "src/app.py"
    before_err = _err(fname, 3, "E501", "line too long (120 > 79 characters)")
    after_err = _err(fname, 3, "F821", "undefined name 'bar'")
    before = {ErrorSignature.from_error(before_err)}

    project_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)

    am, holder = _mk_audit(project_dir, after_errors=[after_err])
    holder["ctx"] = _mk_context({fname: before}, working_dir)

    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is False, (
        f"подмена ошибки при равном счётчике — регрессия, а не демаскирование: {res}"
    )
    assert fname in res["failed_segments"]


# --- 3. Кортежи из restored state (H6) принимаются наравне с объектами ---
def test_restored_tuple_sigs_accepted():
    fname = "src/app.py"
    err = _err(fname, 3, "E501", "line too long (120 > 79 characters)")
    sig_tuple = ErrorSignature.from_error(err).to_tuple()
    before = {sig_tuple}  # как после save/load state.json

    project_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)

    am, holder = _mk_audit(project_dir, after_errors=[err])
    holder["ctx"] = _mk_context({fname: before}, working_dir)

    res = am.audit_run(holder["ctx"])
    assert res["audit_ok"] is True, f"кортежная форма before-сигнатур должна матчиться: {res}"


# --- 4. Испорченный legacy-персист (строки) не роняет аудит ---
def test_corrupted_string_sigs_do_not_crash():
    fname = "src/app.py"
    project_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, _CONTENT)

    am, holder = _mk_audit(project_dir, after_errors=[])
    holder["ctx"] = _mk_context({fname: "{ErrorSignature(...)}"}, working_dir)

    res = am.audit_run(holder["ctx"])
    # Строка — не set/list → before пустой; после — ошибок нет → passed.
    assert fname in res["passed_files"]


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
    print(f"\nAudit signature types: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
