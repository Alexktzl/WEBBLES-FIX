"""
Stage I.2 — LSP integration (опциональный точный слой над regex-индексом I.1).

Talks to a language server (rust-analyzer / pyright / typescript-language-server)
по протоколу LSP поверх stdio (JSON-RPC + Content-Length framing) и отдаёт ТОЧНЫЕ
`definition` / `references` для позиции ошибки. Это дороже и точнее, чем regex-овый
`ProjectSymbolIndex` (I.1): настоящий scope/type-резолвинг сервера, а не «по имени».

Honest-ограничения и безопасность (в духе project-инструкций — без новой
архитектуры, всё опционально и деградирует мягко):

  * Если сервер не установлен (нет бинаря в PATH) — `LspSymbolResolver.for_language`
    вернёт None, и слой молча выключается (fallback на regex-индекс I.1).
  * Жёсткие таймауты на каждый запрос; ЛЮБОЙ сбой → пустой результат, мы не падаем.
  * По умолчанию выключено флагом `config.pipeline.use_lsp` (как `run_tests` /
    `semantic_audit`); включается осознанно.

Модуль самодостаточен: low-level `LspClient` (framing + JSON-RPC) и high-level
`LspSymbolResolver` (discovery бинаря + форматирование блока для case-file).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Discovery: какой сервер для какого языка
# --------------------------------------------------------------------------
_LANG_ID = {
    "rust": "rust", "rs": "rust",
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
}


def _server_candidates(language: str) -> List[List[str]]:
    """Список команд-кандидатов (первый найденный в PATH побеждает)."""
    l = (language or "").lower()
    if l in ("rust", "rs"):
        return [["rust-analyzer"]]
    if l in ("python", "py"):
        return [["pyright-langserver", "--stdio"], ["pylsp"]]
    if l in ("javascript", "js", "typescript", "ts"):
        return [["typescript-language-server", "--stdio"]]
    return []


def find_server(language: str) -> Optional[List[str]]:
    """Возвращает команду запуска language-server'а или None, если бинаря нет."""
    for cmd in _server_candidates(language):
        if cmd and shutil.which(cmd[0]):
            return list(cmd)
    return None


def is_server_available(language: str) -> bool:
    return find_server(language) is not None


# --------------------------------------------------------------------------
# URI <-> path helpers
# --------------------------------------------------------------------------
def _uri_to_path(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path)
        # Windows: "/C:/foo" -> "C:/foo"
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return path
    return uri


def _as_locations(result: Any) -> List[Dict[str, Any]]:
    """Нормализует ответ definition/references к списку {uri, range}.

    LSP может вернуть None, одну Location, список Location или список
    LocationLink (с targetUri/targetRange) — приводим всё к одному виду.
    """
    if not result:
        return []
    items = result if isinstance(result, list) else [result]
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if "uri" in it and "range" in it:
            out.append({"uri": it["uri"], "range": it["range"]})
        elif "targetUri" in it:  # LocationLink
            out.append({
                "uri": it["targetUri"],
                "range": it.get("targetRange") or it.get("targetSelectionRange") or {},
            })
    return out


# --------------------------------------------------------------------------
# Low-level JSON-RPC client over stdio
# --------------------------------------------------------------------------
class LspClient:
    """Минимальный LSP-клиент: framing + request/notify + reader-поток.

    Не претендует на полноту протокола — ровно то, что нужно для
    definition/references по одной позиции. Всё обёрнуто так, чтобы любой
    сбой приводил к None/пустому результату, а не к исключению наружу.
    """

    def __init__(self, cmd: List[str], root: Path, timeout: float = 8.0):
        self.cmd = list(cmd)
        self.root = Path(root)
        self.timeout = float(timeout)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cond = threading.Condition()
        self._wlock = threading.Lock()
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1
        self._stop = threading.Event()

    # ---- framing -----------------------------------------------------
    def _read_line(self) -> Optional[bytes]:
        line = b""
        while True:
            ch = self._proc.stdout.read(1)
            if not ch:
                return line or None
            line += ch
            if ch == b"\n":
                return line

    def _read_exactly(self, n: int) -> Optional[bytes]:
        data = b""
        while len(data) < n:
            chunk = self._proc.stdout.read(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _read_message(self) -> Optional[Dict[str, Any]]:
        headers: Dict[bytes, bytes] = {}
        while True:
            line = self._read_line()
            if line is None:
                return None
            line = line.strip()
            if line == b"":
                break
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get(b"content-length", b"0") or b"0")
        if length <= 0:
            return {}
        body = self._read_exactly(length)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {}

    def _write(self, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg).encode("utf-8")
        header = ("Content-Length: %d\r\n\r\n" % len(data)).encode("ascii")
        with self._wlock:
            self._proc.stdin.write(header + data)
            self._proc.stdin.flush()

    # ---- reader loop -------------------------------------------------
    def _reader_loop(self) -> None:
        try:
            while not self._stop.is_set():
                msg = self._read_message()
                if msg is None:
                    break
                if not msg:
                    continue
                mid = msg.get("id")
                if mid is not None and ("result" in msg or "error" in msg):
                    with self._cond:
                        self._responses[mid] = msg
                        self._cond.notify_all()
                elif mid is not None and "method" in msg:
                    # server-initiated request — минимально подтверждаем,
                    # чтобы сервер не завис в ожидании ответа.
                    try:
                        self._write({"jsonrpc": "2.0", "id": mid, "result": None})
                    except Exception:
                        pass
                # notifications (без id) игнорируем
        except Exception as e:  # pragma: no cover
            logger.debug("LSP reader-поток остановлен: %s", e)

    # ---- public API --------------------------------------------------
    def request(self, method: str, params: Any, timeout: Optional[float] = None) -> Any:
        timeout = self.timeout if timeout is None else timeout
        with self._cond:
            mid = self._next_id
            self._next_id += 1
        try:
            self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except Exception as e:
            logger.debug("LSP write упал (%s): %s", method, e)
            return None
        deadline = time.time() + timeout
        with self._cond:
            while mid not in self._responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            msg = self._responses.pop(mid)
        if "error" in msg:
            logger.debug("LSP %s вернул error: %s", method, msg.get("error"))
            return None
        return msg.get("result")

    def notify(self, method: str, params: Any) -> None:
        try:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception as e:
            logger.debug("LSP notify упал (%s): %s", method, e)

    def start(self) -> bool:
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
        except Exception as e:
            logger.debug("LSP не удалось запустить %s: %s", self.cmd, e)
            return False
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        init = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.root.as_uri(),
            "capabilities": {},
        })
        if init is None:
            self.stop()
            return False
        self.notify("initialized", {})
        return True

    def did_open(self, uri: str, language_id: str, text: str, version: int = 1) -> None:
        self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": language_id,
                             "version": version, "text": text},
        })

    def definition(self, uri: str, line: int, char: int) -> Any:
        return self.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        })

    def references(self, uri: str, line: int, char: int, include_decl: bool = True) -> Any:
        return self.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
            "context": {"includeDeclaration": include_decl},
        })

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._proc and self._proc.poll() is None:
                self.request("shutdown", None, timeout=2.0)
                self.notify("exit", None)
        except Exception:
            pass
        try:
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------
# High-level resolver (то, что дёргает case-file)
# --------------------------------------------------------------------------
class LspSymbolResolver:
    """Достаёт точный cross-file контекст для позиции ошибки через LSP."""

    def __init__(self, language: str, cmd: List[str], root, timeout: float = 8.0):
        self.language = (language or "").lower()
        self.cmd = list(cmd) if cmd else []
        self.root = Path(root)
        self.timeout = float(timeout)

    @classmethod
    def for_language(cls, language: str, root, timeout: float = 8.0) -> Optional["LspSymbolResolver"]:
        cmd = find_server(language)
        if not cmd:
            return None
        return cls(language, cmd, root, timeout)

    @property
    def available(self) -> bool:
        return bool(self.cmd)

    # ------------------------------------------------------------------
    def cross_file_context_at(self, rel_file: str, line: int, column: int,
                              text: Optional[str] = None) -> str:
        """Точный блок «где определён / где используется символ под курсором».

        Возвращает форматированный текст для case-file либо "" при любом сбое
        (нет сервера, таймаут, сервер не дал результата). 1-based line/column
        на входе (как в error-dict) → конвертируем в 0-based для LSP.
        """
        if not self.available:
            return ""
        try:
            abs_path = (self.root / rel_file)
            if text is None:
                text = abs_path.read_text(encoding="utf-8", errors="ignore")
            uri = abs_path.as_uri()
            lang_id = _LANG_ID.get(self.language, self.language)
            client = LspClient(self.cmd, self.root, timeout=self.timeout)
            if not client.start():
                return ""
            try:
                client.did_open(uri, lang_id, text)
                lsp_line = max(0, int(line) - 1)
                lsp_char = max(0, int(column) - 1) if column else 0
                defs = client.definition(uri, lsp_line, lsp_char)
                refs = client.references(uri, lsp_line, lsp_char)
            finally:
                client.stop()
            return self._format(rel_file, line, defs, refs)
        except Exception as e:
            logger.debug("LSP cross_file_context_at упал: %s", e)
            return ""

    # ------------------------------------------------------------------
    def _loc_str(self, loc: Dict[str, Any]) -> str:
        path = _uri_to_path(loc.get("uri", ""))
        rel = path
        try:
            rel = str(Path(path).resolve().relative_to(self.root.resolve()))
        except Exception:
            rel = path
        start = (loc.get("range") or {}).get("start") or {}
        ln = int(start.get("line", 0)) + 1
        col = int(start.get("character", 0)) + 1
        return f"{rel}:{ln}:{col}"

    def _format(self, rel_file: str, line: int, defs: Any, refs: Any) -> str:
        def_locs = _as_locations(defs)
        ref_locs = _as_locations(refs)
        if not def_locs and not ref_locs:
            return ""
        server = self.cmd[0] if self.cmd else "lsp"
        parts: List[str] = [f"### LSP `{rel_file}:{line}` — precise (server: {server})"]
        for loc in def_locs[:5]:
            parts.append(f"- def: {self._loc_str(loc)}")
        if ref_locs:
            parts.append(f"used in {len(ref_locs)} place(s):")
            for loc in ref_locs[:10]:
                parts.append(f"  - {self._loc_str(loc)}")
            if len(ref_locs) > 10:
                parts.append(f"  - ... (+{len(ref_locs) - 10} more)")
        return "\n".join(parts)
