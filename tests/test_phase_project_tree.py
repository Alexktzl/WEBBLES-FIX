"""
agent/project_tree.py — снимок дерева для UI левой панели.

Покрытие:
1) Детерминированный порядок: dir перед file, по имени.
2) Skip служебных каталогов (target/.git/node_modules/__pycache__/...).
3) Скрытые файлы/каталоги отбрасываются.
4) Лимит max_files → truncated=True; ниже лимита — truncated=False.
5) Несуществующий корень → ([], False).
6) Корректный payload для события (root/items/truncated/total).
7) Сквозная wiring через run_fix_agent: событие project_tree эмитится между
   run_started и run_finished, на не-Chat режиме.

Запуск: python3 tests/test_phase_project_tree.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent.project_tree import scan_project_tree, build_tree_event_payload
from agent import modes
from agent.events import CollectingSink
from agent.run_fix import run_fix_agent

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _mkfile(p: Path, content: str = ""):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# --- 1+2+3: порядок, skip-каталоги, скрытые ----------------------------
def test_order_and_skip_dirs():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root / "src" / "main.py", "# main")
        _mkfile(root / "src" / "lib" / "helper.py")
        _mkfile(root / "README.md")
        _mkfile(root / "node_modules" / "x.js")        # skip
        _mkfile(root / ".git" / "HEAD")                # skip
        _mkfile(root / "__pycache__" / "cached.pyc")   # skip
        _mkfile(root / ".env")                         # скрытый файл — skip
        _mkfile(root / ".vscode" / "settings.json")    # skip
        items, truncated = scan_project_tree(root)
        paths = [it["path"] for it in items]
        # Корневой src идёт перед корневым README.md (dir<file)
        check("order_src_before_readme",
              paths.index("src") < paths.index("README.md"))
        # src/lib папка идёт перед src/main.py (depth=1, dir<file)
        check("order_lib_before_main",
              paths.index("src/lib") < paths.index("src/main.py"))
        # node_modules / .git / __pycache__ / .vscode не попали
        for skipped in ("node_modules", ".git", "__pycache__", ".vscode"):
            check(f"skip_dir_{skipped}", not any(p.startswith(skipped) for p in paths))
        # Скрытый .env — пропущен
        check("skip_hidden_dotenv", ".env" not in paths)
        check("order_not_truncated", truncated is False)
        # Глубины корректные
        depth_map = {it["path"]: it["depth"] for it in items}
        check("depth_root_files", depth_map.get("README.md") == 0)
        check("depth_src_dir", depth_map.get("src") == 0)
        check("depth_main_py", depth_map.get("src/main.py") == 1)
        check("depth_helper_py", depth_map.get("src/lib/helper.py") == 2)


# --- 4: лимит truncated --------------------------------------------------
def test_truncation_limit():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(50):
            _mkfile(root / f"f{i:03d}.txt")
        items, truncated = scan_project_tree(root, max_files=20)
        # Файлов ровно 20 (лимит)
        files = [it for it in items if it["kind"] == "file"]
        check("trunc_files_at_limit", len(files) == 20)
        check("trunc_flag_set", truncated is True)
        # И без лимита — 50
        items_full, full_truncated = scan_project_tree(root, max_files=200)
        check("trunc_full_no_trunc", full_truncated is False)
        check("trunc_full_count",
              sum(1 for it in items_full if it["kind"] == "file") == 50)


# --- 5: несуществующий корень -------------------------------------------
def test_missing_root():
    items, truncated = scan_project_tree("/sessions/non_existent_root_xyzzy_42")
    check("missing_empty", items == [])
    check("missing_no_trunc", truncated is False)


# --- 6: payload-обёртка --------------------------------------------------
def test_payload_shape():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.py").write_text("# a", encoding="utf-8")
        payload = build_tree_event_payload(d)
        check("payload_has_root", "root" in payload and payload["root"])
        check("payload_has_items_list", isinstance(payload["items"], list))
        check("payload_has_total", payload["total"] == len(payload["items"]))
        check("payload_has_truncated_bool",
              isinstance(payload["truncated"], bool))


# --- 7: wiring run_fix_agent → событие project_tree ----------------------
_FAKE_RESULT = {
    "status": "COMPLETED",
    "accepted_patches": 0, "rejected_patches": 0,
    "needs_review_count": 0, "needs_review_items": [],
    "remaining_errors": [],
    "initial_error_count": 0, "final_error_count": 0,
}


class _FakeController:
    def load_config(self):
        return {"pipeline": {}}
    def run_pipeline(self, project_path, language, config, *, event_emitter=None):
        return _FAKE_RESULT


def test_run_fix_emits_project_tree():
    with tempfile.TemporaryDirectory() as d:
        try:
            # положим маркерный файл, чтобы дерево не было пустым
            (Path(d) / "marker.py").write_text("# marker", encoding="utf-8")
            sink = CollectingSink()
            run_fix_agent(d, "python", mode=modes.Mode.FIX,
                          controller=_FakeController(), emit=sink, changes_root=Path(d))
            types = sink.types
            check("wire_project_tree_emitted", "project_tree" in types)
            # ordering: run_started → project_tree → run_finished
            i_start = types.index("run_started")
            i_tree = types.index("project_tree")
            i_finish = types.index("run_finished")
            check("wire_order_start_before_tree", i_start < i_tree)
            check("wire_order_tree_before_finish", i_tree < i_finish)
            # payload контентного дерева содержит marker.py
            tree_evt = [e for e in sink.events if e.type.value == "project_tree"][0]
            paths = [it["path"] for it in tree_evt.data.get("items", [])]
            check("wire_tree_payload_has_marker", "marker.py" in paths)
        finally:
            import shutil
            from core.pipeline_engine import PipelineEngine
            shutil.rmtree(PipelineEngine._project_runtime_dir(d), ignore_errors=True)


def test_chat_mode_does_not_emit_tree():
    """В Chat-режиме gating уводит ДО эмиссии project_tree."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "x.py").write_text("# x", encoding="utf-8")
        sink = CollectingSink()
        run_fix_agent(d, "python", mode=modes.Mode.CHAT,
                      controller=_FakeController(), emit=sink)
        check("chat_mode_no_tree", "project_tree" not in sink.types)


if __name__ == "__main__":
    print("project_tree smoke:")
    test_order_and_skip_dirs()
    test_truncation_limit()
    test_missing_root()
    test_payload_shape()
    test_run_fix_emits_project_tree()
    test_chat_mode_does_not_emit_tree()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nproject_tree: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
