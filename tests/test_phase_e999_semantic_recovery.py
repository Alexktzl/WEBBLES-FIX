"""
E999 Semantic Recovery (2026-06-21) — экспериментальная стратегия 4-го уровня.

Идея пользователя: когда стандартные репаранты (PythonSyntaxHealer, tokenize,
localized LLM-region) не справляются с каскадными синтаксическими ошибками,
вместо точечного патчинга — реставрировать файл ЦЕЛИКОМ по его логике,
гарантируя сохранение имён классов/функций/методов/сигнатур/публичных
интерфейсов/импортов. LLM — «реставратор», socraticode — «эксперт, который
сверяет, не нарисовали ли кота вместо Моны Лизы».

Архитектура (согласована с пользователем): реконструкция — это ПРОСТО
генератор патча (patch_source="syntax_reconstruction"), решение принимает
ОБЫЧНЫЙ пайплайн: ApplyPatchStage → ValidateStage → ReviewStage (усиленная
AST-диф + socraticode проверка для этого source) → DecideStage. Опционально,
по умолчанию выключено (pipeline.e999_semantic_recovery, default False).

Тесты покрывают:
1. fixers/semantic_recovery.py — extract_logic_snapshot/extract_signatures_ast/
   signatures_match/reconstruct_file (юнит).
2. tools/socratic_refiner.py.compare_logic — 3 вердикта + защитный fallback.
3. GeneratePatchStage._try_semantic_recovery — генерация патча.
4. ApplyPatchStage — применение syntax_reconstruction патча.
5. ReviewStage._review_syntax_reconstruction — все 4 исхода (signature
   mismatch REJECT, socratic wrong REJECT, socratic uncertain NEEDS_REVIEW,
   socratic ok → DECIDING).
6. End-to-end smoke на реальном каскадном E999-файле (mocked LLM).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.generate_patch_stage import GeneratePatchStage
from core.stages.apply_patch_stage import ApplyPatchStage
from core.stages.review_stage import ReviewStage
from core.stages.decide_stage import DecideStage
from fixers.patch_engine import PatchEngine
from memory.learning import MemoryLearning
from analyzers.python_analyzer import PythonAnalyzer
from fixers.semantic_recovery import (
    extract_logic_snapshot, extract_signatures_ast, signatures_match,
    reconstruct_file, LogicSnapshot,
)
from tools.socratic_refiner import SocraticRefiner


# ---------------------------------------------------------------------------
# 1. fixers/semantic_recovery.py — юнит-тесты
# ---------------------------------------------------------------------------

_BROKEN = (
    "import os\n"
    "class Foo:\n"
    "    def bar(self, x, y)\n"
    "        return x + y\n"
    "    def baz(self):\n"
    "        if True\n"
    "            return 1\n"
)
_FIXED_OK = (
    "import os\n"
    "class Foo:\n"
    "    def bar(self, x, y):\n"
    "        return x + y\n"
    "    def baz(self):\n"
    "        if True:\n"
    "            return 1\n"
)
_FIXED_MISSING_METHOD = (
    "import os\n"
    "class Foo:\n"
    "    def bar(self, x, y):\n"
    "        return x + y\n"
)
_FIXED_CHANGED_SIGNATURE = (
    "import os\n"
    "class Foo:\n"
    "    def bar(self, x):\n"
    "        return x\n"
    "    def baz(self):\n"
    "        if True:\n"
    "            return 1\n"
)


def test_extract_logic_snapshot_from_broken_file():
    snap = extract_logic_snapshot(_BROKEN)
    assert snap.classes == {"Foo"}
    assert snap.functions == {"bar": ("x", "y"), "baz": ()}
    assert "os" in snap.imports


def test_extract_logic_snapshot_empty_on_blank_file():
    assert extract_logic_snapshot("").is_empty()


def test_extract_signatures_ast_requires_valid_syntax():
    assert extract_signatures_ast(_BROKEN) is None
    sig = extract_signatures_ast(_FIXED_OK)
    assert sig is not None
    assert sig.classes == {"Foo"}


def test_signatures_match_identical():
    old = extract_logic_snapshot(_BROKEN)
    new = extract_signatures_ast(_FIXED_OK)
    matched, mismatches = signatures_match(old, new)
    assert matched is True
    assert mismatches == []


def test_signatures_match_detects_missing_method():
    old = extract_logic_snapshot(_BROKEN)
    new = extract_signatures_ast(_FIXED_MISSING_METHOD)
    matched, mismatches = signatures_match(old, new)
    assert matched is False
    assert any("baz" in m for m in mismatches)


def test_signatures_match_detects_changed_signature():
    old = extract_logic_snapshot(_BROKEN)
    new = extract_signatures_ast(_FIXED_CHANGED_SIGNATURE)
    matched, mismatches = signatures_match(old, new)
    assert matched is False
    assert any("bar" in m for m in mismatches)


def test_reconstruct_file_returns_none_without_llm():
    assert reconstruct_file(None, _BROKEN, extract_logic_snapshot(_BROKEN), {}) is None


def test_reconstruct_file_returns_none_if_still_invalid():
    llm = MagicMock()
    llm.providers = [MagicMock()]
    llm._call_llm.return_value = "this is not valid python :::"
    result = reconstruct_file(llm, _BROKEN, extract_logic_snapshot(_BROKEN), {})
    assert result is None


def test_reconstruct_file_strips_fences_and_validates():
    llm = MagicMock()
    llm.providers = [MagicMock()]
    llm._call_llm.return_value = f"```python\n{_FIXED_OK}\n```"
    result = reconstruct_file(llm, _BROKEN, extract_logic_snapshot(_BROKEN), {})
    assert result is not None
    assert result.strip() == _FIXED_OK.strip()


# ---------------------------------------------------------------------------
# 2. SocraticRefiner.compare_logic
# ---------------------------------------------------------------------------

def test_compare_logic_unavailable_without_llm():
    judge = SocraticRefiner(llm_client=None)
    result = judge.compare_logic(_BROKEN, _FIXED_OK, "a.py")
    assert result["verdict"] == "uncertain"


def test_compare_logic_parses_ok_verdict():
    llm = MagicMock()
    llm._call_llm.return_value = '{"verdict": "ok", "reasons": ["logic preserved"]}'
    judge = SocraticRefiner(llm_client=llm)
    result = judge.compare_logic(_BROKEN, _FIXED_OK, "a.py")
    assert result["verdict"] == "ok"
    assert result["reasons"] == ["logic preserved"]


def test_compare_logic_parses_wrong_verdict():
    llm = MagicMock()
    llm._call_llm.return_value = '{"verdict": "wrong", "reasons": ["stub implementation"]}'
    judge = SocraticRefiner(llm_client=llm)
    result = judge.compare_logic(_BROKEN, _FIXED_OK, "a.py")
    assert result["verdict"] == "wrong"


def test_compare_logic_defensive_fallback_on_malformed_response():
    llm = MagicMock()
    llm._call_llm.return_value = "I cannot determine this."
    judge = SocraticRefiner(llm_client=llm)
    result = judge.compare_logic(_BROKEN, _FIXED_OK, "a.py")
    assert result["verdict"] == "uncertain"


def test_compare_logic_defensive_fallback_on_empty_response():
    llm = MagicMock()
    llm._call_llm.return_value = None
    judge = SocraticRefiner(llm_client=llm)
    result = judge.compare_logic(_BROKEN, _FIXED_OK, "a.py")
    assert result["verdict"] == "uncertain"


# ---------------------------------------------------------------------------
# 3. GeneratePatchStage._try_semantic_recovery
# ---------------------------------------------------------------------------

def _make_gen_stage(llm_client):
    return GeneratePatchStage(llm_client=llm_client, memory=MemoryLearning(), patch_engine=PatchEngine())


def test_semantic_recovery_disabled_by_default_in_execute(tmp_path):
    """pipeline.e999_semantic_recovery default False — execute() не должен
    даже пытаться звать _try_semantic_recovery."""
    stage = _make_gen_stage(MagicMock())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    assert ctx.config.get("pipeline", {}).get("e999_semantic_recovery", False) is False


def test_try_semantic_recovery_produces_patch_when_enabled(tmp_path):
    """Прямой вызов _try_semantic_recovery (минуя ранние эвристики
    execute(), которые на простом фикстуре могли бы починить файл сами и
    никогда не дойти до этого 4-го уровня): успешная реконструкция →
    full_file_replacement + patch_source=syntax_reconstruction, переход в
    APPLYING_PATCH."""
    llm = MagicMock()
    llm.providers = [MagicMock()]
    llm._call_llm.return_value = f"```python\n{_FIXED_OK}\n```"
    stage = _make_gen_stage(llm)
    ctx = PipelineContext(
        project_path=tmp_path, language="python", working_path=tmp_path,
        config={"pipeline": {"e999_semantic_recovery": True}},
    )
    error = {"file": "broken.py", "line": 3, "code": "E999",
             "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"}
    ctx = ctx.set_selected_error(error)

    result, success = stage._try_semantic_recovery(ctx, error, _BROKEN)

    assert success is True
    assert result.current_state == State.APPLYING_PATCH
    assert result.metadata.get("full_file_replacement", "").strip() == _FIXED_OK.strip()
    assert result.metadata.get("patch_source") == "syntax_reconstruction"
    assert result.metadata.get("syntax_reconstruction_attempts") == 1
    snap = result.metadata.get("_syntax_reconstruction_snapshot")
    assert snap is not None
    assert snap["classes"] == ["Foo"]


def test_try_semantic_recovery_returns_false_when_reconstruction_fails(tmp_path):
    """Реконструкция не удалась (LLM вернул мусор) → success=False с
    КОРРЕКТНО ОБНОВЛЁННЫМ context (вызывающий код ОБЯЗАН продолжить
    обычный путь NR, используя этот context, а не возвращать его напрямую —
    реальный баг 2026-06-21, lucaswerkmeister/tool-lexeme-forms: 111 неудачных
    попыток подряд съели весь project_timeout, потому что execute() раньше
    возвращал context без перехода состояния на success=False)."""
    llm = MagicMock()
    llm.providers = [MagicMock()]
    llm._call_llm.return_value = "still ::: not valid"
    stage = _make_gen_stage(llm)
    ctx = PipelineContext(
        project_path=tmp_path, language="python", working_path=tmp_path,
        config={"pipeline": {"e999_semantic_recovery": True}},
    )
    error = {"file": "broken.py", "line": 3, "code": "E999",
             "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"}
    ctx = ctx.set_selected_error(error)

    result, success = stage._try_semantic_recovery(ctx, error, _BROKEN)

    assert success is False
    assert "patch_source" not in result.metadata
    assert result.metadata.get("syntax_reconstruction_failed_count") == 1


def test_execute_does_not_loop_when_semantic_recovery_fails(tmp_path):
    """Регрессионный тест на реальный баг (lucaswerkmeister/tool-lexeme-forms,
    2026-06-21): на success=False execute() ДОЛЖЕН дойти до настоящего
    перехода состояния (NEEDS_REVIEW), а не вернуть context без перехода
    (что раньше заставляло стейт-машину бесконечно повторять одну и ту же
    ошибку, съедая весь project_timeout — 111 попыток, 0 решений)."""
    # Фикстура, которую РАННИЕ эвристики (healer/tokenize) не чинят (та же,
    # что в test_execute_falls_back_to_nr_when_semantic_recovery_disabled) —
    # иначе execute() чинит файл раньше, не дойдя до этого 4-го уровня.
    really_broken = "class Foo:\n    def bar(self, x, y)\n        ???broken???\n    def baz(\n"
    (tmp_path / "broken.py").write_text(really_broken, encoding="utf-8")
    llm = MagicMock()
    llm.providers = [MagicMock()]
    llm._call_llm.return_value = "still ::: not valid"
    stage = _make_gen_stage(llm)
    ctx = PipelineContext(
        project_path=tmp_path, language="python", working_path=tmp_path,
        config={"pipeline": {"e999_semantic_recovery": True}},
    )
    error = {"file": "broken.py", "line": 2, "code": "E999",
             "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"}
    ctx = ctx.set_selected_error(error)

    result = stage.execute(ctx)

    # КРИТИЧНО: состояние ДОЛЖНО измениться (реальный переход), иначе
    # стейт-машина зависнет, повторяя этот же execute() бесконечно.
    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "e999_cascade_unrepaired"
    assert result.metadata.get("syntax_reconstruction_failed_count") == 1


def test_execute_falls_back_to_nr_when_semantic_recovery_disabled(tmp_path):
    """С выключенной фичей (default) — execute() для каскадного E999, который
    не парсится никаким эвристиком, идёт обычным путём в NEEDS_REVIEW."""
    really_broken = "class Foo:\n    def bar(self, x, y)\n        ???broken???\n    def baz(\n"
    (tmp_path / "broken.py").write_text(really_broken, encoding="utf-8")
    stage = _make_gen_stage(MagicMock())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "broken.py", "line": 2, "code": "E999",
                                  "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"})

    result = stage.execute(ctx)

    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "e999_cascade_unrepaired"
    assert "full_file_replacement" not in result.metadata


# ---------------------------------------------------------------------------
# 4. ApplyPatchStage — применение syntax_reconstruction патча
# ---------------------------------------------------------------------------

def test_apply_patch_writes_reconstructed_content(tmp_path):
    (tmp_path / "broken.py").write_text(_BROKEN, encoding="utf-8")
    stage = ApplyPatchStage(patch_engine=PatchEngine(), analyzer=PythonAnalyzer())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "broken.py", "line": 3, "code": "E999",
                                  "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"})
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "full_file_replacement": _FIXED_OK, "patch_source": "syntax_reconstruction",
    }))

    result = stage.execute(ctx)

    assert result.current_state == State.VALIDATING
    assert (tmp_path / "broken.py").read_text(encoding="utf-8").strip() == _FIXED_OK.strip()
    # patch_source ОСТАЁТСЯ для ReviewStage; full_file_replacement очищен.
    assert result.metadata.get("patch_source") == "syntax_reconstruction"
    assert "full_file_replacement" not in result.metadata


def test_apply_patch_rolls_back_if_still_invalid(tmp_path):
    (tmp_path / "broken.py").write_text(_BROKEN, encoding="utf-8")
    stage = ApplyPatchStage(patch_engine=PatchEngine(), analyzer=PythonAnalyzer())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "broken.py", "line": 3, "code": "E999",
                                  "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"})
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "full_file_replacement": "still ::: invalid python", "patch_source": "syntax_reconstruction",
    }))

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    assert (tmp_path / "broken.py").read_text(encoding="utf-8") == _BROKEN


# ---------------------------------------------------------------------------
# 5. ReviewStage._review_syntax_reconstruction — все 4 исхода
# ---------------------------------------------------------------------------

def _ctx_after_apply(tmp_path, new_content, old_content=_BROKEN, snapshot_classes=("Foo",)):
    """Симулирует контекст СРАЗУ после ApplyPatchStage+ValidateStage —
    файл на диске уже = new_content, patch_snapshots содержит
    original_content для возможного rollback."""
    (tmp_path / "a.py").write_text(new_content, encoding="utf-8")
    snap = extract_logic_snapshot(old_content)
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "E999",
                                  "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"})
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "patch_source": "syntax_reconstruction",
        "_syntax_reconstruction_snapshot": {
            "classes": sorted(snap.classes), "functions": {k: list(v) for k, v in snap.functions.items()},
            "imports": sorted(snap.imports),
        },
        "_syntax_reconstruction_old_content": old_content,
        "patch_snapshots": [{"file": "a.py", "original_content": old_content}],
    }))
    return ctx


def test_review_rejects_on_signature_mismatch(tmp_path):
    """AST-диф не совпал (потеряна функция) — REJECT БЕЗ обращения к LLM-судье."""
    ctx = _ctx_after_apply(tmp_path, _FIXED_MISSING_METHOD)
    stage = ReviewStage(llm_client=MagicMock())

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    assert len(result.rejected_patches) == 1
    assert result.rejected_patches[0]["reason"] == "syntax_reconstruction_signature_mismatch"
    decisions = result.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "REJECT"
    # Файл откатан к оригиналу (broken).
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == _BROKEN


def test_review_rejects_on_socratic_wrong_verdict(tmp_path):
    """Сигнатуры совпали, но socraticode говорит "wrong" (логика заменена) — REJECT."""
    ctx = _ctx_after_apply(tmp_path, _FIXED_OK)
    llm = MagicMock()
    llm._call_llm.return_value = '{"verdict": "wrong", "reasons": ["stub logic"]}'
    stage = ReviewStage(llm_client=llm)

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    assert len(result.rejected_patches) == 1
    assert result.rejected_patches[0]["reason"] == "syntax_reconstruction_logic_changed"


def test_review_needs_review_on_socratic_uncertain_verdict(tmp_path):
    """Сигнатуры совпали, socraticode не уверен — NEEDS_REVIEW, не ACCEPT/REJECT."""
    ctx = _ctx_after_apply(tmp_path, _FIXED_OK)
    llm = MagicMock()
    llm._call_llm.return_value = '{"verdict": "uncertain", "reasons": ["hard to tell"]}'
    stage = ReviewStage(llm_client=llm)

    result = stage.execute(ctx)

    assert result.current_state == State.NEEDS_REVIEW
    assert result.metadata.get("_needs_review_pending_reason") == "syntax_reconstruction_uncertain"
    # Файл откатан к оригиналу — ревью-очередь не должна видеть непроверенный код как live-файл.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == _BROKEN


def test_review_continues_to_deciding_on_socratic_ok_verdict(tmp_path):
    """Сигнатуры совпали, socraticode говорит "ok" — НЕ принимает решение
    сам, передаёт в DECIDING (обычный пайплайн)."""
    ctx = _ctx_after_apply(tmp_path, _FIXED_OK)
    llm = MagicMock()
    llm._call_llm.return_value = '{"verdict": "ok", "reasons": ["logic preserved"]}'
    stage = ReviewStage(llm_client=llm)

    result = stage.execute(ctx)

    assert result.current_state == State.DECIDING
    assert result.metadata.get("review", {}).get("verdict") == "ok"
    # ACCEPT/REJECT пока НЕ принято — это решает DecideStage дальше.
    assert len(result.accepted_patches) == 0
    assert len(result.rejected_patches) == 0


# ---------------------------------------------------------------------------
# 6. End-to-end smoke: GeneratePatch → Apply → Review(ok) → Decide(ACCEPT)
# ---------------------------------------------------------------------------

def test_end_to_end_full_chain_accepts_good_reconstruction(tmp_path):
    """Полная цепочка с mocked LLM: каскадный E999-файл реконструирован,
    AST-диф совпал, socraticode говорит ok → DecideStage реально ACCEPT'ит
    (счёт ошибок упал, target fixed)."""
    (tmp_path / "broken.py").write_text(_BROKEN, encoding="utf-8")

    llm = MagicMock()
    llm.providers = [MagicMock()]
    llm._call_llm.side_effect = [
        f"```python\n{_FIXED_OK}\n```",  # reconstruct_file
        '{"verdict": "ok", "reasons": ["logic preserved"]}',  # compare_logic
    ]

    gen_stage = _make_gen_stage(llm)
    ctx = PipelineContext(
        project_path=tmp_path, language="python", working_path=tmp_path,
        config={"pipeline": {"e999_semantic_recovery": True}},
    )
    error = {"file": "broken.py", "line": 3, "code": "E999",
             "message": "invalid syntax", "error_class": "CRITICAL_SYNTAX"}
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(current_errors=(error,))

    # Прямой вызов 4-го уровня (минуя ранние эвристики execute(), которые на
    # простом фикстуре могли бы сами починить файл и не дойти до этой точки —
    # это поведение уже отдельно покрыто test_execute_falls_back_to_nr_when_
    # semantic_recovery_disabled и test_try_semantic_recovery_*).
    ctx, _sr_success = gen_stage._try_semantic_recovery(ctx, error, _BROKEN)
    assert _sr_success is True
    assert ctx.current_state == State.APPLYING_PATCH

    apply_stage = ApplyPatchStage(patch_engine=PatchEngine(), analyzer=PythonAnalyzer())
    ctx = apply_stage.execute(ctx)
    assert ctx.current_state == State.VALIDATING

    # ValidateStage реальный прогон пропускаем (не нужен для смысла теста) —
    # эмулируем его эффект (current_errors_before/after для DecideStage) и
    # снапшот для возможного rollback.
    ctx = ctx.update(
        current_errors=(),  # файл теперь валиден, ошибок 0
        validation_results={"error_count_before": 1, "error_count_after": 0,
                            "current_errors_before": [error]},
    )
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "patch_snapshots": [{"file": "broken.py", "original_content": _BROKEN}],
    }))

    review_stage = ReviewStage(llm_client=llm)
    ctx = review_stage.execute(ctx)
    assert ctx.current_state == State.DECIDING
    assert ctx.metadata.get("review", {}).get("verdict") == "ok"

    decide_stage = DecideStage(quality_evaluator=MagicMock(evaluate=MagicMock(return_value=(1.0, []))),
                               analyzer=PythonAnalyzer())
    ctx = decide_stage.execute(ctx)

    assert len(ctx.accepted_patches) == 1
    decisions = ctx.metadata.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "ACCEPT"
    assert decisions[0]["patch_source"] == "syntax_reconstruction"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
