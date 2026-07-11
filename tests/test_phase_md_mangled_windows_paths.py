"""
MD-mangled Windows-пути (2026-07-08, Delgan/loguru — 108 CRITICAL_SYNTAX-
откатов за прогон, 4 прогона подряд без единого ACCEPT на живом коде).

Root cause: путь с Windows-backslash в промпте ("loguru\\_recattrs.py") LLM
читает как markdown-escape `\\_` и возвращает "loguru_recattrs.py" — все
файлы пакета loguru начинаются с '_', поэтому ВЕСЬ пакет был неанкорим:
EditSet не находил контент, filter_for_target отбрасывал валидные правки
(«Патч не затрагивает целевой файл»), fallback-apply мял файл → откат.
Тот же класс наблюдался на httpx (httpx\\_models.py → httpx_models.py).

Инварианты:
1. Промпты LLM получают путь ТОЛЬКО с forward-slash (корень).
2. EditSet.to_unified_diff матчит md-mangled путь к каноническому и
   подставляет канонический в diff-заголовки (страховка).
3. filter_for_target распознаёт целевой файл в md-mangled виде.
4. Mangled-fallback не срабатывает, если mangled-имя совпадает с РЕАЛЬНО
   существующим файлом (не перехватываем чужой контент).

Запуск: python tests/test_phase_md_mangled_windows_paths.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from fixers.structured_edit import Anchor, Edit, EditSet  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


CONTENT = "import pickle\n\nclass RecordException:\n    pass\n"


def _edit(file, match="import pickle", replace="import json"):
    return Edit(
        file=file,
        anchor=Anchor(line=1, match=match),
        kind="replace",
        new=replace,
    )


def test_mangled_path_resolves_to_canonical_content():
    """LLM вернула 'loguru_recattrs.py' вместо 'loguru/_recattrs.py' —
    diff строится, заголовки содержат КАНОНИЧЕСКИЙ путь."""
    es = EditSet(intent="fix pickle", edits=[_edit("loguru_recattrs.py")], confidence=0.9)
    diff = es.to_unified_diff({"loguru\\_recattrs.py": CONTENT})
    check("mangled_diff_not_empty", bool(diff and diff.strip()))
    check("diff_header_canonical", "a/loguru/_recattrs.py" in diff, f"diff head: {diff[:120]!r}")
    check("no_mangled_header", "a/loguru_recattrs.py" not in diff)


def test_exact_and_norm_paths_still_win():
    """Обычные пути работают как раньше (нормализация / и basename)."""
    es = EditSet(intent="x", edits=[_edit("loguru/_recattrs.py")], confidence=0.9)
    diff = es.to_unified_diff({"loguru\\_recattrs.py": CONTENT})
    check("norm_path_diff_ok", bool(diff and diff.strip()) and "a/loguru/_recattrs.py" in diff)


def test_mangled_does_not_steal_real_file():
    """Если 'loguru_recattrs.py' СУЩЕСТВУЕТ как настоящий файл — mangled-
    fallback не должен перехватывать его имя для 'loguru/_recattrs.py'."""
    real_other = "OTHER = 1\n"
    es = EditSet(
        intent="x",
        edits=[_edit("loguru_recattrs.py", match="OTHER = 1", replace="OTHER = 2")],
        confidence=0.9,
    )
    diff = es.to_unified_diff({
        "loguru_recattrs.py": real_other,        # настоящий файл с таким именем
        "loguru\\_recattrs.py": CONTENT,
    })
    check("real_file_wins", "OTHER = 2" in diff and "import json" not in diff,
          f"diff: {diff[:200]!r}")


def test_filter_to_target_window_accepts_mangled_edit_file():
    """filter_to_target_window: правка с md-mangled именем целевого файла
    распознаётся как целевая (не отбрасывается защитой окна)."""
    es = EditSet(intent="x", edits=[_edit("loguru_recattrs.py")], confidence=0.9)
    kept, dropped = es.filter_to_target_window(
        target_file="loguru\\_recattrs.py", target_line=1, error_code="F401",
    )
    check("filter_keeps_mangled_target", len(kept.edits) == 1,
          f"kept: {[e.file for e in kept.edits]}, dropped: {dropped}")


def test_prompt_paths_forward_slash_only():
    """Корень: в user-промпт structured-пути уходят только с forward-slash.
    Проверяем сборку prompt_file в generate_structured_fix по коду — грубый
    инвариант: в fixers/llm_client.py нет f\"FILE: {target_file}\" с сырым путём."""
    src = (ROOT / "fixers" / "llm_client.py").read_text(encoding="utf-8", errors="replace")
    check("no_raw_target_file_in_prompt", 'f"FILE: {target_file}"' not in src)
    check("prompt_file_normalized", 'prompt_file = str(target_file).replace("\\\\", "/")' in src)


if __name__ == "__main__":
    test_mangled_path_resolves_to_canonical_content()
    test_exact_and_norm_paths_still_win()
    test_mangled_does_not_steal_real_file()
    test_filter_to_target_window_accepts_mangled_edit_file()
    test_prompt_paths_forward_slash_only()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"md_mangled_windows_paths: {passed}/{len(results)} passed")
    if failed:
        sys.exit(1)
    sys.exit(0)
