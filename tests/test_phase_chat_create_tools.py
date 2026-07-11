"""
Create-режим: write_file и make_dir доступны через ChatSession,
а в обычном/Fix-режиме их нет.

Запуск: python3 tests/test_phase_chat_create_tools.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent.chat_session import ChatSession
from agent.chat_tools import (
    tool_schemas, write_tool_schemas, dispatch_tool_call, ToolError,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ----------------------------------------------------------------------
# 1. Сами tools работают (write/make_dir, безопасность)
# ----------------------------------------------------------------------
def test_write_file_creates_in_root():
    with tempfile.TemporaryDirectory() as d:
        from agent.chat_tools import tool_write_file
        r = tool_write_file(Path(d), path="src/app.py", content="print('hi')\n")
        check("write_path", r["path"] == "src/app.py")
        check("write_bytes", r["bytes"] > 0)
        check("write_exists", (Path(d) / "src" / "app.py").exists())


def test_write_file_refuses_traversal():
    with tempfile.TemporaryDirectory() as d:
        from agent.chat_tools import tool_write_file
        try:
            tool_write_file(Path(d), path="../etc/passwd", content="x")
            check("write_traversal_blocked", False)
        except ToolError:
            check("write_traversal_blocked", True)


def test_write_file_refuses_binary_extension():
    with tempfile.TemporaryDirectory() as d:
        from agent.chat_tools import tool_write_file
        try:
            tool_write_file(Path(d), path="x.exe", content="MZ")
            check("write_binary_blocked", False)
        except ToolError:
            check("write_binary_blocked", True)


def test_make_dir():
    with tempfile.TemporaryDirectory() as d:
        from agent.chat_tools import tool_make_dir
        r = tool_make_dir(Path(d), path="a/b/c")
        check("mkdir_path", r["path"] == "a/b/c")
        check("mkdir_exists", (Path(d) / "a" / "b" / "c").is_dir())


# ----------------------------------------------------------------------
# 2. Gating в ChatSession: режим определяет схемы tools
# ----------------------------------------------------------------------
def _capture_llm():
    captured = {"messages": None, "tools": None}
    def _f(messages, tools=None, **kw):
        captured["messages"] = messages
        captured["tools"] = tools
        return {"role": "assistant", "content": "done"}
    return _f, captured


def test_chat_mode_has_no_write_tools():
    with tempfile.TemporaryDirectory() as d:
        llm, cap = _capture_llm()
        s = ChatSession(d, llm_callable=llm, mode="chat")
        s.send("hi")
        names = sorted([t["function"]["name"] for t in (cap["tools"] or [])])
        check("chat_no_write", "write_file" not in names)
        check("chat_no_mkdir", "make_dir" not in names)
        check("chat_has_read", "read_file" in names)


def test_create_mode_has_write_tools():
    with tempfile.TemporaryDirectory() as d:
        llm, cap = _capture_llm()
        s = ChatSession(d, llm_callable=llm, mode="create")
        s.send("scaffold")
        names = sorted([t["function"]["name"] for t in (cap["tools"] or [])])
        check("create_has_write", "write_file" in names)
        check("create_has_mkdir", "make_dir" in names)
        check("create_keeps_read", "read_file" in names)


def test_dispatch_unknown_in_chat_mode():
    """Даже если LLM каким-то образом пришлёт write_file в chat-режиме —
    dispatch это допустит (gating уровня tool-схем). Это намеренно: чтобы
    защита не зависела от одной точки. Защита на write_file отдельно.
    """
    with tempfile.TemporaryDirectory() as d:
        out = dispatch_tool_call(Path(d), "write_file",
                                 {"path": "x.py", "content": "ok"})
        # должно успешно записать в произвольный путь — это уровень dispatch.
        assert (Path(d) / "x.py").exists()
        check("dispatch_write_works_at_low_level", True)


# ----------------------------------------------------------------------
# 3. write_tool_schemas формат
# ----------------------------------------------------------------------
def test_schemas_shape():
    schemas = write_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    check("schemas_has_write", "write_file" in names)
    check("schemas_has_mkdir", "make_dir" in names)
    for s in schemas:
        check(f"schema_{s['function']['name']}_required",
              "required" in s["function"]["parameters"])


if __name__ == "__main__":
    print("chat create-tools smoke:")
    test_write_file_creates_in_root()
    test_write_file_refuses_traversal()
    test_write_file_refuses_binary_extension()
    test_make_dir()
    test_chat_mode_has_no_write_tools()
    test_create_mode_has_write_tools()
    test_dispatch_unknown_in_chat_mode()
    test_schemas_shape()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nchat create-tools: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
