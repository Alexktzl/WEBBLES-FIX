"""
Control series 2026-06-21, находка #1 (продолжение) — обнаружено в живой
серии из 10 проектов (decision-integrity verification), репро на
tdaira/blerpc: встроенная сверка (_check_decision_integrity) поймала
расхождение REJECT: decisions[]=2 vs rejected_patches=3, gap=1, не
объяснённый известным invalid_context-bypass.

Точная причина: GeneratePatchStage имел ДВА пути, принимающих финальное
решение (REJECT/ACCEPT) и пишущих в accepted_patches/rejected_patches
напрямую, минуя append_decision_log (DecideStage не вызывается для них
вовсе — решение принимается раньше, на этапе генерации патча):
1. `invalid_context` (строка ~279) — файл ошибки не существует на диске.
2. `empty_response` (строка ~1153) — LLM не вернул патч ни структурным, ни
   legacy-путём (репродуцировано живьём: tdaira/blerpc,
   central_rn/android/app/src/main/AndroidManifest.xml).

Плюс отдельно найден (не в этой серии, при аудите всех add_accepted_patch/
add_rejected_patch вызовов) третий путь — SyntaxRepairStage (pre-pipeline
pass для E999, выполняется ДО первого DecideStage.execute()) — ACCEPT без
логирования.

Все три закрыты явными вызовами append_decision_log в соответствующих
точках принятия решения.

При продолжении той же серии (10 проектов) на nuclear-treestump/pydepgate
найден ГОРАЗДО более крупный разрыв: decisions[]=1 NEEDS_REVIEW vs
needs_review_items=109. Локализовано: GeneratePatchStage имеет ДВА
дополнительных прямых перехода в State.NEEDS_REVIEW, минуя DecideStage
полностью (он вообще не вызывается для этих случаев):
4. `no_llm_codes` (строка ~428) — F821/W503/W504, rule-based fix не сработал,
   решение пропустить LLM и сразу уйти в NR. Самый частый код-путь — основной
   источник разрыва на 1442-error проекте.
5. `e999_cascade_unrepaired` (строка ~1585) — CRITICAL_SYNTAX, все эвристики
   репарации не сработали, файл всё ещё не парсится — представительная
   ошибка файла уходит в NR (остальные ошибки того же файла bulk-skip'аются
   через processed_errors, не идут в decisions[] по дизайну — это НЕ те же
   109, считаются по-другому).
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.generate_patch_stage import GeneratePatchStage
from core.stages.syntax_repair_stage import SyntaxRepairStage
from fixers.patch_engine import PatchEngine
from memory.learning import MemoryLearning


def test_invalid_context_reject_logs_decision(tmp_path):
    """Файл ошибки не существует на диске → REJECT(invalid_context),
    решение принимается ДО DecideStage — должно попасть в decisions[]."""
    stage = GeneratePatchStage(llm_client=MagicMock(), memory=MemoryLearning(),
                               patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "does_not_exist.py", "line": 1, "code": "E1",
                                  "message": "x", "error_class": "UNKNOWN"})

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    assert len(result.rejected_patches) == 1
    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "REJECT"
    assert decisions[0]["reason"] == "invalid_context"


def test_empty_llm_response_infra_failure_not_rejected(tmp_path):
    """LLM не вернул патч вовсе (ни structured, ни legacy) — репродуцировано
    живьём на tdaira/blerpc. До 2026-06-22 это логировалось как REJECT
    (empty_response), инфлируя REJECT-счётчик случаями без единого патча
    для оценки. empty_response диагностика (расследование REJECT-аномалии
    для локальной LLM): категория "empty" — инфраструктурный сбой, идёт в
    llm_infra_failure_count/_items, НЕ в rejected_patches/decisions[]."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    llm = MagicMock()
    llm.generate_structured_fix.return_value = None
    llm.last_failure_category = "empty"
    llm.last_raw_response = None
    llm.generate_fix.return_value = ""

    stage = GeneratePatchStage(llm_client=llm, memory=MemoryLearning(), patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "ZZZ999",
                                  "message": "custom", "error_class": "UNKNOWN"})

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    assert len(result.rejected_patches) == 0
    assert result.metadata.get("llm_infra_failure_count") == 1
    assert result.metadata["llm_infra_failure_items"][0]["category"] == "empty"
    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 0


def test_llm_timeout_infra_failure_not_rejected(tmp_path):
    """Категория "timeout" (LLM hard timeout) — тот же инфраструктурный
    путь, что "empty": НЕ настоящий REJECT."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    llm = MagicMock()
    llm.generate_structured_fix.return_value = None
    llm.last_failure_category = "timeout"
    llm.last_raw_response = None
    llm.generate_fix.return_value = ""

    stage = GeneratePatchStage(llm_client=llm, memory=MemoryLearning(), patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "ZZZ999",
                                  "message": "custom", "error_class": "UNKNOWN"})

    result = stage.execute(ctx)

    assert len(result.rejected_patches) == 0
    assert result.metadata.get("llm_infra_failure_count") == 1
    assert result.metadata["llm_infra_failure_items"][0]["category"] == "timeout"
    assert len(result.metadata.get("decisions", [])) == 0


def test_llm_anchor_mismatch_still_rejected(tmp_path):
    """LLM ДЕЙСТВИТЕЛЬНО предложил EditSet (валидный JSON, непустые правки),
    но anchor.match не найден в реальном файле (галлюцинация содержимого) —
    это ЕСТЬ что оценивать и отклонять, остаётся настоящим REJECT (категория
    empty_response_anchor_mismatch), в отличие от "нет ответа вовсе"."""
    from fixers.structured_edit import EditSet, Edit, Anchor

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    bogus_edit_set = EditSet(
        intent="fix",
        edits=[Edit(file="a.py", anchor=Anchor(line=1, match="this text does not exist"),
                     kind="replace", new="y = 2\n")],
        confidence=0.5,
    )
    llm = MagicMock()
    llm.generate_structured_fix.return_value = bogus_edit_set
    llm.last_failure_category = None
    llm.last_raw_response = '{"intent":"fix","edits":[...]}'
    llm.generate_fix.return_value = ""

    stage = GeneratePatchStage(llm_client=llm, memory=MemoryLearning(), patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "ZZZ999",
                                  "message": "custom", "error_class": "UNKNOWN"})

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    assert len(result.rejected_patches) == 1
    assert result.rejected_patches[0]["reason"] == "empty_response_anchor_mismatch"
    assert result.metadata.get("llm_infra_failure_count", 0) == 0
    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "REJECT"
    assert decisions[0]["reason"] == "empty_response_anchor_mismatch"


def test_syntax_repair_stage_accept_logs_decision(tmp_path):
    """Pre-pipeline E999-репарация (выполняется ДО первого
    DecideStage.execute()) — ACCEPT должен попасть в decisions[]."""
    (tmp_path / "broken.py").write_text("x = [1, 2, 3\n", encoding="utf-8")
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)

    new_ctx, accepted, n_failed = SyntaxRepairStage().execute(ctx, tmp_path, llm_client=None)

    assert n_failed == 0
    assert len(accepted) == 1
    assert len(new_ctx.accepted_patches) == 1
    decisions = new_ctx.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "ACCEPT"
    assert decisions[0]["reason"] == "syntax_repair_stage:tokenize_bracket"


def test_no_llm_codes_needs_review_logs_decision(tmp_path):
    """F821/W503/W504 → NR без LLM (no_llm_codes) — главный источник разрыва
    decisions[]=1 vs needs_review_items=109 на nuclear-treestump/pydepgate.

    После структурного рефакторинга (2026-06-21) GeneratePatchStage только
    устанавливает metadata["_needs_review_pending_reason"] и переходит в
    State.NEEDS_REVIEW; реальное логирование — внутри NeedsReviewStage.
    execute() (единственная точка), которую тест вызывает явно, как сделал
    бы state-machine dispatcher."""
    (tmp_path / "a.py").write_text("x = undefined_name\n", encoding="utf-8")
    stage = GeneratePatchStage(llm_client=MagicMock(), memory=MemoryLearning(),
                               patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "F821",
                                  "message": "undefined name", "error_class": "UNKNOWN"})

    result = stage.execute(ctx)

    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "no_llm_codes"

    from core.stages.needs_review_stage import NeedsReviewStage
    result = NeedsReviewStage().execute(result)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "NEEDS_REVIEW"
    assert decisions[0]["reason"] == "no_llm_codes"


def test_e999_cascade_unrepaired_needs_review_logs_decision(tmp_path):
    """CRITICAL_SYNTAX, все эвристики репарации не сработали → представительная
    NR-ошибка файла должна попасть в decisions[] (через NeedsReviewStage,
    см. комментарий в test_no_llm_codes_needs_review_logs_decision)."""
    (tmp_path / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    stage = GeneratePatchStage(llm_client=MagicMock(), memory=MemoryLearning(),
                               patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "broken.py", "line": 1, "code": "E999",
                                  "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"})

    result = stage.execute(ctx)

    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "e999_cascade_unrepaired"

    from core.stages.needs_review_stage import NeedsReviewStage
    result = NeedsReviewStage().execute(result)

    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "NEEDS_REVIEW"
    assert decisions[0]["reason"] == "e999_cascade_unrepaired"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
