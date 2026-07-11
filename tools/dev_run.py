"""tools/dev_run.py — CLI-обёртка вокруг того же pipeline, что Studio
запускает по нажатию Run.

Зовёт ровно ту функцию (`agent.run_fix.run_fix_agent`), которую Studio
вызывает в `start_fix`. Никакой собственной логики починки/анализа — только
парсинг аргументов, подъём логов как в Studio и краткий итог.

Примеры запуска
---------------
    python tools/dev_run.py --project C:\\Users\\zov31\\test_python --lang python
    python tools/dev_run.py --project /tmp/my_rust_app --lang rust
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

# Корень репо — родитель папки tools/.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Совместимое со Studio окружение
# ---------------------------------------------------------------------------
# Маппинг алиасов языков совпадает с `studio._LANG_MAP`. Дублируем потому что
# studio.py при импорте делает `eel.init(...)` (поднимает UI-веб-сервер) —
# импортировать его в CLI нельзя.
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


def _load_env() -> None:
    """Та же логика, что у `studio._load_env`: читает .env и переносит ключи
    в `os.environ`. Дублирует, потому что studio.py нельзя импортировать."""
    env_path = ROOT / ".env"
    if env_path.exists():
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception as e:
            print(f"[dev_run] .env not read: {e}", file=sys.stderr)
    key = os.environ.get("WEBBLES_LLM_API_KEY")
    if key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = key
    if key:
        print(f"[dev_run] LLM key loaded (...{key[-4:]})", file=sys.stderr)
    else:
        print("[dev_run] WARNING: WEBBLES_LLM_API_KEY not found in .env", file=sys.stderr)


def _setup_logging(log_path: Path) -> None:
    """Поднимает root logger со ТЕМИ ЖЕ форматом/уровнем, что и Studio,
    но пишет в `.webbles_logs/latest.log` (overwrite, mode='w'), а не в
    `webbles_fix.log` (RotatingFileHandler) — по требованию dev-режима.
    Параллельно дублирует в stdout.
    """
    cfg_path = ROOT / "webles_config.json"
    verbose = False
    try:
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            verbose = bool(cfg.get("verbose", False))
    except Exception as e:
        print(f"[dev_run] config not read for logging: {e}", file=sys.stderr)

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    # Снимаем свои предыдущие handler'ы, чтобы повторный запуск в одном
    # процессе не плодил дубли.
    for h in list(root.handlers):
        if getattr(h, "_dev_run_owned", False):
            root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    console._dev_run_owned = True  # type: ignore[attr-defined]
    root.addHandler(console)

    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    fh._dev_run_owned = True  # type: ignore[attr-defined]
    root.addHandler(fh)

    print(f"[dev_run] logging: level={logging.getLevelName(level)} file={log_path}",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# Event consumer (UI-эмиттер, но в терминал)
# ---------------------------------------------------------------------------
def _print_event(evt) -> None:
    """Компактная печать события в stdout. Не дублирует root-логгер: тот
    пишет полные сообщения стадий, а здесь — короткие маркеры (как UI
    показывает в чате)."""
    try:
        d = evt.to_dict() if hasattr(evt, "to_dict") else dict(evt)
    except Exception:
        return
    etype = d.get("type", "?")
    data = d.get("data") or {}
    if etype == "run_started":
        print(f"[event] RUN_STARTED mode={data.get('mode')} lang={data.get('language')}",
              flush=True)
    elif etype == "error_dequeued":
        msg = (data.get("message") or "")[:80]
        print(f"[event] ERROR  {data.get('file','?')}:{data.get('line','?')} "
              f"[{data.get('code','?')}] {msg}", flush=True)
    elif etype == "applied":
        print(f"[event] ACCEPT {data.get('file','?')}:{data.get('line','?')} "
              f"[{data.get('code','?')}] src={data.get('patch_source','?')} "
              f"conf={data.get('confidence','?')}", flush=True)
    elif etype == "needs_review":
        print(f"[event] REVIEW {data.get('file','?')}:{data.get('line','?')} "
              f"[{data.get('code','?')}]", flush=True)
    elif etype == "failed":
        reason = (data.get("reason") or "")[:60]
        print(f"[event] FAILED {data.get('file','?')}:{data.get('line','?')} "
              f"[{data.get('code','?')}] {reason}", flush=True)
    elif etype == "run_finished":
        print(f"[event] RUN_FINISHED status={data.get('status','?')} "
              f"accept={data.get('accepted',0)} review={data.get('needs_review',0)} "
              f"failed={data.get('failed',0)}", flush=True)
    elif etype == "run_error":
        print(f"[event] RUN_ERROR {data.get('message','')}", flush=True)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
_OK_STATUSES = {"OK", "FIXED", "CONVERGED", "SUCCESS", "PASSED"}


def _is_success(result: Dict[str, Any]) -> bool:
    """Success = непустой dict без `refused`, статус из белого списка,
    и не упало в FAILED. Дополнительно: если `final_error_count` сообщают и
    он >= initial — это явно НЕ success."""
    if not result or result.get("refused"):
        return False
    raw_status = str(result.get("status", "")).upper()
    if raw_status in _OK_STATUSES:
        return True
    # status может быть пустым / нестандартным — fallback по счётчикам:
    try:
        initial = int(result.get("initial_error_count", 0) or 0)
        final = int(result.get("final_error_count", 0) or 0)
        accepted = int(result.get("accepted_patches", 0) or 0)
        if initial > 0 and final < initial and accepted > 0:
            return True
    except Exception:
        pass
    return False


def _format_summary(
    result: Dict[str, Any],
    project: str,
    lang: str,
    log_path: Path,
    status_ok: bool,
) -> str:
    lines = [
        "=" * 64,
        "Webbles dev_run — summary",
        "=" * 64,
        f"  language     : {lang}",
        f"  project      : {project}",
        f"  status       : {'SUCCESS' if status_ok else 'FAILED'}  "
        f"(raw={result.get('status', '?')})",
        f"  accepted     : {result.get('accepted_patches', 0)}",
        f"  needs_review : {result.get('needs_review_count', 0)}",
        f"  errors final : {result.get('final_error_count', 0)} "
        f"(initial: {result.get('initial_error_count', 0)})",
        f"  log file     : {log_path}",
        "=" * 64,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dev_run",
        description=(
            "Запуск Webbles из терминала. Использует тот же pipeline "
            "(agent.run_fix.run_fix_agent), который вызывает Studio при "
            "нажатии Run."
        ),
    )
    p.add_argument(
        "--project",
        required=True,
        help="Путь к папке тестового проекта (абсолютный или относительный).",
    )
    p.add_argument(
        "--lang",
        required=True,
        help=(
            "Язык проекта. Поддерживается: "
            "rust/python/javascript/typescript/cpp/csharp/go/java/kotlin "
            "(а также короткие алиасы: rs, py, js, ts, cs, c#, cxx)."
        ),
    )
    return p.parse_args()


def main() -> int:
    try:
        args = parse_args()
    except SystemExit as e:
        # argparse уже напечатал --help / usage — пробрасываем его exit code.
        return int(getattr(e, "code", 1) or 0)

    # 1) Проверка пути.
    try:
        project_path = Path(args.project).expanduser().resolve()
    except Exception as e:
        print(f"[dev_run] ERROR: bad project path: {e}", file=sys.stderr)
        return 1
    if not project_path.exists() or not project_path.is_dir():
        print(
            f"[dev_run] ERROR: project path does not exist or is not a directory: "
            f"{project_path}",
            file=sys.stderr,
        )
        return 1

    # 2) Нормализация языка.
    lang_alias = (args.lang or "").strip().lower()
    if not lang_alias:
        print("[dev_run] ERROR: --lang is required and must be non-empty", file=sys.stderr)
        return 1
    lang = _LANG_MAP.get(lang_alias)
    if not lang:
        supported = ", ".join(sorted(set(_LANG_MAP.values())))
        print(
            f"[dev_run] ERROR: language not supported: {args.lang!r}. "
            f"Supported: {supported}",
            file=sys.stderr,
        )
        return 1

    # 3) Подготовка лог-файла.
    log_path = ROOT / ".webbles_logs" / "latest.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[dev_run] ERROR: cannot create log dir {log_path.parent}: {e}",
              file=sys.stderr)
        return 1

    # 4) Env + logging — ровно как в Studio, но с overwrite-файлом.
    _load_env()
    try:
        _setup_logging(log_path)
    except Exception as e:
        print(f"[dev_run] ERROR: logging init failed: {e}", file=sys.stderr)
        return 1

    logger = logging.getLogger("dev_run")
    logger.info("dev_run: project=%s lang=%s", project_path, lang)

    # 5) Импорт ПОСЛЕ env + logging, чтобы инициализация Controller / llm_client
    #    попадала в latest.log (зеркало studio.py поведения).
    try:
        from agent.run_fix import run_fix_agent
        from agent.events import EventBus
    except Exception as e:
        logger.error("dev_run: cannot import agent.run_fix: %s", e, exc_info=True)
        print(f"[dev_run] ERROR: import failed: {e}", file=sys.stderr)
        return 1

    bus = EventBus()
    try:
        bus.subscribe(_print_event)
    except Exception as e:
        logger.warning("dev_run: cannot subscribe to EventBus: %s", e)

    # 6) Запуск pipeline.
    try:
        result = run_fix_agent(str(project_path), lang, emit=bus)
    except KeyboardInterrupt:
        logger.warning("dev_run: interrupted by user")
        print("\n[dev_run] interrupted by user", file=sys.stderr)
        return 1
    except Exception as e:
        logger.error("dev_run: pipeline crashed: %s", e, exc_info=True)
        print(f"[dev_run] PIPELINE CRASHED: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    if not isinstance(result, dict):
        logger.error("dev_run: pipeline returned non-dict result: %r", result)
        print("[dev_run] ERROR: pipeline returned non-dict result", file=sys.stderr)
        return 1

    if result.get("refused"):
        logger.warning("dev_run: pipeline refused: %s", result.get("message"))
        print(f"[dev_run] PIPELINE REFUSED: {result.get('message', '')}",
              file=sys.stderr)
        return 1

    # 7) Итог + exit code.
    status_ok = _is_success(result)
    summary = _format_summary(result, str(project_path), lang, log_path, status_ok)
    print()
    print(summary)
    logger.info("dev_run finished status=%s", "SUCCESS" if status_ok else "FAILED")
    return 0 if status_ok else 1


if __name__ == "__main__":
    sys.exit(main())
