"""
Webbles Studio — десктоп-окно поверх Fix-агента (Stage L.5).

Поднимает `webbles_ui.html` как окно через Eel и связывает кнопки интерфейса с
агентом: выбор папки проекта, запуск починки, и стрим событий обратно в UI
(`applyAgentEvent` — уже определён в HTML). Агент крутится в фоновом потоке,
чтобы окно не зависало.

Также экспонирует чат: `chat_send(text)` — отдельный диалог с LLM поверх
`agent.chat_session.ChatSession`. Чат использует ОТДЕЛЬНЫЙ ключ
`WEBBLES_CHAT_API_KEY` (см. `_load_env`), не пересекаясь с ключом починки.

Снимки «Было / Стало»: `chat_file_diff(path)`, `chat_explain_before(path)`,
`chat_explain_after(path)` — для клика по файлу в дереве проекта.

Запуск:
    python studio.py

Требования: установлен Chrome или Edge, `pip install -r requirements-min.txt`,
ключи в `.env`:
  WEBBLES_LLM_API_KEY   — для починки.
  WEBBLES_CHAT_API_KEY  — для чата.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _setup_logging() -> None:
    cfg_path = ROOT / "webles_config.json"
    verbose = False
    log_file_name = "webbles_fix.log"
    try:
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            verbose = bool(cfg.get("verbose", False))
            log_file_name = str(cfg.get("log_file") or log_file_name)
    except Exception as e:
        print(f"[studio] не смог прочитать конфиг для логов: {e}", file=sys.stderr)

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, "_studio_owned", False):
            root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    console._studio_owned = True  # type: ignore[attr-defined]
    root.addHandler(console)

    try:
        log_path = ROOT / log_file_name
        fh = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024,
                                 backupCount=5, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        fh._studio_owned = True  # type: ignore[attr-defined]
        root.addHandler(fh)
        print(f"[studio] логирование: level={logging.getLevelName(level)}, "
              f"file={log_path}", file=sys.stderr)
    except Exception as e:
        print(f"[studio] файловый лог не поднялся: {e}", file=sys.stderr)


def _load_env() -> None:
    """Читает .env и кладёт ключи в окружение (LLM + CHAT)."""
    env_path = ROOT / ".env"
    if env_path.exists():
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception as e:
            print(f"[studio] .env не прочитан: {e}", file=sys.stderr)
    key = os.environ.get("WEBBLES_LLM_API_KEY")
    if key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = key
    if key:
        print(f"[studio] LLM-ключ загружен (…{key[-4:]})", file=sys.stderr)
    else:
        print("[studio] ВНИМАНИЕ: WEBBLES_LLM_API_KEY не найден.", file=sys.stderr)
    chat_key = os.environ.get("WEBBLES_CHAT_API_KEY")
    if chat_key:
        print(f"[studio] чат-ключ загружен (…{chat_key[-4:]})", file=sys.stderr)

    # Авто-подключение локального LLM чата из webles_config.json.
    # Если WEBBLES_LOCAL_LLM_URL ещё не задан — берём из первого провайдера конфига.
    # Это позволяет chat_session использовать ту же локальную Gemma что и patch-движок.
    if not os.environ.get("WEBBLES_LOCAL_LLM_URL"):
        try:
            cfg = json.loads((ROOT / "webles_config.json").read_text(encoding="utf-8"))
            # Проверяем chat_llm секцию (тип cloud = не трогаем, local = берём из providers)
            chat_cfg = cfg.get("chat_llm") or {}
            if chat_cfg.get("type") == "cloud" and chat_cfg.get("base_url"):
                if not os.environ.get("WEBBLES_CHAT_BASE_URL"):
                    os.environ["WEBBLES_CHAT_BASE_URL"] = chat_cfg["base_url"]
                if not os.environ.get("WEBBLES_CHAT_API_KEY") and chat_cfg.get("api_key"):
                    os.environ["WEBBLES_CHAT_API_KEY"] = chat_cfg["api_key"]
                if not os.environ.get("WEBBLES_CHAT_MODEL") and chat_cfg.get("model"):
                    os.environ["WEBBLES_CHAT_MODEL"] = chat_cfg["model"]
                print(f"[studio] чат: cloud {chat_cfg.get('base_url','')}", file=sys.stderr)
            else:
                # local: берём из первого провайдера с profile=patch
                provs = cfg.get("llm", {}).get("providers") or []
                prov = next((p for p in provs if p.get("profile") == "patch"), provs[0] if provs else {})
                local_url = prov.get("base_url") or ""
                if local_url:
                    os.environ["WEBBLES_LOCAL_LLM_URL"] = local_url
                    if prov.get("model"):
                        os.environ.setdefault("WEBBLES_LOCAL_LLM_MODEL", prov["model"])
                    print(f"[studio] чат: локальный LLM {local_url}", file=sys.stderr)
        except Exception as e:
            print(f"[studio] не удалось читать конфиг для LLM чата: {e}", file=sys.stderr)


_load_env()
_setup_logging()

import eel  # noqa: E402

from agent.run_fix import run_fix_agent  # noqa: E402
from agent.events import EventBus        # noqa: E402
from agent.chat_session import ChatSession  # noqa: E402
from agent import chat_registry as _chat_registry  # noqa: E402

eel.init(str(ROOT))

# Активный чат — из chat/index.json (или создаётся первый раз).
_active_chat_id = _chat_registry.ensure_active(ROOT, default_name="Чат 1", default_mode="chat")
_CHAT_SESSION = ChatSession(ROOT, tools_root=ROOT,
                            chat_id=_active_chat_id, mode="chat")
_CHAT_PROJECT_PATH = ""

_LANG_MAP = {
    "rust": "rust", "rs": "rust",
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "c#": "csharp", "csharp": "csharp", "cs": "csharp",
    "c++": "cpp", "cpp": "cpp", "cxx": "cpp",
    "go": "go", "golang": "go",
    "java": "java",
    "kotlin": "kotlin", "kt": "kotlin",
}


def _push(evt: dict) -> None:
    """Шлёт событие в страницу (вызывает JS applyAgentEvent). Сбой — молча."""
    try:
        eel.applyAgentEvent(evt)()  # type: ignore[attr-defined]
    except Exception:
        pass


@eel.expose
def pick_folder() -> str:
    """Нативный диалог выбора папки проекта."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Выберите папку проекта")
        root.destroy()
        return path or ""
    except Exception as e:
        _push({"type": "run_error", "data": {"message": f"folder pick failed: {e}"}})
        return ""


@eel.expose
def start_fix(project_path: str, language: str) -> bool:
    """Запускает агента в фоне; события летят в UI через _push."""
    global _CHAT_PROJECT_PATH
    lang = _LANG_MAP.get((language or "").strip().lower())
    if not project_path or not lang:
        _push({"type": "run_error",
               "data": {"message": "не выбран проект или язык"}})
        return False

    # Tech debt audit 2026-06-21, #6: eel.expose-методы принимали путь от
    # JS-стороны без проверки — теоретическая path-traversal поверхность
    # (хоть и низкий риск для loopback-only desktop-приложения). Не сужаем
    # доступ к какому-то "разрешённому корню" (это ломало бы легитимный
    # сценарий — пользователь чинит ЛЮБОЙ свой проект), просто требуем, чтобы
    # путь реально резолвился в существующую директорию.
    try:
        _resolved = Path(project_path).resolve(strict=True)
    except (OSError, RuntimeError) as e:
        _push({"type": "run_error",
               "data": {"message": f"Путь к проекту не найден: {e}"}})
        return False
    if not _resolved.is_dir():
        _push({"type": "run_error",
               "data": {"message": f"Путь к проекту не является директорией: {_resolved}"}})
        return False
    project_path = str(_resolved)

    try:
        _CHAT_SESSION.set_tools_root(project_path)
        _CHAT_PROJECT_PATH = project_path
    except Exception as e:
        print(f"[studio] tools_root для чата не обновлён: {e}", file=sys.stderr)

    def _run() -> None:
        bus = EventBus()
        bus.subscribe(lambda e: _push(e.to_dict()))
        try:
            result = run_fix_agent(project_path, lang, emit=bus)
            if result.get("refused"):
                _push({"type": "run_error", "data": {"message": result.get("message", "")}})
        except Exception as ex:
            _push({"type": "run_error", "data": {"message": f"{type(ex).__name__}: {ex}"}})

    threading.Thread(target=_run, daemon=True).start()
    return True


@eel.expose
def chat_send(text: str) -> str:
    """Принимает сообщение пользователя, возвращает ответ ассистента."""
    text = (text or "").strip()
    if not text:
        return ""
    has_local = bool(os.environ.get("WEBBLES_LOCAL_LLM_URL"))
    has_cloud = bool(os.environ.get("WEBBLES_CHAT_API_KEY") or os.environ.get("WEBBLES_CHAT_BASE_URL"))
    if not has_local and not has_cloud:
        return ("⚠️ LLM для чата не настроен. Укажи локальный LLM в Настройках → LLM "
                "или задай `WEBBLES_CHAT_API_KEY` в `.env`.")
    try:
        return _CHAT_SESSION.send(text)
    except Exception as e:
        return f"⚠️ Ошибка чата: {type(e).__name__}: {e}"


@eel.expose
def chat_clear_history() -> bool:
    try:
        _CHAT_SESSION.clear_history()
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# Множественные чаты (per-chat history). UI рендерит из этих API.
# ----------------------------------------------------------------------
@eel.expose
def chat_list() -> list:
    """[{id, name, ts, mode, active, messages}, ...]."""
    try:
        return _chat_registry.list_chats(ROOT)
    except Exception as e:
        print(f"[studio] chat_list упал: {e}", file=sys.stderr)
        return []


@eel.expose
def chat_new(name: str = "", mode: str = "chat") -> str:
    """Создаёт новый чат, делает его активным, возвращает id."""
    global _active_chat_id
    try:
        chat_id = _chat_registry.create_chat(ROOT, name or None,
                                             mode=(mode or "chat"),
                                             set_as_active=True)
        _active_chat_id = chat_id
        _CHAT_SESSION.set_chat_id(chat_id)
        _CHAT_SESSION.set_mode(mode or "chat")
        return chat_id
    except Exception as e:
        print(f"[studio] chat_new упал: {e}", file=sys.stderr)
        return ""


@eel.expose
def chat_switch(chat_id: str) -> bool:
    """Переключает активный чат."""
    global _active_chat_id
    if not chat_id:
        return False
    try:
        ok = _chat_registry.set_active(ROOT, chat_id)
        if not ok:
            return False
        _active_chat_id = chat_id
        _CHAT_SESSION.set_chat_id(chat_id)
        for c in _chat_registry.list_chats(ROOT):
            if c.get("id") == chat_id and c.get("mode"):
                _CHAT_SESSION.set_mode(c["mode"])
                break
        return True
    except Exception as e:
        print(f"[studio] chat_switch упал: {e}", file=sys.stderr)
        return False


@eel.expose
def chat_delete(chat_id: str) -> bool:
    """Удаляет чат и его историю; если был активным — переключит на другой."""
    global _active_chat_id
    if not chat_id:
        return False
    try:
        ok = _chat_registry.delete_chat(ROOT, chat_id)
        if ok and _active_chat_id == chat_id:
            new_active = _chat_registry.ensure_active(ROOT)
            _active_chat_id = new_active
            _CHAT_SESSION.set_chat_id(new_active)
        return ok
    except Exception as e:
        print(f"[studio] chat_delete упал: {e}", file=sys.stderr)
        return False


@eel.expose
def chat_rename(chat_id: str, name: str) -> bool:
    try:
        return _chat_registry.rename_chat(ROOT, chat_id, name or "Без названия")
    except Exception:
        return False


@eel.expose
def chat_set_mode(mode: str) -> bool:
    """Меняет режим текущего чата: 'chat' | 'fix' | 'create'."""
    try:
        m = (mode or "chat").strip().lower()
        if m not in ("chat", "fix", "create"):
            return False
        _CHAT_SESSION.set_mode(m)
        if _active_chat_id:
            _chat_registry.set_mode(ROOT, _active_chat_id, m)
        return True
    except Exception as e:
        print(f"[studio] chat_set_mode упал: {e}", file=sys.stderr)
        return False


@eel.expose
def chat_history(chat_id: str = "") -> list:
    """История активного (или указанного) чата для рендера UI."""
    try:
        from agent.chat_session import _read_history
        target = chat_id or _active_chat_id
        return _read_history(ROOT, chat_id=target, limit=200)
    except Exception as e:
        print(f"[studio] chat_history упал: {e}", file=sys.stderr)
        return []


# ----------------------------------------------------------------------
# Снимки «Было / Стало» по файлу.
# ----------------------------------------------------------------------
@eel.expose
def chat_file_diff(rel_path: str) -> dict:
    """Возвращает `{path, before, after, error_lines, changes, missing, ...}`."""
    try:
        from agent import file_snapshots as _fs
        return _fs.get_file_diff(ROOT, rel_path)
    except Exception as e:
        return {"path": rel_path, "before": "", "after": "",
                "error_lines": [], "changes": [], "missing": True,
                "error": f"{type(e).__name__}: {e}"}


def _chat_request_or_hint(prompt: str) -> str:
    # Чат доступен если задан локальный LLM URL или облачный ключ.
    has_local = bool(os.environ.get("WEBBLES_LOCAL_LLM_URL"))
    has_cloud = bool(os.environ.get("WEBBLES_CHAT_API_KEY") or os.environ.get("WEBBLES_CHAT_BASE_URL"))
    if not has_local and not has_cloud:
        return ("⚠️ LLM для чата не настроен. Укажи локальный LLM в Настройках → LLM "
                "или задай `WEBBLES_CHAT_API_KEY` в `.env`.")
    try:
        return _CHAT_SESSION.send(prompt)
    except Exception as e:
        return f"⚠️ Ошибка чата: {type(e).__name__}: {e}"


def _format_error_lines(errs):
    if not errs:
        return "(в снимке нет error_lines)"
    out = []
    for e in errs[:8]:
        ln = e.get("line", "?")
        code = e.get("code", "?")
        msg = (e.get("message") or "").replace("\n", " ")[:200]
        out.append(f"  - стр. {ln} [{code}]" + (f" — {msg}" if msg else ""))
    return "\n".join(out)


def _format_changes(changes):
    if not changes:
        return "(вердиктов по файлу пока нет)"
    out = []
    for c in changes[:8]:
        v = c.get("verdict", "?")
        code = c.get("code", "?")
        intent = (c.get("intent") or "").replace("\n", " ")[:200]
        src = c.get("patch_source") or ""
        conf = c.get("confidence")
        src_tag = f" [{src}]" if src else ""
        intent_tag = f" — {intent}" if intent else ""
        conf_tag = f" · conf {conf:.2f}" if isinstance(conf, (int, float)) else ""
        out.append(f"  - {v}{src_tag}{intent_tag}{conf_tag} [{code}]")
    return "\n".join(out)


@eel.expose
def chat_explain_before(rel_path: str) -> str:
    """LLM объясняет, ЧТО было не так в файле до починки."""
    try:
        from agent import file_snapshots as _fs
        snap = _fs.get_file_diff(ROOT, rel_path)
    except Exception as e:
        return f"⚠️ Не получилось прочитать снимок: {e}"
    if snap.get("missing"):
        return (f"По файлу `{rel_path}` нет снимка. Запусти Fix-режим, чтобы "
                "движок зафиксировал «Было/Стало».")
    errs = snap.get("error_lines") or []
    before = (snap.get("before") or "")[:6000]
    prompt = (
        f"Объясни кратко и по делу, ЧТО БЫЛО НЕ ТАК в файле `{rel_path}` ДО "
        f"починки. Перечислены ошибки, найденные движком:\n\n"
        f"{_format_error_lines(errs)}\n\n"
        f"Вот исходный текст файла (фрагмент, до правок):\n```\n{before}\n```\n\n"
        f"Если данных не хватает — прочитай нужные файлы через `read_file`/`grep`."
    )
    return _chat_request_or_hint(prompt)


@eel.expose
def chat_explain_after(rel_path: str) -> str:
    """LLM объясняет, КАК и ПОЧЕМУ движок исправил файл."""
    try:
        from agent import file_snapshots as _fs
        snap = _fs.get_file_diff(ROOT, rel_path)
    except Exception as e:
        return f"⚠️ Не получилось прочитать снимок: {e}"
    if snap.get("missing"):
        return (f"По файлу `{rel_path}` нет снимка. Запусти Fix-режим, чтобы "
                "движок зафиксировал «Было/Стало».")
    changes = snap.get("changes") or []
    errs = snap.get("error_lines") or []
    after = (snap.get("after") or "")[:6000]
    prompt = (
        f"Объясни кратко и по делу, КАК и ПОЧЕМУ движок исправил файл "
        f"`{rel_path}`.\n\n"
        f"Список вердиктов движка по этому файлу:\n{_format_changes(changes)}\n\n"
        f"Исходные ошибки, на которые движок реагировал:\n"
        f"{_format_error_lines(errs)}\n\n"
        f"Текущее (после правок) содержимое файла (фрагмент):\n```\n{after}\n```\n\n"
        f"Сначала суть одной фразой, затем разбор по каждому вердикту."
    )
    return _chat_request_or_hint(prompt)


# ----------------------------------------------------------------------
# #8 + #9 — очередь NEEDS_REVIEW: list / apply / reject + LLM-ревью.
# ----------------------------------------------------------------------

@eel.expose
def needs_review_list() -> list:
    """Список всех патчей, ожидающих ручного просмотра."""
    proj = _CHAT_PROJECT_PATH
    if not proj:
        return []
    try:
        from agent.review_actions import list_pending
        return list_pending(proj)
    except Exception as e:
        print(f"[studio] needs_review_list: {e}", file=sys.stderr)
        return []


@eel.expose
def needs_review_apply(sig: str) -> dict:
    """Применить один патч из очереди к проекту."""
    proj = _CHAT_PROJECT_PATH
    if not proj:
        return {"ok": False, "reason": "no_project_open"}
    try:
        from agent.review_actions import apply_pending
        res = apply_pending(proj, sig)
        # Сообщаем UI через event-bus, чтобы счётчики обновились.
        if res.get("ok"):
            _push({"type": "needs_review_applied",
                   "data": {"sig": sig, "file": res.get("file", "")}})
        return res
    except Exception as e:
        return {"ok": False, "reason": f"exception: {e}", "sig": sig}


@eel.expose
def needs_review_reject(sig: str) -> dict:
    """Удалить запись из очереди без применения."""
    proj = _CHAT_PROJECT_PATH
    if not proj:
        return {"ok": False, "reason": "no_project_open"}
    try:
        from agent.review_actions import reject_pending
        res = reject_pending(proj, sig)
        if res.get("ok"):
            _push({"type": "needs_review_rejected", "data": {"sig": sig}})
        return res
    except Exception as e:
        return {"ok": False, "reason": f"exception: {e}", "sig": sig}


@eel.expose
def needs_review_get_diff(sig: str) -> dict:
    """Возвращает полное содержимое файла ДО и ПОСЛЕ применения патча.

    Применение производится во временный файл (оригинал не трогается).
    """
    proj = _CHAT_PROJECT_PATH
    if not proj:
        return {"ok": False, "reason": "no_project_open"}
    try:
        from agent.review_actions import read_pending
        data = read_pending(proj, sig)
        if data is None:
            return {"ok": False, "reason": "not_found"}
        err = data.get("error") or {}
        file_rel = err.get("file") or ""
        patch_text = data.get("patch") or ""
        if not file_rel:
            return {"ok": False, "reason": "no_file"}
        target = pathlib.Path(proj) / file_rel
        if not target.exists():
            return {"ok": False, "reason": "file_missing"}
        before_content = target.read_text(encoding="utf-8", errors="replace")
        after_content = before_content
        if patch_text:
            try:
                import tempfile, shutil
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", encoding="utf-8",
                    delete=False
                ) as tmp:
                    tmp.write(before_content)
                    tmp_path = pathlib.Path(tmp.name)
                from fixers.patch_engine import PatchEngine
                eng = PatchEngine()
                ok = eng.apply_patch(tmp_path, patch_text)
                if ok:
                    after_content = tmp_path.read_text(encoding="utf-8", errors="replace")
                tmp_path.unlink(missing_ok=True)
            except Exception as e:
                print(f"[studio] needs_review_get_diff apply error: {e}", file=sys.stderr)
        return {
            "ok": True,
            "file": file_rel,
            "before": before_content,
            "after": after_content,
            "patch": patch_text,
        }
    except Exception as e:
        print(f"[studio] needs_review_get_diff: {e}", file=sys.stderr)
        return {"ok": False, "reason": str(e)}


@eel.expose
def needs_review_retry(sig: str) -> dict:
    """Удаляет запись из очереди, сигнализируя о повторной генерации."""
    proj = _CHAT_PROJECT_PATH
    if not proj:
        return {"ok": False, "reason": "no_project_open"}
    try:
        from agent.review_actions import reject_pending
        res = reject_pending(proj, sig)
        if res.get("ok"):
            _push({"type": "needs_review_retry", "data": {"sig": sig}})
        return res
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@eel.expose
def create_init_project(name: str, parent_path: str = "") -> dict:
    """Создаёт папку для нового проекта, переключает tools_root на неё.

    Если parent_path передан — создаёт <parent_path>/<name>.
    Иначе — в runtime/created_projects/.
    Возвращает {ok, path}.
    """
    global _CHAT_PROJECT_PATH
    try:
        safe_name = "".join(c if c.isalnum() or c in "-_ .-" else "_" for c in (name or "project")).strip()
        if not safe_name:
            safe_name = "project"
        if parent_path and Path(parent_path).is_dir():
            proj_dir = Path(parent_path) / safe_name
        else:
            import time
            ts = str(int(time.time()))[-6:]
            proj_dir = ROOT / "runtime" / "created_projects" / f"{safe_name}_{ts}"
        proj_dir.mkdir(parents=True, exist_ok=True)
        _CHAT_PROJECT_PATH = str(proj_dir)
        _CHAT_SESSION.set_tools_root(str(proj_dir))
        print(f"[studio] create: проект инициализирован → {proj_dir}", file=sys.stderr)
        return {"ok": True, "path": str(proj_dir)}
    except Exception as e:
        print(f"[studio] create_init_project: {e}", file=sys.stderr)
        return {"ok": False, "reason": str(e)}


@eel.expose
def create_run_fix(language: str = "python") -> bool:
    """Запускает Fix-пайплайн на текущем Create-проекте."""
    proj = _CHAT_PROJECT_PATH
    if not proj:
        _push({"type": "run_error", "data": {"message": "Проект не инициализирован"}})
        return False
    return bool(start_fix(proj, language or "python"))


@eel.expose
def needs_review_chat_opinion(sig: str) -> dict:
    """#8: LLM-чат ревьюит один патч и возвращает вердикт.

    Контракт ответа (всегда):
        {
          "ok": bool,
          "verdict": "approve" | "reject" | "unclear",
          "summary": "<краткое объяснение>",
          "concerns": ["<пункт>", ...],
          "disclaimer": "LLM может ошибиться — проверьте сами"
        }

    LLM — это диалоговый чат-агент (тот же DeepSeek), не системный движок.
    Если ключа нет / LLM упала / нет записи → возвращаем ok=False.
    """
    proj = _CHAT_PROJECT_PATH
    if not proj:
        return {"ok": False, "reason": "no_project_open"}
    try:
        from agent.review_actions import read_pending
        rec = read_pending(proj, sig)
    except Exception as e:
        return {"ok": False, "reason": f"read_failed: {e}"}
    if rec is None:
        return {"ok": False, "reason": "not_found", "sig": sig}

    err = rec.get("error") or {}
    prompt = (
        "Ты — внимательный код-ревьюер. Оцени НИЖЕ предложенный патч.\n\n"
        "Ошибка, которую патч пытается починить:\n"
        f"  file: {err.get('file','')}\n"
        f"  line: {err.get('line', 0)}\n"
        f"  code: {err.get('code','')}\n"
        f"  message: {err.get('message','')}\n\n"
        f"Намерение патча (intent): {rec.get('intent','')}\n"
        f"Источник патча: {rec.get('patch_source','')}\n"
        f"Заявленная уверенность: {rec.get('confidence')}\n\n"
        "Сам unified-diff:\n"
        "```diff\n"
        f"{(rec.get('patch') or '')[:6000]}\n"
        "```\n\n"
        "Ответь СТРОГО в формате JSON с полями: "
        '{"verdict": "approve"|"reject"|"unclear", '
        '"summary": "<1-2 предложения>", '
        '"concerns": ["<пункт>", ...]}. '
        "Никаких markdown-обёрток, чистый JSON. Если сомневаешься — verdict=unclear."
    )
    raw = _chat_request_or_hint(prompt)
    # Пытаемся разобрать JSON. Если не получилось — отдаём как есть в summary.
    import json as _json
    parsed: Dict[str, Any]
    try:
        # Локально: ищем первый { и последний }, парсим срез.
        s = raw.strip()
        i = s.find("{"); j = s.rfind("}")
        parsed = _json.loads(s[i:j + 1]) if (i != -1 and j > i) else {"summary": s}
    except Exception:
        parsed = {"summary": raw}

    verdict = str(parsed.get("verdict") or "").lower().strip()
    if verdict not in ("approve", "reject", "unclear"):
        verdict = "unclear"
    summary = str(parsed.get("summary") or "")[:600]
    concerns = parsed.get("concerns") or []
    if not isinstance(concerns, list):
        concerns = [str(concerns)]
    concerns = [str(c)[:300] for c in concerns][:8]

    return {
        "ok": True,
        "sig": sig,
        "verdict": verdict,
        "summary": summary,
        "concerns": concerns,
        # Жёсткий disclaimer — UI обязан его показывать рядом с любым ответом.
        "disclaimer": "⚠️ LLM может ошибиться — проверьте патч сами перед применением.",
    }


# ── Config API ──────────────────────────────────────────────────────────────

_CFG_PATH = ROOT / "webles_config.json"


@eel.expose
def get_config() -> dict:
    """Возвращает webles_config.json как dict для отображения в Settings."""
    try:
        return json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[studio] get_config: {e}", file=sys.stderr)
        return {}


@eel.expose
def save_config(data: dict) -> dict:
    """Сохраняет dict обратно в webles_config.json и перезагружает LLM-чат."""
    try:
        _CFG_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _reload_chat_llm(data)
        return {"ok": True}
    except Exception as e:
        print(f"[studio] save_config: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def _reload_chat_llm(cfg: dict) -> None:
    """Обновляет WEBBLES_LOCAL_LLM_URL / WEBBLES_CHAT_* из нового конфига без перезапуска."""
    try:
        chat_cfg = cfg.get("chat_llm") or {}
        if chat_cfg.get("type") == "cloud" and chat_cfg.get("base_url"):
            os.environ["WEBBLES_CHAT_BASE_URL"] = chat_cfg["base_url"]
            os.environ["WEBBLES_CHAT_MODEL"] = chat_cfg.get("model") or "deepseek-chat"
            if chat_cfg.get("api_key"):
                os.environ["WEBBLES_CHAT_API_KEY"] = chat_cfg["api_key"]
            os.environ.pop("WEBBLES_LOCAL_LLM_URL", None)
            print("[studio] чат переключён на cloud:", chat_cfg["base_url"], file=sys.stderr)
        else:
            provs = cfg.get("llm", {}).get("providers") or []
            prov = next((p for p in provs if p.get("profile") == "patch"), provs[0] if provs else {})
            local_url = prov.get("base_url") or ""
            if local_url:
                os.environ["WEBBLES_LOCAL_LLM_URL"] = local_url
                if prov.get("model"):
                    os.environ["WEBBLES_LOCAL_LLM_MODEL"] = prov["model"]
                os.environ.pop("WEBBLES_CHAT_BASE_URL", None)
                print("[studio] чат переключён на локальный LLM:", local_url, file=sys.stderr)
    except Exception as e:
        print(f"[studio] _reload_chat_llm: {e}", file=sys.stderr)


@eel.expose
def get_review_count() -> int:
    """Число патчей в очереди ревью для текущего проекта."""
    proj = _CHAT_PROJECT_PATH
    if not proj:
        return 0
    try:
        from agent.review_actions import list_pending
        return len(list_pending(proj))
    except Exception:
        return 0


@eel.expose
def detect_language(project_path: str) -> dict:
    """Определяет язык проекта по расширениям файлов."""
    from pathlib import Path as _P
    import collections
    p = _P(project_path)
    if not p.is_dir():
        return {"language": None, "supported": False, "message": "Папка не найдена"}
    ext_count: dict = collections.Counter()
    for f in p.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            ext_count[f.suffix.lower()] += 1
    supported = {
        ".py": "python", ".rs": "rust", ".js": "javascript",
        ".ts": "typescript", ".go": "go", ".java": "java",
    }
    for ext, lang in sorted(supported.items(), key=lambda x: -ext_count.get(x[0], 0)):
        if ext_count.get(ext, 0) > 0:
            return {"language": lang, "supported": True, "message": f"Обнаружен: {lang}"}
    top = ext_count.most_common(3)
    top_exts = [e for e, _ in top]
    return {
        "language": None, "supported": False,
        "message": f"Язык не поддерживается (найдено: {', '.join(top_exts) or 'нет файлов'})"
    }


def main() -> int:
    page = "webbles_ui.html"
    port = 8000
    for mode in ("edge", None):
        try:
            eel.start(page, mode=mode, port=port, size=(1400, 900))
            return 0
        except (SystemExit, KeyboardInterrupt):
            return 0
        except Exception as e:
            print(f"[studio] режим {mode!r} не сработал: {e}", file=sys.stderr)
    url = f"http://localhost:{port}/{page}"
    print(f"[studio] Не удалось открыть окно автоматически.\n"
          f"[studio] Открой эту ссылку в браузере вручную: {url}", file=sys.stderr)
    try:
        eel.start(page, mode=False, port=port, block=True)
    except (SystemExit, KeyboardInterrupt):
        return 0
    except Exception as e:
        print(f"[studio] Не удалось поднять сервер: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
