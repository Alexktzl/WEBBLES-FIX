"""
Приоритет детерминированного vector→tuple фиксера над memory/golden в
GeneratePatchStage (2026-07-03, pyca/bcrypt — замер №7).

Инцидент: на первой же ошибке блока тест-векторов (list-of-lists с
гетерогенными рядами) memory-реплей (bracket-flatten: литералы целы →
O.19-гейт молчит) структурно мутировал блок ДО того, как честный
zero-collateral фиксер `_py_list_item_tuple_repair` (стоящий в обычной
диспетчеризации ПОСЛЕ memory) успевал сработать. После мутации строгий
precondition фиксера («все inner — list-литералы») падал для остальных
ошибок блока → они уходили в LLM whack-a-mole, а файл целиком откатывался
финальным аудитом (audit_reverted).

Фикс: для кодов list-item/arg-type даём детерминированному фиксеру приоритет
над memory/golden (_try_vector_repair_preempt в execute()). Он чинит весь
блок за один replace_file, остальные ошибки блока исчезают.

Инвариант, который кодируют эти тесты:
  * vector-блок + list-item/arg-type → patch_source == "rule_based",
    ДАЖЕ если для той же сигнатуры в памяти лежит (отравленный) патч;
  * применённый патч НЕ меняет ни один литерал (мультимножество Constant
    совпадает) и превращает внутренние списки в кортежи;
  * НЕ-vector arg-type (одиночная строка, не list-of-lists) НЕ перехватывается
    — pre-empt возвращает None, управление идёт в обычную цепочку.

Запуск: venv/Scripts/python.exe -m pytest tests/test_phase_vector_repair_preempt.py -v
"""

from __future__ import annotations

import ast
import collections
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.generate_patch_stage import GeneratePatchStage
from fixers.patch_engine import PatchEngine
from memory.learning import MemoryLearning


VECTOR_FILE = '''\
import pytest


@pytest.mark.parametrize(
    ("rounds", "password", "salt", "expected"),
    [
        [
            4,
            b"password",
            b"salt",
            b"\\x5b\\xbf\\x0c\\xc2",
        ],
        [
            # nul bytes
            4,
            b"pass\\x00wor",
            b"sa\\0l",
            b"\\xc2\\xbf\\xfd\\x9d",
        ],
    ],
)
def test_hashpw(rounds, password, salt, expected):
    assert rounds
'''

# НЕ-vector arg-type: одиночный вызов, никаких list-of-lists.
NON_VECTOR_FILE = '''\
import bcrypt


def test_hashpw_str_password():
    bcrypt.checkpw("password", b"$2b$04$abc")
'''


def _constants(src: str) -> "collections.Counter":
    c: "collections.Counter" = collections.Counter()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Constant):
            c[(type(n.value).__name__, n.value)] += 1
    return c


def _vector_line(content: str, needle: str) -> int:
    for i, ln in enumerate(content.splitlines(), start=1):
        if needle in ln:
            return i
    raise AssertionError(f"строка с {needle!r} не найдена")


def _make_stage_and_ctx(tmp_path, content, line, code):
    (tmp_path / "test_bcrypt.py").write_text(content, encoding="utf-8")
    stage = GeneratePatchStage(
        llm_client=MagicMock(), memory=MemoryLearning(), patch_engine=PatchEngine(),
    )
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({
        "file": "test_bcrypt.py", "line": line, "code": code,
        "message": "incompatible type", "error_class": "BLOCKING",
    })
    return stage, ctx


# ---------------------------------------------------------------------------
# Позитив: pre-empt бьёт memory
# ---------------------------------------------------------------------------
def test_vector_argtype_preempts_memory(tmp_path):
    line = _vector_line(VECTOR_FILE, 'b"password"')
    stage, ctx = _make_stage_and_ctx(tmp_path, VECTOR_FILE, line, "arg-type")

    # Засеваем ОТРАВЛЕННУЮ memory-запись для той же сигнатуры: без фикса
    # порядка она выиграла бы гонку диспетчеризации.
    err = ctx.selected_error
    sig = stage._error_signature(err)
    stage.memory.record_success(
        sig, patch="--- a/test_bcrypt.py\n+++ b/test_bcrypt.py\n@@ -1 +1 @@\n-x\n+y\n",
        score=1.0, patch_source="memory", language="python", file_ext=".py",
    )

    result = stage.execute(ctx)

    assert result.current_state == State.APPLYING_PATCH
    assert result.metadata.get("patch_source") == "rule_based", result.metadata
    assert "tuple" in (result.metadata.get("intent", "") or "").lower()


def test_vector_listitem_preempts_memory(tmp_path):
    line = _vector_line(VECTOR_FILE, 'b"sa\\0l"')
    stage, ctx = _make_stage_and_ctx(tmp_path, VECTOR_FILE, line, "list-item")
    err = ctx.selected_error
    sig = stage._error_signature(err)
    stage.memory.record_success(
        sig, patch="--- a/test_bcrypt.py\n+++ b/test_bcrypt.py\n@@ -1 +1 @@\n-x\n+y\n",
        score=1.0, patch_source="memory", language="python", file_ext=".py",
    )

    result = stage.execute(ctx)
    assert result.current_state == State.APPLYING_PATCH
    assert result.metadata.get("patch_source") == "rule_based", result.metadata


def test_preempt_patch_preserves_literals(tmp_path):
    """Применённый pre-empt-патч не меняет ни один литерал и превращает
    внутренние списки в кортежи."""
    line = _vector_line(VECTOR_FILE, 'b"password"')
    stage, ctx = _make_stage_and_ctx(tmp_path, VECTOR_FILE, line, "arg-type")
    result = stage.execute(ctx)
    assert result.current_state == State.APPLYING_PATCH

    patch = result.generated_patch
    assert patch, "pre-empt обязан выставить непустой патч"
    # Применяем патч к реальному файлу на диске (единый движок apply_patch).
    target = tmp_path / "test_bcrypt.py"
    ok = PatchEngine().apply_patch(target, patch)
    assert ok, "патч должен примениться чисто"
    new_content = target.read_text(encoding="utf-8")
    assert new_content != VECTOR_FILE
    # Литералы дословно целы (внутренний O.19-супергард фиксера).
    assert _constants(VECTOR_FILE) == _constants(new_content)
    # Внутренние ряды стали кортежами.
    tree = ast.parse(new_content)
    outers = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.List) and n.elts
        and all(isinstance(e, (ast.List, ast.Tuple)) for e in n.elts)
    ]
    assert outers
    for L0 in outers:
        assert all(isinstance(e, ast.Tuple) for e in L0.elts)


# ---------------------------------------------------------------------------
# Негатив: не-vector arg-type не перехватывается
# ---------------------------------------------------------------------------
def test_non_vector_argtype_not_hijacked(tmp_path):
    stage, ctx = _make_stage_and_ctx(tmp_path, NON_VECTOR_FILE, 5, "arg-type")
    # Прямой вызов helper'а: для не-vector файла должен вернуть None,
    # чтобы управление ушло в обычную цепочку (memory/LLM).
    err = ctx.selected_error
    sig = stage._error_signature(err)
    assert stage._try_vector_repair_preempt(ctx, err, sig) is None


def test_anti_loop_after_net_delta_regression(tmp_path):
    """Если rule_based уже вызвал NET_DELTA-регрессию для этой сигнатуры,
    pre-empt не повторяет идемпотентный патч — возвращает None."""
    line = _vector_line(VECTOR_FILE, 'b"password"')
    stage, ctx = _make_stage_and_ctx(tmp_path, VECTOR_FILE, line, "arg-type")
    err = ctx.selected_error
    sig = stage._error_signature(err)
    md = dict(ctx.metadata)
    md["patch_source"] = "rule_based"
    md["_net_delta_error_retries"] = {sig: 1}
    ctx = ctx.update(metadata=md)
    assert stage._try_vector_repair_preempt(ctx, err, sig) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
