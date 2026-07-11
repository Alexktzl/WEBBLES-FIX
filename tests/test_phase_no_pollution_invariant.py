"""
P0.6 — инвариант «чистого проекта пользователя» (2026-07-03, диагностика
88 детерминированных REJECT, пункт 1б).

    После доставки (`_copy_passed_files_to_original`) в `project_path` НЕ
    появляется НИЧЕГО нового, кроме: (а) служебной папки `.webbles_backups/`
    и (б) файлов, ЯВНО одобренных прогоном (passed_files / accepted_patches —
    например манифест зависимостей, доставляемый обычным accept-потоком,
    см. CLAUDE.md §8, решение Алекса 2026-07-02). И НИ ОДИН исходный файл
    проекта не должен молча исчезнуть.

Зачем инвариант отдельно от M7 (test_phase_delivery_invariant_e2e): M7
сверяет СОДЕРЖИМОЕ доставленных файлов, но не следит за МНОЖЕСТВОМ путей —
sandbox за прогон накапливает мусор (`__pycache__`, `.mypy_cache`, runtime-
артефакты, бэкап-копии), и протечка любого из них в чистый проект человека —
прямой удар по доверию («вы мне насрали в репозиторий»). Этот тест сверяет
именно множество путей project_path до/после.

Собрано по образцу tests/test_phase_delivery_invariant_e2e.py
(_setup/_snapshot/_engine, PipelineEngine через __new__).

Запуск: python tests/test_phase_no_pollution_invariant.py
    или: venv/Scripts/python.exe -m pytest tests/test_phase_no_pollution_invariant.py -q
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

from core.pipeline_engine import PipelineEngine  # noqa: E402


# ----------------------------------------------------------------------
# Скаффолдинг (совместим с test_phase_delivery_invariant_e2e.py)
# ----------------------------------------------------------------------
def _engine(project: Path, accepted_files: list) -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    eng.context = SimpleNamespace(accepted_patches=[
        {"error": {"file": f, "message": "x", "code": "X"}} for f in accepted_files
    ])
    return eng


def _setup(d: str, files_in_proj: dict, files_in_sandbox: dict):
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


def _paths(root: Path) -> set:
    """Множество rel-путей всех файлов под root, исключая .webbles_backups/."""
    out = set()
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".webbles_backups/") or rel == ".webbles_backups":
            continue
        out.add(rel)
    return out


def assert_no_pollution(proj: Path, original_paths: set, approved: set) -> None:
    """ИНВАРИАНТ P0.6.

    * new_paths = (пути проекта сейчас) − (исходные пути) ⊆ approved
      (единственное легитимное пополнение — явно одобренные файлы);
    * ни один исходный путь не исчез из проекта.
    `.webbles_backups/` уже исключён в `_paths`.
    """
    current = _paths(proj)
    new_paths = current - original_paths
    leaked = new_paths - approved
    assert not leaked, (
        f"ЗАГРЯЗНЕНИЕ ПРОЕКТА: в project_path появились неодобренные файлы "
        f"{sorted(leaked)} (approved={sorted(approved)})"
    )
    deleted = original_paths - current
    assert not deleted, (
        f"ИСХОДНЫЕ ФАЙЛЫ ИСЧЕЗЛИ из project_path: {sorted(deleted)}"
    )


# ----------------------------------------------------------------------
# 1. Sandbox-мусор (не одобрен) НЕ протекает в проект.
# ----------------------------------------------------------------------
def test_sandbox_junk_never_leaks_into_project():
    with tempfile.TemporaryDirectory() as d:
        original = {"a.py": "A_ORIG", "pkg/b.py": "B_ORIG"}
        proj, sandbox = _setup(
            d,
            files_in_proj=dict(original),
            files_in_sandbox={
                "a.py": "A_NEW",          # одобренная правка
                "pkg/b.py": "B_ORIG",
                # мусор, накопленный в sandbox за прогон — НЕ одобрен:
                "__pycache__/a.cpython-311.pyc": "BYTECODE",
                ".mypy_cache/x.json": "{}",
                "runtime_artifact.json": "{}",
                "pkg/__pycache__/b.pyc": "BYTECODE",
                "scratch_from_llm.py": "print('leaked')",
            },
        )
        audit_result = {"passed_files": ["a.py"], "failed_segments": {}}
        eng = _engine(proj, accepted_files=["a.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={})

        assert (proj / "a.py").read_text(encoding="utf-8") == "A_NEW"
        assert_no_pollution(proj, set(original), approved={"a.py"})


# ----------------------------------------------------------------------
# 2. Одобренный НОВЫЙ файл (манифест зависимостей) — легитимное пополнение.
#    Он ЕДИНСТВЕННОЕ, что добавляется сверх оригинала (плюс .webbles_backups).
# ----------------------------------------------------------------------
def test_approved_new_manifest_is_allowed_but_only_that():
    with tempfile.TemporaryDirectory() as d:
        original = {"app.py": "APP"}
        proj, sandbox = _setup(
            d,
            files_in_proj=dict(original),
            files_in_sandbox={
                "app.py": "APP",
                "requirements.txt": "requests==2.0\n",   # новый, одобрен
                ".mypy_cache/y.json": "{}",               # мусор, НЕ одобрен
            },
        )
        audit_result = {"passed_files": ["requirements.txt"], "failed_segments": {}}
        eng = _engine(proj, accepted_files=["requirements.txt"])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={})

        assert_no_pollution(proj, set(original), approved={"requirements.txt"})
        # Мусор не протёк, даже будучи в sandbox рядом с одобренным файлом.
        assert not (proj / ".mypy_cache").exists()


# ----------------------------------------------------------------------
# 3. Файл, проваливший аудит (failed_segments), не создаётся в проекте,
#    даже если он одновременно числится в accepted_patches (C3-класс).
# ----------------------------------------------------------------------
def test_failed_segment_file_not_materialized_in_project():
    with tempfile.TemporaryDirectory() as d:
        original = {"keep.py": "KEEP"}
        proj, sandbox = _setup(
            d,
            files_in_proj=dict(original),
            files_in_sandbox={
                "keep.py": "KEEP",
                "broken_new.py": "def f(:\n",   # новый + повреждён, аудит завалил
            },
        )
        audit_result = {
            "passed_files": [],
            "failed_segments": {"broken_new.py": [(1, 0, "")]},
        }
        eng = _engine(proj, accepted_files=["broken_new.py"])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={})

        # Повреждённый новый файл НЕ должен материализоваться в проекте
        # через accept-fallback вопреки вердикту аудита.
        assert_no_pollution(proj, set(original), approved=set())
        assert not (proj / "broken_new.py").exists(), (
            "файл из failed_segments не может быть создан в project_path"
        )


# ----------------------------------------------------------------------
# 4. Чистый прогон без единого passed-файла ничего не добавляет и не удаляет.
# ----------------------------------------------------------------------
def test_noop_run_leaves_project_path_set_unchanged():
    with tempfile.TemporaryDirectory() as d:
        original = {"a.py": "A", "b.py": "B", "sub/c.py": "C"}
        proj, sandbox = _setup(
            d,
            files_in_proj=dict(original),
            files_in_sandbox={
                "a.py": "A", "b.py": "B", "sub/c.py": "C",
                "__pycache__/junk.pyc": "X",
            },
        )
        audit_result = {"passed_files": [], "failed_segments": {}}
        eng = _engine(proj, accepted_files=[])
        eng._copy_passed_files_to_original(sandbox, audit_result, restore_status={})
        assert_no_pollution(proj, set(original), approved=set())


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
    print(f"\nno-pollution invariant: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
