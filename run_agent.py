"""
Запуск живого Fix-агента (Stage L) одной командой.

    python run_agent.py <путь_к_проекту> <язык>

Примеры:
    python run_agent.py C:\\dev\\my_broken_app python
    python run_agent.py ./some_rust_proj rust --quiet

Язык: rust / python / javascript / typescript.
Ключ DeepSeek берётся из .env (WEBBLES_LLM_API_KEY) — см. .env.example.
Агент поднимает реальный пайплайн, чинит ошибки и печатает поток событий
(что взял в работу, какой вердикт, что осталось на ручной просмотр).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# корень проекта в sys.path (на случай запуска из другого каталога)
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _fmt(event) -> str:
    d = event.to_dict()
    t, data = d["type"], d["data"]
    if t == "run_started":
        return f"▶  Старт ({data.get('language')}): {data.get('project')}"
    if t == "error_dequeued":
        return f"   • взял {data.get('code')} @ {data.get('file')}:{data.get('line')}"
    if t == "verdict":
        return f"     вердикт: {data.get('verdict')}"
    if t == "needs_review":
        return f"     → на ручной просмотр: {data.get('file')}"
    if t == "run_finished":
        return (f"✓  Готово. Принято: {data.get('accepted')}, "
                f"на ревью: {data.get('needs_review')}, осталось: {data.get('failed')} "
                f"(статус {data.get('status')})")
    return f"   [{t}] {data}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Webbles Fix — живой Fix-агент")
    ap.add_argument("project_path", help="путь к проекту, который нужно починить")
    ap.add_argument("language",
                    choices=["rust", "python", "javascript", "typescript", "js", "ts"],
                    help="язык проекта")
    ap.add_argument("--quiet", action="store_true", help="не печатать поток событий")
    ap.add_argument("--resume", action="store_true",
                    help="продолжить с checkpoint (пропустить уже обработанные ошибки)")
    args = ap.parse_args()

    proj = Path(args.project_path)
    if not proj.exists():
        print(f"Путь не найден: {proj}", file=sys.stderr)
        return 2

    from agent.run_fix import run_fix_agent
    from agent.events import EventBus

    bus = EventBus()
    if not args.quiet:
        bus.subscribe(lambda e: print(_fmt(e)))

    result = run_fix_agent(str(proj), args.language, emit=bus, resume=args.resume)

    if result.get("refused"):
        print("Отказано:", result.get("message"))
        return 2

    print("\n=== ИТОГ ===")
    print("Статус:           ", result.get("status"))
    # 2026-06-24: independent_scan_before/after (fresh full scan, project_path,
    # один и тот же метод до/после) — точка истины для прогресса. baseline_remaining
    # больше НЕ заголовочный показатель — это внутренняя инкрементальная модель
    # очереди движка, может разойтись с реальным состоянием диска (см.
    # queue_delta vs independent_scan_delta, project_*_audit_2026_06_24).
    print("Ошибок было (independent_scan_before):", result.get("independent_scan_before", result.get("initial_error_count")))
    print("Ошибок сейчас (independent_scan_after):", result.get("independent_scan_after", result.get("total_current_errors")))
    print("independent_scan_delta:", result.get("independent_scan_delta"))
    print("  из них flake8:  ", result.get("flake8_current_errors"))
    print("  из них semgrep: ", result.get("semgrep_current_errors"))
    print("  из них bandit:  ", result.get("bandit_current_errors"))
    print("queue_delta (старая метрика, справочно):", result.get("queue_delta"))
    print("real_fix_impact (по файлам с ACCEPT):", result.get("real_fix_impact"))
    if result.get("accept_cancelled_count"):
        print("accept_cancelled (осциллирующие ACCEPT, net_change=0):", result.get("accept_cancelled_count"))
    print("Принято патчей:   ", result.get("accepted_patches"))
    print("Отклонено:        ", result.get("rejected_patches"))
    print("attempted_errors_count:", result.get("attempted_errors_count"))
    print("На ручной просмотр:", result.get("needs_review_count"),
          f"(с патчем: {result.get('needs_review_with_patch')}, "
          f"без попытки: {result.get('needs_review_no_patch_unprocessed')})")
    if result.get("cycles_run") or result.get("stop_reason"):
        print("Циклов выполнено: ", result.get("cycles_run", 0))
        print("Причина стопа:    ", result.get("stop_reason", ""))
    if result.get("project_timeout_triggered"):
        print("PROJECT_TIMEOUT:   да (бюджет исчерпан)")
    if result.get("llm_timeout_count"):
        print("LLM hard timeout: ", result.get("llm_timeout_count"), "раз")
    if result.get("net_delta_rollback_count"):
        print("Net-delta откатов:", result.get("net_delta_rollback_count"))
    if result.get("net_delta_uncertain_count"):
        print("Net-delta NR:     ", result.get("net_delta_uncertain_count"))
    if result.get("ruff_autofix_total_fixed"):
        print("Ruff autofix:     ", result.get("ruff_autofix_total_fixed"), "нарушений")
    if result.get("unsupported_python_version_count"):
        print(
            "Несовместимость версии Python:",
            result.get("unsupported_python_version_count"),
            "файл(ов) исключено из анализа (не E999, требуется новее Python)",
        )
    if result.get("llm_infra_failure_count"):
        from collections import Counter as _Counter
        _by_cat = _Counter(
            it.get("category", "") for it in result.get("llm_infra_failure_items", [])
        )
        print(
            "LLM инфраструктурных сбоев:", result.get("llm_infra_failure_count"),
            "(" + ", ".join(f"{k}={v}" for k, v in _by_cat.most_common()) + ")",
        )
    try:
        from core.pipeline_engine import PipelineEngine
        _rt = PipelineEngine._project_runtime_dir(proj)
    except Exception:
        _rt = proj / ".webbles"
    print("\nЖурнал агента:    ", str(_rt / ".webbles" / "fix_state.md"))
    if result.get("needs_review_count"):
        print("Очередь ревью:    ", str(_rt / "needs_review"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
