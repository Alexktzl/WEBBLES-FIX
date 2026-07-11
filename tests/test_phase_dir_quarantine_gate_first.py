"""
Гейт dir-карантина стоит в НАЧАЛЕ GeneratePatchStage.execute — раньше
детерминированных путей (2026-07-08, series5_loguru5).

Инцидент: гейт стоял ниже rule_based/memory-путей, и корпусные F841/E712
чинились rule_based-фиксером В ОБХОД карантина — каждый такой патч жёг
полный цикл Apply→full-scan→rollback (~15s), 115 откатов ≈ весь бюджет
прогона при cycles_run=0. Карантин означает «сломанные ДАННЫЕ, не чинить
НИЧЕМ» — ни LLM, ни rule-based, ни синтаксис-ремонтом.

Инвариант: ошибка из карантинного каталога уходит в NEEDS_REVIEW ДО любых
попыток генерации (llm_client не вызывается, memory не опрашивается,
патч в контексте не появляется).

Запуск: python tests/test_phase_dir_quarantine_gate_first.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from core.anti_loop import FileAntiLoop  # noqa: E402
from core.pipeline_context import PipelineContext  # noqa: E402
from core.stages.generate_patch_stage import GeneratePatchStage  # noqa: E402
from core.state_machine import State  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def _quarantined_stage():
    stage = GeneratePatchStage.__new__(GeneratePatchStage)
    stage.llm_client = MagicMock()
    stage.memory = MagicMock()
    fal = FileAntiLoop()
    for f in ("tests/data/a.py", "tests/data/b.py", "tests/data/c.py"):
        for _ in range(3):
            fal.record_attempt(f, 1, failure_reason="SAME: stable")
    stage.file_anti_loop = fal
    return stage


def test_gate_method_returns_nr_before_any_generation():
    """_dir_quarantine_gate: ошибка карантинного каталога (rule_based-чинимый
    F841!) → NEEDS_REVIEW; llm_client и memory НЕ тронуты."""
    stage = _quarantined_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    error = {"file": "tests/data/d.py", "line": 5, "code": "F841",
             "message": "local variable assigned but never used"}
    out = stage._dir_quarantine_gate(ctx, error)
    check("gate_returned_context", out is not None)
    check("gate_state_needs_review",
          out.state_history and out.state_history[-1] == State.NEEDS_REVIEW,
          f"states: {out.state_history}")
    check("gate_reason_set",
          out.metadata.get("_needs_review_pending_reason") == "dir_quarantined_fixture_corpus")
    check("llm_untouched", stage.llm_client.mock_calls == [])
    check("memory_untouched", stage.memory.mock_calls == [])


def test_gate_none_for_clean_dir():
    stage = _quarantined_stage()
    ctx = PipelineContext(project_path=Path("."), language="python")
    error = {"file": "src/main.py", "line": 1, "code": "F401", "message": "unused import"}
    check("clean_dir_passes", stage._dir_quarantine_gate(ctx, error) is None)


def test_gate_called_at_top_of_execute():
    """Позиция в execute: вызов _dir_quarantine_gate идёт РАНЬШЕ первого
    обращения к memory/known_fix/rule_based (грубая проверка по индексам
    в исходнике — кодирует порядок, а не реализацию)."""
    src = (ROOT / "core" / "stages" / "generate_patch_stage.py").read_text(
        encoding="utf-8", errors="replace")
    exec_start = src.index("def execute(")
    gate_pos = src.index("_dir_quarantine_gate(context", exec_start)
    memory_pos = src.index("get_known_fix", exec_start)
    check("gate_before_memory_path", gate_pos < memory_pos,
          f"gate@{gate_pos} vs memory@{memory_pos}")


if __name__ == "__main__":
    test_gate_method_returns_nr_before_any_generation()
    test_gate_none_for_clean_dir()
    test_gate_called_at_top_of_execute()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"dir_quarantine_gate_first: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
