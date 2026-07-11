"""
Stage H — smoke / regression: InvariantGuard 2.0 (docstring ↔ code).

Покрывает:
  H.1 DocstringExtractor — python / rust / js.
  H.3 InvariantGuard.check_intent_vs_code — ok / mismatch / graceful (stub-LLM).
  H.4 AuditCache — round-trip + TTL + miss.
  H.2 SemanticAuditor — выдаёт SEMANTIC_MISMATCH, использует кэш, без guard → [].
  H.5 SEMANTIC_MISMATCH — вес в ErrorClassifier + constraints.

LLM не вызывается вживую — стабится `_call_llm`.
Запуск: python3 tests/test_phase_h_semantic.py
"""

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from analysis.docstring_extractor import DocstringExtractor
from analysis.invariant_guard import InvariantGuard
from analysis.audit_cache import AuditCache
from analysis.semantic_auditor import SemanticAuditor, SEMANTIC_MISMATCH_CLASS
from analysis.error_classifier import ErrorClassifier
from analysis.constraints.error_constraints import get_constraints, is_known

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class _StubLLM:
    """Заглушка llm_client: возвращает заранее заданный ответ или бросает."""

    def __init__(self, response=None, boom=False):
        self._response = response
        self._boom = boom

    def _call_llm(self, prompt):
        if self._boom:
            raise RuntimeError("llm down")
        return self._response


# --- H.1: DocstringExtractor ----------------------------------------
_PY = '''\
def add(a, b):
    """Return the sum of a and b."""
    return a + b


class Counter:
    """Counts things."""
    pass


def no_doc(x):
    return x
'''

_RS = '''\
/// Adds two numbers and returns the sum.
pub fn add(a: i32, b: i32) -> i32 {
    a - b
}

/// A player in the game.
struct Player {
    name: String,
}
'''

_JS = '''\
/**
 * Returns the sum of two numbers.
 * @param {number} a
 */
function add(a, b) {
  return a - b;
}
'''


def test_extract_python():
    items = DocstringExtractor().extract(_PY, "python")
    names = {i.name: i for i in items}
    check("py_finds_documented", "add" in names and "Counter" in names)
    check("py_skips_undocumented", "no_doc" not in names)
    check("py_doc_text", names["add"].doc == "Return the sum of a and b.")
    check("py_code_has_body", "return a + b" in names["add"].code)
    check("py_kind", names["Counter"].kind == "class")


def test_extract_rust():
    items = DocstringExtractor().extract(_RS, "rust")
    names = {i.name: i for i in items}
    check("rs_finds_fn_and_struct", "add" in names and "Player" in names)
    check("rs_doc_text", "Adds two numbers" in names["add"].doc)
    check("rs_code_has_body", "a - b" in names["add"].code)
    check("rs_kind_struct", names["Player"].kind == "struct")


def test_extract_js():
    items = DocstringExtractor().extract(_JS, "javascript")
    names = {i.name: i for i in items}
    check("js_finds_function", "add" in names)
    check("js_doc_text", "sum of two numbers" in names["add"].doc)
    check("js_code_has_body", "return a - b" in names["add"].code)


# --- H.3: check_intent_vs_code --------------------------------------
def test_check_intent():
    g_ok = InvariantGuard(_StubLLM('{"verdict": "ok", "reason": "matches"}'))
    r = g_ok.check_intent_vs_code("Return sum", "return a+b", "python", "add")
    check("intent_ok", r["verdict"] == "ok" and r["confidence"] > 0)

    g_mm = InvariantGuard(_StubLLM('{"verdict": "mismatch", "reason": "subtracts instead"}'))
    r = g_mm.check_intent_vs_code("Return sum", "return a-b", "python", "add")
    check("intent_mismatch", r["verdict"] == "mismatch")
    check("intent_mismatch_reason", "subtract" in r["reason"].lower())

    # фоллбэк по ключевому слову (не-JSON ответ)
    g_kw = InvariantGuard(_StubLLM("This is a clear MISMATCH between doc and code."))
    check("intent_keyword_fallback",
          g_kw.check_intent_vs_code("d", "c", "python").get("verdict") == "mismatch")

    # graceful: None / exception / пустой ввод → unknown
    check("intent_none_unknown",
          InvariantGuard(_StubLLM(None)).check_intent_vs_code("d", "c").get("verdict") == "unknown")
    check("intent_boom_unknown",
          InvariantGuard(_StubLLM(boom=True)).check_intent_vs_code("d", "c").get("verdict") == "unknown")
    check("intent_empty_unknown",
          g_ok.check_intent_vs_code("", "code").get("verdict") == "unknown")


# --- H.4: AuditCache ------------------------------------------------
def test_audit_cache():
    with tempfile.TemporaryDirectory() as d:
        c = AuditCache(Path(d) / "ac", ttl_days=30)
        check("cache_miss", c.get("doc", "code") is None)
        c.put("doc", "code", {"verdict": "mismatch", "reason": "x"})
        got = c.get("doc", "code")
        check("cache_roundtrip", got and got["verdict"] == "mismatch")
        # другой код → другой ключ → miss
        check("cache_key_sensitive", c.get("doc", "other code") is None)
        # TTL=0 → протухло
        c0 = AuditCache(Path(d) / "ac", ttl_days=0)
        time.sleep(0.02)
        check("cache_ttl_expired", c0.get("doc", "code") is None)


# --- H.2: SemanticAuditor -------------------------------------------
class _StubGuard:
    def __init__(self, verdict):
        self._verdict = verdict
        self.calls = 0

    def check_intent_vs_code(self, doc, code, language="", name=""):
        self.calls += 1
        return {"verdict": self._verdict, "reason": "stub reason", "confidence": 0.8}


def test_auditor_flags_mismatch():
    guard = _StubGuard("mismatch")
    auditor = SemanticAuditor(guard=guard)
    errs = auditor.audit_source(_PY, "python", file="m.py")
    # add и Counter документированы → оба «mismatch» по stub
    check("auditor_emits_errors", len(errs) >= 1)
    e = errs[0]
    check("auditor_code", e["code"] == "SEMANTIC_MISMATCH")
    check("auditor_class", e["error_class"] == SEMANTIC_MISMATCH_CLASS)
    check("auditor_has_line", e["line"] > 0)
    check("auditor_msg_has_reason", "stub reason" in e["message"])


def test_auditor_ok_no_errors():
    auditor = SemanticAuditor(guard=_StubGuard("ok"))
    check("auditor_ok_empty", auditor.audit_source(_PY, "python") == [])


def test_auditor_no_guard():
    auditor = SemanticAuditor()  # ни llm, ни guard
    check("auditor_no_guard_empty", auditor.audit_source(_PY, "python") == [])


def test_auditor_uses_cache():
    with tempfile.TemporaryDirectory() as d:
        cache = AuditCache(Path(d) / "ac", ttl_days=30)
        guard = _StubGuard("mismatch")
        auditor = SemanticAuditor(guard=guard, cache=cache)
        auditor.audit_source(_PY, "python", file="m.py")
        calls_after_first = guard.calls
        # второй прогон — берёт из кэша, guard не дёргается заново
        auditor.audit_source(_PY, "python", file="m.py")
        check("auditor_cache_avoids_recall", guard.calls == calls_after_first)


# --- H.5: SEMANTIC_MISMATCH класс -----------------------------------
def test_semantic_mismatch_class():
    clf = ErrorClassifier()
    w = clf.get_weight({"error_class": "SEMANTIC_MISMATCH"})
    check("weight_value", w == 55.0)
    check("weight_ordering",
          clf.get_weight({"error_class": "CLEANUP"}) < w
          < clf.get_weight({"error_class": "TEST_FAILURE"}))
    do, dont = get_constraints("SEMANTIC_MISMATCH")
    check("constraints_known", is_known("SEMANTIC_MISMATCH"))
    check("constraints_do_dont", len(do) >= 2 and len(dont) >= 2)
    check("constraints_dont_delete_doc", any("doc" in x.lower() for x in dont))


if __name__ == "__main__":
    print("Stage H — semantic audit smoke:")
    test_extract_python()
    test_extract_rust()
    test_extract_js()
    test_check_intent()
    test_audit_cache()
    test_auditor_flags_mismatch()
    test_auditor_ok_no_errors()
    test_auditor_no_guard()
    test_auditor_uses_cache()
    test_semantic_mismatch_class()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage H: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
