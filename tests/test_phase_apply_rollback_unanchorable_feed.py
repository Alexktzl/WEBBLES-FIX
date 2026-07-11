"""
Инцидент 2026-07-04/07 (pytest-homeassistant-custom-component, три прогона):
cycles_run=0, ~1900s каждый сгорели в 43-48 CRITICAL_SYNTAX-откатах (~35s LLM-
генерация на каждый). Fast-fail 3bd4813 (per-file anchor-fail счётчик +
structured-unanchorable NR-гейт в GeneratePatchStage.execute():697) сработал
0 раз: счётчик `_anchor_fail_counts` пополнялся ТОЛЬКО в ветке ПУСТОГО ответа
LLM (generate_patch_stage.py:1646), а реальный burn-путь — непустой патч →
fallback apply мнёт YAML/py → откат в ApplyPatchStage — счётчик не кормил.

Фикс (этот коммит):
  A) ApplyPatchStage._note_unanchorable_attempt кормит ТОТ ЖЕ счётчик из
     rollback-веток самого Apply (apply-fail / AST-invalid / brace-imbalance /
     anchor-loss / project-wide CRITICAL_SYNTAX).
  B) Все rollback-выходы ApplyPatchStage кладут СТАБИЛЬНУЮ (без цифр)
     причину в MetadataKeys.LAST_PATCH_FAILURE — иначе FileAntiLoop
     ("застрял на одной причине", 76a16ca) не видит streak.

Образцы: tests/test_phase_unanchorable_format_fastfail.py,
tests/test_phase_file_anti_loop_stuck_failure_detection.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from unittest.mock import MagicMock  # noqa: E402

from core.contract import MetadataKeys  # noqa: E402
from core.pipeline_context import PipelineContext  # noqa: E402
from core.stages.apply_patch_stage import ApplyPatchStage  # noqa: E402
from core.stages.generate_patch_stage import GeneratePatchStage  # noqa: E402
from core.state_machine import State  # noqa: E402
from fixers.patch_engine import PatchEngine  # noqa: E402
from fixers.structured_edit import Anchor, Edit, EditSet  # noqa: E402


# ---------------------------------------------------------------------------
# Стабы ApplyPatchStage.patch_engine / analyzer для отдельных rollback-веток.
# ---------------------------------------------------------------------------

class _FailApplyEngine:
    """apply_patch не смог заанкорить правку ни в одном файле."""
    last_error_code = ""

    def __init__(self, target_file: str):
        self._target_file = target_file

    def files_in_patch(self, patch):
        return [self._target_file]

    def apply_patch(self, file_path, patch):
        return False


class _AstBreakEngine:
    """apply_patch «применился», но результат синтаксически невалиден."""
    last_error_code = ""

    def __init__(self, target_file: str, broken_text: str):
        self._target_file = target_file
        self._broken = broken_text

    def files_in_patch(self, patch):
        return [self._target_file]

    def apply_patch(self, file_path, patch):
        file_path.write_text(self._broken, encoding="utf-8")
        return True


class _WriteEngine:
    """apply_patch пишет заданный (по умолчанию валидный) контент."""
    last_error_code = ""

    def __init__(self, target_file: str, new_text: str):
        self._target_file = target_file
        self._new_text = new_text

    def files_in_patch(self, patch):
        return [self._target_file]

    def apply_patch(self, file_path, patch):
        file_path.write_text(self._new_text, encoding="utf-8")
        return True


class _CriticalAfterAnalyzer:
    """После патча проектный скан находит новую CRITICAL_SYNTAX ошибку."""
    def __init__(self, target_file: str):
        self._target_file = target_file

    def analyze(self, project_dir, clean_before_each=False, files=None):
        return [{
            "file": self._target_file, "line": 1, "code": "E999",
            "message": "broken", "error_class": "CRITICAL_SYNTAX",
        }]


def _base_ctx(tmp_path, error):
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error).set_patch(
        f"--- a/{error['file']}\n+++ b/{error['file']}\n"
    )
    ctx = ctx.set_errors([])  # before_critical = 0
    return ctx


# ---------------------------------------------------------------------------
# 1. Три отката РАЗНЫМИ ветками по одному файлу -> файл помечен
#    structured-unanchorable (Изменение A).
# ---------------------------------------------------------------------------

def test_three_different_rollback_branches_feed_shared_counter(tmp_path):
    file_name = "shared.py"
    target = tmp_path / file_name
    error = {"file": file_name, "line": 1, "code": "X", "message": "x",
             "error_class": "SECURITY"}

    metadata: dict = {}

    # --- Откат №1: не удалось применить патч ни к одному файлу ---
    target.write_text("x = 1\n", encoding="utf-8")
    stage1 = ApplyPatchStage(patch_engine=_FailApplyEngine(file_name))
    ctx1 = _base_ctx(tmp_path, error).update(metadata=dict(metadata))
    r1 = stage1.execute(ctx1)
    metadata = dict(r1.metadata)
    assert metadata.get("_anchor_fail_counts", {}).get(file_name) == 1
    assert file_name not in (metadata.get("_structured_unanchorable_files") or [])

    # --- Откат №2: AST-invalid после патча ---
    target.write_text("x = 1\n", encoding="utf-8")
    stage2 = ApplyPatchStage(patch_engine=_AstBreakEngine(file_name, "x = 'unterminated\n"))
    ctx2 = _base_ctx(tmp_path, error).update(metadata=dict(metadata))
    r2 = stage2.execute(ctx2)
    metadata = dict(r2.metadata)
    assert metadata.get("_anchor_fail_counts", {}).get(file_name) == 2
    assert file_name not in (metadata.get("_structured_unanchorable_files") or [])

    # --- Откат №3: project-wide новые CRITICAL_SYNTAX ---
    target.write_text("x = 1\n", encoding="utf-8")
    stage3 = ApplyPatchStage(
        patch_engine=_WriteEngine(file_name, "x = 2\n"),
        analyzer=_CriticalAfterAnalyzer(file_name),
    )
    ctx3 = _base_ctx(tmp_path, error).update(metadata=dict(metadata))
    r3 = stage3.execute(ctx3)
    metadata = dict(r3.metadata)
    assert metadata.get("_anchor_fail_counts", {}).get(file_name) == 3
    assert file_name in (metadata.get("_structured_unanchorable_files") or []), (
        "после 3 откатов (разными ветками) файл обязан быть помечен "
        "structured-unanchorable"
    )


# ---------------------------------------------------------------------------
# 2. После пометки — следующая НЕ-синтаксическая ошибка этого файла в
#    GeneratePatchStage уходит в NEEDS_REVIEW БЕЗ вызова llm_client (гейт
#    execute():697, сквозная связка с форматом ключа из ApplyPatchStage).
# ---------------------------------------------------------------------------

def test_unanchorable_marked_file_short_circuits_to_nr_without_llm(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    llm_client = MagicMock()
    stage = GeneratePatchStage(llm_client=llm_client, memory=MagicMock(), patch_engine=PatchEngine())

    error = {"file": "mod.py", "line": 1, "code": "E1", "message": "x", "error_class": ""}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_structured_unanchorable_files": ["mod.py"],
    }))

    result = stage.execute(ctx)

    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "structured_unanchorable_format"
    llm_client.generate_fix.assert_not_called()
    llm_client.generate_structured_fix.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Синтаксические ошибки НЕ глушатся гейтом даже для уже помеченного файла
#    (CRITICAL_SYNTAX уходит в _handle_critical_syntax РАНЬШЕ гейта в
#    execute() — не доходит до fast-fail-NR веткии).
# ---------------------------------------------------------------------------

def test_syntax_errors_not_suppressed_by_unanchorable_gate(tmp_path):
    target = tmp_path / "ci.yml"
    target.write_text("name: ci\n", encoding="utf-8")

    llm_client = MagicMock()
    stage = GeneratePatchStage(llm_client=llm_client, memory=MagicMock(), patch_engine=PatchEngine())

    called = {}

    def _fake_handle_critical_syntax(*args, **kwargs):
        called["invoked"] = True
        ctx = args[0]
        return ctx.add_state_to_history(State.NEEDS_REVIEW)

    stage._handle_critical_syntax = _fake_handle_critical_syntax

    error = {"file": "ci.yml", "line": 1, "code": "E999", "message": "syntax error",
             "error_class": "CRITICAL_SYNTAX"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_structured_unanchorable_files": ["ci.yml"],
        "_anchor_fail_counts": {"ci.yml": 3},
    }))

    result = stage.execute(ctx)

    assert called.get("invoked") is True, (
        "CRITICAL_SYNTAX обязан уйти в _handle_critical_syntax (строка 585, "
        "раньше по коду, чем fast-fail-гейт на 697), а не в NR без LLM"
    )
    llm_client.generate_fix.assert_not_called()
    llm_client.generate_structured_fix.assert_not_called()
    assert result.metadata.get("_needs_review_pending_reason") != "structured_unanchorable_format"


# ---------------------------------------------------------------------------
# 4. Каждый из шести rollback-выходов ApplyPatchStage кладёт непустой
#    LAST_PATCH_FAILURE без цифр (Изменение B — стабильность для FileAntiLoop).
# ---------------------------------------------------------------------------

def _assert_stable_reason(result):
    reason = result.metadata.get(MetadataKeys.LAST_PATCH_FAILURE, "")
    assert reason, "rollback обязан положить непустой LAST_PATCH_FAILURE"
    assert not any(ch.isdigit() for ch in reason), (
        f"причина неудачи обязана быть стабильной (без цифр): {reason!r}"
    )
    return reason


def test_apply_failed_branch_sets_stable_reason(tmp_path):
    file_name = "a.py"
    target = tmp_path / file_name
    target.write_text("x = 1\n", encoding="utf-8")
    error = {"file": file_name, "line": 1, "code": "X", "message": "x", "error_class": ""}
    stage = ApplyPatchStage(patch_engine=_FailApplyEngine(file_name))
    result = stage.execute(_base_ctx(tmp_path, error))
    reason = _assert_stable_reason(result)
    assert "APPLY_FAILED" in reason


def test_ast_invalid_branch_sets_stable_reason(tmp_path):
    file_name = "b.py"
    target = tmp_path / file_name
    target.write_text("x = 1\n", encoding="utf-8")
    error = {"file": file_name, "line": 1, "code": "X", "message": "x", "error_class": ""}
    stage = ApplyPatchStage(patch_engine=_AstBreakEngine(file_name, "x = 'unterminated\n"))
    result = stage.execute(_base_ctx(tmp_path, error))
    reason = _assert_stable_reason(result)
    assert "AST_INVALID" in reason


def test_llm_noise_branch_sets_stable_reason(tmp_path):
    file_name = "c.py"
    target = tmp_path / file_name
    target.write_text("x = 1\n", encoding="utf-8")
    error = {"file": file_name, "line": 1, "code": "X", "message": "x", "error_class": ""}
    # LLM_NOISE_LINE_PATTERNS компилируются БЕЗ re.MULTILINE — "^" анкорится
    # только к самому началу content, поэтому маркер должен быть первой строкой.
    stage = ApplyPatchStage(patch_engine=_WriteEngine(file_name, "# GLOBAL CONTEXT\nx = 1\n"))
    result = stage.execute(_base_ctx(tmp_path, error))
    reason = _assert_stable_reason(result)
    assert "LLM_NOISE" in reason


def test_brace_imbalance_branch_sets_stable_reason(tmp_path):
    file_name = "d.rs"
    target = tmp_path / file_name
    target.write_text("fn foo() {}\n", encoding="utf-8")
    error = {"file": file_name, "line": 1, "code": "X", "message": "x",
             "error_class": "CRITICAL_SYNTAX"}
    stage = ApplyPatchStage(patch_engine=_WriteEngine(file_name, "fn foo() {\n"))
    result = stage.execute(_base_ctx(tmp_path, error))
    reason = _assert_stable_reason(result)
    assert "BRACE_IMBALANCE" in reason


def test_anchor_lost_branch_sets_stable_reason(tmp_path):
    file_name = "e.rs"
    target = tmp_path / file_name
    target.write_text("fn foo() {}\n", encoding="utf-8")
    error = {"file": file_name, "line": 1, "code": "X", "message": "x", "error_class": ""}
    stage = ApplyPatchStage(patch_engine=_WriteEngine(file_name, "// removed\n"))
    result = stage.execute(_base_ctx(tmp_path, error))
    reason = _assert_stable_reason(result)
    assert "ANCHOR_LOST" in reason


def test_error_report_leak_branch_sets_stable_reason(tmp_path):
    file_name = "f.py"
    target = tmp_path / file_name
    target.write_text("x = 1\n", encoding="utf-8")
    error = {"file": file_name, "line": 1, "code": "X", "message": "x", "error_class": ""}
    stage = ApplyPatchStage(
        patch_engine=_WriteEngine(file_name, 'x = 1\nreport = "--- ALL ERRORS somewhere"\n')
    )
    result = stage.execute(_base_ctx(tmp_path, error))
    reason = _assert_stable_reason(result)
    assert "ERROR_REPORT_LEAK" in reason


def test_critical_syntax_project_wide_branch_sets_stable_reason(tmp_path):
    file_name = "g.py"
    target = tmp_path / file_name
    target.write_text("x = 1\n", encoding="utf-8")
    error = {"file": file_name, "line": 1, "code": "X", "message": "x", "error_class": "SECURITY"}
    stage = ApplyPatchStage(
        patch_engine=_WriteEngine(file_name, "x = 2\n"),
        analyzer=_CriticalAfterAnalyzer(file_name),
    )
    result = stage.execute(_base_ctx(tmp_path, error))
    reason = _assert_stable_reason(result)
    assert "CRITICAL_SYNTAX" in reason


# ---------------------------------------------------------------------------
# 5. Успешный structured-патч сбрасывает счётчик файла (существующий reset
#    в GeneratePatchStage._single_llm_attempt:~1603) — сквозная проверка,
#    что файл, накопивший anchor-fail streak, разблокируется после успеха.
# ---------------------------------------------------------------------------

def test_success_resets_anchor_fail_counter_for_file(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("value = 1\n", encoding="utf-8")

    edit = Edit(file="mod.py", anchor=Anchor(line=1, match="value = 1"),
                kind="replace", new="value = 2")
    edit_set = EditSet(intent="bump value", edits=[edit], confidence=0.9, risks=[])

    llm_client = MagicMock()
    llm_client.generate_structured_fix.return_value = edit_set

    stage = GeneratePatchStage(llm_client=llm_client, memory=MagicMock(), patch_engine=PatchEngine())

    error = {"file": "mod.py", "line": 1, "code": "E1", "message": "x", "error_class": ""}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_anchor_fail_counts": {"mod.py": 2},
    }))

    result = stage.execute(ctx)

    assert "mod.py" not in (result.metadata.get("_anchor_fail_counts") or {}), (
        "успешный structured-патч обязан сбросить anchor-fail счётчик файла "
        "(zero-collateral: анкоримые файлы не должны копить порог)"
    )


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))
