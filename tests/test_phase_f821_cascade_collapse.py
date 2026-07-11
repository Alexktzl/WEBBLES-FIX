"""
Каскадное схлопывание F821-групп (2026-07-02, вариант A утверждённого
дизайна deep-reasoner; тесты дописаны оркестратором после обрыва
fast-worker по session-limit).

Root cause: error_signature (file::code::normalized_message) строко-
независима — N вхождений одного неопределённого имени в файле = одна
сигнатура, но селекция гейтится по process_key (::L{line}) → N dequeue →
N NR-item на ОДИН root-cause (httpx: F821 encoding/elapsed — 7 записей на
2 root-cause). rule_based `_py_f821_typo_fix` детерминирован на None-пути
(функция от file_content+имени, не от строки) → каскадный бан группы
безопасен.

Тестируем через NeedsReviewStage (потребление _nr_group_occurrences,
merge при дедупе) и прямую проверку контракта generate-ветки
(cascade-бан siblings через process_key).

Запуск: python tests/test_phase_f821_cascade_collapse.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext  # noqa: E402
from core.pipeline_stage import PipelineStage  # noqa: E402
from core.stages.needs_review_stage import NeedsReviewStage  # noqa: E402
from core.utils import process_key as _process_key  # noqa: E402


def _err(file, line, name):
    return {"file": file, "line": line, "code": "F821",
            "message": f"undefined name '{name}'"}


def _nr_pass(ctx: PipelineContext, error: dict, occurrences=None) -> PipelineContext:
    """Прогон через NeedsReviewStage так, как это делает generate-ветка
    no_llm_codes: reason + (для каскада) _nr_group_occurrences в metadata."""
    m = dict(ctx.metadata)
    m["_needs_review_pending_reason"] = "no_llm_codes"
    if occurrences:
        m["_nr_group_occurrences"] = list(occurrences)
    ctx = ctx.update(metadata=m).set_selected_error(dict(error))
    return NeedsReviewStage().execute(ctx)


# --- 1. Группа → один item с occurrences; вторая группа — отдельный item ---
def test_groups_collapse_to_single_items_with_occurrences():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = PipelineContext(project_path=work, language="python", working_path=work)

        # root-cause #1: 'encoding' на строках 173 и 617 (представитель — 173)
        ctx = _nr_pass(ctx, _err("httpx/_models.py", 173, "encoding"),
                       occurrences=[173, 617])
        # root-cause #2: 'elapsed' на 563
        ctx = _nr_pass(ctx, _err("httpx/_models.py", 563, "elapsed"),
                       occurrences=[563])

        items = list(ctx.metadata.get("needs_review_items", []))
        assert len(items) == 2, f"две root-cause → два item: {items}"
        by_line = {it["line"]: it for it in items}
        assert by_line[173]["occurrences"] == [173, 617], items
        assert by_line[563]["occurrences"] == [563], items
        # decision-integrity: NR-decisions == items
        nr_dec = [dd for dd in ctx.metadata.get("decisions", [])
                  if dd.get("decision") == "NEEDS_REVIEW"]
        assert len(nr_dec) == 2, nr_dec
        assert ctx.metadata.get("needs_review_count") == 2


# --- 2. Повторный проход той же группы (другой представитель) → merge, не дубль ---
def test_same_group_from_other_representative_merges():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = PipelineContext(project_path=work, language="python", working_path=work)
        ctx = _nr_pass(ctx, _err("m.py", 10, "zzqjx"), occurrences=[10, 20])
        # тот же error_sig, но представитель — строка 20, и occurrences пришли шире
        ctx = _nr_pass(ctx, _err("m.py", 20, "zzqjx"), occurrences=[20, 30])

        items = list(ctx.metadata.get("needs_review_items", []))
        assert len(items) == 1, f"группа обязана слиться в один item: {items}"
        assert items[0]["occurrences"] == [10, 20, 30], items
        assert ctx.metadata.get("needs_review_count") == 1
        nr_dec = [dd for dd in ctx.metadata.get("decisions", [])
                  if dd.get("decision") == "NEEDS_REVIEW"]
        assert len(nr_dec) == 1, "merge не должен давать второй NR-decision"


# --- 3. Одно имя в РАЗНЫХ файлах — отдельные item (сигнатура включает файл) ---
def test_same_name_different_files_not_merged():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = PipelineContext(project_path=work, language="python", working_path=work)
        ctx = _nr_pass(ctx, _err("a.py", 5, "encoding"), occurrences=[5])
        ctx = _nr_pass(ctx, _err("b.py", 7, "encoding"), occurrences=[7])
        items = list(ctx.metadata.get("needs_review_items", []))
        assert len(items) == 2, items


# --- 4. Не-каскадные (W503-стиль, синглтон occurrences) не схлопываются между строками ---
def test_singleton_occurrences_lines_stay_separate():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = PipelineContext(project_path=work, language="python", working_path=work)
        w = {"file": "m.py", "code": "W503", "message": "line break before binary operator"}
        ctx = _nr_pass(ctx, {**w, "line": 3})   # без occurrences → синглтон [3]
        ctx = _nr_pass(ctx, {**w, "line": 9})   # синглтон [9]
        items = list(ctx.metadata.get("needs_review_items", []))
        assert len(items) == 2, (
            f"построчные no_llm-коды не должны ложно схлопываться: {items}"
        )


# --- 5. occurrences доезжают до JSON-payload на диске, has_patch=False ---
def test_occurrences_in_disk_payload():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = PipelineContext(project_path=work, language="python", working_path=work)
        ctx = _nr_pass(ctx, _err("m.py", 10, "zzqjx"), occurrences=[10, 20])
        items = list(ctx.metadata.get("needs_review_items", []))
        saved = Path(items[0]["saved_path"])
        assert saved.exists(), items
        payload = json.loads(saved.read_text(encoding="utf-8"))
        assert payload.get("occurrences") == [10, 20], payload
        assert payload.get("has_patch") is False, payload


# --- 6. Контракт generate-ветки: siblings банятся через process_key (::L) ---
def test_generate_branch_contract_process_key_ban():
    """Прямой контракт-тест логики каскадного бана (та же формула, что в
    generate_patch_stage): для каждого sibling пишется process_key С линией —
    ключ, который читает селекция root_cause_stage."""
    siblings = [_err("m.py", 10, "zzqjx"), _err("m.py", 20, "zzqjx")]
    sig0 = PipelineStage._static_signature(siblings[0])
    sig1 = PipelineStage._static_signature(siblings[1])
    assert sig0 == sig1, "root-cause ключ (сигнатура без строки) обязан совпасть"
    k0, k1 = _process_key(siblings[0]), _process_key(siblings[1])
    assert k0 != k1 and "::L10" in k0 and "::L20" in k1, (k0, k1)


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
    print(f"\nF821 cascade collapse: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
