"""
Расследование REJECT-аномалии empty_response для локальной LLM (2026-06-22).

121 из 146 REJECT в контрольной серии 20 проектов были одной размытой
причиной "empty_response", смешивающей семь РАЗНЫХ ситуаций:
  - LLM не ответил вовсе (timeout / transport_error / circuit-breaker)
  - LLM ответил пустой строкой без ошибки (empty)
  - LLM ответил не-JSON текстом (bad_format)
  - LLM ответил валидным JSON, но не EditSet-схемой (parser_fail)
  - LLM предложил правку с anchor.match == "" — структурный баг, не
    содержимое (anchor_empty, см. W291-расследование той же сессии)
  - LLM предложил правку с anchor.match, которого нет в файле — вероятная
    галлюцинация содержимого (anchor_mismatch)
  - правки сошлись по anchor, но итоговый текст не изменился / файла нет
    в контексте (diff_fail)

Эти тесты проверяют классификацию на каждом уровне и маршрутизацию:
timeout/transport_error/empty/bad_format/parser_fail — НЕ REJECT (нет
патча для оценки, инфраструктурный сбой); anchor_empty/anchor_mismatch/
diff_fail — настоящий REJECT (LLM предложил конкретную правку, мы её
оценили и отклонили).
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.generate_patch_stage import GeneratePatchStage
from fixers.llm_client import LLMClient
from fixers.patch_engine import PatchEngine
from fixers.structured_edit import Anchor, Edit, EditSet
from memory.learning import MemoryLearning


# ---------------------------------------------------------------------------
# Уровень 1: классификация ошибки вызова LLM (timeout / transport_error)
# ---------------------------------------------------------------------------

def test_classify_hard_timeout():
    assert LLMClient._classify_call_err("hard_timeout_180s") == "timeout"


def test_classify_llm_unresponsive():
    assert LLMClient._classify_call_err("llm_unresponsive") == "timeout"


def test_classify_transport_error():
    assert LLMClient._classify_call_err("ConnectionError: refused") == "transport_error"


def test_classify_none_err_is_empty():
    assert LLMClient._classify_call_err(None) == "empty"


# ---------------------------------------------------------------------------
# Уровень 2: EditSet.from_json — bad_format vs parser_fail vs empty
# ---------------------------------------------------------------------------

def test_from_json_empty_raw():
    diag = {}
    assert EditSet.from_json("", diag=diag) is None
    assert diag["reason"] == "empty"


def test_from_json_bad_format_non_json_text():
    diag = {}
    assert EditSet.from_json("I cannot help with that request.", diag=diag) is None
    assert diag["reason"] == "bad_format"


def test_from_json_parser_fail_valid_json_wrong_schema():
    diag = {}
    assert EditSet.from_json('{"foo": "bar"}', diag=diag) is None
    assert diag["reason"] == "parser_fail"


def test_from_json_parser_fail_no_valid_edits():
    diag = {}
    payload = json.dumps({"intent": "x", "edits": [{"file": "", "anchor": {}, "kind": "replace"}]})
    assert EditSet.from_json(payload, diag=diag) is None
    assert diag["reason"] == "parser_fail"


def test_from_json_success_no_diag_needed():
    payload = json.dumps({
        "intent": "fix", "confidence": 0.9,
        "edits": [{"file": "a.py", "anchor": {"line": 1, "match": "x = 1"},
                    "kind": "replace", "new": "x = 2\n"}],
    })
    es = EditSet.from_json(payload)
    assert es is not None
    assert len(es.edits) == 1


# ---------------------------------------------------------------------------
# Уровень 3: EditSet.to_unified_diff — anchor_empty / anchor_mismatch / diff_fail
# ---------------------------------------------------------------------------

def test_to_unified_diff_anchor_empty():
    es = EditSet(edits=[Edit(file="a.py", anchor=Anchor(line=2, match="   "),
                               kind="replace", new="y = 2\n")])
    diag = {}
    assert es.to_unified_diff({"a.py": "x = 1\n   \nz = 3\n"}, diag=diag) is None
    assert diag["reason"] == "anchor_empty"


def test_to_unified_diff_anchor_mismatch():
    es = EditSet(edits=[Edit(file="a.py", anchor=Anchor(line=1, match="this text is not in the file"),
                               kind="replace", new="y = 2\n")])
    diag = {}
    assert es.to_unified_diff({"a.py": "x = 1\n"}, diag=diag) is None
    assert diag["reason"] == "anchor_mismatch"


def test_to_unified_diff_diff_fail_no_op():
    """anchor сошёлся, но new совпадает с уже существующей строкой — нет
    реального изменения."""
    es = EditSet(edits=[Edit(file="a.py", anchor=Anchor(line=1, match="x = 1"),
                               kind="replace", new="x = 1\n")])
    diag = {}
    assert es.to_unified_diff({"a.py": "x = 1\n"}, diag=diag) is None
    assert diag["reason"] == "diff_fail"


def test_to_unified_diff_diff_fail_missing_content():
    es = EditSet(edits=[Edit(file="missing.py", anchor=Anchor(line=1, match="x"),
                               kind="replace", new="y\n")])
    diag = {}
    assert es.to_unified_diff({"a.py": "x = 1\n"}, diag=diag) is None
    assert diag["reason"] == "diff_fail"


def test_to_unified_diff_success_no_diag_needed():
    es = EditSet(edits=[Edit(file="a.py", anchor=Anchor(line=1, match="x = 1"),
                               kind="replace", new="x = 2\n")])
    diff = es.to_unified_diff({"a.py": "x = 1\n"})
    assert diff is not None
    assert "x = 2" in diff


# ---------------------------------------------------------------------------
# Уровень 4: маршрутизация в GeneratePatchStage — REJECT vs infra-счётчик
# ---------------------------------------------------------------------------

def _make_stage():
    return GeneratePatchStage(llm_client=MagicMock(), memory=MemoryLearning(),
                               patch_engine=PatchEngine())


@pytest.mark.parametrize("category", [
    "timeout", "transport_error", "empty", "bad_format", "parser_fail",
])
def test_infra_categories_not_rejected(tmp_path, category):
    stage = _make_stage()
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "X", "message": "m"})
    result = stage._record_llm_failure(ctx, ctx.selected_error, category)
    assert len(result.rejected_patches) == 0
    assert result.metadata["llm_infra_failure_count"] == 1
    assert result.metadata["llm_infra_failure_items"][0]["category"] == category


@pytest.mark.parametrize("category", ["anchor_empty", "anchor_mismatch", "diff_fail"])
def test_reject_categories_are_rejected(tmp_path, category):
    stage = _make_stage()
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "X", "message": "m"})
    result = stage._record_llm_failure(ctx, ctx.selected_error, category)
    assert len(result.rejected_patches) == 1
    assert result.rejected_patches[0]["reason"] == f"empty_response_{category}"
    assert result.metadata.get("llm_infra_failure_count", 0) == 0


# ---------------------------------------------------------------------------
# Уровень 5: сырой ответ модели сохраняется для отладки
# ---------------------------------------------------------------------------

def test_save_llm_debug_writes_jsonl(tmp_path, monkeypatch):
    from core.pipeline_engine import PipelineEngine
    debug_dir = tmp_path / "llm_debug_test"
    monkeypatch.setattr(PipelineEngine, "_project_llm_debug_dir", classmethod(lambda cls, p: debug_dir))

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    error = {"file": "a.py", "line": 5, "code": "X", "message": "m"}
    GeneratePatchStage._save_llm_debug(ctx, error, "anchor_mismatch", "structured_diff", "raw model output")

    log_file = debug_dir / "llm_debug.jsonl"
    assert log_file.exists()
    rec = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["category"] == "anchor_mismatch"
    assert rec["stage"] == "structured_diff"
    assert rec["raw_response"] == "raw model output"
    assert rec["file"] == "a.py"


def test_save_llm_debug_truncates_long_response(tmp_path, monkeypatch):
    from core.pipeline_engine import PipelineEngine
    debug_dir = tmp_path / "llm_debug_test2"
    monkeypatch.setattr(PipelineEngine, "_project_llm_debug_dir", classmethod(lambda cls, p: debug_dir))

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    error = {"file": "a.py", "line": 1, "code": "X", "message": "m"}
    long_text = "x" * 10000
    GeneratePatchStage._save_llm_debug(ctx, error, "empty", "structured_call", long_text)

    rec = json.loads((debug_dir / "llm_debug.jsonl").read_text(encoding="utf-8").strip())
    assert len(rec["raw_response"]) == 4000


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
