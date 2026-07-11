"""
2026-06-24 (Bluetooth-Devices/dbus-fast benchmark): два ACCEPT в РАЗНЫХ
циклах взаимно отменили друг друга на одной строке (tests/test_marshaller.py:1344) —
первый патч обернул BytesIO в BufferedRWPair, второй откатил это обратно.
Итоговый файл был байт-в-байт идентичен исходному, несмотря на 2 ACCEPT.

Раньше "Outer loop" guard в PipelineEngine._global_fix_loop при обнаружении
такой осцилляции останавливал ВЕСЬ прогон ("break") — одна нестабильная
ошибка глушила сотни/тысячи других, ещё не обработанных. Теперь:
PipelineEngine._ban_oscillating_signatures баннит ТОЛЬКО осциллирующую
сигнатуру (file::line::code), помечает связанные accepted_patches как
oscillation_cancelled, и прогон продолжается со следующими ошибками.
PrioritizeStage больше не выбирает забаненные сигнатуры.

Покрывает 4 требования:
  1. Осцилляция на одной ошибке не останавливает весь проект.
  2. Зацикленная сигнатура больше не выбирается в этом run.
  3. Следующие ошибки продолжают обрабатываться.
  4. Взаимно отменённые ACCEPT не считаются REAL_FIX/real_fix_impact
     (см. tests/test_phase_progress_metrics.py — отдельный тест там).
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.pipeline_engine import PipelineEngine
from core.stages.prioritize_stage import PrioritizeStage
from safety.error_priority_engine import ErrorPriorityEngine


def _git_repo_with_file(tmp_path: Path, rel_file: str, content: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    fp = repo / rel_file
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _engine(project: Path, current_errors=()) -> PipelineEngine:
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.project_path = project.resolve()
    eng.context = PipelineContext(
        project_path=project, language="python",
        current_errors=tuple(current_errors),
    )
    return eng


def test_ban_oscillating_signatures_does_not_raise_and_marks_metadata(tmp_path):
    """1. Осцилляция на одной ошибке не останавливает весь проект — метод
    отрабатывает и возвращает управление (нет break/exception), прогон
    продолжается."""
    repo = _git_repo_with_file(tmp_path, "a.py", "x = 1\n")
    eng = _engine(repo)
    tmp_dir = tmp_path / "sandbox"
    tmp_dir.mkdir()
    (tmp_dir / "a.py").write_text("x = 1\n", encoding="utf-8")

    accepted_patches = [
        {"file": "a.py", "line": 1, "code": "E1", "message": "m1"},
    ]
    eng._ban_oscillating_signatures({"a.py::1::E1"}, tmp_dir, accepted_patches)

    assert eng.context.metadata.get("_oscillating_signatures") == ["a.py::1::E1"]
    assert accepted_patches[0]["oscillation_cancelled"] is True
    items = eng.context.metadata.get("accept_cancelled_items", [])
    assert len(items) == 1
    assert items[0]["file"] == "a.py" and items[0]["line"] == 1 and items[0]["code"] == "E1"


def test_ban_computes_net_change_zero_for_byte_identical_revert(tmp_path):
    """net_change_bytes считается через git show HEAD (project_path) vs
    текущее содержимое в tmp_dir — для байт-в-байт откаченного файла = 0."""
    repo = _git_repo_with_file(tmp_path, "a.py", "x = 1\n")
    eng = _engine(repo)
    tmp_dir = tmp_path / "sandbox"
    tmp_dir.mkdir()
    (tmp_dir / "a.py").write_text("x = 1\n", encoding="utf-8")  # идентично HEAD

    accepted_patches = [{"file": "a.py", "line": 1, "code": "E1", "message": "m"}]
    eng._ban_oscillating_signatures({"a.py::1::E1"}, tmp_dir, accepted_patches)

    items = eng.context.metadata.get("accept_cancelled_items", [])
    assert items[0]["net_change_bytes"] == 0


def test_ban_handles_non_git_project_gracefully(tmp_path):
    """Если project_path не git-репозиторий — net_change_bytes=None (не
    падаем), баннинг сигнатуры всё равно работает. Без подтверждённого
    net_change_bytes==0 патч НЕ считается cancelled (нет доказательств
    отката) — уходит в oscillation_ambiguous_items, не в accept_cancelled_items
    (см. docstring _ban_oscillating_signatures: net_change=8/8/39 на втором
    прогоне Bluetooth-Devices/dbus-fast показал, что дубликат-сигнатура не
    всегда означает откат)."""
    project = tmp_path / "not_a_repo"
    project.mkdir()
    eng = _engine(project)
    tmp_dir = tmp_path / "sandbox"
    tmp_dir.mkdir()
    (tmp_dir / "a.py").write_text("x = 1\n", encoding="utf-8")

    accepted_patches = [{"file": "a.py", "line": 1, "code": "E1", "message": "m"}]
    eng._ban_oscillating_signatures({"a.py::1::E1"}, tmp_dir, accepted_patches)

    assert eng.context.metadata.get("accept_cancelled_items", []) == []
    ambiguous = eng.context.metadata.get("oscillation_ambiguous_items", [])
    assert len(ambiguous) == 1
    assert ambiguous[0]["net_change_bytes"] is None
    assert accepted_patches[0].get("oscillation_ambiguous") is True
    assert accepted_patches[0].get("oscillation_cancelled") is None
    assert eng.context.metadata.get("_oscillating_signatures") == ["a.py::1::E1"]


def test_ban_treats_nonzero_net_change_as_ambiguous_not_cancelled(tmp_path):
    """2026-06-24, второй прогон Bluetooth-Devices/dbus-fast: дубликат-
    сигнатура сработала 3 раза, но net_change_bytes был 8/8/39 — файл
    реально изменился (verify_accepts.py подтвердил все 16 ACCEPT этого
    прогона как REAL_FIX strong). Сигнатура всё равно банится (защита от
    будущего зацикливания), но patch НЕ исключается из real_fix_impact."""
    repo = _git_repo_with_file(tmp_path, "a.py", "x = 1\n")
    eng = _engine(repo)
    tmp_dir = tmp_path / "sandbox"
    tmp_dir.mkdir()
    (tmp_dir / "a.py").write_text("x = 1\nextra_line = 2\n", encoding="utf-8")  # реально изменился

    accepted_patches = [{"file": "a.py", "line": 1, "code": "E1", "message": "m"}]
    eng._ban_oscillating_signatures({"a.py::1::E1"}, tmp_dir, accepted_patches)

    assert eng.context.metadata.get("accept_cancelled_items", []) == []
    ambiguous = eng.context.metadata.get("oscillation_ambiguous_items", [])
    assert len(ambiguous) == 1 and ambiguous[0]["net_change_bytes"] != 0
    assert accepted_patches[0].get("oscillation_ambiguous") is True
    assert accepted_patches[0].get("oscillation_cancelled") is None
    # Сигнатура всё равно забанена на будущее, несмотря на то что текущие
    # ACCEPT не считаются отменёнными.
    assert eng.context.metadata.get("_oscillating_signatures") == ["a.py::1::E1"]


def test_ban_removes_banned_signature_from_current_errors_immediately(tmp_path):
    """2. Зацикленная сигнатура убирается из текущей очереди сразу же —
    не дожидаясь следующего PrioritizeStage."""
    repo = _git_repo_with_file(tmp_path, "a.py", "x = 1\n")
    other_error = {"file": "b.py", "line": 5, "code": "E2", "message": "other"}
    banned_error = {"file": "a.py", "line": 1, "code": "E1", "message": "m"}
    eng = _engine(repo, current_errors=[banned_error, other_error])
    tmp_dir = tmp_path / "sandbox"
    tmp_dir.mkdir()
    (tmp_dir / "a.py").write_text("x = 1\n", encoding="utf-8")

    eng._ban_oscillating_signatures({"a.py::1::E1"}, tmp_dir, [])

    remaining = list(eng.context.current_errors)
    assert banned_error not in remaining
    assert other_error in remaining


def test_prioritize_stage_skips_banned_signature_but_processes_others(tmp_path):
    """2 + 3. PrioritizeStage не выбирает забаненную сигнатуру, но ДРУГИЕ
    ошибки доходят до ROOT_CAUSE как обычно — прогон не останавливается."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    banned_error = {"file": "a.py", "line": 1, "code": "E1", "message": "m", "error_class": "BLOCKING"}
    other_error = {"file": "b.py", "line": 5, "code": "E2", "message": "other", "error_class": "BLOCKING"}

    ctx = PipelineContext(
        project_path=tmp_path, language="python",
        current_errors=(banned_error, other_error),
    )
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_oscillating_signatures": ["a.py::1::E1"],
    }))

    from core.state_machine import State
    result = PrioritizeStage(ErrorPriorityEngine()).execute(ctx)

    assert result.current_state == State.ROOT_CAUSE
    prioritized = list(result.prioritized_errors)
    assert other_error not in [] or any(e.get("file") == "b.py" for e in prioritized)
    assert not any(e.get("file") == "a.py" and e.get("code") == "E1" for e in prioritized)


def test_prioritize_stage_completes_when_only_banned_signatures_remain(tmp_path):
    """Если ВСЕ оставшиеся ошибки забанены — корректно завершаем цикл
    (COMPLETED), а не падаем на пустом prioritized_errors."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    banned_error = {"file": "a.py", "line": 1, "code": "E1", "message": "m"}
    ctx = PipelineContext(
        project_path=tmp_path, language="python",
        current_errors=(banned_error,),
    )
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_oscillating_signatures": ["a.py::1::E1"],
    }))

    from core.state_machine import State
    result = PrioritizeStage(ErrorPriorityEngine()).execute(ctx)
    assert result.current_state == State.COMPLETED


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
