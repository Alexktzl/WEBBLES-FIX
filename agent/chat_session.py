"""
Сессия чата с LLM — персистентная между запусками, с file-browsing.

Сохраняет в папке `<root>/chat/`:
  - `rules.md`       — правила поведения чата (system-prompt). При первом
                       запуске пишем дефолт.
  - `history.jsonl`  — каждая строка `{role, content, ts}` — переживает
                       перезапуск окна.
  - `changes.jsonl`  — лог изменений движка (ведётся `changes_log`, мы
                       читаем последние N и кладём в system-prompt).

LLM поддерживает function-calling. Доступные tools (см. `chat_tools.py`):
  * `read_file(path, max_bytes?, head?)`,
  * `list_dir(path?)`,
  * `grep(pattern, glob?, max_hits?)`.
Корнем для tools служит `tools_root` (по умолчанию — каталог проекта,
выбранный пользователем; если не задан — `root`).

`ChatSession.send(user_text)` крутит цикл: LLM → tool_calls → результаты →
снова LLM → … → финальный текст. Лимит итераций — `max_tool_iters`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.changes_log import CHAT_DIR_NAME, read_changes
from agent.chat_tools import (
    dispatch_tool_call, tool_schemas, write_tool_schemas,
    cross_chat_tool_schemas,
)
from agent import chat_registry as _registry

logger = logging.getLogger(__name__)

HISTORY_FILE_NAME = "history.jsonl"
RULES_FILE_NAME = "rules.md"

HISTORY_WINDOW = 40
CHANGES_IN_PROMPT = 20
DEFAULT_TOOL_ITERS = 4

DEFAULT_RULES_RU = """\
# Правила поведения чата Webbles

## Кто ты
Ты — встроенный ассистент Webbles, помогаешь объяснять и обсуждать изменения,
которые движок применил к коду пользователя. Отвечаешь по-русски, кратко и по
делу.

## Что МОЖНО делать
- Объяснять, ЧТО, ГДЕ, КАК и ПОЧЕМУ движок изменил в проекте — опирайся на
  раздел `## ЛОГ ИЗМЕНЕНИЙ` системного промпта (он формируется из
  `chat/changes.jsonl`) и на содержимое файлов, которые ты прочитаешь через
  доступные tools.
- Когда не хватает контекста, читай файлы проекта: используй `read_file`,
  `list_dir`, `grep`. Цитируй ровно те фрагменты, на которые опираешься, и
  указывай путь+строку.
- Помогать с разбором патчей «на ручной просмотр» — что они делают, как принять
  или откатить.
- Подсказывать команды CLI/конфиг-флаги Webbles, если пользователь спросил.

## Чего делать НЕЛЬЗЯ
- Не выдумывать изменения, которых нет в логе. Если данных не хватает —
  сначала попробуй прочитать нужный файл, и только потом ответь. Если и после
  чтения не уверен — честно скажи «не нашёл записи в логе» или «в файле этого
  нет».
- Не предлагать через чат удалять или массово переписывать файлы пользователя
  напрямую. Для правок есть Fix-режим и Create-режим — ты их обсуждаешь, но
  сам не дергаешь.
- Не запрашивать и не показывать секреты: API-ключи, токены, пароли. Если
  пользователь вставил ключ — попроси убрать его.
- Не писать вредоносный код, не помогать обходить лицензии или защиту.
- Не давать юридических, медицинских и финансовых советов — отсылай к
  специалисту.

## Стиль
- Сначала суть в одном-двух предложениях, потом, если надо, детали.
- При цитировании файла — пиши путь и строку.
- Если уверенность низкая — явно скажи об этом.
"""


# ---------------------------------------------------------------------- IO
def _chat_dir(root) -> Path:
    p = Path(root) / CHAT_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_rules_file(root) -> Path:
    p = _chat_dir(root) / RULES_FILE_NAME
    if not p.exists():
        try:
            p.write_text(DEFAULT_RULES_RU, encoding="utf-8")
        except Exception as e:
            logger.debug("rules.md не создан: %s", e)
    return p


def _read_rules(root) -> str:
    p = _ensure_rules_file(root)
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return DEFAULT_RULES_RU


def _history_file(root, chat_id: Optional[str]) -> Path:
    """Возвращает путь к history.jsonl.

    Если задан `chat_id` — `chat/sessions/<chat_id>/history.jsonl` (per-chat).
    Иначе — `chat/history.jsonl` (легаси-режим, обратная совместимость).
    """
    if chat_id:
        return _registry.history_path(root, chat_id)
    return _chat_dir(root) / HISTORY_FILE_NAME


def _read_history(root, *, chat_id: Optional[str] = None,
                  limit: int = HISTORY_WINDOW) -> List[Dict[str, Any]]:
    p = _history_file(root, chat_id)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except Exception:
                    continue
    except Exception as e:
        logger.debug("history не прочитан: %s", e)
        return []
    return out[-limit:]


def _append_history(root, role: str, content: str,
                    *, chat_id: Optional[str] = None) -> None:
    rec = {"role": role, "content": content, "ts": _now_iso()}
    p = _history_file(root, chat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("history не дописан: %s", e)


# ---------------------------------------------------------------------- prompt
def _format_changes_for_prompt(changes: List[Dict[str, Any]]) -> str:
    if not changes:
        return "(пока пусто — движок не вносил изменений)"
    lines = []
    for c in changes:
        ts = c.get("ts", "")
        v = c.get("verdict", "?")
        f = c.get("file", "?")
        ln = c.get("line", 0)
        code = c.get("code", "?")
        src = c.get("patch_source") or ""
        intent = c.get("intent") or ""
        msg = (c.get("message") or "").replace("\n", " ")
        src_tag = f" [{src}]" if src else ""
        intent_tag = f" — {intent}" if intent else ""
        msg_tag = f" — {msg[:120]}" if msg else ""
        lines.append(f"- {ts} {v} {f}:{ln} [{code}]{src_tag}{intent_tag}{msg_tag}")
    return "\n".join(lines)


def build_system_prompt(rules: str, changes: List[Dict[str, Any]],
                        tools_root: Optional[Path] = None,
                        mode: str = "chat") -> str:
    """Финальный system-prompt: правила + лог изменений + подсказка про tools.

    `mode` управляет тем, какой блок инструкций добавляется в хвост:
      * fix    — упор на чтение проекта;
      * create — упор на write_file/make_dir, ЯВНАЯ просьба ИСПОЛЬЗОВАТЬ
                 эти tools (без этого LLM иногда просто пишет код в ответ,
                 не вызывая инструмент);
      * chat   — обычный режим + cross-chat tools (list_chats/read_chat).
    """
    tail = (
        "\n\n## ЛОГ ИЗМЕНЕНИЙ (последние записи из chat/changes.jsonl)\n\n"
        + _format_changes_for_prompt(changes)
    )
    if tools_root is not None:
        tail += (
            f"\n\n## КОРЕНЬ ПРОЕКТА ДЛЯ ИНСТРУМЕНТОВ\n`{tools_root}` — все пути в "
            "`read_file`/`list_dir`/`grep` относительны этого корня. Перед "
            "ответом, если нужно подтверждение — прочитай нужный файл."
        )

    m = str(mode or "chat").strip().lower()
    if m == "create":
        # #2: явная инструкция — в Create-режиме НУЖНО создавать файлы через
        # инструменты, а не «писать код в чате». Без этой инструкции LLM
        # иногда отвечала только текстом и игнорировала tools.
        tail += (
            "\n\n## РЕЖИМ CREATE — ВАЖНО\n"
            "Ты в режиме CREATE: пользователь хочет, чтобы ты СОЗДАВАЛ "
            "файлы и папки проекта. У тебя есть два инструмента:\n"
            "  * `make_dir(path)` — создать каталог (рекурсивно).\n"
            "  * `write_file(path, content, overwrite=false)` — создать "
            "или перезаписать файл с указанным содержимым.\n"
            "Правила:\n"
            "  1. Когда пользователь просит «создай», «сделай», «сгенерируй» — "
            "     это команда. Сначала создай файлы через инструменты, потом "
            "     коротко напиши, что сделал.\n"
            "  2. Не пиши длинный код в ответ — клади его в `write_file(content=...)`. "
            "     Пользователь хочет файлы в проекте, а не цитату в чате.\n"
            "  3. Несколько файлов — несколько вызовов `write_file` в одном ответе.\n"
            "  4. Пути всегда ОТНОСИТЕЛЬНЫЕ от корня проекта (без ведущего `/`).\n"
            "  5. Для папок: сначала `make_dir`, затем `write_file` для файлов внутри.\n"
            "  6. После создания файлов скажи пользователю «готово: создано N файлов» "
            "     и перечисли их. БЕЗ дублирования содержимого в тексте.\n"
            "  7. В `.env` / `secrets.txt` можно писать только в этом режиме "
            "     (Create) — если нужны переменные окружения, заполни их там."
        )
    elif m == "fix":
        tail += (
            "\n\n## РЕЖИМ FIX\n"
            "Ты в режиме FIX: проект УЖЕ СУЩЕСТВУЕТ, и его чинит движок. "
            "Твоя задача — отвечать на вопросы пользователя о починке, "
            "цитировать конкретные файлы/строки и объяснять, ЧТО и ПОЧЕМУ "
            "изменилось. Никаких новых файлов не создавай (инструментов "
            "записи в этом режиме нет). Файлы `.env`/`secrets.*` НЕЛЬЗЯ "
            "ни читать, ни писать."
        )
    else:
        tail += (
            "\n\n## РЕЖИМ CHAT\n"
            "Обычный разговор + чтение проекта через `read_file`/`list_dir`/"
            "`grep`. У тебя также есть `list_chats()` и `read_chat(name|chat_id)` "
            "— используй их, когда пользователь говорит «посмотри что было в чате X»."
        )

    tail += (
        "\n\nЕсли вопрос пользователя касается конкретной правки, цитируй "
        "файл и строку. Если в логе записи нет — попробуй найти через `grep` "
        "или просмотреть файл `read_file`. Если и так не нашёл — честно скажи."
    )
    return rules.rstrip() + tail


# ---------------------------------------------------------------------- class
class ChatSession:
    """Тонкая обёртка над storage + LLM + tools.

    Параметры:
      root          — корень, под которым лежит `chat/` (обычно репозиторий
                      webbles_fix);
      tools_root    — корень, по которому работают file-tools (обычно проект,
                      выбранный пользователем для починки); если None,
                      используется `root`;
      llm_callable  — `Callable[[messages, tools=...], dict]`. Возвращает
                      assistant-сообщение (`content`/`tool_calls`); если None,
                      собирается дефолтный HTTPChatLLM из ENV;
      max_history   — сколько последних сообщений отдавать LLM;
      max_changes   — сколько последних записей changes.jsonl класть в prompt;
      max_tool_iters — максимум обходов tool_calls в одном ответе.
    """

    def __init__(
        self,
        root,
        *,
        tools_root: Optional[Any] = None,
        llm_callable: Optional[Callable[..., Dict[str, Any]]] = None,
        max_history: int = HISTORY_WINDOW,
        max_changes: int = CHANGES_IN_PROMPT,
        max_tool_iters: int = DEFAULT_TOOL_ITERS,
        chat_id: Optional[str] = None,
        mode: str = "chat",
    ):
        self.root = Path(root)
        _ensure_rules_file(self.root)
        self.tools_root = Path(tools_root) if tools_root else self.root
        self._llm = llm_callable
        self.max_history = int(max_history)
        self.max_changes = int(max_changes)
        self.max_tool_iters = int(max_tool_iters)
        self.chat_id = chat_id  # None → легаси-история chat/history.jsonl
        # Режим определяет набор tools и формулировку system-prompt. По умолчанию
        # — обычный чат (read-only tools). В create-режиме добавляем write_file.
        self.mode = str(mode or "chat").strip().lower()

    # --- API ----------------------------------------------------------
    def set_tools_root(self, tools_root) -> None:
        self.tools_root = Path(tools_root) if tools_root else self.root

    def set_chat_id(self, chat_id: Optional[str]) -> None:
        """Переключение активного чата без пересоздания объекта."""
        self.chat_id = chat_id

    def set_mode(self, mode: str) -> None:
        self.mode = str(mode or "chat").strip().lower()

    def rules_text(self) -> str:
        return _read_rules(self.root)

    def history(self) -> List[Dict[str, Any]]:
        return _read_history(self.root, chat_id=self.chat_id, limit=self.max_history)

    def _tools_for_mode(self) -> List[Dict[str, Any]]:
        """Набор tools зависит от режима:
          * chat   — read_file/list_dir/grep + list_chats/read_chat (#4 cross-chat)
          * fix    — read_file/list_dir/grep (только чтение проекта)
          * create — read_file/list_dir/grep + write_file/make_dir
        """
        schemas = list(tool_schemas())
        if self.mode == "create":
            schemas.extend(write_tool_schemas())
        if self.mode in ("chat", ""):
            # Cross-chat tools только в режиме чата — в fix/create они отвлекают LLM
            # от основной задачи (починка / создание проекта).
            schemas.extend(cross_chat_tool_schemas())
        return schemas

    def build_messages(self, user_text: str) -> List[Dict[str, str]]:
        """Финальный массив messages для первого запроса LLM."""
        rules = self.rules_text()
        changes = read_changes(self.root, limit=self.max_changes)
        sys_prompt = build_system_prompt(rules, changes,
                                          tools_root=self.tools_root,
                                          mode=self.mode)
        msgs: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
        for h in self.history():
            role = h.get("role")
            content = h.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content:
                msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def send(self, user_text: str) -> str:
        """Полный цикл: записываем user → дёргаем LLM → выполняем tool_calls,
        если есть → повторяем до финального текстового ответа.
        """
        text = (user_text or "").strip()
        if not text:
            return ""
        _append_history(self.root, "user", text, chat_id=self.chat_id)

        llm = self._llm
        if llm is None:
            from agent.chat_llm import make_default_chat
            llm = make_default_chat()

        messages = self.build_messages(text)
        tools = self._tools_for_mode()

        final_text = ""
        for _it in range(max(1, self.max_tool_iters)):
            try:
                msg = llm(messages, tools=tools)
            except TypeError:
                # callable из тестов мог не принимать tools — пробуем без.
                msg = llm(messages)
            if not isinstance(msg, dict):
                # Некоторые мок-LLM возвращают сразу строку.
                msg = {"role": "assistant", "content": str(msg or "")}
            # Сохраняем assistant-сообщение в messages, чтобы следующий tool-цикл
            # шёл от него.
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": msg.get("tool_calls") or []})
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                final_text = (msg.get("content") or "").strip()
                break
            # Выполняем все tool_calls в порядке поступления.
            for tc in tool_calls:
                tc_id = tc.get("id") or ""
                fn = (tc.get("function") or {})
                name = fn.get("name") or ""
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                except Exception:
                    args = {}
                # #4: cross-chat tools (list_chats/read_chat) читают из `self.root`
                # (это папка chat/), не из `self.tools_root` (проект).
                tool_result = dispatch_tool_call(self.tools_root, name, args,
                                                  mode=self.mode,
                                                  chat_root=self.root)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": name,
                    "content": tool_result,
                })
        else:
            # Не вышли из цикла — лимит итераций. Используем последний content.
            final_text = (messages[-1].get("content") if messages else "") or \
                "Не удалось завершить ответ — слишком много вызовов инструментов."

        if final_text:
            _append_history(self.root, "assistant", final_text, chat_id=self.chat_id)
        return final_text

    def clear_history(self) -> None:
        p = _history_file(self.root, self.chat_id)
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            logger.debug("history не удалён: %s", e)
