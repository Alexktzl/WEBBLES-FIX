"""
Regress: фильтр применимости memory-патчей по language / file_ext.

Защищает от:
  - реплея rust-патча в python-файле (и наоборот) при коллизии сигнатур
    (одинаковый file/code/msg в разных языковых проектах),
  - использования «безродных» legacy-записей (без language-тэга) в строгом
    языковом контексте.

Запуск: python3 tests/test_memory_language_filter.py
"""

import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.learning import MemoryLearning


def _mk_mem():
    return MemoryLearning(memory_file=None)


def test_get_known_fix_no_filter_back_compat():
    """Без language-фильтра — поведение как раньше: возвращаем что записано."""
    m = _mk_mem()
    m.record_success("sig1", "PATCH-A\n", score=1.0)
    assert m.get_known_fix("sig1") == "PATCH-A\n"


def test_get_known_fix_language_match():
    m = _mk_mem()
    m.record_success("sig1", "RUST-PATCH\n", score=1.0, language="rust", file_ext=".rs")
    assert m.get_known_fix("sig1", language="rust") == "RUST-PATCH\n"
    assert m.get_known_fix("sig1", language="rust", file_ext=".rs") == "RUST-PATCH\n"


def test_get_known_fix_language_mismatch():
    """Rust-патч не отдаётся для python-запроса."""
    m = _mk_mem()
    m.record_success("sig1", "RUST-PATCH\n", score=1.0, language="rust", file_ext=".rs")
    assert m.get_known_fix("sig1", language="python") is None
    assert m.get_known_fix("sig1", language="rust", file_ext=".py") is None


def test_get_known_fix_untagged_skipped_with_filter():
    """Legacy запись без тэгов — пропускается при наличии фильтра."""
    m = _mk_mem()
    m.record_success("sig1", "LEGACY-NOTAG\n", score=1.0)  # без language/file_ext
    assert m.get_known_fix("sig1") == "LEGACY-NOTAG\n"          # без фильтра ок
    assert m.get_known_fix("sig1", language="rust") is None     # с фильтром скип
    assert m.get_known_fix("sig1", file_ext=".rs") is None


def test_get_known_fix_picks_matching_over_first():
    """Если best-score запись не подходит по языку — берётся следующая
    подходящая, а не None."""
    m = _mk_mem()
    m.record_success("sig1", "RUST\n", score=0.99, language="rust", file_ext=".rs")
    m.record_success("sig1", "PY\n", score=0.50, language="python", file_ext=".py")
    # Для rust-запроса — даже если python-запись имеет более низкий score,
    # должна вернуться rust (по фильтру).
    assert m.get_known_fix("sig1", language="rust") == "RUST\n"
    # Для python-запроса — python запись (rust пропускается, хоть и выше score).
    assert m.get_known_fix("sig1", language="python") == "PY\n"


def test_get_similar_fixes_filter():
    m = _mk_mem()
    m.record_success("sig1", "R1\n", score=0.9, language="rust")
    m.record_success("sig1", "R2\n", score=0.8, language="rust")
    m.record_success("sig1", "P1\n", score=0.95, language="python")
    rust_sims = m.get_similar_fixes("sig1", n=5, language="rust")
    assert len(rust_sims) == 2 and all(e.get("language") == "rust" for e in rust_sims)
    py_sims = m.get_similar_fixes("sig1", n=5, language="python")
    assert len(py_sims) == 1 and py_sims[0]["patch"] == "P1\n"


def test_record_persists_language_tag():
    """После записи запись содержит language/file_ext."""
    m = _mk_mem()
    m.record_success("sig1", "P\n", score=1.0, language="Rust", file_ext=".RS")
    entries = m.successful_fixes["sig1"]
    assert len(entries) == 1
    assert entries[0]["language"] == "rust"   # нормализовано в lower
    assert entries[0]["file_ext"] == ".rs"


def test_persist_load_round_trip():
    """Тэги переживают save/load."""
    import os
    fp = Path(tempfile.mkdtemp()) / "mem.json"
    m1 = MemoryLearning(memory_file=fp)
    m1.record_success("sig1", "RP\n", score=1.0, language="rust", file_ext=".rs")
    m1.record_success("sig1", "PP\n", score=0.9, language="python", file_ext=".py")
    assert fp.exists()
    m2 = MemoryLearning(memory_file=fp)
    assert m2.get_known_fix("sig1", language="rust") == "RP\n"
    assert m2.get_known_fix("sig1", language="python") == "PP\n"
    assert m2.get_known_fix("sig1", language="javascript") is None


if __name__ == "__main__":
    tests = [
        ("no_filter_back_compat", test_get_known_fix_no_filter_back_compat),
        ("language_match", test_get_known_fix_language_match),
        ("language_mismatch", test_get_known_fix_language_mismatch),
        ("untagged_skipped_with_filter", test_get_known_fix_untagged_skipped_with_filter),
        ("picks_matching_over_first", test_get_known_fix_picks_matching_over_first),
        ("similar_fixes_filter", test_get_similar_fixes_filter),
        ("record_persists_tag", test_record_persists_language_tag),
        ("persist_load_round_trip", test_persist_load_round_trip),
    ]
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
    print()
    print(f"Memory language filter regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
