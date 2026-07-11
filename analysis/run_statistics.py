"""
RunStatistics — сбор и запись статистики прогона в `<project>/statistic/`.

Зачем: после каждого прогона `webbles_fix` пользователь хочет видеть
читаемый отчёт «что было / что стало / на сколько ускорил» и иметь
машиночитаемый дамп для последующего анализа или показа другим людям.

Контракт чистый: класс ничего не знает про pipeline_engine. Его кормят
методами `record_*`, в конце вызывают `write_to_disk(project_path)`.
Это позволяет тестировать класс без подъёма движка.

Каталог `<project>/statistic/` после прогона содержит:
  - `run_<YYYYMMDD-HHMMSS>.json` — машиночитаемый дамп всех метрик.
  - `run_<YYYYMMDD-HHMMSS>.md`   — читаемый отчёт того же прогона.
  - `summary.md`                 — последний прогон (overwritten каждый раз).
  - `runs.jsonl`                 — append-only хвост (по строке на прогон,
    сжатый — для трендов).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DecisionRecord:
    """Одна запись по одной ошибке: что DecideStage решил с ней сделать."""
    file: str = ""
    line: int = 0
    code: str = ""
    error_class: str = ""
    message: str = ""
    decision: str = ""           # ACCEPT / NEEDS_REVIEW / REJECT
    reason: str = ""
    patch_source: str = ""       # rule_based / structured_llm / memory / ...
    confidence: Optional[float] = None
    review_verdict: str = ""     # ok / noisy / wrong / skipped / unavailable
    symbol_regression: bool = False  # O.14: ушли def/class?
    ts_iso: str = ""             # время решения


@dataclass
class RunStatistics:
    """Накопитель метрик одного прогона. После прогона зовём `write_to_disk`."""

    project_path: str = ""
    language: str = ""
    started_at_iso: str = ""
    finished_at_iso: str = ""
    duration_seconds: float = 0.0
    initial_error_count: int = 0
    final_error_count: int = 0
    # independent_scan_before/after (2026-07-03, bcrypt-прогон, deep-reasoner):
    # заголовок отчёта раньше строился от final_error_count — это длина
    # ВНУТРЕННЕЙ инкрементальной очереди context.current_errors, которая на
    # bcrypt распухла от де-маскированных mypy-ошибок (dependency recovery
    # поставил pytest в sandbox) и показала «до 24, после 58 (-141.7%)», хотя
    # честный независимый full scan дал 24 → 22 (unsafe_accept=0). Источник
    # правды — тот же `run_full_scan` до/после, что и в
    # `compute_progress_metrics` (core/pipeline_engine.py). None означает
    # «скан недоступен» — тогда to_markdown() падает обратно на очередь.
    independent_scan_before: Optional[int] = None
    independent_scan_after: Optional[int] = None
    initial_errors_by_class: Dict[str, int] = field(default_factory=dict)
    initial_errors_by_code: Dict[str, int] = field(default_factory=dict)
    decisions: List[DecisionRecord] = field(default_factory=list)
    accepted_files: List[str] = field(default_factory=list)
    rejected_files: List[str] = field(default_factory=list)
    needs_review_items: List[Dict[str, Any]] = field(default_factory=list)
    audit_passed_files: List[str] = field(default_factory=list)
    audit_failed_files: List[str] = field(default_factory=list)
    iterations: int = 0
    rollbacks: int = 0
    notes: List[str] = field(default_factory=list)
    # Timeout & improvement telemetry
    llm_timeout_count: int = 0
    project_timeout_triggered: bool = False
    stop_reason: str = ""
    net_delta_rollback_count: int = 0
    net_delta_uncertain_count: int = 0
    ruff_autofix_total_fixed: int = 0
    # E999 Semantic Recovery (2026-06-21, опциональная стратегия) — отдельная
    # телеметрия для разбора, насколько эффективна реконструкция целиком,
    # отдельно от обычного успеха/провала пайплайна.
    syntax_reconstruction_attempts: int = 0
    syntax_reconstruction_failed_count: int = 0
    # Python version-aware analysis (2026-06-22) — счётчик "ошибок", которые
    # на самом деле несовместимость версий Python (PEP 695 и др.), не баг.
    # Отдельная категория: НЕ ACCEPT/REJECT/NEEDS_REVIEW, не входит в
    # decisions[], не портит метрики конверсии.
    unsupported_python_version_count: int = 0
    unsupported_python_version_items: List[Dict[str, Any]] = field(default_factory=list)
    # empty_response диагностика (2026-06-22) — инфраструктурные сбои LLM
    # (timeout/transport_error/empty/bad_format/parser_fail): LLM не вернул
    # НИЧЕГО оцениваемого. НЕ входит в rejected_patches/decisions[] REJECT —
    # та же логика, что unsupported_python_version выше (категория сбоев,
    # которая не должна портить воронку конверсии).
    llm_infra_failure_count: int = 0
    llm_infra_failure_items: List[Dict[str, Any]] = field(default_factory=list)

    # -------- lifecycle ---------------------------------------------

    def start(self, project_path: str, language: str) -> None:
        self.project_path = str(project_path or "")
        self.language = str(language or "")
        self.started_at_iso = _iso_now()

    def record_initial_errors(self, errors: List[Dict[str, Any]]) -> None:
        self.initial_error_count = len(errors or [])
        by_class = Counter()
        by_code = Counter()
        for e in (errors or []):
            cls = str(e.get("error_class") or e.get("error_type") or "UNKNOWN")
            code = str(e.get("code") or "")
            by_class[cls] += 1
            if code:
                by_code[code] += 1
        self.initial_errors_by_class = dict(by_class)
        self.initial_errors_by_code = dict(by_code)

    def record_decision(self, decision: DecisionRecord) -> None:
        self.decisions.append(decision)

    def add_note(self, note: str) -> None:
        if note:
            self.notes.append(str(note))

    def record_final(
        self,
        *,
        final_error_count: int,
        accepted_patches: List[Dict[str, Any]],
        rejected_patches: List[Dict[str, Any]],
        needs_review_items: List[Dict[str, Any]],
        audit_result: Dict[str, Any],
        iterations: int = 0,
        rollbacks: int = 0,
        llm_timeout_count: int = 0,
        project_timeout_triggered: bool = False,
        stop_reason: str = "",
        net_delta_rollback_count: int = 0,
        net_delta_uncertain_count: int = 0,
        ruff_autofix_total_fixed: int = 0,
        syntax_reconstruction_attempts: int = 0,
        syntax_reconstruction_failed_count: int = 0,
        unsupported_python_version_count: int = 0,
        unsupported_python_version_items: Optional[List[Dict[str, Any]]] = None,
        llm_infra_failure_count: int = 0,
        llm_infra_failure_items: Optional[List[Dict[str, Any]]] = None,
        independent_scan_before: Optional[int] = None,
        independent_scan_after: Optional[int] = None,
    ) -> None:
        self.finished_at_iso = _iso_now()
        self.duration_seconds = max(
            0.0, _iso_diff_seconds(self.started_at_iso, self.finished_at_iso)
        )
        self.final_error_count = int(final_error_count or 0)
        # bcrypt 2026-07-03: сохраняем как есть (None допустим — «скан
        # недоступен», не 0 — 0 означало бы «после фикса ошибок не осталось»).
        self.independent_scan_before = (
            int(independent_scan_before) if independent_scan_before is not None else None
        )
        self.independent_scan_after = (
            int(independent_scan_after) if independent_scan_after is not None else None
        )
        self.accepted_files = _files_from_patches(accepted_patches)
        self.rejected_files = _files_from_patches(rejected_patches)
        self.needs_review_items = list(needs_review_items or [])
        audit_result = audit_result or {}
        self.audit_passed_files = list(audit_result.get("passed_files") or [])
        self.audit_failed_files = list((audit_result.get("failed_segments") or {}).keys())
        self.iterations = int(iterations or 0)
        self.rollbacks = int(rollbacks or 0)
        self.llm_timeout_count = int(llm_timeout_count or 0)
        self.project_timeout_triggered = bool(project_timeout_triggered)
        self.stop_reason = str(stop_reason or "")
        self.net_delta_rollback_count = int(net_delta_rollback_count or 0)
        self.net_delta_uncertain_count = int(net_delta_uncertain_count or 0)
        self.ruff_autofix_total_fixed = int(ruff_autofix_total_fixed or 0)
        self.syntax_reconstruction_attempts = int(syntax_reconstruction_attempts or 0)
        self.syntax_reconstruction_failed_count = int(syntax_reconstruction_failed_count or 0)
        self.unsupported_python_version_count = int(unsupported_python_version_count or 0)
        self.unsupported_python_version_items = list(unsupported_python_version_items or [])
        self.llm_infra_failure_count = int(llm_infra_failure_count or 0)
        self.llm_infra_failure_items = list(llm_infra_failure_items or [])

    # -------- aggregation ------------------------------------------

    def aggregate(self) -> Dict[str, Any]:
        """Сводные показатели для summary.md / dashboards."""
        by_decision = Counter(d.decision or "" for d in self.decisions)
        by_source = Counter(d.patch_source or "" for d in self.decisions if d.patch_source)
        by_review = Counter(d.review_verdict or "" for d in self.decisions if d.review_verdict)
        sym_regs = [d for d in self.decisions if d.symbol_regression]
        syntax_reconstruction_outcomes = Counter(
            d.decision or "" for d in self.decisions
            if d.patch_source == "syntax_reconstruction"
        )
        delta = self.initial_error_count - self.final_error_count
        success_pct = (
            100.0 * delta / self.initial_error_count
            if self.initial_error_count else 0.0
        )
        # independent_scan_delta (bcrypt 2026-07-03) — None-безопасно: если
        # хотя бы одно из значений отсутствует, скан недоступен для этого
        # прогона, delta тоже None (не 0 — иначе выглядело бы как «ничего не
        # изменилось»).
        if self.independent_scan_before is not None and self.independent_scan_after is not None:
            independent_scan_delta = self.independent_scan_before - self.independent_scan_after
        else:
            independent_scan_delta = None
        return {
            "initial_errors": self.initial_error_count,
            "final_errors": self.final_error_count,
            "errors_fixed_delta": delta,
            "errors_fixed_pct": round(success_pct, 1),
            "independent_scan_before": self.independent_scan_before,
            "independent_scan_after": self.independent_scan_after,
            "independent_scan_delta": independent_scan_delta,
            "decisions_total": len(self.decisions),
            "decisions_by_outcome": dict(by_decision),
            "patches_by_source": dict(by_source),
            "review_by_verdict": dict(by_review),
            "symbol_regression_count": len(sym_regs),
            "accepted_files_count": len(self.accepted_files),
            "rejected_files_count": len(self.rejected_files),
            "needs_review_count": len(self.needs_review_items),
            "audit_passed_files_count": len(self.audit_passed_files),
            "audit_failed_files_count": len(self.audit_failed_files),
            "iterations": self.iterations,
            "rollbacks": self.rollbacks,
            "duration_seconds": round(self.duration_seconds, 1),
            "llm_timeout_count": self.llm_timeout_count,
            "project_timeout_triggered": self.project_timeout_triggered,
            "stop_reason": self.stop_reason,
            "net_delta_rollback_count": self.net_delta_rollback_count,
            "net_delta_uncertain_count": self.net_delta_uncertain_count,
            "ruff_autofix_total_fixed": self.ruff_autofix_total_fixed,
            "syntax_reconstruction_attempts": self.syntax_reconstruction_attempts,
            "syntax_reconstruction_failed_count": self.syntax_reconstruction_failed_count,
            "syntax_reconstruction_outcomes": dict(syntax_reconstruction_outcomes),
            "unsupported_python_version_count": self.unsupported_python_version_count,
            "llm_infra_failure_count": self.llm_infra_failure_count,
            "llm_infra_failure_by_category": dict(
                Counter(it.get("category", "") for it in self.llm_infra_failure_items)
            ),
        }

    # -------- serialization ----------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["aggregate"] = self.aggregate()
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)

    def to_markdown(self) -> str:
        """Читаемый отчёт. Простой Markdown без таблиц-зебр —
        выводится в любом README/issue."""
        agg = self.aggregate()
        lines: List[str] = []
        lines.append(f"# Webbles Fix — отчёт прогона")
        lines.append("")
        lines.append(f"- **Проект:** `{self.project_path}`")
        lines.append(f"- **Язык:** `{self.language}`")
        lines.append(f"- **Старт:** {self.started_at_iso}")
        lines.append(f"- **Финиш:** {self.finished_at_iso}")
        lines.append(f"- **Длительность:** {agg['duration_seconds']} сек")
        lines.append(f"- **Итераций движка:** {self.iterations}")
        lines.append(f"- **Откатов:** {self.rollbacks}")
        lines.append("")
        lines.append("## Итог")
        lines.append("")
        # bcrypt-прогон 2026-07-03: заголовок «Итог» раньше строился от
        # final_error_count — длины ВНУТРЕННЕЙ инкрементальной очереди
        # движка, распухшей от де-маскированных mypy-ошибок (dependency
        # recovery поставил pytest в sandbox) — отчёт показал «до 24, после
        # 58 (-141.7%)», хотя честный независимый скан дал 24 → 22
        # (unsafe_accept=0). Теперь «Итог» строится от независимого скана,
        # если он доступен; очередь движка уходит в отдельную диагностическую
        # строку ниже. Если скан недоступен (None) — прежнее поведение
        # (числа очереди) без изменений, это fallback.
        if agg["independent_scan_before"] is not None and agg["independent_scan_after"] is not None:
            _scan_before = agg["independent_scan_before"]
            _scan_after = agg["independent_scan_after"]
            _scan_delta = agg["independent_scan_delta"]
            _scan_pct = (
                round(100.0 * _scan_delta / _scan_before, 1) if _scan_before else 0.0
            )
            lines.append(
                f"- Ошибок до: **{_scan_before}**, после: **{_scan_after}** "
                f"(закрыто {_scan_delta}, {_scan_pct}%) — независимый скан"
            )
            lines.append(
                f"- Очередь движка (инкрементальная): до {agg['initial_errors']}, "
                f"после {agg['final_errors']} — диагностика, не источник правды "
                "(см. инцидент bcrypt 2026-07-03)"
            )
        else:
            lines.append(
                f"- Ошибок до: **{agg['initial_errors']}**, после: **{agg['final_errors']}** "
                f"(закрыто {agg['errors_fixed_delta']}, {agg['errors_fixed_pct']}%)"
            )
        lines.append(f"- Решений всего: **{agg['decisions_total']}**")
        if agg["decisions_by_outcome"]:
            parts = ", ".join(f"{k}: {v}" for k, v in agg["decisions_by_outcome"].items())
            lines.append(f"  - распределение: {parts}")
        lines.append(f"- Файлы изменены (ACCEPT): {agg['accepted_files_count']}")
        lines.append(f"- На ручной просмотр: {agg['needs_review_count']}")
        lines.append(f"- Аудит: прошло {agg['audit_passed_files_count']} файлов, "
                     f"частично откачено {agg['audit_failed_files_count']}")
        if agg["symbol_regression_count"]:
            lines.append(f"- **O.14: регрессия символов** в {agg['symbol_regression_count']} патчах "
                         "(отправлены в NEEDS_REVIEW вместо ACCEPT)")
        lines.append("")

        if self.initial_errors_by_class:
            lines.append("## Ошибки на старте — по классам")
            lines.append("")
            for cls, n in sorted(self.initial_errors_by_class.items(),
                                 key=lambda x: -x[1]):
                lines.append(f"- `{cls}`: {n}")
            lines.append("")

        if self.initial_errors_by_code:
            top = sorted(self.initial_errors_by_code.items(),
                         key=lambda x: -x[1])[:10]
            lines.append("## Топ-10 кодов ошибок")
            lines.append("")
            for code, n in top:
                lines.append(f"- `{code}`: {n}")
            lines.append("")

        if agg["patches_by_source"]:
            lines.append("## Источники патчей")
            lines.append("")
            for src, n in sorted(agg["patches_by_source"].items(),
                                 key=lambda x: -x[1]):
                lines.append(f"- `{src or '<unknown>'}`: {n}")
            lines.append("")

        if agg["review_by_verdict"]:
            lines.append("## Вердикты ревью (D.3)")
            lines.append("")
            for v, n in sorted(agg["review_by_verdict"].items(),
                               key=lambda x: -x[1]):
                lines.append(f"- `{v}`: {n}")
            lines.append("")

        if self.accepted_files:
            lines.append(f"## Принятые файлы ({len(self.accepted_files)})")
            lines.append("")
            for f in self.accepted_files:
                lines.append(f"- `{f}`")
            lines.append("")

        if self.needs_review_items:
            lines.append(f"## На ручной просмотр ({len(self.needs_review_items)})")
            lines.append("")
            for it in self.needs_review_items[:20]:
                f = it.get("file", "?")
                ln = it.get("line", 0)
                code = it.get("error_code", "?")
                lines.append(f"- `{f}:{ln}` [{code}]")
            if len(self.needs_review_items) > 20:
                lines.append(f"- ... и ещё {len(self.needs_review_items) - 20}")
            lines.append("")
            lines.append("Лежат в `.webbles_fix/needs_review/`.")
            lines.append("")

        if self.decisions:
            lines.append("## Журнал решений")
            lines.append("")
            for d in self.decisions:
                conf = f"{d.confidence:.2f}" if isinstance(d.confidence, (int, float)) else "?"
                lines.append(
                    f"- **{d.decision}** `{d.file}:{d.line}` [{d.code}] — "
                    f"источник `{d.patch_source or '<none>'}` "
                    f"(conf={conf}, review={d.review_verdict or '<none>'})"
                )
                if d.reason:
                    lines.append(f"  - причина: {d.reason}")
                if d.symbol_regression:
                    lines.append("  - O.14: после патча исчезли def/class — NEEDS_REVIEW")
            lines.append("")

        # Timeout / net-delta / ruff telemetry section
        _has_telemetry = (
            self.llm_timeout_count > 0
            or self.project_timeout_triggered
            or self.net_delta_rollback_count > 0
            or self.net_delta_uncertain_count > 0
            or self.ruff_autofix_total_fixed > 0
        )
        if _has_telemetry:
            lines.append("## Телеметрия")
            lines.append("")
            if self.project_timeout_triggered:
                lines.append("- **PROJECT_TIMEOUT** сработал — цикл прерван по бюджету времени")
            if self.stop_reason:
                lines.append(f"- Причина остановки: `{self.stop_reason}`")
            if self.llm_timeout_count:
                lines.append(f"- LLM hard timeout: **{self.llm_timeout_count}** раз")
            if self.net_delta_rollback_count:
                lines.append(f"- Net-delta regression откаты: {self.net_delta_rollback_count}")
            if self.net_delta_uncertain_count:
                lines.append(f"- Net-delta uncertain → NEEDS_REVIEW: {self.net_delta_uncertain_count}")
            if self.ruff_autofix_total_fixed:
                lines.append(f"- Ruff autofix (pre-pipeline): исправлено {self.ruff_autofix_total_fixed} нарушений")
            lines.append("")

        if self.notes:
            lines.append("## Заметки")
            lines.append("")
            for n in self.notes:
                lines.append(f"- {n}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    # -------- disk ---------------------------------------------------

    @staticmethod
    def _webbles_fix_root() -> Path:
        """Корень установки webbles_fix (где лежит сам этот модуль).

        analysis/run_statistics.py → parents[1] = корень webbles_fix.
        """
        return Path(__file__).resolve().parents[1]

    @staticmethod
    def statistics_dir(project_path) -> Path:
        """P0.3: statistic/ теперь живёт в папке СИСТЕМЫ webbles_fix
        (не в ремонтируемом проекте). Так пользователь имеет один
        центральный dashboard по всем своим проектам, а проект остаётся
        чистым."""
        proj_name = Path(project_path).name or "unknown_project"
        return RunStatistics._webbles_fix_root() / "statistic" / proj_name

    def write_to_disk(self, project_path: Optional[str] = None) -> Dict[str, Path]:
        """Пишет 4 файла в `<webbles_fix>/statistic/<project_name>/`.
        Любой сбой → пустой dict.

        Возвращает {summary_md, run_md, run_json, runs_jsonl} с путями.
        """
        proj = Path(project_path or self.project_path)
        try:
            stat_dir = self.statistics_dir(proj)
            stat_dir.mkdir(parents=True, exist_ok=True)
            ts = (self.started_at_iso or _iso_now()).replace(":", "").replace("-", "")
            ts = ts.split(".")[0]  # YYYYMMDDTHHMMSS+TZ
            ts_safe = ts.replace("T", "-").replace("+", "p").replace("Z", "z")[:20]
            run_md = stat_dir / f"run_{ts_safe}.md"
            run_json = stat_dir / f"run_{ts_safe}.json"
            summary_md = stat_dir / "summary.md"
            runs_jsonl = stat_dir / "runs.jsonl"

            md = self.to_markdown()
            run_md.write_text(md, encoding="utf-8", newline="")
            run_json.write_text(self.to_json(), encoding="utf-8", newline="")
            summary_md.write_text(md, encoding="utf-8", newline="")

            # Append одну строку для трендов.
            agg = self.aggregate()
            agg_row = {
                "started_at": self.started_at_iso,
                "language": self.language,
                "project_path": self.project_path,
                **agg,
            }
            with runs_jsonl.open("a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(agg_row, ensure_ascii=False, default=str) + "\n")

            return {
                "summary_md": summary_md,
                "run_md": run_md,
                "run_json": run_json,
                "runs_jsonl": runs_jsonl,
            }
        except Exception as e:
            logger.warning("RunStatistics: не удалось записать в %s: %s", proj, e)
            return {}


# ---------------------------------------------------------------------
# Хелперы.
# ---------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_diff_seconds(a: str, b: str) -> float:
    try:
        ta = datetime.strptime(a, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        tb = datetime.strptime(b, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (tb - ta).total_seconds()
    except Exception:
        return 0.0


def _files_from_patches(patches: Optional[List[Dict[str, Any]]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for p in (patches or []):
        p = p or {}
        # Nested format: {"error": {"file": ...}} (from context.accepted_patches / tests)
        # Flat format:   {"file": ..., "message": ..., "code": ...} (from _global_fix_loop)
        err = p.get("error") or {}
        f = err.get("file") or p.get("file") or ""
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    """int(value) без риска ValueError на нечисловой строке/None/др. типах.

    Найдено при живой ревалидации находки #1 (2026-06-21): `int((error or
    {}).get("line") or 0)` мог поднять ValueError на нестандартных типах
    `line` из некоторых анализаторов (например, строковое представление
    вместо int) — ИСКЛЮЧЕНИЕ глушилось верхним `except Exception: pass` в
    append_decision_log БЕЗ ЛОГА, и вся decision-запись пропадала молча, не
    только битое поле line. decision_from_metadata теперь не должен падать
    вовсе ни на каком входе — это единственная надёжная защита от повторения
    находки.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def decision_from_metadata(error: Dict[str, Any], metadata: Dict[str, Any],
                           decision: str, reason: str) -> DecisionRecord:
    """Удобный конструктор: вытаскивает поля из `error` + `context.metadata`.

    Намеренно НЕ поднимает исключений на странном входе (см. _safe_int) —
    единственный потребитель (append_decision_log) больше не должен терять
    запись целиком из-за одного нечислового поля."""
    meta = metadata or {}
    review = meta.get("review") or {}
    sym_reg = meta.get("symbol_regression") or {}
    has_reg = bool(sym_reg and (sym_reg.get("missing_defs") or sym_reg.get("missing_classes")))
    conf = meta.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except Exception:
        conf = None
    return DecisionRecord(
        file=str((error or {}).get("file") or ""),
        line=_safe_int((error or {}).get("line"), 0),
        code=str((error or {}).get("code") or ""),
        error_class=str((error or {}).get("error_class") or ""),
        message=str((error or {}).get("message") or "")[:200],
        decision=str(decision or ""),
        reason=str(reason or ""),
        patch_source=str(meta.get("patch_source") or ""),
        confidence=conf,
        review_verdict=str(review.get("verdict") or ""),
        symbol_regression=has_reg,
        ts_iso=_iso_now(),
    )


def append_decision_log(metadata: Dict[str, Any], error: Dict[str, Any],
                        decision: str, reason: str) -> None:
    """Кладёт DecisionRecord-as-dict в `metadata['decisions']` для RunStatistics.

    Централизованная точка записи (control series 2026-06-21 находка #1):
    раньше эта логика была приватным static-методом DecideStage, и пути, не
    проходящие через DecideStage.execute() (NeedsReviewStage.execute(),
    NET_DELTA rollback/needs_review в validate_stage.py — оба напрямую
    переходят в NEXT_ERROR/NEEDS_REVIEW state, минуя DecideStage), никогда
    его не вызывали — decisions[] оставался пуст для этих REJECT/NEEDS_REVIEW
    исходов, даже когда aggregate-счётчики (rejected_files_count/
    needs_review_count) были корректны. Мутирует `metadata` IN PLACE (как и
    раньше) — вызывающий код передаёт СВОЙ локальный dict перед
    context.update(metadata=...).

    except Exception теперь логирует на WARNING (не глушит молча) — именно
    тихое проглатывание здесь маскировало бы РЕЦИДИВ находки #1 (см. живую
    ревалидацию 2026-06-21: одна decision-запись из реального прогона не
    попала в decisions[], точная причина не подтверждена, но decision_from_
    metadata сделан defensively-safe против самой вероятной гипотезы —
    нечисловое поле `line`).
    """
    try:
        from dataclasses import asdict
        rec = decision_from_metadata(error or {}, metadata or {}, decision, reason)
        metadata.setdefault("decisions", []).append(asdict(rec))
    except Exception as e:
        logger.warning(
            "append_decision_log: запись решения потеряна (decision=%s reason=%s "
            "error=%r): %s", decision, reason, error, e,
        )
