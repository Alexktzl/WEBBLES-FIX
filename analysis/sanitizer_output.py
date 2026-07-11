"""
Парсер отчётов sanitizers (ASan/UBSan/MSan/TSan/LeakSan/Valgrind).

ЗАЧЕМ. Sanitizers — динамические анализаторы: ловят UB только когда
программа РЕАЛЬНО выполняется на входе, который этот UB триггерит. Это
ортогонально статическим тулам (cppcheck/clang-tidy): где статика молчит,
динамика часто срабатывает, и наоборот.

ЧТО ДЕЛАЕТ. Этот модуль НЕ запускает программу — это работа отдельного
runner'а (требует runnable test suite). Здесь только парсер: получает
текст stderr/stdout с уже сработавшим sanitizer-отчётом и превращает в
наш `error_dict` контракт с `error_class="RUNTIME_UB"`.

Формат отчётов:
  * ASan: `==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x... at pc 0x... bp 0x... sp 0x...`
    + stack trace: `    #0 0x... in foo /path/to/file.cpp:42:7`
  * UBSan: `runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'`
    + location: `path/to/file.cpp:42:5`
  * MSan: `==12345==WARNING: MemorySanitizer: use-of-uninitialized-value`
  * TSan: `WARNING: ThreadSanitizer: data race (pid=12345)`
  * LeakSan: `Direct leak of N byte(s) in 1 object(s) allocated from:`
  * Valgrind: `==12345== Invalid read of size 4` + `==12345==    at 0x...: foo (file.cpp:42)`

Поддерживаем все эти форматы. Никогда не падаем — на любой странный
ввод возвращаем []. Используется автономно (юзер сам запустил тесты с
sanitizers и подал нам лог) ИЛИ через будущий run-test-with-sanitizer
layer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ASan / MSan / TSan / LeakSan — общая «==PID==ERROR/WARNING: <Sanitizer>: <kind>»
# ---------------------------------------------------------------------------
_RE_SAN_HEAD = re.compile(
    r"==\d+==\s*(?:ERROR|WARNING):\s+"
    r"(?P<san>AddressSanitizer|MemorySanitizer|ThreadSanitizer|LeakSanitizer):\s*"
    r"(?P<kind>[A-Za-z][\w\- ]*)"
)
# Stack frame: "    #N 0x... in <symbol> /path/to/file.cpp:42:7"
_RE_STACK_FRAME = re.compile(
    r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+(?P<sym>[^\s]+)\s+"
    r"(?P<file>[^\s:]+):(?P<line>\d+)(?::(?P<col>\d+))?"
)

# ---------------------------------------------------------------------------
# UBSan: `path/to/file.cpp:42:5: runtime error: <kind>: <details>`
# ---------------------------------------------------------------------------
_RE_UBSAN = re.compile(
    r"^(?P<file>[^\s:]+\.(?:c|cc|cpp|cxx|h|hpp)):(?P<line>\d+):(?P<col>\d+):\s*"
    r"runtime error:\s*(?P<msg>.+)$"
)

# ---------------------------------------------------------------------------
# Valgrind: первая строка `==PID== <kind>` (Invalid read/write, Conditional...)
# затем `==PID==    at 0x...: <sym> (<file>:<line>)`
# ---------------------------------------------------------------------------
_RE_VG_HEAD = re.compile(
    r"==\d+==\s*(?P<kind>Invalid (?:read|write|free)(?: of size \d+)?|"
    r"Conditional jump or move depends on uninitialised value\(s\)|"
    r"Use of uninitialised value|"
    r"Mismatched free\(\) / delete / delete \[\]|"
    r"\d+ bytes in \d+ blocks are definitely lost|"
    r"\d+ bytes in \d+ blocks are still reachable)"
)
_RE_VG_LOC = re.compile(
    r"==\d+==\s+(?:at|by)\s+0x[0-9a-fA-F]+:\s+(?P<sym>[^\(]+?)\s*"
    r"\((?P<file>[^):]+):(?P<line>\d+)\)"
)


# Маппинг kind → стабильный код Webbles (мы добавляем префикс SAN_).
_KIND_TO_CODE = {
    "heap-buffer-overflow":           "SAN_HEAP_OVERFLOW",
    "stack-buffer-overflow":          "SAN_STACK_OVERFLOW",
    "global-buffer-overflow":         "SAN_GLOBAL_OVERFLOW",
    "heap-use-after-free":            "SAN_USE_AFTER_FREE",
    "use-after-poison":               "SAN_USE_AFTER_POISON",
    "double-free":                    "SAN_DOUBLE_FREE",
    "alloc-dealloc-mismatch":         "SAN_ALLOC_MISMATCH",
    "new-delete-type-mismatch":       "SAN_NEW_DELETE_MISMATCH",
    "memory leaks":                   "SAN_MEMORY_LEAK",
    "Direct leak":                    "SAN_DIRECT_LEAK",
    "Indirect leak":                  "SAN_INDIRECT_LEAK",
    "use-of-uninitialized-value":     "SAN_UNINIT",
    "data race":                      "SAN_DATA_RACE",
    "thread leak":                    "SAN_THREAD_LEAK",
    "lock-order-inversion":           "SAN_LOCK_ORDER",
}


def _kind_to_code(kind: str) -> str:
    norm = kind.strip().lower()
    for key, code in _KIND_TO_CODE.items():
        if key.lower() in norm:
            return code
    # fallback
    return "SAN_" + re.sub(r"[^A-Za-z0-9]+", "_", kind).strip("_").upper()


def _first_stack_frame(lines: List[str], start_idx: int,
                       project_path: Optional[Path]) -> Optional[Tuple[str, int, int, str]]:
    """Ищет первый `#N 0x... in <sym> file:line[:col]` начиная с start_idx.
    Если задан `project_path`, предпочитает фреймы, путь которых лежит в нём
    (отсев системного STL/libc)."""
    best: Optional[Tuple[int, str, int, int, str]] = None  # (priority, file, line, col, sym)
    for i, line in enumerate(lines[start_idx:start_idx + 40], start=start_idx):
        m = _RE_STACK_FRAME.match(line)
        if not m:
            # Если уже не stack frame — стек закончился
            if best is not None and not line.lstrip().startswith("#"):
                break
            continue
        f = m.group("file")
        ln = int(m.group("line"))
        col = int(m.group("col") or 0)
        sym = m.group("sym")
        # Приоритет: фреймы внутри project_path
        in_proj = 0
        if project_path is not None:
            try:
                rp = Path(f).resolve()
                if str(rp).startswith(str(project_path.resolve())):
                    in_proj = 1
            except Exception:
                pass
        # Отсекаем явные системные/STL фреймы
        is_system = any(s in f for s in ("/usr/", "/lib/", "libc++", "libstdc++"))
        priority = (1 if in_proj else 0) - (1 if is_system else 0)
        if best is None or priority > best[0]:
            best = (priority, f, ln, col, sym)
    if best is None:
        return None
    return (best[1], best[2], best[3], best[4])


def _safe_rel(file_path: str, project_path: Optional[Path]) -> str:
    if not project_path:
        return file_path
    try:
        return str(Path(file_path).resolve().relative_to(project_path.resolve()))
    except (ValueError, OSError):
        return Path(file_path).name


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def parse_sanitizer_output(text: str,
                           project_path: Optional[Path] = None
                           ) -> List[Dict[str, Any]]:
    """Парсит произвольный текст (stderr+stdout с прогона тестов под sanitizers)
    и возвращает list of error_dicts.

    Контракт error_dict:
        {file, line, column, message, code (SAN_*), severity,
         error_type='runtime', error_class='RUNTIME_UB',
         sanitizer (имя инструмента)}.

    Никогда не падает — при странном вводе возвращает [].
    """
    if not text:
        return []
    try:
        lines = text.splitlines()
        out: List[Dict[str, Any]] = []
        seen: set = set()

        for i, line in enumerate(lines):
            # ASan/MSan/TSan/LeakSan
            m = _RE_SAN_HEAD.search(line)
            if m:
                san = m.group("san")
                kind = m.group("kind").strip()
                # Сообщение — первая строка целиком (для контекста LLM)
                full_msg = line.strip()
                frame = _first_stack_frame(lines, i + 1, project_path)
                if frame is None:
                    continue
                file_path, ln, col, sym = frame
                code = _kind_to_code(kind)
                rel = _safe_rel(file_path, project_path)
                key = (rel, ln, code)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "file": rel, "line": ln, "column": col,
                    "message": f"{san}: {kind} in {sym}",
                    "code": code, "severity": "error",
                    "error_type": "runtime",
                    "error_class": "RUNTIME_UB",
                    "sanitizer": san,
                })
                continue

            # UBSan: одиночный однострочный отчёт
            mu = _RE_UBSAN.match(line)
            if mu:
                file_path = mu.group("file")
                rel = _safe_rel(file_path, project_path)
                ln = int(mu.group("line"))
                col = int(mu.group("col"))
                # Код: первое слово после "runtime error:" обычно ключевое (signed-integer-overflow, ...)
                detail = mu.group("msg")
                key_kind = detail.split(":", 1)[0].strip()
                code = _kind_to_code("ubsan-" + key_kind) if key_kind else "SAN_UBSAN"
                key = (rel, ln, code)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "file": rel, "line": ln, "column": col,
                    "message": f"UBSan: {detail}",
                    "code": code, "severity": "error",
                    "error_type": "runtime",
                    "error_class": "RUNTIME_UB",
                    "sanitizer": "UndefinedBehaviorSanitizer",
                })
                continue

            # Valgrind: ищем kind, потом локацию `at 0x...:`
            mv = _RE_VG_HEAD.search(line)
            if mv:
                kind = mv.group("kind").strip()
                # Ищем первую `at` локацию в ближайших ~20 строках
                loc: Optional[Tuple[str, int, str]] = None
                for j in range(i + 1, min(i + 20, len(lines))):
                    ml = _RE_VG_LOC.search(lines[j])
                    if ml:
                        loc = (ml.group("file"), int(ml.group("line")),
                               ml.group("sym").strip())
                        break
                if loc is None:
                    continue
                file_path, ln, sym = loc
                rel = _safe_rel(file_path, project_path)
                code = _kind_to_code(kind)
                key = (rel, ln, code)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "file": rel, "line": ln, "column": 0,
                    "message": f"Valgrind: {kind} in {sym}",
                    "code": code, "severity": "error",
                    "error_type": "runtime",
                    "error_class": "RUNTIME_UB",
                    "sanitizer": "Valgrind",
                })
                continue

        logger.info("sanitizer_output: %d findings", len(out))
        return out
    except Exception as e:
        logger.warning("parse_sanitizer_output упал: %s", e)
        return []


def parse_sanitizer_log_file(path: Path,
                             project_path: Optional[Path] = None
                             ) -> List[Dict[str, Any]]:
    """Удобная обёртка: читает файл и парсит. Graceful на отсутствующий файл."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return parse_sanitizer_output(text, project_path)
