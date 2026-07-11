"""
Smoke-тесты для чат-бэкенда: chat_session + chat_tools + changes_log.

LLM/сеть не дёргаем — везде инжектируем callable. Файловые операции — в
TemporaryDirectory.

Запуск: python3 tests/test_phase_chat.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent.changes_log import (
    append_change, read_changes, event_to_change, CHAT_DIR_NAME,
)
from agent.chat_session import (
    ChatSession, build_system_prompt, DEFAULT_RULES_RU,
)
from agent.chat_tools import (
    tool_read_file, tool_list_dir, tool_grep, dispatch_tool_call, ToolError,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ----------------------------------------------------------------------
# 1. changes_log
# ----------------------------------------------------------------------
def test_changes_append_read_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        ok = append_change(d, {"verdict": "ACCEPT", "file": "a.py", "line": 1,
                                "code": "F401", "intent": "drop unused import"})
        check("changes_append_ok", ok is True)
        ok2 = append_change(d, {"verdict": "NEEDS_REVIEW", "file": "b.rs", "line": 9})
        check("changes_append_ok2", ok2 is True)
        items = read_changes(d)
        check("changes_count", len(items) == 2)
        check("changes_first_ts_set", "ts" in items[0] and items[0]["ts"])
        check("changes_first_verdict", items[0]["verdict"] == "ACCEPT")
        # last limit
        last1 = read_changes(d, limit=1)
        check("changes_limit_1", len(last1) == 1 and last1[0]["verdict"] == "NEEDS_REVIEW")


def test_changes_skip_corrupt_lines():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / CHAT_DIR_NAME / "changes.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"verdict":"ACCEPT","file":"x.py"}\n'
                     '<<< broken json >>>\n'
                     '\n'
                     '{"verdict":"FAILED","file":"y.py"}\n', encoding="utf-8")
        items = read_changes(d)
        check("changes_skip_broken", len(items) == 2)


def test_event_to_change_mapping():
    check("ev_applied", event_to_change("applied", {"file": "a", "line": 1})["verdict"] == "ACCEPT")
    check("ev_needs_review",
          event_to_change("needs_review", {"file": "a"})["verdict"] == "NEEDS_REVIEW")
    check("ev_failed",
          event_to_change("failed", {"file": "a"})["verdict"] == "FAILED")
    check("ev_other_none", event_to_change("error_dequeued", {"file": "a"}) is None)
    check("ev_no_data_none", event_to_change("applied", {}) is None)


# ----------------------------------------------------------------------
# 2. chat_tools
# ----------------------------------------------------------------------
def test_tools_read_file():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("print('hi')\nprint('bye')\n", encoding="utf-8")
        r = tool_read_file(root, path="src/main.py")
        check("tool_read_path", r["path"] == "src/main.py")
        check("tool_read_content", "print('hi')" in r["content"])
        check("tool_read_not_truncated", r["truncated"] is False)
        # head=1
        r1 = tool_read_file(root, path="src/main.py", head=1)
        check("tool_read_head", "bye" not in r1["content"])
        check("tool_read_head_truncated", r1["truncated"] is True)


def test_tools_read_file_safety():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "x.py").write_text("ok", encoding="utf-8")
        # выход за корень
        try:
            tool_read_file(root, path="../etc/passwd")
            check("safety_traversal_blocked", False)
        except ToolError:
            check("safety_traversal_blocked", True)
        # абсолютный путь
        try:
            tool_read_file(root, path="/etc/passwd")
            check("safety_absolute_blocked", False)
        except ToolError:
            check("safety_absolute_blocked", True)
        # служебный каталог
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
        try:
            tool_read_file(root, path="__pycache__/x.pyc")
            check("safety_skipdir_blocked", False)
        except ToolError:
            check("safety_skipdir_blocked", True)
        # бинарь
        (root / "bin").write_bytes(b"\x00\x01\x02\x00")
        try:
            tool_read_file(root, path="bin")
            check("safety_binary_blocked", False)
        except ToolError:
            check("safety_binary_blocked", True)


def test_tools_list_dir():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("a", encoding="utf-8")
        (root / "README.md").write_text("readme", encoding="utf-8")
        (root / "node_modules").mkdir()  # skip
        (root / "node_modules" / "x.js").write_text("x", encoding="utf-8")
        r = tool_list_dir(root, path="")
        paths = [it["path"] for it in r["entries"]]
        check("list_root_has_src", "src" in paths)
        check("list_root_has_readme", "README.md" in paths)
        check("list_root_skips_node_modules", "node_modules" not in paths)
        # каталог
        r2 = tool_list_dir(root, path="src")
        sub = [it["path"] for it in r2["entries"]]
        check("list_subdir_has_a_py", "src/a.py" in sub)


def test_tools_grep():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("import os\nDEBUG = True\nprint('hi')\n",
                                            encoding="utf-8")
        (root / "src" / "b.py").write_text("DEBUG = False\n", encoding="utf-8")
        r = tool_grep(root, pattern=r"\bDEBUG\b")
        files = sorted({h["path"] for h in r["hits"]})
        check("grep_finds_both", files == ["src/a.py", "src/b.py"])
        # glob
        r2 = tool_grep(root, pattern=r"\bDEBUG\b", glob="src/a.py")
        files2 = sorted({h["path"] for h in r2["hits"]})
        check("grep_glob_filters", files2 == ["src/a.py"])


def test_tools_dispatch_unknown():
    with tempfile.TemporaryDirectory() as d:
        out = dispatch_tool_call(Path(d), "totally_unknown", {"x": 1})
        # должен вернуть JSON со словом error
        check("dispatch_unknown_error", '"error"' in out)


# ----------------------------------------------------------------------
# 3. chat_session (без сети, fake LLM)
# ----------------------------------------------------------------------
def _fake_llm_text(reply):
    """Простой LLM, который игнорирует messages и возвращает фиксированный текст."""
    def _f(messages, tools=None, **kw):
        return {"role": "assistant", "content": reply}
    return _f


def test_session_creates_rules_and_appends_history():
    with tempfile.TemporaryDirectory() as d:
        sess = ChatSession(d, llm_callable=_fake_llm_text("hello"))
        rules_path = Path(d) / "chat" / "rules.md"
        check("session_rules_file_created", rules_path.exists())
        check("session_rules_default_content",
              "Правила поведения чата Webbles" in rules_path.read_text(encoding="utf-8"))
        reply = sess.send("привет")
        check("session_reply_text", reply == "hello")
        # history содержит обе записи
        hist = sess.history()
        roles = [h["role"] for h in hist]
        check("session_history_roles", roles == ["user", "assistant"])
        check("session_history_user_content", hist[0]["content"] == "привет")
        check("session_history_assistant_content", hist[1]["content"] == "hello")
        # Второй запрос видит первый ответ в истории.
        captured = {}
        def _cap_llm(messages, tools=None, **kw):
            captured["messages"] = messages
            return {"role": "assistant", "content": "ok2"}
        sess2 = ChatSession(d, llm_callable=_cap_llm)
        sess2.send("ещё вопрос")
        msgs = captured["messages"]
        contents = [m.get("content") for m in msgs]
        check("persisted_history_in_next_call",
              "привет" in contents and "hello" in contents)


def test_session_clear_history():
    with tempfile.TemporaryDirectory() as d:
        sess = ChatSession(d, llm_callable=_fake_llm_text("x"))
        sess.send("раз")
        sess.send("два")
        check("history_before_clear", len(sess.history()) == 4)
        sess.clear_history()
        check("history_after_clear", sess.history() == [])


def test_system_prompt_includes_changes():
    with tempfile.TemporaryDirectory() as d:
        # пишем change
        append_change(d, {"verdict": "ACCEPT", "file": "a.py", "line": 1,
                          "code": "F401", "intent": "drop unused"})
        sess = ChatSession(d, llm_callable=_fake_llm_text("ok"))
        msgs = sess.build_messages("вопрос")
        sysm = msgs[0]["content"]
        check("sp_has_rules", "Правила поведения чата" in sysm)
        check("sp_has_changes_section", "ЛОГ ИЗМЕНЕНИЙ" in sysm)
        check("sp_has_change_file", "a.py:1" in sysm)
        check("sp_has_change_intent", "drop unused" in sysm)


def test_session_tool_calling_cycle():
    """LLM возвращает tool_call → мы выполняем → LLM формирует финальный ответ."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("VAR = 42\n", encoding="utf-8")

        calls = {"n": 0}
        def _two_step_llm(messages, tools=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                # Просит прочитать файл
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "tc_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "src/main.py"}),
                        },
                    }],
                }
            # Второй вызов — финальный ответ. Проверим, что tool-result есть.
            last = messages[-1]
            assert last["role"] == "tool" and last["name"] == "read_file"
            assert "VAR = 42" in last["content"]
            return {"role": "assistant", "content": "В файле задано VAR=42"}

        sess = ChatSession(d, tools_root=d, llm_callable=_two_step_llm)
        reply = sess.send("что в main.py?")
        check("tool_cycle_two_calls", calls["n"] == 2)
        check("tool_cycle_final_text", "VAR=42" in reply)
        # история не должна содержать сырых tool-обменов — только пользовательский
        # вопрос и финальный ответ.
        hist = sess.history()
        check("tool_cycle_history_clean",
              [h["role"] for h in hist] == ["user", "assistant"])
        check("tool_cycle_history_final", hist[1]["content"] == "В файле задано VAR=42")


def test_session_tool_iter_limit():
    """Если LLM зациклилась на tool_calls — не выходим за max_tool_iters."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "f.txt").write_text("x", encoding="utf-8")
        loop_count = {"n": 0}
        def _looping(messages, tools=None, **kw):
            loop_count["n"] += 1
            return {
                "role": "assistant",
                "content": "step " + str(loop_count["n"]),
                "tool_calls": [{
                    "id": f"tc_{loop_count['n']}",
                    "type": "function",
                    "function": {"name": "list_dir",
                                 "arguments": json.dumps({"path": ""})},
                }],
            }
        sess = ChatSession(d, tools_root=d, llm_callable=_looping, max_tool_iters=3)
        reply = sess.send("крутись")
        check("iter_limit_capped", loop_count["n"] <= 3)
        check("iter_limit_returns_something", isinstance(reply, str))


def test_session_callable_without_tools_kw():
    """Совместимость с тестовым callable, который не принимает tools=..."""
    with tempfile.TemporaryDirectory() as d:
        def _no_tools(messages):
            return {"role": "assistant", "content": "ok"}
        sess = ChatSession(d, llm_callable=_no_tools)
        check("no_tools_kw_works", sess.send("x") == "ok")


def test_build_system_prompt_pure():
    sp = build_system_prompt(DEFAULT_RULES_RU, [])
    check("build_sp_empty_changes_marker", "(пока пусто" in sp)
    sp2 = build_system_prompt("RULES", [{"verdict": "ACCEPT", "file": "a", "line": 1,
                                          "code": "F401"}])
    check("build_sp_has_rules_pass", sp2.startswith("RULES"))
    check("build_sp_has_change_line", "ACCEPT a:1 [F401]" in sp2)


if __name__ == "__main__":
    print("chat backend smoke:")
    test_changes_append_read_roundtrip()
    test_changes_skip_corrupt_lines()
    test_event_to_change_mapping()
    test_tools_read_file()
    test_tools_read_file_safety()
    test_tools_list_dir()
    test_tools_grep()
    test_tools_dispatch_unknown()
    test_session_creates_rules_and_appends_history()
    test_session_clear_history()
    test_system_prompt_includes_changes()
    test_session_tool_calling_cycle()
    test_session_tool_iter_limit()
    test_session_callable_without_tools_kw()
    test_build_system_prompt_pure()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nChat backend: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
