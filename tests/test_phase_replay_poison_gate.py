"""
O.19-гейт реплея memory/golden + карантин (2026-07-03).

Диагностика 88 детерминированных REJECT (deep-reasoner): memory хранит
патчи, записанные ДО появления guard-ов — в архиве живут патчи с порчей
данных (bcrypt: b"salt"→"salt", приняты в июне). Они реплеились в каждом
прогоне, глушили честный rule_based vector→tuple фиксер (memory стоит
раньше в диспетчеризации) и давали 4 из 6 «живых» отказов. Golden-реплей
вообще не имел pre-apply dry-run.

Фикс: _memory_patch_safe принимает error_code/error_sig, гоняет dry-run
результат через check_data_literal_mangling; отравленный memory-патч
карантинится (MemoryLearning.forget по hash патча).

Запуск: python tests/test_phase_replay_poison_gate.py
"""

from __future__ import annotations

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

from core.stages.generate_patch_stage import GeneratePatchStage  # noqa: E402
from fixers.patch_engine import PatchEngine  # noqa: E402
from memory.learning import MemoryLearning  # noqa: E402

_ORIGINAL = (
    "vectors = [\n"
    "    [\n"
    "        4,\n"
    "        b\"password\",\n"
    "        b\"salt\",\n"
    "    ],\n"
    "]\n"
)

# Отравленный патч: b"salt" → "salt" (порча данных, класс O.19)
_POISON_PATCH = (
    "--- a/vec.py\n"
    "+++ b/vec.py\n"
    "@@ -3,4 +3,4 @@\n"
    "         4,\n"
    "         b\"password\",\n"
    "-        b\"salt\",\n"
    "+        \"salt\",\n"
    "     ],\n"
)

# Честный патч того же файла: не трогает литералы (добавляет комментарий)
_CLEAN_PATCH = (
    "--- a/vec.py\n"
    "+++ b/vec.py\n"
    "@@ -1,3 +1,4 @@\n"
    "+# test vectors\n"
    " vectors = [\n"
    "     [\n"
    "         4,\n"
)

_SIG = "vec.py::list-item::list item has incompatible type"


def _stage_with_memory(mem: MemoryLearning) -> GeneratePatchStage:
    stage = GeneratePatchStage.__new__(GeneratePatchStage)
    stage.patch_engine = PatchEngine()
    stage.memory = mem
    return stage


# --- 1. Отравленный реплей отбраковывается И карантинится ---
def test_poisoned_replay_rejected_and_quarantined():
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "vec.py"
        fp.write_text(_ORIGINAL, encoding="utf-8")

        mem = MemoryLearning(memory_file=None)
        mem.record_success(_SIG, _POISON_PATCH, score=1.0)
        assert mem.get_known_fix(_SIG) == _POISON_PATCH

        stage = _stage_with_memory(mem)
        ok = stage._memory_patch_safe(
            _POISON_PATCH, fp, error_code="list-item", error_sig=_SIG,
        )
        assert ok is False, "патч с порчей bytes-литерала обязан отбраковываться"
        assert mem.get_known_fix(_SIG) is None, (
            "отравленная запись обязана карантиниться — иначе реплей вечен"
        )


# --- 2. Честный реплей проходит, память цела ---
def test_clean_replay_passes():
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "vec.py"
        fp.write_text(_ORIGINAL, encoding="utf-8")

        mem = MemoryLearning(memory_file=None)
        mem.record_success(_SIG, _CLEAN_PATCH, score=1.0)
        stage = _stage_with_memory(mem)
        ok = stage._memory_patch_safe(
            _CLEAN_PATCH, fp, error_code="list-item", error_sig=_SIG,
        )
        assert ok is True, "чистый патч не должен отбраковываться O.19-гейтом"
        assert mem.get_known_fix(_SIG) == _CLEAN_PATCH


# --- 3. Не-data-код: гейт не вмешивается (обратная совместимость) ---
def test_non_data_code_skips_o19_gate():
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "vec.py"
        fp.write_text(_ORIGINAL, encoding="utf-8")
        stage = _stage_with_memory(MemoryLearning(memory_file=None))
        # тот же «порченый» дифф, но код ошибки E501 (вне DATA_MYPY_CODES) —
        # решает не O.19-гейт, а прочие проверки (контекст/скобки ок → True)
        ok = stage._memory_patch_safe(
            _POISON_PATCH, fp, error_code="E501", error_sig=_SIG,
        )
        assert ok is True


# --- 4. forget: точечный карантин по hash не трогает соседние записи ---
def test_forget_targets_specific_patch():
    mem = MemoryLearning(memory_file=None)
    mem.record_success(_SIG, _POISON_PATCH, score=0.9)
    mem.record_success(_SIG, _CLEAN_PATCH, score=1.0)
    removed = mem.forget(_SIG, patch=_POISON_PATCH)
    assert removed == 1
    assert mem.get_known_fix(_SIG) == _CLEAN_PATCH, (
        "карантин по hash обязан сохранить здоровую запись той же сигнатуры"
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
    print(f"\nReplay poison gate: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
