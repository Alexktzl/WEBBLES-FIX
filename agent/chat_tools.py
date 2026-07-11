"""
Инструменты для чат-LLM: возможность «смотреть файлы» проекта.

Концепция: LLM поддерживает function/tool calling (OpenAI-совместимый формат).
В ответе модели может прийти `tool_calls` с просьбой вызвать функцию вроде
`read_file("src/main.rs")`. Мы безопасно её выполняем (только в пределах
указанного `root`), возвращаем результат обратно как `tool`-сообщение, и
повторяем цикл, пока LLM не сформулирует финальный ответ.

Что нельзя:
  * выходить за пределы `root` (`..` развёртываем и валидируем);
  * читать файлы крупнее `max_bytes` (по умолчанию 64 KiB) — отдадим обрезку;
  * заходить в служебные каталоги (`.git`, `node_modules`, `.webbles_fix`, ...).

Модули, которые LLM может вызывать:
  * `read_file(path, max_bytes?, head?)`  — текст файла (обрезка по `max_bytes`,
                                           опционально только первые `head`
                                           строк);
  * `list_dir(path?)`                      — список содержимого папки;
  * `grep(pattern, glob?, max_hits?)`      — наивный поиск по тексту.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Каталоги, в которые tools НЕ заходят — синхронно с project_tree.SKIP_DIRS,
# но дополнительно прячем сам chat/ (там история и changes — личное).
SKIP_DIRS = {
    "target", "node_modules", ".git", "venv", ".venv",
    "__pycache__", ".webbles_fix", ".webbles", ".webbles_backups",
    ".webles_sandbox", ".webbles_sandbox",
    "dist", "build", ".idea", ".vscode", ".tox", ".pytest_cache",
    ".next", ".cache", ".gradle", ".mvn",
    "chat",  # личная папка чата
    "statistic",  # отчёты прогонов
}

# P0.4: имена файлов (basename), которые чат-агент НИКОГДА не должен видеть,
# даже если они лежат не в служебной директории. Это последняя линия обороны
# против утечки секретов в LLM-промпт. Сравнение case-insensitive по basename.
SENSITIVE_FILENAMES = {
    ".env", ".env.local", ".env.development", ".env.production",
    ".env.test", ".envrc",
    "secrets.txt", "secrets.json", "secrets.yaml", "secrets.yml",
    "credentials.json", "credentials.yaml", "credentials.yml",
    "webles_secrets.json", "webbles_secrets.json",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".netrc", ".pgpass", ".aws/credentials",
}
# Глобы-расширения, которые тоже блокируем.
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
DEFAULT_MAX_BYTES = 64 * 1024
MAX_GREP_HITS = 100
MAX_LIST_ENTRIES = 500


def _is_sensitive_file(path_str: str) -> bool:
    """True если путь указывает на secret/.env/key — чат не должен этого видеть."""
    if not path_str:
        return False
    norm = path_str.replace("\\", "/")
    name = norm.rsplit("/", 1)[-1].lower()
    if name in {f.lower() for f in SENSITIVE_FILENAMES}:
        return True
    if any(name.endswith(s) for s in SENSITIVE_SUFFIXES):
        return True
    # Парные «*.local.env» / `*_secrets.json` тоже считаем секретом.
    if "secret" in name and name.endswith((".json", ".yaml", ".yml", ".txt", ".ini")):
        return True
    return False


class ToolError(RuntimeError):
    """Любая ошибка выполнения tool — сообщение LLM получит как `tool` ответ."""


def _resolve_safe(root: Path, rel: str) -> Path:
    """Возвращает абсолютный путь, гарантированно внутри `root`.

    Любая попытка выйти за `root` через `..` или абсолютный путь — `ToolError`.
    """
    if rel is None:
        rel = ""
    rel = str(rel).replace("\\", "/").lstrip("/")
    # Запрещаем абсолютные пути и URI.
    if rel and (rel.startswith("/") or ":" in rel.split("/", 1)[0]):
        raise ToolError(f"путь должен быть относительным: {rel}")
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise ToolError(f"путь вне корня проекта: {rel}")
    return target


def _is_skipped(rel: str) -> bool:
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    return any(p in SKIP_DIRS for p in parts)


# ----------------------------------------------------------------------
# Реализации tools
# ----------------------------------------------------------------------
def tool_read_file(root: Path, *, path: str,
                   max_bytes: int = DEFAULT_MAX_BYTES,
                   head: Optional[int] = None) -> Dict[str, Any]:
    """Читает текст файла (UTF-8 с заменой). Бинарь — отказ."""
    if not path:
        raise ToolError("параметр path обязателен")
    if _is_skipped(path):
        raise ToolError(f"путь в служебной директории, читать нельзя: {path}")
    if _is_sensitive_file(path):
        raise ToolError(
            f"файл {path!r} содержит чувствительные данные "
            "(.env / secrets / private key) — чтение запрещено"
        )
    target = _resolve_safe(root, path)
    if not target.exists():
        raise ToolError(f"нет такого файла: {path}")
    if target.is_dir():
        raise ToolError(f"это директория, не файл: {path}")
    try:
        raw = target.read_bytes()
    except Exception as e:
        raise ToolError(f"не удалось прочитать: {e}")
    # Грубая эвристика бинарных файлов — много NUL.
    if b"\x00" in raw[:4096]:
        raise ToolError(f"файл выглядит бинарным: {path}")
    max_bytes = max(1024, int(max_bytes or DEFAULT_MAX_BYTES))
    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True
    text = raw.decode("utf-8", errors="replace")
    if head is not None and head > 0:
        lines = text.splitlines()
        text = "\n".join(lines[: int(head)])
        if len(lines) > head:
            truncated = True
    return {
        "path": str(target.relative_to(root.resolve())).replace("\\", "/"),
        "bytes": len(raw),
        "truncated": truncated,
        "content": text,
    }


def tool_list_dir(root: Path, *, path: str = "") -> Dict[str, Any]:
    """Листинг каталога относительно root."""
    if _is_skipped(path):
        raise ToolError(f"служебная директория: {path}")
    target = _resolve_safe(root, path or "")
    if not target.exists():
        raise ToolError(f"нет такого каталога: {path}")
    if target.is_file():
        raise ToolError(f"это файл, не каталог: {path}")
    items: List[Dict[str, Any]] = []
    try:
        entries = sorted(target.iterdir(),
                         key=lambda p: (0 if p.is_dir() else 1, p.name.lower()))
    except Exception as e:
        raise ToolError(f"не удалось прочитать каталог: {e}")
    truncated = False
    for entry in entries:
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        # P0.4: secrets/.env / *.pem / id_rsa — не светим их даже в listing.
        if entry.is_file() and _is_sensitive_file(entry.name):
            continue
        rel = str(entry.relative_to(root.resolve())).replace("\\", "/")
        size = 0
        try:
            if entry.is_file():
                size = entry.stat().st_size
        except Exception:
            pass
        items.append({
            "path": rel,
            "kind": "dir" if entry.is_dir() else "file",
            "size": size,
        })
        if len(items) >= MAX_LIST_ENTRIES:
            truncated = True
            break
    return {
        "path": str(target.relative_to(root.resolve())).replace("\\", "/") or ".",
        "entries": items,
        "truncated": truncated,
    }


def tool_grep(root: Path, *, pattern: str, glob: str = "**/*",
              max_hits: int = MAX_GREP_HITS) -> Dict[str, Any]:
    """Наивный grep по тексту. Регистронезависимый, regex."""
    if not pattern:
        raise ToolError("параметр pattern обязателен")
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        raise ToolError(f"невалидный regex: {e}")
    hits: List[Dict[str, Any]] = []
    truncated = False
    glob = glob or "**/*"
    base = root.resolve()
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(base)).replace("\\", "/")
        if _is_skipped(rel):
            continue
        # P0.4: secrets/.env-файлы — не грепаем (чтобы значения не утекли в hits).
        if _is_sensitive_file(rel):
            continue
        # glob фильтр — поддерживаем простой fnmatch.
        if glob and glob != "**/*" and not fnmatch.fnmatch(rel, glob):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append({"path": rel, "line": i, "text": line.strip()[:200]})
                if len(hits) >= int(max_hits or MAX_GREP_HITS):
                    truncated = True
                    return {"pattern": pattern, "hits": hits, "truncated": truncated}
    return {"pattern": pattern, "hits": hits, "truncated": truncated}


# ----------------------------------------------------------------------
# Tool-schemas для OpenAI-совместимого function calling
# ----------------------------------------------------------------------
def tool_schemas() -> List[Dict[str, Any]]:
    """Возвращает массив `tools` для запроса к LLM."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Прочитать текст файла внутри корня проекта. "
                               "Бинарные/служебные пути запрещены.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Относительный путь от корня проекта."},
                        "max_bytes": {"type": "integer",
                                      "description": "Максимум байт (по умолчанию 65536)."},
                        "head": {"type": "integer",
                                 "description": "Только первые N строк."},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "Список содержимого каталога внутри корня проекта.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Относительный путь; '' — корень."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Регекс-поиск по файлам проекта (regex, "
                               "регистронезависимый).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "glob": {"type": "string",
                                 "description": "fnmatch-glob фильтра путей, "
                                                "например 'src/**/*.rs'."},
                        "max_hits": {"type": "integer"},
                    },
                    "required": ["pattern"],
                },
            },
        },
    ]


# ----------------------------------------------------------------------
# Create-режим: создание файлов и каталогов в проекте
# ----------------------------------------------------------------------
MAX_WRITE_BYTES = 256 * 1024

# Расширения, в которые писать БЕЗ предупреждения. Бинарные форматы — отказ.
_TEXT_LIKE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".kts", ".scala", ".rb",
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx",
    ".cs", ".vb",
    ".md", ".rst", ".txt", ".log",
    ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".html", ".htm", ".css", ".scss", ".sass",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1",
    ".sql", ".graphql",
    ".env.example", ".env", ".gitignore", ".editorconfig",
}


def _is_text_like(path: Path) -> bool:
    suf = path.suffix.lower()
    return suf in _TEXT_LIKE_EXT or path.name in {".gitignore", ".env", ".env.example"}


def tool_write_file(root: Path, *, path: str, content: str,
                    overwrite: bool = True,
                    allow_sensitive: bool = False) -> Dict[str, Any]:
    """Создаёт или перезаписывает текстовый файл внутри `root`.

    Параметр `allow_sensitive` управляет правом писать в `.env`/`secrets.*`/
    `*.pem` и т.п.:
      * Fix-режим: `allow_sensitive=False` (default) — секреты неприкосновенны,
        чтобы LLM не записала в них ошибочный/сгенерированный контент.
      * Create-режим: `allow_sensitive=True` — пользователь явно создаёт
        новый проект и `.env` / `secrets.txt` тут могут быть нужны как
        часть scaffold'а.
    """
    if not path:
        raise ToolError("параметр path обязателен")
    if _is_skipped(path):
        raise ToolError(f"путь в служебной директории — писать нельзя: {path}")
    # P0.4: чувствительные имена. Разрешены ТОЛЬКО в Create-режиме (явное
    # `allow_sensitive=True`). В Fix-режиме — отказ.
    if _is_sensitive_file(path) and not allow_sensitive:
        raise ToolError(
            f"запрещено создавать/перезаписывать файл с чувствительным "
            f"именем в Fix-режиме: {path} "
            "(.env / secrets / private key и т.п.). В Create-режиме разрешено."
        )
    target = _resolve_safe(root, path)
    if target.exists() and target.is_dir():
        raise ToolError(f"это директория, не файл: {path}")
    if not _is_text_like(target):
        raise ToolError(f"можно писать только в текстовые файлы (расширение): {path}")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ToolError("параметр content должен быть строкой")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolError(f"файл слишком большой (>{MAX_WRITE_BYTES} байт): {path}")
    if target.exists() and not overwrite:
        raise ToolError(f"файл уже существует: {path}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        raise ToolError(f"не удалось записать: {e}")
    return {
        "path": str(target.relative_to(root.resolve())).replace("\\", "/"),
        "bytes": len(content.encode("utf-8")),
        "created": True,
    }


def tool_make_dir(root: Path, *, path: str) -> Dict[str, Any]:
    """Создаёт каталог (рекурсивно) внутри `root`."""
    if not path:
        raise ToolError("параметр path обязателен")
    if _is_skipped(path):
        raise ToolError(f"служебная директория, создавать нельзя: {path}")
    target = _resolve_safe(root, path)
    if target.exists() and target.is_file():
        raise ToolError(f"уже существует как файл: {path}")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise ToolError(f"не удалось создать каталог: {e}")
    return {
        "path": str(target.relative_to(root.resolve())).replace("\\", "/") or ".",
        "created": True,
    }


# ----------------------------------------------------------------------
# #4: cross-chat tools — список своих чатов + чтение истории конкретного.
# Корень для этих tools — папка `chat/` (там лежит index.json + sessions/),
# а НЕ корень проекта. Поэтому функции принимают `chat_root` отдельно.
# ----------------------------------------------------------------------
def tool_list_chats(chat_root: Path) -> Dict[str, Any]:
    """Список всех чатов пользователя (без содержимого)."""
    from agent import chat_registry as cr
    try:
        chats = cr.list_chats(Path(chat_root))
    except Exception as e:
        raise ToolError(f"не удалось прочитать список чатов: {e}")
    # Прячем активный чат не нужно — это публичная информация для LLM.
    return {"chats": chats, "count": len(chats)}


def tool_read_chat(chat_root: Path, *, chat_id: str = "",
                   name: str = "", limit: int = 20) -> Dict[str, Any]:
    """Читает последние N сообщений из чата (по id или по имени)."""
    from agent import chat_registry as cr
    chat_root = Path(chat_root)
    if not chat_id and not name:
        raise ToolError("укажите chat_id или name")
    target_id = chat_id or ""
    if not target_id and name:
        # Ищем по имени (case-insensitive substring), берём самый новый.
        n_low = str(name).strip().lower()
        candidates = [
            c for c in cr.list_chats(chat_root)
            if n_low in (c.get("name") or "").lower()
        ]
        if not candidates:
            raise ToolError(f"чат с именем '{name}' не найден")
        # list_chats уже отсортирован по ts desc — берём первый.
        target_id = candidates[0].get("id") or ""
    if not target_id:
        raise ToolError("не удалось определить chat_id")

    try:
        hp = cr.history_path(chat_root, target_id)
    except Exception as e:
        raise ToolError(f"путь к истории чата невалиден: {e}")
    if not hp.exists():
        return {"chat_id": target_id, "messages": [], "count": 0}

    limit = max(1, min(int(limit or 20), 200))
    # Читаем последние `limit` строк (jsonl). Простой подход: считываем все,
    # берём хвост — истории чата редко превышают пару сотен KB.
    out: List[Dict[str, Any]] = []
    try:
        with hp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                role = rec.get("role")
                if role not in ("user", "assistant"):
                    continue  # tool-сообщения и системные пропускаем
                content = rec.get("content")
                if not isinstance(content, str):
                    continue
                # Обрезаем длинные сообщения, чтобы не раздувать промпт LLM.
                if len(content) > 800:
                    content = content[:800] + "… [truncated]"
                out.append({
                    "role": role,
                    "content": content,
                    "ts": rec.get("ts", ""),
                })
    except Exception as e:
        raise ToolError(f"не удалось прочитать историю: {e}")

    tail = out[-limit:]
    return {
        "chat_id": target_id,
        "messages": tail,
        "count": len(tail),
        "total_messages_in_chat": len(out),
    }


def cross_chat_tool_schemas() -> List[Dict[str, Any]]:
    """Schemas для tools, которые работают в режиме `chat` (не Fix/Create).
    Дают LLM возможность по запросу пользователя «посмотри в чате X»
    подгрузить краткий контекст из соседнего чата того же пользователя."""
    return [
        {
            "type": "function",
            "function": {
                "name": "list_chats",
                "description": (
                    "Список всех чатов пользователя (для перекрёстных "
                    "ссылок). Используй, когда нужно найти chat_id по "
                    "названию или показать пользователю, какие у него есть чаты."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_chat",
                "description": (
                    "Прочитать последние сообщения другого чата того же "
                    "пользователя. Принимает chat_id ИЛИ name (частичное "
                    "совпадение). Возвращает массив {role, content, ts}. "
                    "Используй для краткого контекста, когда пользователь "
                    "просит «посмотри что было в чате X»."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string",
                                    "description": "ID чата из list_chats."},
                        "name": {"type": "string",
                                 "description": "Имя чата (substring, case-insensitive)."},
                        "limit": {"type": "integer",
                                  "description": "Сколько последних сообщений (default 20, max 200)."},
                    },
                },
            },
        },
    ]


def write_tool_schemas() -> List[Dict[str, Any]]:
    """Tools, доступные ТОЛЬКО в Create-режиме."""
    return [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Создать или перезаписать текстовый файл в "
                               "корне проекта (только Create-режим).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Относительный путь от корня проекта."},
                        "content": {"type": "string",
                                    "description": "Полный текст файла."},
                        "overwrite": {"type": "boolean",
                                      "description": "Разрешить перезапись (по умолчанию true)."},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "make_dir",
                "description": "Создать каталог (рекурсивно) внутри корня "
                               "проекта (только Create-режим).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Относительный путь от корня проекта."},
                    },
                    "required": ["path"],
                },
            },
        },
    ]


def dispatch_tool_call(root: Path, name: str, args: Dict[str, Any],
                       mode: str = "chat",
                       chat_root: Optional[Path] = None) -> str:
    """Выполняет один tool_call, возвращает JSON-строку для роли `tool`.

    `mode` — текущий режим чата (chat / fix / create). От него зависит,
    можно ли писать в чувствительные файлы: `.env`, `secrets.*`, `*.pem`.
    Только в режиме "create" даём write_file/make_dir создавать такие
    файлы (это часть scaffold нового проекта). В fix/chat — отказ.

    `chat_root` — путь к папке `chat/` для cross-chat tools (#4). Если
    не передан, используется `root` (для обратной совместимости).
    """
    args = args or {}
    is_create = str(mode or "").lower() == "create"
    chat_root_path = Path(chat_root) if chat_root else Path(root)
    try:
        if name == "read_file":
            result = tool_read_file(root,
                                    path=args.get("path", ""),
                                    max_bytes=int(args.get("max_bytes") or DEFAULT_MAX_BYTES),
                                    head=args.get("head"))
        elif name == "list_dir":
            result = tool_list_dir(root, path=args.get("path", ""))
        elif name == "grep":
            result = tool_grep(root,
                               pattern=args.get("pattern", ""),
                               glob=args.get("glob", "**/*"),
                               max_hits=int(args.get("max_hits") or MAX_GREP_HITS))
        elif name == "write_file":
            result = tool_write_file(root,
                                     path=args.get("path", ""),
                                     content=args.get("content", ""),
                                     overwrite=bool(args.get("overwrite", True)),
                                     allow_sensitive=is_create)
        elif name == "make_dir":
            result = tool_make_dir(root, path=args.get("path", ""))
        elif name == "list_chats":
            result = tool_list_chats(chat_root_path)
        elif name == "read_chat":
            result = tool_read_chat(
                chat_root_path,
                chat_id=str(args.get("chat_id") or ""),
                name=str(args.get("name") or ""),
                limit=int(args.get("limit") or 20),
            )
        else:
            return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    except ToolError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:
        logger.warning("tool %s упал: %s", name, e)
        return json.dumps({"error": f"internal: {e}"}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)
