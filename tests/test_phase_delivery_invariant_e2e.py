"""
M7 (PROJECT_AUDIT_REPORT.md) — property-стиль тест ГЛАВНОГО инварианта доставки:

    После фазы `_finalize_and_audit` каждый файл в `project_path` байтово
    равен ЛИБО своему исходному содержимому (то, что лежало в проекте ДО
    прогона), ЛИБО содержимому из sandbox, явно одобренному аудитом
    (`passed_files`) или DECIDE (`accepted_patches`) — а файлы из
    `failed_segments` НИКОГДА не доставляются повреждёнными.

Это не тест одной функции, а инвариант, который обязан выполняться для
ЛЮБОЙ комбинации: чистая доставка, полный откат (через реальный
AuditManager.audit_run — детект потери символов), провал restore, исчезший
из sandbox файл, смешанный многофайловый прогон. Каждый сценарий ниже после
самого действия прогоняет ОДИН общий хелпер `assert_delivery_invariant`,
который сверяет ВСЕ файлы project_path байт-в-байт — не только те, что
сценарий явно проверяет.

Собрано по образцу tests/test_phase_delivery_respects_audit.py (постройка
proj/sandbox, PipelineEngine через __new__, вызов
`_copy_passed_files_to_original`) и tests/test_phase_audit_symbol_regression.py
(реальный `AuditManager` через importlib, mock analyzer) —
tests/test_phase_finalize_copy.py (паттерн `_engine()`/`_setup()`).

Запуск: python tests/test_phase_delivery_invariant_e2e.py
    или: venv/Scripts/python.exe -m pytest tests/test_phase_delivery_invariant_e2e.py -q
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# Заглушка для tomlkit (core.stages.__init__ тянет toml_patcher) — как в
# соседних тестах этой директории.
try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_engine import PipelineEngine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _load_audit():
    """Импортируем AuditManager напрямую, минуя core.engine.__init__ (тяжёлые
    зависимости пакета не нужны для юнит-уровня аудита)."""
    spec = importlib.util.spec_from_file_location(
        "audit_for_test_delivery_invariant", str(ROOT / "core" / "engine" / "audit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AuditManager


def _engine(project: Path, accepted_files: list) -> PipelineEngine:
    """PipelineEngine через __new__ без __init__ — шарим минимум полей,
    которые реально трогает `_copy_passed_files_to_original`."""
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    eng.context = SimpleNamespace(accepted_patches=[
        {"error": {"file": f, "message": "x", "code": "X"}} for f in accepted_files
    ])
    return eng


def _setup(d: str, files_in_proj: dict, files_in_sandbox: dict):
    """
    ВАЖНО: пишем через write_bytes(content.encode("utf-8")), а НЕ write_text —
    на Windows текстовый режим open() транслирует "\n" -> "\r\n" при записи,
    из-за чего байтовый снимок (_snapshot читает read_bytes()) расходился бы
    с исходной Python-строкой `content`, ломая байтовое сравнение в
    assert_delivery_invariant для любого многострочного содержимого.
    """
    proj = Path(d) / "proj"
    sandbox = Path(d) / "sandbox"
    proj.mkdir(parents=True, exist_ok=True)
    sandbox.mkdir(parents=True, exist_ok=True)
    for rel, content in files_in_proj.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    for rel, content in files_in_sandbox.items():
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    return proj, sandbox


def _snapshot(root: Path) -> dict:
    """Байтовый снимок всех файлов под `root` (rel_path -> bytes), исключая
    служебную папку бэкапов — она не часть инварианта доставки."""
    out = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".webbles_backups/") or rel == ".webbles_backups":
            continue
        out[rel] = p.read_bytes()
    return out


def assert_delivery_invariant(proj: Path, original: dict, sandbox_final: dict, approved: set) -> None:
    """
    ГЛАВНЫЙ ИНВАРИАНТ ДОСТАВКИ (M7).

    Для каждого файла, который сейчас реально лежит в `project_path`:
      -- ЛИБО его байты равны исходному содержимому (`original[rel]`),
      -- ЛИБО его байты равны содержимому из sandbox (`sandbox_final[rel]`)
         И `rel` явно входит в `approved` (passed_files ИЛИ accepted_patches).

    Если оба варианта не выполняются — инвариант нарушен: в проект уехало
    содержимое, которое не является ни оригиналом, ни явно одобренной
    правкой (то есть потенциально повреждённый/неподтверждённый sandbox-файл).
    """
    current = _snapshot(proj)
    for rel, content in current.items():
        orig = original.get(rel)
        orig_bytes = orig.encode("utf-8") if isinstance(orig, str) else orig
        is_orig = orig_bytes is not None and content == orig_bytes
        if is_orig:
            continue
        # Содержимое отличается от оригинала -> ОБЯЗАНО быть явно одобренной
        # sandbox-версией, а не чем-то ещё (частично применённым откатом,
        # мусором из failed_segments и т.п.).
        assert rel in approved, (
            f"ИНВАРИАНТ НАРУШЕН: {rel} в project_path изменён, но НЕ входит "
            f"в approved={sorted(approved)} (ни оригинал, ни одобренная правка)"
        )
        sbox = sandbox_final.get(rel)
        assert sbox is not None and content == sbox, (
            f"ИНВАРИАНТ НАРУШЕН: {rel} одобрен ({rel in approved}), но байты в "
            f"проекте не совпадают с финальным содержимым sandbox"
        )


# ----------------------------------------------------------------------
# 1. Чистая доставка: 1 из 3 файлов изменён и passed -> ровно он доставлен,
#    остальные нетронуты, бэкап оригинала создан.
# ----------------------------------------------------------------------
def test_clean_delivery_only_passed_file_changes():
    with tempfile.TemporaryDirectory() as d:
        original = {"a.py": "A_ORIG", "b.py": "B_ORIG", "c.py": "C_ORIG"}
        proj, sandbox = _setup(
            d,
            files_in_proj=dict(original),
            files_in_sandbox={"a.py": "A_ORIG", "b.py": "B_ORIG", "c.py": "C_NEW"},
        )
        audit_result = {"passed_files": ["c.py"], "failed_segments": {}}
        eng = _engine(proj, accepted_files=["c.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={})

        assert (proj / "c.py").read_text(encoding="utf-8") == "C_NEW"
        assert (proj / "a.py").read_text(encoding="utf-8") == "A_ORIG"
        assert (proj / "b.py").read_text(encoding="utf-8") == "B_ORIG"
        backup_dir = proj / ".webbles_backups"
        assert backup_dir.exists(), "бэкап оригинала должен быть создан"
        assert any("c.py" in f.name for f in backup_dir.iterdir())

        assert_delivery_invariant(
            proj, original=original, sandbox_final=_snapshot(sandbox), approved={"c.py"},
        )


# ----------------------------------------------------------------------
# 2. Полный откат: реальный AuditManager.audit_run сам находит потерю def
#    (символьная проверка, без единой новой lint-ошибки — bcrypt/httpx-
#    сценарий), restore_failed_segments чинит sandbox, доставка в проект
#    не происходит -> оригинал байт-в-байт.
# ----------------------------------------------------------------------
def test_full_rollback_detected_by_real_audit_run():
    AuditManager = _load_audit()
    with tempfile.TemporaryDirectory() as d:
        original_content = (
            "def foo():\n"
            "    return 1\n"
            "\n"
            "def bar():\n"
            "    return 2\n"
        )
        corrupted_content = (
            "def foo():\n"
            "    return 1\n"
        )  # bar() пропал, ни одной новой lint-ошибки
        original = {"app.py": original_content}
        proj, sandbox = _setup(
            d,
            files_in_proj=original,
            files_in_sandbox={"app.py": corrupted_content},
        )

        analyzer = MagicMock()
        analyzer.analyze = MagicMock(return_value=[])
        holder = {"ctx": None}
        am = AuditManager(
            project_path=proj, language="python", analyzer=analyzer,
            classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
            context_getter=lambda: holder["ctx"],
        )
        ctx = MagicMock()
        ctx.metadata = {"file_error_signatures_before": {"app.py": []}, "patch_snapshots": []}
        ctx.working_path = sandbox
        holder["ctx"] = ctx

        audit_result = am.audit_run(ctx)
        assert audit_result["audit_ok"] is False, audit_result
        assert "app.py" in audit_result["failed_segments"], audit_result

        restore_status = am.restore_failed_segments(sandbox, audit_result["failed_segments"])
        assert restore_status.get("app.py") is True, restore_status
        assert (sandbox / "app.py").read_text(encoding="utf-8") == original_content

        # DECIDE могла принять патч ДО того, как финальный аудит поймал
        # потерю символа -- доставка обязана уважать вердикт аудита, а не ACCEPT.
        eng = _engine(proj, accepted_files=["app.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status)

        assert (proj / "app.py").read_text(encoding="utf-8") == original_content

        assert_delivery_invariant(
            proj, original=original, sandbox_final=_snapshot(sandbox), approved=set(),
        )


# ----------------------------------------------------------------------
# 3. Провал restore: файл в failed_segments И в accepted_patches, но
#    restore_status=False -> НЕ доставлен, оригинал байт-в-байт.
# ----------------------------------------------------------------------
def test_failed_restore_never_delivered_even_if_accepted():
    with tempfile.TemporaryDirectory() as d:
        original = {"models.py": "ORIGINAL"}
        proj, sandbox = _setup(
            d,
            files_in_proj=original,
            files_in_sandbox={"models.py": "DAMAGED"},
        )
        audit_result = {
            "passed_files": [],
            "failed_segments": {"models.py": [(1, 0, "ORIGINAL")]},
        }
        eng = _engine(proj, accepted_files=["models.py"])
        eng._copy_passed_files_to_original(
            sandbox, audit_result, restore_status={"models.py": False},
        )

        assert (proj / "models.py").read_text(encoding="utf-8") == "ORIGINAL"

        assert_delivery_invariant(
            proj, original=original, sandbox_final=_snapshot(sandbox), approved=set(),
        )


# ----------------------------------------------------------------------
# 4. Файл исчез из sandbox: audit_run даёт failed + missing_in_sandbox,
#    restore пересоздаёт его из оригинала В SANDBOX, project_path не тронут.
# ----------------------------------------------------------------------
def test_missing_in_sandbox_restored_without_touching_project():
    AuditManager = _load_audit()
    with tempfile.TemporaryDirectory() as d:
        original_content = "def f():\n    pass\n"
        original = {"src/app.py": original_content}
        proj = Path(d) / "proj"
        sandbox = Path(d) / "sandbox"
        (proj / "src").mkdir(parents=True)
        sandbox.mkdir(parents=True)
        # write_bytes, не write_text -- см. комментарий в _setup() про CRLF-
        # трансляцию на Windows, которая ломает байтовое сравнение снимков.
        (proj / "src" / "app.py").write_bytes(original_content.encode("utf-8"))
        # sandbox НЕ содержит src/app.py -- файл «исчез» посреди прогона.

        analyzer = MagicMock()
        analyzer.analyze = MagicMock(return_value=[])
        holder = {"ctx": None}
        am = AuditManager(
            project_path=proj, language="python", analyzer=analyzer,
            classifier=MagicMock(), compiler=MagicMock(), config={"pipeline": {}},
            context_getter=lambda: holder["ctx"],
        )
        ctx = MagicMock()
        ctx.metadata = {"file_error_signatures_before": {"src/app.py": set()},
                        "patch_snapshots": []}
        ctx.working_path = sandbox
        holder["ctx"] = ctx

        audit_result = am.audit_run(ctx)
        assert audit_result["audit_ok"] is False, audit_result
        assert "src/app.py" in audit_result["failed_segments"], audit_result
        assert "src/app.py" in audit_result.get("missing_in_sandbox", []), audit_result

        restore_status = am.restore_failed_segments(sandbox, audit_result["failed_segments"])
        assert restore_status.get("src/app.py") is True, restore_status
        assert (sandbox / "src" / "app.py").read_text(encoding="utf-8") == original_content

        eng = _engine(proj, accepted_files=[])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status)

        assert (proj / "src" / "app.py").read_text(encoding="utf-8") == original_content

        assert_delivery_invariant(
            proj, original=original, sandbox_final=_snapshot(sandbox), approved=set(),
        )


# ----------------------------------------------------------------------
# 5. Многофайловость: 2 passed + 1 failed(полный откат) + 1 accepted вне
#    аудита -> доставлены ровно 3 одобренных, failed -- оригинал.
# ----------------------------------------------------------------------
def test_multifile_mixed_outcomes():
    with tempfile.TemporaryDirectory() as d:
        original = {
            "p1.py": "P1_ORIG",
            "p2.py": "P2_ORIG",
            "p3.py": "P3_ORIG",
            "p4.py": "P4_ORIG",
        }
        proj, sandbox = _setup(
            d,
            files_in_proj=dict(original),
            files_in_sandbox={
                "p1.py": "P1_NEW",       # passed
                "p2.py": "P2_NEW",       # passed
                "p3.py": "P3_BROKEN",    # failed, full rollback (end == 0)
                "p4.py": "P4_NEW",       # accepted, но вне passed_files/failed_segments
            },
        )
        audit_result = {
            "passed_files": ["p1.py", "p2.py"],
            "failed_segments": {"p3.py": [(1, 0, "P3_ORIG")]},
        }
        eng = _engine(proj, accepted_files=["p1.py", "p2.py", "p4.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={})

        assert (proj / "p1.py").read_text(encoding="utf-8") == "P1_NEW"
        assert (proj / "p2.py").read_text(encoding="utf-8") == "P2_NEW"
        assert (proj / "p3.py").read_text(encoding="utf-8") == "P3_ORIG"
        assert (proj / "p4.py").read_text(encoding="utf-8") == "P4_NEW"

        assert_delivery_invariant(
            proj, original=original, sandbox_final=_snapshot(sandbox),
            approved={"p1.py", "p2.py", "p4.py"},
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
    print(f"\nDelivery invariant e2e: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
