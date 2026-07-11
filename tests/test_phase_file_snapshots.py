"""
file_snapshots — снимки «Было/Стало» по файлу + аккумулирование ошибок и вердиктов.

Без LLM/сети/Eel. Тестируем чистые функции в TemporaryDirectory.
Запуск: python3 tests/test_phase_file_snapshots.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent import file_snapshots as fs

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _mkproj(d, files):
    """Создаёт временный проект с набором файлов {rel: content}."""
    root = Path(d)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


# ----------------------------------------------------------------------
# 1. record_before / record_after / get_file_diff round-trip
# ----------------------------------------------------------------------
def test_basic_roundtrip():
    with tempfile.TemporaryDirectory() as snap_d, \
            tempfile.TemporaryDirectory() as proj_d:
        proj = _mkproj(proj_d, {"src/main.rs": "fn main() { 0 }\n"})
        ok = fs.record_before(snap_d, proj, "src/main.rs")
        check("rb_record_before_ok", ok is True)
        diff = fs.get_file_diff(snap_d, "src/main.rs")
        check("rb_before_recorded", diff["before"].startswith("fn main()"))
        check("rb_after_empty", diff["after"] == "")
        check("rb_path", diff["path"] == "src/main.rs")
        # Изменяем файл, фиксируем after
        (proj / "src" / "main.rs").write_text("fn main() { 42 }\n", encoding="utf-8")
        n = fs.record_after_all(snap_d, proj)
        check("rb_after_count", n == 1)
        diff = fs.get_file_diff(snap_d, "src/main.rs")
        check("rb_after_recorded", "42" in diff["after"])
        # before не перетёрт
        check("rb_before_preserved", "fn main() { 0 }" in diff["before"])


def test_record_before_idempotent():
    """Повторный record_before не перетирает уже зафиксированный before."""
    with tempfile.TemporaryDirectory() as snap_d, \
            tempfile.TemporaryDirectory() as proj_d:
        proj = _mkproj(proj_d, {"x.py": "v1\n"})
        fs.record_before(snap_d, proj, "x.py")
        # Меняем файл и пытаемся снова record_before
        (proj / "x.py").write_text("v2\n", encoding="utf-8")
        fs.record_before(snap_d, proj, "x.py")
        diff = fs.get_file_diff(snap_d, "x.py")
        check("idempotent_before_first_wins", diff["before"].strip() == "v1")


# ----------------------------------------------------------------------
# 2. add_error_line + дедуп
# ----------------------------------------------------------------------
def test_add_error_line_dedup():
    with tempfile.TemporaryDirectory() as snap_d:
        fs.add_error_line(snap_d, "a.rs", 10, "E0382", "moved")
        fs.add_error_line(snap_d, "a.rs", 11, "E0382", "moved twice")
        # Дубль той же (line, code) — не должен умножиться
        fs.add_error_line(snap_d, "a.rs", 10, "E0382", "moved again")
        diff = fs.get_file_diff(snap_d, "a.rs")
        check("err_count", len(diff["error_lines"]) == 2)
        lines = sorted(e["line"] for e in diff["error_lines"])
        check("err_lines_values", lines == [10, 11])


# ----------------------------------------------------------------------
# 3. add_change аккумулирует
# ----------------------------------------------------------------------
def test_add_change_accumulates():
    with tempfile.TemporaryDirectory() as snap_d:
        fs.add_change(snap_d, "lib.rs", {"verdict": "ACCEPT", "code": "E0432",
                                          "intent": "add crate", "patch_source": "rule_based"})
        fs.add_change(snap_d, "lib.rs", {"verdict": "NEEDS_REVIEW", "code": "E0609",
                                          "intent": "rename field", "patch_source": "structured_llm"})
        diff = fs.get_file_diff(snap_d, "lib.rs")
        check("ch_count", len(diff["changes"]) == 2)
        verdicts = [c["verdict"] for c in diff["changes"]]
        check("ch_order_preserved", verdicts == ["ACCEPT", "NEEDS_REVIEW"])
        check("ch_has_ts", all("ts" in c and c["ts"] for c in diff["changes"]))


# ----------------------------------------------------------------------
# 4. Безопасность чтения: путь вне проекта
# ----------------------------------------------------------------------
def test_record_before_path_traversal_safe():
    with tempfile.TemporaryDirectory() as snap_d, \
            tempfile.TemporaryDirectory() as proj_d:
        ok = fs.record_before(snap_d, proj_d, "../../../etc/passwd")
        # Не должно ничего записать (выход за корень → None из _read_file_text).
        check("safety_no_traversal_record", ok is False)


def test_record_before_binary_skipped():
    with tempfile.TemporaryDirectory() as snap_d, \
            tempfile.TemporaryDirectory() as proj_d:
        proj = Path(proj_d)
        (proj / "img.bin").write_bytes(b"\x00\x01\x02\x00\xff")
        ok = fs.record_before(snap_d, proj, "img.bin")
        check("safety_binary_skipped", ok is False)


# ----------------------------------------------------------------------
# 5. list_snapshots + clear
# ----------------------------------------------------------------------
def test_list_and_clear():
    with tempfile.TemporaryDirectory() as snap_d, \
            tempfile.TemporaryDirectory() as proj_d:
        proj = _mkproj(proj_d, {"a.py": "x", "b/c.py": "y"})
        fs.record_before(snap_d, proj, "a.py")
        fs.record_before(snap_d, proj, "b/c.py")
        ls = fs.list_snapshots(snap_d)
        check("list_returns_both", sorted(ls) == ["a.py", "b/c.py"])
        n = fs.clear(snap_d)
        check("clear_count", n == 2)
        check("after_clear_empty", fs.list_snapshots(snap_d) == [])


# ----------------------------------------------------------------------
# 6. get_file_diff для несуществующего файла
# ----------------------------------------------------------------------
def test_missing_returns_flag():
    with tempfile.TemporaryDirectory() as snap_d:
        diff = fs.get_file_diff(snap_d, "no/such.rs")
        check("missing_flag", diff["missing"] is True)
        check("missing_empty_fields",
              diff["before"] == "" and diff["after"] == ""
              and diff["error_lines"] == [] and diff["changes"] == [])


# ----------------------------------------------------------------------
# 7. record_after для несуществующего файла после before — не падает
# ----------------------------------------------------------------------
def test_record_after_missing_file_safe():
    with tempfile.TemporaryDirectory() as snap_d, \
            tempfile.TemporaryDirectory() as proj_d:
        proj = _mkproj(proj_d, {"keep.py": "before"})
        fs.record_before(snap_d, proj, "keep.py")
        # Удаляем файл и зовём record_after_all
        (proj / "keep.py").unlink()
        n = fs.record_after_all(snap_d, proj)
        check("after_missing_no_update", n == 0)
        diff = fs.get_file_diff(snap_d, "keep.py")
        # before остался, after не записан
        check("after_missing_before_preserved", diff["before"] == "before")
        check("after_missing_after_empty", diff["after"] == "")


# ----------------------------------------------------------------------
# 8. record_after_all с новым файлом (before не было) — фиксирует before=""
# ----------------------------------------------------------------------
def test_record_after_new_file_appears_via_add_change():
    """Если файл засветился через add_change (например, на applied), но без
    record_before — record_after_all всё равно зафиксирует его."""
    with tempfile.TemporaryDirectory() as snap_d, \
            tempfile.TemporaryDirectory() as proj_d:
        proj = _mkproj(proj_d, {"new.py": "fresh\n"})
        # Засветим файл через add_change без before
        fs.add_change(snap_d, "new.py", {"verdict": "ACCEPT", "code": "X"})
        n = fs.record_after_all(snap_d, proj)
        check("new_file_recorded_after", n == 1)
        diff = fs.get_file_diff(snap_d, "new.py")
        check("new_file_before_default_empty", diff["before"] == "")
        check("new_file_after_content", "fresh" in diff["after"])


if __name__ == "__main__":
    print("file_snapshots smoke:")
    test_basic_roundtrip()
    test_record_before_idempotent()
    test_add_error_line_dedup()
    test_add_change_accumulates()
    test_record_before_path_traversal_safe()
    test_record_before_binary_skipped()
    test_list_and_clear()
    test_missing_returns_flag()
    test_record_after_missing_file_safe()
    test_record_after_new_file_appears_via_add_change()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nfile_snapshots: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
