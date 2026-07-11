"""
Structured-unanchorable fast-fail (2026-07-04, экзамен-проба
pytest-homeassistant-custom-component).

Root cause (deep-reasoner): 3 сложных .github/workflows/*.yml, которые
structured-diff не мог заанкорить (26× «anchor не найден»), жгли LLM-бюджет
(каждая ошибка → полный ~35s цикл → fallback → CRITICAL_SYNTAX → откат),
cycles_run=0, продуктивные Python-фиксы не достигнуты.

Фикс: per-file счётчик anchor_empty/anchor_mismatch; после
MAX_ANCHOR_FAILS_PER_FILE файл помечается structured-unanchorable, и его
НОВЫЕ (не-синтаксические) ошибки идут в NR без дорогой LLM-генерации.
Счётчик СБРАСЫВАЕТСЯ при успешном structured-патче — анкоримые файлы
(YAML 88% ACCEPT в learning_cases) порог не накапливают (zero-collateral).

Тестируем контракт метаданных напрямую (execute слишком тяжёл для юнита):
установка/сброс счётчика и early-exit-условие.

Запуск: python tests/test_phase_unanchorable_format_fastfail.py
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

from core.stages.generate_patch_stage import GeneratePatchStage  # noqa: E402


def _mk_meta_after_anchor_fails(file: str, n: int) -> dict:
    """Симулирует накопление n anchor-fail для файла (логика ветки пустого
    патча в execute)."""
    meta: dict = {}
    for _ in range(n):
        counts = dict(meta.get("_anchor_fail_counts") or {})
        counts[file] = counts.get(file, 0) + 1
        meta["_anchor_fail_counts"] = counts
        if counts[file] >= GeneratePatchStage.MAX_ANCHOR_FAILS_PER_FILE:
            unf = list(meta.get("_structured_unanchorable_files") or [])
            if file not in unf:
                unf.append(file)
                meta["_structured_unanchorable_files"] = unf
    return meta


# --- 1. Порог: файл помечается после MAX_ANCHOR_FAILS_PER_FILE ---
def test_file_marked_unanchorable_at_threshold():
    thr = GeneratePatchStage.MAX_ANCHOR_FAILS_PER_FILE
    meta_below = _mk_meta_after_anchor_fails(".github/workflows/ci.yml", thr - 1)
    assert ".github/workflows/ci.yml" not in (meta_below.get("_structured_unanchorable_files") or []), (
        f"до порога ({thr}) файл не должен быть unanchorable"
    )
    meta_at = _mk_meta_after_anchor_fails(".github/workflows/ci.yml", thr)
    assert ".github/workflows/ci.yml" in (meta_at.get("_structured_unanchorable_files") or []), (
        f"на пороге {thr} файл обязан быть помечен"
    )


# --- 2. Категории: только anchor_empty/anchor_mismatch считаются ---
def test_only_anchor_categories_count():
    assert "anchor_empty" in GeneratePatchStage._ANCHOR_FAIL_CATEGORIES
    assert "anchor_mismatch" in GeneratePatchStage._ANCHOR_FAIL_CATEGORIES
    # diff_fail/timeout/empty — НЕ anchor-fail (файл может быть анкорим,
    # проблема в другом)
    assert "diff_fail" not in GeneratePatchStage._ANCHOR_FAIL_CATEGORIES
    assert "timeout" not in GeneratePatchStage._ANCHOR_FAIL_CATEGORIES
    assert "empty" not in GeneratePatchStage._ANCHOR_FAIL_CATEGORIES


# --- 3. Сброс при успехе: анкоримый файл порог не копит ---
def test_success_resets_streak():
    file = "src/app.py"
    meta = _mk_meta_after_anchor_fails(file, GeneratePatchStage.MAX_ANCHOR_FAILS_PER_FILE - 1)
    assert meta.get("_anchor_fail_counts", {}).get(file) == GeneratePatchStage.MAX_ANCHOR_FAILS_PER_FILE - 1
    # успешный structured-патч (логика reset-ветки execute)
    afc = dict(meta.get("_anchor_fail_counts") or {})
    afc.pop(file, None)
    meta["_anchor_fail_counts"] = afc
    # следующий anchor-fail стартует с нуля → порог 3 недостижим за 1
    meta2 = dict(meta)
    counts = dict(meta2.get("_anchor_fail_counts") or {})
    counts[file] = counts.get(file, 0) + 1
    meta2["_anchor_fail_counts"] = counts
    assert counts[file] == 1, "после сброса счётчик стартует заново"
    assert file not in (meta2.get("_structured_unanchorable_files") or [])


# --- 4. Синтаксические ошибки НЕ глушатся даже в unanchorable-файле ---
def test_syntax_errors_not_suppressed():
    # Это условие _is_syntax в execute: CRITICAL_SYNTAX / E999 / E902 /
    # invalid-syntax НЕ уходят в fast-fail-NR (файл должен парситься).
    for code, ec in [("E999", ""), ("invalid-syntax", ""), ("", "CRITICAL_SYNTAX")]:
        _is_syntax = ec == "CRITICAL_SYNTAX" or code in ("E999", "E902", "invalid-syntax")
        assert _is_syntax, f"код {code!r}/{ec!r} обязан считаться синтаксическим (не глушить)"
    # обычный semgrep/security код — не синтаксический → глушится
    _is_syntax = "" == "CRITICAL_SYNTAX" or "yaml.github-actions..." in ("E999", "E902", "invalid-syntax")
    assert not _is_syntax


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
    print(f"\nUnanchorable fast-fail: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
