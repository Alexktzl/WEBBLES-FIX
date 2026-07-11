"""
Stage D.3 — ReviewStage.

LLM-ревью применённого патча перед окончательным решением (DECIDING).
Запрашивает у модели verdict ∈ {ok, noisy, wrong} и `confidence_adjustment`
в диапазоне [-0.2, +0.2]. Результат складывает в `metadata["review"]` и
сразу корректирует `metadata["confidence"]` (clamped в [0, 1]).

Принципы:

1. **Skip для high-trust источников.** rule-based фиксы (quote/brace/splice),
   golden, memory и tool-based (correctr, semantic_repair) НЕ нуждаются в
   ревью — их confidence уже отражает реальность. Гоняем reviewer только
   на LLM-выходах (structured_llm, llm, llm_critical, llm_blocking_*,
   llm_segmented) и compiler_hint (rustc_suggestion — иногда тоже шумит).

2. **Гарантия прохода.** Если LLM не ответил или ответил мусором — ничего
   не корректируем и идём в DECIDING как обычно. Reviewer — это
   улучшение, не блокировка.

3. **Снапшот before-errors.** Перед APPLYING_PATCH мы уже знаем
   `current_errors` — это «BEFORE». `validation_results.get("current_errors_after")`
   (или текущий `context.current_errors` после VALIDATING) — это «AFTER».
   Если ни того, ни другого нет, передаём пустые списки — reviewer сделает
   что сможет.
"""

import logging
from typing import Any, Dict, List

from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

logger = logging.getLogger(__name__)


class ReviewStage(PipelineStage):
    """Strict reviewer между VALIDATING и DECIDING."""

    # Источники, для которых ревью НЕ запускается. Confidence у них и так
    # на высоте (см. core.contract._DEFAULT_CONFIDENCE_BY_SOURCE).
    HIGH_TRUST_SOURCES = frozenset({
        "golden", "memory", "rule_based",
        "quote_heuristic", "brace_heuristic", "splice_recovery",
        "correctr", "semantic_repair",
        # Конверсия-1 (2026-07-02): детерминированный однострочный
        # `# type: ignore[...]` (см. _make_type_ignore_patch) — штатный и
        # единственно доступный фикс для _UNFIXABLE_MYPY_CODES; LLM-ревью
        # здесь добавляло только рандом (verdict noisy → REJECT реально
        # сработавшего фикса, 13 REJECT в learning_cases 06-25..07-02).
        "type_ignore",
    })

    # Максимальное число символов сниппета вокруг ошибки, который шлём reviewer-у.
    _FILE_CONTEXT_RADIUS_LINES = 30

    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия REVIEW: LLM-ревью патча...")

        # 2026-06-24: см. аналогичную точку в GeneratePatchStage.execute() —
        # выставляем PROJECT_DEADLINE на клиенте перед обращением к LLM,
        # чтобы внутренние retry-петли llm_client тоже его уважали.
        if self.llm_client is not None:
            self.llm_client.project_deadline = context.metadata.get(MetadataKeys.PROJECT_DEADLINE)

        error = context.selected_error
        if not error:
            logger.debug("  нет selected_error — пропускаем REVIEW")
            return context.add_state_to_history(State.DECIDING)

        patch_source = context.metadata.get(MetadataKeys.PATCH_SOURCE, "")

        # E999 Semantic Recovery (2026-06-21): этот источник применяется как
        # full_file_replacement (generated_patch == None, см.
        # GeneratePatchStage._try_semantic_recovery/ApplyPatchStage) — ниже
        # обычный bail-out "нет patch" пропустил бы ревью целиком, а это
        # ИМЕННО тот источник, которому усиленная проверка нужнее всего.
        if patch_source == "syntax_reconstruction":
            return self._review_syntax_reconstruction(context, error)

        patch = context.generated_patch
        if not isinstance(patch, str) or not patch.strip():
            logger.debug("  нет применённого patch — пропускаем REVIEW")
            return context.add_state_to_history(State.DECIDING)

        if patch_source in self.HIGH_TRUST_SOURCES:
            logger.info(
                "  source=%r — high-trust, ревью пропускаем", patch_source,
            )
            new_metadata = dict(context.metadata)
            new_metadata["review"] = {
                "verdict": "skipped",
                "reasons": [f"skipped: high-trust source ({patch_source})"],
                "confidence_adjustment": 0.0,
            }
            context = context.update(metadata=new_metadata)
            return context.add_state_to_history(State.DECIDING)

        # before/after — берём из validation_results, иначе пустые списки.
        vres = context.validation_results or {}
        before = list(vres.get("current_errors_before", []) or [])
        # AFTER — самое свежее, что есть на данный момент.
        after = list(context.current_errors or [])

        file_context = self._extract_file_context(context, error)

        try:
            verdict_obj = self.llm_client.review_patch(
                error=error,
                structured_edit=context.metadata.get("structured_edit"),
                patch=patch,
                before_errors=before,
                after_errors=after,
                file_context=file_context,
                language=context.language,
            )
        except Exception as e:
            logger.warning("  reviewer упал: %s — пропускаем без adjust", e)
            verdict_obj = None

        new_metadata = dict(context.metadata)
        if not verdict_obj:
            # Mark, чтобы DecideStage / финальный отчёт видели «ревью было,
            # но без вывода» — это отличается от «ревью не запускалось».
            new_metadata["review"] = {
                "verdict": "unavailable",
                "reasons": ["LLM did not return a parseable verdict"],
                "confidence_adjustment": 0.0,
            }
            context = context.update(metadata=new_metadata)
            return context.add_state_to_history(State.DECIDING)

        # Сохраняем raw-вердикт и корректируем confidence (с clamp).
        new_metadata["review"] = verdict_obj
        old_conf = float(new_metadata.get(MetadataKeys.CONFIDENCE, 0.5) or 0.5)
        adj = float(verdict_obj.get("confidence_adjustment", 0.0) or 0.0)
        new_conf = max(0.0, min(1.0, old_conf + adj))
        new_metadata[MetadataKeys.CONFIDENCE] = new_conf
        logger.info(
            "  reviewer verdict=%s adj=%+.2f → confidence %.2f → %.2f",
            verdict_obj.get("verdict"), adj, old_conf, new_conf,
        )

        context = context.update(metadata=new_metadata)
        return context.add_state_to_history(State.DECIDING)

    # ------------------------------------------------------------------
    # E999 Semantic Recovery (2026-06-21) — усиленная проверка для
    # patch_source="syntax_reconstruction". См. fixers/semantic_recovery.py
    # и tools/socratic_refiner.py.compare_logic.
    # ------------------------------------------------------------------

    def _review_syntax_reconstruction(self, context: PipelineContext, error: Dict) -> PipelineContext:
        from fixers.semantic_recovery import LogicSnapshot, extract_signatures_ast, signatures_match

        file_name = error.get("file", "") if error else ""
        work_path = getattr(context, "working_path", None) or context.project_path
        target = work_path / file_name if file_name else None

        snap_raw = context.metadata.get("_syntax_reconstruction_snapshot") or {}
        old_snapshot = LogicSnapshot(
            classes=set(snap_raw.get("classes") or []),
            functions={k: tuple(v) for k, v in (snap_raw.get("functions") or {}).items()},
            imports=set(snap_raw.get("imports") or []),
        )
        try:
            new_content = target.read_text(encoding="utf-8") if target and target.exists() else ""
        except Exception as e:
            logger.warning("  syntax_reconstruction review: не удалось прочитать %s: %s", target, e)
            new_content = ""

        new_signatures = extract_signatures_ast(new_content) if new_content else None

        # 1) Детерминированный AST-диф имён/сигнатур — самый дешёвый и
        # надёжный фильтр, не требует LLM.
        if new_signatures is None:
            matched, mismatches = False, ["реконструированный файл не парсится AST"]
        else:
            matched, mismatches = signatures_match(old_snapshot, new_signatures)

        if not matched:
            logger.warning(
                "  syntax_reconstruction REJECT: AST-диф не совпал для %s: %s",
                file_name, mismatches,
            )
            return self._finish_syntax_reconstruction(
                context, error, "REJECT", "syntax_reconstruction_signature_mismatch",
                rollback_target=target,
            )

        # 2) Сигнатуры совпали — socraticode судит, сохранён ли СМЫСЛ
        # (то, что AST-диф не видит).
        old_content = context.metadata.get("_syntax_reconstruction_old_content", "")
        from tools.socratic_refiner import SocraticRefiner
        judge = SocraticRefiner(llm_client=self.llm_client)
        verdict_obj = judge.compare_logic(old_content, new_content, file_name)
        verdict = verdict_obj.get("verdict", "uncertain")
        logger.info(
            "  syntax_reconstruction: socraticode verdict=%s reasons=%s",
            verdict, verdict_obj.get("reasons"),
        )

        new_metadata = dict(context.metadata)
        new_metadata["review"] = {
            "verdict": verdict,
            "reasons": verdict_obj.get("reasons", []),
            "confidence_adjustment": 0.0,
        }
        context = context.update(metadata=new_metadata)

        if verdict == "wrong":
            return self._finish_syntax_reconstruction(
                context, error, "REJECT", "syntax_reconstruction_logic_changed",
                rollback_target=target,
            )
        if verdict == "uncertain":
            return self._finish_syntax_reconstruction(
                context, error, "NEEDS_REVIEW", "syntax_reconstruction_uncertain",
                rollback_target=target,
            )

        # ok → обычный пайплайн: DecideStage решает ACCEPT по своим обычным
        # правилам (уменьшился ли счёт ошибок и т.п.).
        logger.info("  syntax_reconstruction: socraticode verdict=ok — продолжаем обычным путём")
        return context.add_state_to_history(State.DECIDING)

    def _finish_syntax_reconstruction(
        self, context: PipelineContext, error: Dict, outcome: str, reason: str,
        rollback_target,
    ) -> PipelineContext:
        """REJECT/NEEDS_REVIEW исход syntax_reconstruction — откатывает файл
        к pre-patch снапшоту (тот же снапшот, что ValidateStage сохраняет для
        ЛЮБОГО патча) и завершает обработку этой ошибки."""
        from core.utils import process_key as _process_key

        file_name = error.get("file", "") if error else ""
        snapshots = context.metadata.get("patch_snapshots", [])
        relevant = [s for s in snapshots if s.get("file") == file_name]
        if relevant and rollback_target is not None:
            original = relevant[-1].get("original_content")
            if original:
                try:
                    rollback_target.write_text(original, encoding="utf-8", newline="")
                except Exception as e:
                    logger.warning("  syntax_reconstruction rollback: cannot write %s: %s", rollback_target, e)

        new_metadata = dict(context.metadata)
        new_metadata.pop("_syntax_reconstruction_snapshot", None)
        new_metadata.pop("_syntax_reconstruction_old_content", None)
        # C6 (аудит 2026-07-01): откат выполнен здесь — карта потреблена.
        new_metadata.pop("_pre_patch_content", None)

        if outcome == "REJECT":
            context = context.update(metadata=new_metadata)
            context = context.add_rejected_patch({"error": error, "reason": reason})
            context = context.record_processed_error(_process_key(error))
            return context.add_state_to_history(State.NEXT_ERROR)

        # NEEDS_REVIEW
        new_metadata["_needs_review_pending_reason"] = reason
        context = context.update(metadata=new_metadata)
        return context.add_state_to_history(State.NEEDS_REVIEW)

    # ---- helpers ---------------------------------------------------

    def _extract_file_context(
        self, context: PipelineContext, error: Dict[str, Any],
    ) -> str:
        """Берёт окно `±_FILE_CONTEXT_RADIUS_LINES` строк вокруг error.line
        из актуальной версии файла. Если файла нет — возвращает пустую
        строку (reviewer перетопит)."""
        file_rel = (error.get("file") or "").strip()
        line = int(error.get("line") or 0)
        if not file_rel or line <= 0:
            return ""
        work_dir = (
            getattr(context, "working_path", None)
            or getattr(context, "project_path", None)
        )
        if work_dir is None:
            return ""
        try:
            full = (work_dir / file_rel).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        lines = full.splitlines()
        if not lines:
            return ""
        start = max(1, line - self._FILE_CONTEXT_RADIUS_LINES)
        end = min(len(lines), line + self._FILE_CONTEXT_RADIUS_LINES)
        width = len(str(end))
        snippet_lines: List[str] = []
        for n in range(start, end + 1):
            marker = ">>" if n == line else "  "
            snippet_lines.append(f"{marker} {n:>{width}} | {lines[n - 1]}")
        return "\n".join(snippet_lines)
