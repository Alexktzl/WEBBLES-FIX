"""
Stage A — regression (F.3-extension).

Покрывает закрытые подзадачи Stage A без тяжёлых импортов
(core.stages → tomlkit не тянем):

  A.3  Единая `error_signature` в core/utils.py — детерминизм, формат,
       нормализация сообщения (числа → '#', пунктуация убрана).
  A.1  Память переживает запуски: MemoryLearning round-trip через файл +
       `.webbles_fix_memory.json` НЕ в списке `_cleanup_artifacts`
       (проверка исходника, без импорта pipeline_engine).
  A.5  Ключевые пакеты имеют `__init__.py` (импорт без sys.path-хаков).

Запуск: python3 tests/test_phase_a_foundation.py
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from core.utils import error_signature, normalize_message
from memory.learning import MemoryLearning

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- A.3: error_signature ------------------------------------------
def test_error_signature():
    e = {"file": "src/main.rs", "code": "E0382", "message": "borrow of moved value `x`"}
    sig = error_signature(e)
    check("sig_deterministic", error_signature(dict(e)) == sig)
    check("sig_has_file_and_code", sig.startswith("src/main.rs::E0382::"))

    # числа в сообщении нормализуются → ошибки, отличающиеся только числами,
    # дают ОДНУ сигнатуру (важно для AntiLoop/Memory).
    a = {"file": "f.rs", "code": "E0277", "message": "expected 3 args, found 2"}
    b = {"file": "f.rs", "code": "E0277", "message": "expected 7 args, found 9"}
    check("sig_number_invariant", error_signature(a) == error_signature(b))

    # различия в file/code/тексте → разные сигнатуры
    check("sig_diff_code",
          error_signature({"file": "f.rs", "code": "E1", "message": "m"})
          != error_signature({"file": "f.rs", "code": "E2", "message": "m"}))
    check("sig_diff_file",
          error_signature({"file": "a.rs", "code": "E1", "message": "m"})
          != error_signature({"file": "b.rs", "code": "E1", "message": "m"}))

    # без кода — формат file::normalized
    nc = error_signature({"file": "x.py", "message": "something broke"})
    check("sig_no_code_format", nc.startswith("x.py::") and "::E" not in nc)

    # error_code как алиас code
    check("sig_error_code_alias",
          "::E0001::" in error_signature({"file": "f", "error_code": "E0001", "message": "m"}))

    # не-dict → ""
    check("sig_non_dict_empty", error_signature(None) == "" and error_signature("x") == "")


def test_normalize_message():
    # числа → '#', затем пунктуация (включая '#') убирается → числа исчезают.
    check("normalize_digits", normalize_message("line 42 col 7") == "line col")
    check("normalize_punct", normalize_message("a, b! c.") == "a b c")
    check("normalize_empty", normalize_message("") == "")


# --- A.1: память переживает запуски --------------------------------
def test_memory_persists():
    with tempfile.TemporaryDirectory() as d:
        mem_file = Path(d) / ".webbles_fix_memory.json"
        m1 = MemoryLearning(mem_file)
        m1.record_success("f.rs::E0382::moved", "--- patch ---", score=0.9,
                          intent="borrow instead of move", confidence=0.9,
                          patch_source="structured_llm")
        # новый инстанс из того же файла — данные на месте (как новая сессия)
        m2 = MemoryLearning(mem_file)
        check("memory_known_fix_survives", m2.get_known_fix("f.rs::E0382::moved") == "--- patch ---")
        similar = m2.get_similar_fixes("f.rs::E0382::moved", n=2)
        check("memory_similar_has_intent",
              len(similar) == 1 and similar[0].get("intent") == "borrow instead of move")
        check("memory_unknown_none", m2.get_known_fix("nope") is None)


def test_memory_not_cleaned():
    src = (REPO / "core" / "pipeline_engine.py").read_text(encoding="utf-8")
    i = src.find("def _cleanup_artifacts")
    check("cleanup_method_exists", i != -1)
    # тело метода до следующего def
    j = src.find("\n    def ", i + 1)
    body = src[i:j if j != -1 else len(src)]
    # проверяем именно СПИСОК artifacts = [ ... ], а не docstring (где файл
    # памяти упомянут как «сохраняем между запусками»).
    k = body.find("artifacts = [")
    end = body.find("]", k)
    artifacts_block = body[k:end + 1] if (k != -1 and end != -1) else ""
    check("memory_not_in_cleanup", artifacts_block and "webbles_fix_memory" not in artifacts_block)
    # sanity: реально нашли список. После P0.2 state-файл унесён в
    # `.webbles_fix/state.json`, а в cleanup осталась только ссылка на
    # legacy-имя через константу `LEGACY_STATE_FILE` — её и проверяем.
    check("cleanup_block_sane",
          ("webbles_fix_state.json" in artifacts_block)
          or ("LEGACY_STATE_FILE" in artifacts_block))


# --- A.5: __init__.py в ключевых пакетах ----------------------------
def test_init_files():
    pkgs = ["analysis", "fixers", "memory", "validation", "planning",
            "safety", "reporters", "analyzers", "core", "tools"]
    missing = [p for p in pkgs if not (REPO / p / "__init__.py").exists()]
    check("init_files_present", not missing)
    if missing:
        print("    missing __init__.py in:", missing)


if __name__ == "__main__":
    print("Stage A — foundation regression:")
    test_error_signature()
    test_normalize_message()
    test_memory_persists()
    test_memory_not_cleaned()
    test_init_files()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage A regression: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
