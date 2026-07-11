"""
Stage I.2 — LSP integration smoke.

Проверяем БЕЗ реального rust-analyzer/pyright (их нет в песочнице):
  * helpers: _uri_to_path, _as_locations (single/list/LocationLink);
  * discovery: find_server/is_server_available — мягкий None, когда сервера нет;
  * graceful: resolver с битой командой → cross_file_context_at == "";
  * end-to-end против FAKE LSP-сервера (поддельный stdio JSON-RPC) — реально
    гоняем Content-Length framing + initialize/didOpen/definition/references;
  * пустой ответ сервера → "" (нет шумного блока);
  * wiring в GeneratePatchStage: флаг use_lsp on/off, _get_lsp_resolver
    при выключенном флаге не пытается ничего запускать (None).

Запуск: python3 tests/test_phase_i_lsp.py
"""

import sys
import types
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# core.stages.__init__ тянет generate_patch_stage → fixers.toml_patcher → tomlkit.
try:  # pragma: no cover
    import tomlkit  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from analysis.lsp_client import (
    LspClient, LspSymbolResolver, find_server, is_server_available,
    _uri_to_path, _as_locations,
)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---- FAKE LSP server (записываем во временный .py и запускаем как процесс) ----
_FAKE_SERVER = r'''
import sys, json

def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if line == b"":
            break
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
    n = int(headers.get(b"content-length", b"0") or b"0")
    body = sys.stdin.buffer.read(n) if n else b"{}"
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}

def write_msg(msg):
    data = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()

EMPTY = (len(sys.argv) > 1 and sys.argv[1] == "empty")

while True:
    msg = read_msg()
    if msg is None:
        break
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        write_msg({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
    elif method == "textDocument/definition":
        if EMPTY:
            write_msg({"jsonrpc": "2.0", "id": mid, "result": None})
        else:
            write_msg({"jsonrpc": "2.0", "id": mid, "result": [
                {"uri": "file:///proj/src/other.rs",
                 "range": {"start": {"line": 9, "character": 4},
                           "end": {"line": 9, "character": 8}}}]})
    elif method == "textDocument/references":
        if EMPTY:
            write_msg({"jsonrpc": "2.0", "id": mid, "result": []})
        else:
            write_msg({"jsonrpc": "2.0", "id": mid, "result": [
                {"uri": "file:///proj/src/a.rs",
                 "range": {"start": {"line": 0, "character": 0},
                           "end": {"line": 0, "character": 1}}},
                {"uri": "file:///proj/src/b.rs",
                 "range": {"start": {"line": 2, "character": 0},
                           "end": {"line": 2, "character": 1}}}]})
    elif method == "shutdown":
        write_msg({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        break
    # notifications (initialized / didOpen) — игнорируем
'''


def _write_fake_server(d: Path) -> Path:
    p = d / "fake_lsp_server.py"
    p.write_text(_FAKE_SERVER, encoding="utf-8")
    return p


# ---- helpers -------------------------------------------------------------
def test_uri_to_path():
    check("uri_posix", _uri_to_path("file:///tmp/a/b.rs") == "/tmp/a/b.rs")
    check("uri_windows", _uri_to_path("file:///C:/x/y.rs") == "C:/x/y.rs")
    check("uri_empty", _uri_to_path("") == "")
    check("uri_passthrough", _uri_to_path("untitled:foo") == "untitled:foo")


def test_as_locations():
    single = {"uri": "file:///x", "range": {"start": {"line": 1, "character": 2}}}
    check("loc_single", len(_as_locations(single)) == 1)
    check("loc_list", len(_as_locations([single, single])) == 2)
    link = {"targetUri": "file:///y",
            "targetRange": {"start": {"line": 3, "character": 0}}}
    out = _as_locations([link])
    check("loc_locationlink", len(out) == 1 and out[0]["uri"] == "file:///y")
    check("loc_none", _as_locations(None) == [])
    check("loc_empty", _as_locations([]) == [])


# ---- discovery (мягкая деградация без сервера) ---------------------------
def test_discovery_graceful():
    # В песочнице language-server'ов нет → None/False, без исключений.
    check("find_unknown_lang_none", find_server("cobol") is None)
    rust = find_server("rust")
    check("find_rust_none_or_list", rust is None or isinstance(rust, list))
    check("is_available_bool", isinstance(is_server_available("rust"), bool))


def test_for_language_no_server():
    with tempfile.TemporaryDirectory() as d:
        # Язык без серверов-кандидатов → точно None.
        check("for_language_unknown_none",
              LspSymbolResolver.for_language("cobol", d) is None)


# ---- graceful: битая команда сервера -------------------------------------
def test_bogus_command_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        r = LspSymbolResolver("rust", ["definitely-not-a-real-binary-xyz"], d, timeout=2.0)
        check("bogus_available_true", r.available is True)  # cmd задана
        # но запуститься не сможет → пустой результат, без исключения
        check("bogus_ctx_empty", r.cross_file_context_at("src/lib.rs", 5, 3, text="x") == "")


def test_no_cmd_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        r = LspSymbolResolver("rust", [], d, timeout=2.0)
        check("nocmd_unavailable", r.available is False)
        check("nocmd_ctx_empty", r.cross_file_context_at("src/lib.rs", 5, 3, text="x") == "")


# ---- end-to-end против fake-сервера (реальный framing + JSON-RPC) --------
def test_end_to_end_fake_server():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        fake = _write_fake_server(d)
        r = LspSymbolResolver("rust", [sys.executable, str(fake)], d, timeout=8.0)
        block = r.cross_file_context_at("src/lib.rs", 10, 5, text="fn main() {}\n")
        check("e2e_nonempty", bool(block))
        check("e2e_has_def", "def:" in block and "other.rs" in block)
        check("e2e_has_refs", "used in 2 place(s)" in block)
        check("e2e_ref_files", "a.rs" in block and "b.rs" in block)


def test_end_to_end_empty_result():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        fake = _write_fake_server(d)
        r = LspSymbolResolver("rust", [sys.executable, str(fake), "empty"], d, timeout=8.0)
        block = r.cross_file_context_at("src/lib.rs", 10, 5, text="fn main() {}\n")
        check("e2e_empty_block", block == "")


def test_lspclient_framing_roundtrip():
    # Поднимаем fake-сервер напрямую через LspClient: initialize должен вернуть
    # capabilities → значит Content-Length framing туда-обратно работает.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        fake = _write_fake_server(d)
        c = LspClient([sys.executable, str(fake)], d, timeout=8.0)
        ok = c.start()
        try:
            check("framing_start_ok", ok is True)
            if ok:
                res = c.definition("file:///proj/src/lib.rs", 9, 4)
                locs = _as_locations(res)
                check("framing_definition", len(locs) == 1 and "other.rs" in locs[0]["uri"])
        finally:
            c.stop()


# ---- wiring в GeneratePatchStage -----------------------------------------
def test_wiring_flag_and_resolver():
    from core.stages.generate_patch_stage import GeneratePatchStage as G

    class _Ctx:
        def __init__(self, on):
            self.config = {"pipeline": {"use_lsp": on}}

    check("wire_flag_off", G._lsp_enabled(_Ctx(False)) is False)
    check("wire_flag_on", G._lsp_enabled(_Ctx(True)) is True)
    check("wire_flag_no_config", G._lsp_enabled(object()) is False)

    # _get_lsp_resolver при выключенном флаге не строит резолвер (None),
    # не пытаясь искать сервер.
    stage = G.__new__(G)  # без __init__ — нам нужны только методы-хелперы
    with tempfile.TemporaryDirectory() as d:
        check("wire_resolver_off_none",
              stage._get_lsp_resolver(_Ctx(False), d, "rust") is None)
        # флаг включён, но сервера нет → тоже None (мягкая деградация)
        res_on = stage._get_lsp_resolver(_Ctx(True), d, "rust")
        check("wire_resolver_on_none_when_no_server",
              res_on is None or isinstance(res_on, LspSymbolResolver))


if __name__ == "__main__":
    print("Stage I.2 (LSP) smoke:")
    test_uri_to_path()
    test_as_locations()
    test_discovery_graceful()
    test_for_language_no_server()
    test_bogus_command_returns_empty()
    test_no_cmd_returns_empty()
    test_end_to_end_fake_server()
    test_end_to_end_empty_result()
    test_lspclient_framing_roundtrip()
    test_wiring_flag_and_resolver()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage I.2 LSP: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
