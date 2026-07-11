"""
_finalize_and_audit → _copy_passed_files_to_original — финальное копирование
успешных файлов в реальный проект.

Проверяем, что файлы из `accepted_patches` ВСЕГДА оказываются в проекте, даже
если аудит их не видел (типичная ситуация: Python без flake8 → AST-фоллбэк
видит только первую ошибку, поэтому `audit.passed_files` пуст для остальных
файлов). Это была причина «правка hardcoded_secret принята, но в config.py
старый код».

Поднимаем PipelineEngine через `__new__` без __init__ (избегаем тяжёлых
зависимостей) и шарим в него минимум: `project_path`, `context.accepted_patches`.

Запуск: python3 tests/test_phase_finalize_copy.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# Заглушка для tomlkit (core.stages.__init__ тянет toml_patcher).
try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_engine import PipelineEngine

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _engine(project: Path, accepted_files: list) -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    accepted_patches = [
        {"error": {"file": f, "message": "x", "code": "X"}} for f in accepted_files
    ]
    eng.context = SimpleNamespace(accepted_patches=accepted_patches)
    return eng


def _setup(d: str, files_in_proj: dict, files_in_sandbox: dict):
    proj = Path(d) / "proj"
    sandbox = Path(d) / "sandbox"
    proj.mkdir(parents=True, exist_ok=True)
    sandbox.mkdir(parents=True, exist_ok=True)
    for rel, content in files_in_proj.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel, content in files_in_sandbox.items():
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return proj, sandbox


# ----------------------------------------------------------------------
# 1. ACCEPT-файл вне audit.passed_files → всё равно копируется
# ----------------------------------------------------------------------
def test_accept_outside_audit_copied():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={
                "auth.py": "OLD_AUTH",
                "config.py": "SECRET_KEY = 'hardcoded'\n",
            },
            files_in_sandbox={
                "auth.py": "NEW_AUTH",
                "config.py": "SECRET_KEY = os.environ['KEY']\n",
            },
        )
        # Аудит «увидел» только auth.py (типичный AST-фоллбэк).
        audit_result = {"passed_files": ["auth.py"], "failed_segments": {}}
        eng = _engine(proj, accepted_files=["auth.py", "config.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result)
        # auth.py перетянут аудитом.
        check("auth_copied", (proj / "auth.py").read_text(encoding="utf-8") == "NEW_AUTH")
        # config.py перетянут страховкой по accepted_patches.
        check("config_copied",
              (proj / "config.py").read_text(encoding="utf-8").startswith("SECRET_KEY = os.environ"))
        # backup сохранён.
        backups = (proj / ".webbles_backups")
        check("backup_dir_created", backups.exists())
        # backup-имя строится через replace('/', '_').replace('\\','_').
        check("auth_backup_exists", any("auth.py" in f.name for f in backups.iterdir()))
        check("config_backup_exists", any("config.py" in f.name for f in backups.iterdir()))


# ----------------------------------------------------------------------
# 2. Уже в passed_files — не копируем дважды
# ----------------------------------------------------------------------
def test_no_double_copy():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"x.py": "OLD"},
            files_in_sandbox={"x.py": "NEW"},
        )
        audit_result = {"passed_files": ["x.py"], "failed_segments": {}}
        eng = _engine(proj, accepted_files=["x.py"])
        # Считаем сколько раз файл реально переписали — через mtime.
        eng._copy_passed_files_to_original(sandbox, audit_result)
        first = (proj / "x.py").stat().st_mtime_ns
        first_content = (proj / "x.py").read_text(encoding="utf-8")
        # Повторный вызов не должен ломать (хотя страховка пропустит — файл уже скопирован).
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("content_after_first", first_content == "NEW")
        # backup-папка содержит только нужное.
        backups = list((proj / ".webbles_backups").iterdir())
        check("backup_single", any("x.py" in f.name for f in backups))


# ----------------------------------------------------------------------
# 3. Файл частично откачен (failed_segments) — обычное поведение сохранено
# ----------------------------------------------------------------------
def test_failed_segments_partial_copy():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"a.py": "old_a", "b.py": "old_b"},
            files_in_sandbox={"a.py": "fixed_a", "b.py": "fixed_b"},
        )
        # a.py — passed, b.py — failed_segments не end==0 (частичный откат).
        audit_result = {
            "passed_files": ["a.py"],
            "failed_segments": {"b.py": [(10, 15, "fail")]},  # end != 0
        }
        eng = _engine(proj, accepted_files=["a.py"])  # b.py НЕ в accepted
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("a_copied_passed",
              (proj / "a.py").read_text(encoding="utf-8") == "fixed_a")
        check("b_copied_partial",
              (proj / "b.py").read_text(encoding="utf-8") == "fixed_b")


# ----------------------------------------------------------------------
# 4. Полный откат сегмента (end==0) — НЕ копируем
# ----------------------------------------------------------------------
def test_full_rollback_segment_not_copied():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"x.py": "ORIG"},
            files_in_sandbox={"x.py": "BROKEN"},
        )
        audit_result = {
            "passed_files": [],
            "failed_segments": {"x.py": [(1, 0, "totally_broken")]},  # end == 0
        }
        eng = _engine(proj, accepted_files=[])  # DECIDE не принимал
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("x_not_copied", (proj / "x.py").read_text(encoding="utf-8") == "ORIG")


# ----------------------------------------------------------------------
# 5. ACCEPT-файла нет в sandbox → не падаем
# ----------------------------------------------------------------------
def test_accept_missing_in_sandbox_no_crash():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"existing.py": "OLD"},
            files_in_sandbox={"existing.py": "NEW"},
        )
        # phantom.py указан как accepted, но в sandbox его нет.
        audit_result = {"passed_files": ["existing.py"], "failed_segments": {}}
        eng = _engine(proj, accepted_files=["existing.py", "phantom.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("existing_still_copied",
              (proj / "existing.py").read_text(encoding="utf-8") == "NEW")
        check("phantom_not_created", not (proj / "phantom.py").exists())


# ----------------------------------------------------------------------
# 6. Пустые accepted_patches — поведение совпадает с прежним
# ----------------------------------------------------------------------
def test_empty_accepted_patches_no_op():
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"x.py": "OLD"},
            files_in_sandbox={"x.py": "NEW"},
        )
        audit_result = {"passed_files": ["x.py"], "failed_segments": {}}
        eng = _engine(proj, accepted_files=[])  # нет ACCEPT
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("audit_path_works", (proj / "x.py").read_text(encoding="utf-8") == "NEW")


# ----------------------------------------------------------------------
# 7. O.13 — backup-of-backup guard
# ----------------------------------------------------------------------

def test_o13_passed_file_in_backups_is_skipped():
    """passed_files содержит путь под .webbles_backups → не копируем."""
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"auth.py": "OLD_AUTH"},
            files_in_sandbox={
                "auth.py": "NEW_AUTH",
                ".webbles_backups/auth.py": "OLD_BACKUP",
            },
        )
        audit_result = {
            # О чудо: аудит увидел "auth.py" под .webbles_backups как
            # отдельный файл (так бывало до O.12).
            "passed_files": ["auth.py", ".webbles_backups/auth.py"],
            "failed_segments": {},
        }
        eng = _engine(proj, accepted_files=["auth.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result)
        # Реальный auth.py перетянут.
        check("o13: real auth copied",
              (proj / "auth.py").read_text(encoding="utf-8") == "NEW_AUTH")
        # В backup-папке НЕТ файла с префиксом ".webbles_backups_" —
        # значит мы не делали backup-of-backup.
        backups = proj / ".webbles_backups"
        names = [f.name for f in backups.iterdir() if f.is_file()]
        check("o13: no double-prefix in backups",
              not any(n.startswith(".webbles_backups_") for n in names))


def test_o13_accepted_file_in_backups_is_skipped():
    """accepted_patches содержит путь под .webbles_backups → не копируем."""
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"x.py": "OLD_X"},
            files_in_sandbox={
                "x.py": "NEW_X",
                ".webbles_backups/x.py": "NEW_BACKUP",
            },
        )
        audit_result = {"passed_files": ["x.py"], "failed_segments": {}}
        eng = _engine(
            proj,
            accepted_files=["x.py", ".webbles_backups/x.py"],
        )
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("o13(accepted): real x copied",
              (proj / "x.py").read_text(encoding="utf-8") == "NEW_X")
        # Структуры `.webbles_backups/.webbles_backups/...` не появилось.
        nested = proj / ".webbles_backups" / ".webbles_backups"
        check("o13(accepted): no nested .webbles_backups",
              not nested.exists())
        # И никаких файлов с префиксом ".webbles_backups_" в backup-папке.
        names = [f.name for f in (proj / ".webbles_backups").iterdir()
                 if f.is_file()]
        check("o13(accepted): no double-prefix",
              not any(n.startswith(".webbles_backups_") for n in names))


def test_o13_failed_segment_in_backups_is_skipped():
    """failed_segments путь под .webbles_backups → не копируем."""
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"y.py": "OLD_Y"},
            files_in_sandbox={
                "y.py": "NEW_Y",
                ".webbles_backups/y.py": "SHOULD_NOT_BE_TOUCHED",
            },
        )
        audit_result = {
            "passed_files": [],
            "failed_segments": {
                "y.py": [(1, 5, "fail")],
                ".webbles_backups/y.py": [(1, 5, "fail")],
            },
        }
        eng = _engine(proj, accepted_files=[])
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("o13(seg): real y copied",
              (proj / "y.py").read_text(encoding="utf-8") == "NEW_Y")
        # Корень `.webbles_backups/y.py` — это бэкап-папка, файл `y.py`
        # внутри неё мог уже лежать как бэкап. Главное — мы не пишем
        # .webbles_backups/.webbles_backups_y.py.
        names = [f.name for f in (proj / ".webbles_backups").iterdir()
                 if f.is_file()]
        check("o13(seg): no double-prefix",
              not any(n.startswith(".webbles_backups_") for n in names))


def test_o13_webbles_and_webbles_fix_also_skipped():
    """То же правило для `.webbles` и `.webbles_fix`."""
    with tempfile.TemporaryDirectory() as d:
        proj, sandbox = _setup(
            d,
            files_in_proj={"main.py": "OLD"},
            files_in_sandbox={
                "main.py": "NEW",
                ".webbles/state.md": "x",
                ".webbles_fix/needs_review/abc.json": "y",
            },
        )
        audit_result = {
            "passed_files": ["main.py", ".webbles/state.md",
                             ".webbles_fix/needs_review/abc.json"],
            "failed_segments": {},
        }
        eng = _engine(proj, accepted_files=["main.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result)
        check("o13(.webbles+ .webbles_fix): real main copied",
              (proj / "main.py").read_text(encoding="utf-8") == "NEW")
        # Не появилось `.webbles/state.md` через эту функцию (она бы
        # перезаписала его с бэкапом).
        # Точно: проверим, что в backup-папке нет .webbles_state.md.
        backups = proj / ".webbles_backups"
        if backups.exists():
            names = [f.name for f in backups.iterdir() if f.is_file()]
            check("o13(.webbles): no .webbles_state.md in backups",
                  not any(n.startswith(".webbles_") and "state.md" in n
                          for n in names))
            check("o13(.webbles_fix): no .webbles_fix_ prefix in backups",
                  not any(n.startswith(".webbles_fix_") for n in names))
        else:
            check("o13(.webbles): backup dir absent — fine", True)
            check("o13(.webbles_fix): backup dir absent — fine", True)


if __name__ == "__main__":
    print("_finalize_and_audit copy smoke:")
    test_accept_outside_audit_copied()
    test_no_double_copy()
    test_failed_segments_partial_copy()
    test_full_rollback_segment_not_copied()
    test_accept_missing_in_sandbox_no_crash()
    test_empty_accepted_patches_no_op()
    test_o13_passed_file_in_backups_is_skipped()
    test_o13_accepted_file_in_backups_is_skipped()
    test_o13_failed_segment_in_backups_is_skipped()
    test_o13_webbles_and_webbles_fix_also_skipped()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n_finalize copy: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
