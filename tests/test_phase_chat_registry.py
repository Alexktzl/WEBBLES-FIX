"""
chat_registry — реестр чатов с per-chat history.jsonl.

Тесты без сети/LLM: проверяем create/list/switch/delete/rename, что history
конкретного чата изолирована, и что ensure_active создаёт чат при первом запуске.

Запуск: python3 tests/test_phase_chat_registry.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from agent import chat_registry as reg
from agent.chat_session import ChatSession

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _fake_llm(reply):
    def _f(messages, tools=None, **kw):
        return {"role": "assistant", "content": reply}
    return _f


def test_ensure_active_creates_first():
    with tempfile.TemporaryDirectory() as d:
        check("initial_no_active", reg.get_active(d) is None)
        cid = reg.ensure_active(d, default_name="Hello")
        check("ensure_returns_id", bool(cid))
        check("active_set", reg.get_active(d) == cid)
        chats = reg.list_chats(d)
        check("one_chat", len(chats) == 1)
        check("active_flag_set", chats[0].get("active") is True)
        check("name_persisted", chats[0]["name"] == "Hello")


def test_create_switch_delete():
    with tempfile.TemporaryDirectory() as d:
        a = reg.create_chat(d, "Alpha")
        b = reg.create_chat(d, "Beta")  # auto-active
        check("active_is_b", reg.get_active(d) == b)
        check("two_chats", len(reg.list_chats(d)) == 2)
        ok = reg.set_active(d, a)
        check("switch_ok", ok and reg.get_active(d) == a)
        # удалить B
        check("delete_b", reg.delete_chat(d, b) is True)
        check("one_left", len(reg.list_chats(d)) == 1)
        # удалить активный — переключится на оставшийся
        check("delete_active_a", reg.delete_chat(d, a) is True)
        check("none_left", len(reg.list_chats(d)) == 0)
        check("no_active_after_all_deleted", reg.get_active(d) is None)


def test_per_chat_history_isolated():
    with tempfile.TemporaryDirectory() as d:
        a = reg.create_chat(d, "A")
        b = reg.create_chat(d, "B")
        sess_a = ChatSession(d, chat_id=a, llm_callable=_fake_llm("from-A"))
        sess_b = ChatSession(d, chat_id=b, llm_callable=_fake_llm("from-B"))
        sess_a.send("hi a")
        sess_b.send("hi b")
        ha = sess_a.history()
        hb = sess_b.history()
        check("a_has_own", [m["content"] for m in ha] == ["hi a", "from-A"])
        check("b_has_own", [m["content"] for m in hb] == ["hi b", "from-B"])
        # clear A не задевает B
        sess_a.clear_history()
        check("a_cleared", sess_a.history() == [])
        check("b_intact", [m["content"] for m in sess_b.history()] == ["hi b", "from-B"])


def test_rename_and_set_mode():
    with tempfile.TemporaryDirectory() as d:
        cid = reg.create_chat(d, "Old", mode="chat")
        ok = reg.rename_chat(d, cid, "Brand New")
        check("rename_ok", ok)
        check("renamed", reg.list_chats(d)[0]["name"] == "Brand New")
        ok2 = reg.set_mode(d, cid, "create")
        check("set_mode_ok", ok2)
        check("mode_persisted", reg.list_chats(d)[0]["mode"] == "create")


def test_message_count_in_listing():
    with tempfile.TemporaryDirectory() as d:
        cid = reg.create_chat(d, "Counter")
        sess = ChatSession(d, chat_id=cid, llm_callable=_fake_llm("ok"))
        sess.send("one")
        sess.send("two")
        check("messages_counted", reg.list_chats(d)[0]["messages"] == 4)  # 2 user + 2 assistant


if __name__ == "__main__":
    print("chat_registry smoke:")
    test_ensure_active_creates_first()
    test_create_switch_delete()
    test_per_chat_history_isolated()
    test_rename_and_set_mode()
    test_message_count_in_listing()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nchat_registry: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
