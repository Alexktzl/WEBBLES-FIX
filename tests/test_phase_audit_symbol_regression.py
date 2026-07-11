"""
Regress: финальный аудит обязан ловить потерю/дублирование символов, даже
если патч не породил ни одной новой lint-ошибки на затронутых строках.

Контекст: control series 2026-06-25 (bcrypt, httpx) — unsafe_accept. Патч
стирал десятки `def`/dunder-методов, но т.к. в удалённом коде не было
ошибок и удаление не создало НОВЫХ ошибок на конкретных строках, старый
`AuditManager.audit_run()` (сравнение только по lint-сигнатурам before/after)
считал такой файл "passed". per-patch guard (validate_stage/decide_stage,
`analysis/symbol_regression.py`) ловит это на уровне ОДНОГО патча, но
финальный аудит проекта — последний рубеж на уровне всего прогона — этой
проверки не делал вовсе.

Фикс: audit_run() теперь сравнивает project_path/fname (истинный оригинал)
с audit_path/fname (текущее состояние) через check_symbol_regression /
check_symbol_duplication и форсирует полный откат файла при обнаружении
пропавших/задублированных def/class/import/overload — независимо от
lint-сигнатур.

Запуск: python3 tests/test_phase_audit_symbol_regression.py
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
        "audit_for_test_symreg", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AuditManager


def _mk_context(file_sigs_before, working_path):
    """Мок-контекст: project_path (эталон) и working_path (аудируемая копия) — РАЗНЫЕ
    директории, чтобы симулировать реальный прогон (working copy отдельно от source)."""
    ctx = MagicMock()
    ctx.metadata = {"file_error_signatures_before": file_sigs_before,
                    "patch_snapshots": []}
    ctx.working_path = working_path
    return ctx


def _mk_audit(project_dir, after_errors, language="python"):
    AuditManager = _load_audit()
    analyzer = MagicMock()
    analyzer.analyze = MagicMock(return_value=after_errors)
    ctx_holder = {"ctx": None}

    def getter():
        return ctx_holder["ctx"]

    am = AuditManager(
        project_path=project_dir,
        language=language,
        analyzer=analyzer,
        classifier=MagicMock(),
        compiler=MagicMock(),
        config={"pipeline": {}},
        context_getter=getter,
    )
    return am, ctx_holder


def _write(d: Path, fname: str, content: str) -> Path:
    (d / fname).parent.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(content, encoding="utf-8")
    return d


# --- 1. Функция стёрта, НИ ОДНОЙ новой lint-ошибки — аудит обязан поймать ---
def test_missing_def_no_new_lint_errors_fails():
    """bcrypt/httpx-сценарий: def пропал, before_sigs == after_sigs (пусто)."""
    fname = "tests/test_bcrypt.py"
    original = (
        "def test_checkpw():\n"
        "    assert True\n"
        "\n"
        "def create_user():\n"
        "    return object()\n"
    )
    patched = (
        "def test_checkpw():\n"
        "    assert True\n"
    )
    project_dir = _write(Path(tempfile.mkdtemp()), fname, original)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, patched)

    am, holder = _mk_audit(project_dir, after_errors=[])  # ни одной lint-ошибки после
    holder["ctx"] = _mk_context({fname: []}, working_path=working_dir)  # ни одной до

    res = am.audit_run(holder["ctx"])

    assert res["audit_ok"] is False, f"ждали audit_ok=False (пропал def create_user), получили {res}"
    assert fname in res["failed_segments"], f"ждали {fname} в failed_segments, получили {res}"
    assert fname not in res["passed_files"], "файл с пропавшим символом не должен быть passed"
    # Полный откат: восстановленное содержимое — истинный оригинал.
    segments = res["failed_segments"][fname]
    assert segments == [(1, 0, original)], f"ждали полный откат к оригиналу, получили {segments}"


# --- 2. Класс задублирован (pure-insert bug), новых lint-ошибок нет ---
def test_duplicated_class_no_new_lint_errors_fails():
    fname = "src/models.py"
    original = "class User:\n    pass\n"
    patched = "class User:\n    pass\n\n\nclass User:\n    pass\n"
    project_dir = _write(Path(tempfile.mkdtemp()), fname, original)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, patched)

    am, holder = _mk_audit(project_dir, after_errors=[])
    holder["ctx"] = _mk_context({fname: []}, working_path=working_dir)

    res = am.audit_run(holder["ctx"])

    assert res["audit_ok"] is False, f"ждали audit_ok=False (class User задублирован), получили {res}"
    assert fname in res["failed_segments"]


# --- 3b. restore_failed_segments() обязан ДЕЙСТВИТЕЛЬНО восстановить файл при
#     полном откате (end=0), а не молча перезаписать его устаревшим содержимым ---
def test_restore_failed_segments_full_rollback_actually_restores():
    """control series verify-run 2026-07-01: audit_run() детектировал потерю
    символов (missing_defs) и вернул failed_segments=[(1, 0, original)] —
    но `restore_failed_segments()` делал `shutil.copy2(orig, tmp)` для
    полного отката и СРАЗУ ЖЕ безусловно перезаписывал tmp_file
    устаревшим `current_lines` (прочитанным ДО restore), сводя откат на
    нет. Файл оставался повреждённым на диске несмотря на корректную
    детекцию — именно поэтому httpx/_models.py остался с unsafe_accept=true
    даже после фикса детекции в audit_run()."""
    fname = "httpx/_models.py"
    original = (
        "class Response:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "    def __repr__(self):\n"
        "        return 'Response'\n"
    )
    corrupted = "from __future__ import annotations\n\nimport codecs\n"

    project_dir = _write(Path(tempfile.mkdtemp()), fname, original)
    tmp_dir = _write(Path(tempfile.mkdtemp()), fname, corrupted)

    AuditManager = _load_audit()
    am = AuditManager(
        project_path=project_dir, language="python", analyzer=MagicMock(),
        classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
        context_getter=lambda: None,
    )

    failed_segments = {fname: [(1, 0, original)]}
    am.restore_failed_segments(tmp_dir, failed_segments)

    restored = (tmp_dir / fname).read_text(encoding="utf-8")
    assert restored == original, (
        f"полный откат должен вернуть tmp_dir/{fname} к оригиналу, "
        f"получили:\n{restored!r}"
    )


# --- 3. Контрольный случай: легитимная маленькая правка без потери символов — passed ---
def test_legit_small_patch_still_passes():
    fname = "src/models.py"
    original = "def foo(x):\n    return x+1\n"
    patched = "def foo(x):\n    return x + 1\n"  # только форматирование, символы целы
    project_dir = _write(Path(tempfile.mkdtemp()), fname, original)
    working_dir = _write(Path(tempfile.mkdtemp()), fname, patched)

    am, holder = _mk_audit(project_dir, after_errors=[])
    holder["ctx"] = _mk_context({fname: []}, working_path=working_dir)

    res = am.audit_run(holder["ctx"])

    assert res["audit_ok"] is True, f"ждали audit_ok=True (нет потери символов), получили {res}"
    assert fname in res["passed_files"]


if __name__ == "__main__":
    tests = [
        ("missing_def_no_new_lint_errors_fails", test_missing_def_no_new_lint_errors_fails),
        ("duplicated_class_no_new_lint_errors_fails", test_duplicated_class_no_new_lint_errors_fails),
        ("restore_failed_segments_full_rollback_actually_restores", test_restore_failed_segments_full_rollback_actually_restores),
        ("legit_small_patch_still_passes", test_legit_small_patch_still_passes),
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
    print(f"Audit symbol-regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
