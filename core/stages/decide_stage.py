"""
Стадия принятия решения (ACCEPT / NEEDS_REVIEW / REJECT).
"""

import logging
import re
from types import MappingProxyType
from typing import Dict, Tuple
from analysis.ai_authored_detector import score as ai_authored_score
from core.contract import MetadataKeys, default_confidence_for
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from core.utils import process_key as _process_key

logger = logging.getLogger(__name__)


class DecideStage(PipelineStage):
    MAX_TESP_PER_FILE = 5  # max target_error_still_present rejections per file before bulk-skip

    # Q3 (2026-07-03, спека Алекса, вердикт deep-reasoner по замерам №6-8 bcrypt):
    # сигнатурный containment. Класс mypy-ошибок (arg-type/list-item на
    # parametrize-блоках) неверифицируем в тулчейне — детерминированный фикс не
    # подтверждается recheck-ом, а LLM-попытки плющат блок → бесконечная серия
    # честных REJECT (target_error_still_present / error_count_not_decreased) до
    # project_timeout. Q1-containment (data_toxic_files) сюда не дотягивается,
    # т.к. там нет O.19 data mangling — просто неверифицируемый фикс. Вариант (i):
    # помечаем ТОЛЬКО сигнатуру (file::line::code), не весь файл — остальные
    # ошибки того же файла остаются в очереди (ключевое отличие от Q1).
    #
    # 2026-07-07 (bcrypt 20260703-121449z + решающий эксперимент single-file
    # mypy на пристинном test_bcrypt.py): по данным замеров №6-9 + этого
    # прогона P(верифицированный фикс | mypy-класс уже отвергнут 3 раза) ≈ 0 —
    # порог снижен 4 → 3, чтобы не жечь ещё один честный REJECT/NR впустую.
    MAX_TOXIC_SIG_REJECTS = 3  # отказов (REJECT/NR) одной сигнатуры до пометки toxic

    # Те же правила, что и validate_stage.ValidateStage._MYPY_CODE_RE: mypy-коды —
    # lowercase-слова через дефис (arg-type, list-item, ...); syntax/invalid-syntax
    # исключены — это E999-домен, обрабатывается отдельным (Q1-подобным) путём.
    _MYPY_SIG_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
    _MYPY_SIG_CODE_EXCLUDE = frozenset({"syntax", "invalid-syntax"})

    # Q3 (2026-07-07): причины отказа, которые считаются в toxic-счётчик.
    # До этой даты считались только REJECT-причины (target_error_still_present,
    # error_count_not_decreased) — NR-исходы того же неверифицируемого класса
    # (net_delta_uncertain, target_fixed_reviewer_ok_count_unchanged) в счётчик
    # не шли, и LLM продолжала жечься на классе до конца прогона (bcrypt
    # 20260703-121449z: 11 LLM-решений на list-item/arg-type, real_fix_impact=0).
    _TOXIC_SIG_REASONS = frozenset({
        "target_error_still_present",
        "error_count_not_decreased",
        "net_delta_uncertain",
        "target_fixed_reviewer_ok_count_unchanged",
    })

    # Perf-1 (2026-07-07, дизайн deep-reasoner, конкретизирован техлидом):
    # perf-профиль показал LLM ≈ 90% wall-time, а обречённые решения жгут
    # бюджет до потолка FileAntiLoop (httpx: стрик 5× REJECT structured_llm
    # по test_auth.py ~226с; _models.py дошёл до 11/10 попыток). Q3-toxic
    # считает ТОЛЬКО mypy-коды; TESP-cap — только target_error_still_present;
    # _anchor_fail_counts — только пустые LLM-ответы. Ни один существующий
    # гейт не считает «непустой LLM-патч применился, но счётчик ошибок не
    # упал» на ВСЕХ кодах. Новый счётчик — отдельный от Q3 (дублирование
    # учёта не проблема: гейты независимы, режут в разных местах).
    #
    # Источники по data (learning_cases, июль 2026): REJECT с
    # error_count_not_decreased по LLM-backed источникам —
    # structured_llm 15, structured_llm_blocking 11, llm_blocking_single 3,
    # llm 1 = 30 записей. Список сознательно ограничен НАБЛЮДАВШИМИСЯ в
    # этих данных источниками (не расширять на llm_critical/llm_segmented/
    # structured_llm_critical/llm_blocking_multi/llm_blocking_segment без
    # отдельного подтверждения по данным — §4 CLAUDE.md).
    MAX_LLM_NOEFFECT_PER_CLASS = 3  # безрезультатных LLM-REJECT одного (file, code) до бана LLM-генерации

    _LLM_PATCH_SOURCES = frozenset({
        "llm", "structured_llm", "structured_llm_blocking", "llm_blocking_single",
    })
    _LLM_NOEFFECT_REASONS = frozenset({
        "target_error_still_present", "error_count_not_decreased",
    })

    def __init__(self, quality_evaluator, analyzer=None, recorder=None):
        self.quality_evaluator = quality_evaluator
        self.analyzer = analyzer
        self.recorder = recorder
        logger.debug("DecideStage initialised")

    def _emit_patch_record(
        self,
        context: PipelineContext,
        error: Dict,
        verdict: str,
        reason: str,
        rollback: bool,
    ) -> None:
        if not self.recorder:
            return
        vr = context.validation_results or {}
        meta = context.metadata or {}
        before = vr.get("error_count_before")
        after = vr.get("error_count_after")
        new_errs = 0
        if before is not None and after is not None:
            raw_delta = (after - before)
            new_errs = max(0, raw_delta)
        regression = bool(meta.get("net_delta_regression") or (new_errs > 0 and verdict == "REJECT"))
        # Извлекаем фрагмент исходного кода и патча для learning_cases
        _orig_ctx = ""
        _patch_str = ""
        try:
            _snaps = meta.get("patch_snapshots") or []
            _err_file = error.get("file", "")
            for _s in _snaps:
                if _s.get("file") == _err_file:
                    _orig = _s.get("original_content", "")
                    _err_line = int(error.get("line") or 0)
                    if _orig and _err_line:
                        _ls = _orig.splitlines()
                        _lo = max(0, _err_line - 6)
                        _hi = min(len(_ls), _err_line + 5)
                        _orig_ctx = "\n".join(_ls[_lo:_hi])
                    break
            _patch_str = str(context.generated_patch or "")
        except Exception as _e_orig_ctx:
            logger.debug("  _emit_patch_record: не удалось вычислить _orig_ctx: %s", _e_orig_ctx)
        # v1.3: вычисляем список кодов новых ошибок и номер попытки
        _new_error_codes: list = []
        try:
            _before_sigs = set()
            for _be in (vr.get("current_errors_before") or []):
                _before_sigs.add(PipelineStage._static_signature(_be))
            _new_error_codes = [
                _ce.get("code") or "?"
                for _ce in context.current_errors
                if PipelineStage._static_signature(_ce) not in _before_sigs
            ][:20]
        except Exception as _e_new_codes:
            logger.debug("  _emit_patch_record: не удалось вычислить _new_error_codes: %s", _e_new_codes)
        _attempt_number = 0
        try:
            _target_sig = PipelineStage._static_signature(error)
            _attempt_number = int(context.processed_errors.get(_process_key(error), 0))
        except Exception as _e_attempt:
            logger.debug("  _emit_patch_record: не удалось вычислить _attempt_number: %s", _e_attempt)
        _patch_applied = bool(_patch_str)
        # v1.4: строим stages-блок для полного аудита цепочки
        _patch_source = str(meta.get(MetadataKeys.PATCH_SOURCE) or "")
        _patch_intent = str(meta.get("intent") or "")
        _confidence = float(meta.get(MetadataKeys.CONFIDENCE) or 0.5)
        _stages: Dict = {
            "error": {
                "code": error.get("code") or "",
                "class": error.get("error_class") or "",
                "type": error.get("error_type") or "",
                "severity": error.get("severity") or "",
                "allowed_actions": error.get("allowed_actions") or [],
                "file": error.get("file") or "",
                "line": int(error.get("line") or 0),
            },
            "analysis": {
                "error_class": error.get("error_class") or "",
                "error_type": error.get("error_type") or "",
                "severity": error.get("severity") or "",
                "allowed_actions": error.get("allowed_actions") or [],
                "rule_based_eligible": _patch_source in ("rule_based", "ruff_autofix"),
            },
            "generate": {
                "method": _patch_source,
                "intent": _patch_intent[:200],
                "confidence": round(_confidence, 4),
                "structured_edit": bool(meta.get("structured_edit")),
                "patch_source": _patch_source,
            },
            "apply": {
                "success": _patch_applied,
                "patch_applied": _patch_applied,
                "apply_failure": str(meta.get("LAST_PATCH_FAILURE") or "")[:200] if not _patch_applied else None,
                "attempt_number": _attempt_number,
            },
            "validate": {
                "before": before,
                "after": after,
                "target_removed": (verdict != "REJECT"),
                "new_errors": new_errs,
                "new_error_codes": _new_error_codes[:10],
                "regression": regression,
                "net_delta": (after - before) if (before is not None and after is not None) else None,
            },
            "decide": {
                "verdict": verdict,
                "reason": reason,
                "rollback": rollback,
                "confidence": round(_confidence, 4),
            },
        }
        try:
            self.recorder.record(
                project_id=str(meta.get("_project_id") or ""),
                error=error,
                patch_source=_patch_source,
                patch_intent=_patch_intent,
                patch_confidence=_confidence,
                error_count_before=before,
                error_count_after=after,
                target_fixed=(verdict != "REJECT"),
                new_errors_introduced=new_errs,
                regression=regression,
                verdict=verdict,
                reason=reason,
                rollback=rollback,
                iteration=int(getattr(context, "iteration_count", 0)),
                global_cycle=int(meta.get("_current_global_cycle") or 0),
                total_errors_at_decision=len(context.current_errors),
                original_context=_orig_ctx,
                patch_candidate=_patch_str,
                attempt_number=_attempt_number,
                new_error_codes=_new_error_codes,
                patch_applied=_patch_applied,
                stages=_stages,
            )
        except Exception as _e:
            logger.warning("_emit_patch_record failed: %s", _e, exc_info=True)

    def _record_memory_on_accept(self, context: PipelineContext, target_sig: str) -> None:
        memory = getattr(context, "memory", None)
        if memory is None or not hasattr(memory, "record_success"):
            return
        patch = context.generated_patch
        if not isinstance(patch, str) or not patch:
            return
        meta = context.metadata or {}
        structured = meta.get("structured_edit")
        intent = meta.get("intent", "")

        # P0: drop structured_edit if any of its edits targets a different file.
        # context.metadata persists across iterations — structured_edit from a
        # previous error's fix contaminates the current error's memory entry.
        if isinstance(structured, dict) and structured.get("edits"):
            err_file = (context.selected_error or {}).get("file") or ""
            err_base = err_file.replace("\\", "/").rsplit("/", 1)[-1]
            for ed in structured["edits"]:
                ed_base = str(ed.get("file") or "").replace("\\", "/").rsplit("/", 1)[-1]
                if ed_base and ed_base != err_base:
                    logger.debug(
                        "  memory: dropping structured_edit — file mismatch "
                        "(edit=%r, error=%r)",
                        ed_base, err_base,
                    )
                    structured = None
                    break

        confidence = meta.get("confidence")
        patch_source = meta.get("patch_source", "")
        language = getattr(context, "language", "") or ""
        file_ext = ""
        try:
            err = context.selected_error or {}
            from pathlib import Path as _P
            file_ext = _P(err.get("file", "")).suffix.lower()
        except Exception:
            file_ext = ""
        try:
            memory.record_success(
                error_sig=target_sig, patch=patch,
                score=float(confidence) if confidence is not None else 1.0,
                structured_edit=structured,
                intent=intent if isinstance(intent, str) else "",
                confidence=confidence if isinstance(confidence, (int, float)) else None,
                patch_source=patch_source if isinstance(patch_source, str) else "",
                language=language or None,
                file_ext=file_ext or None,
            )
        except Exception as e:
            logger.debug("memory.record_success failed: %s", e)

    def _build_attempt_data(self, context, result_type: str, reason_code: str,
                            before, after) -> dict:
        """Строит структурированные данные попытки для LAST_ATTEMPT_DATA."""
        try:
            vr = context.validation_results or {}
            _before_sigs = set()
            for _be in (vr.get("current_errors_before") or []):
                _before_sigs.add(PipelineStage._static_signature(_be))
            new_error_codes = [
                (_ce.get("code") or "?")
                for _ce in (context.current_errors or [])
                if PipelineStage._static_signature(_ce) not in _before_sigs
            ][:5]
        except Exception:
            new_error_codes = []
        nd = (after - before) if (before is not None and after is not None) else None
        return {
            "result": result_type,
            "reason": reason_code,
            "net_delta": nd,
            "new_errors": new_error_codes,
        }

    # Коды/маркеры, покрываемые O.20 security-suppression guard.
    _SECURITY_CODES = frozenset({
        "sql_injection", "command_injection", "xss", "dangerous_eval",
        "unsafe_deserialization", "hardcoded_secret",
    })
    _SUPPRESSION_MARKERS = ("# nosec", "#nosec", "# noqa", "#noqa",
                            "nosemgrep", "# type: ignore", "#[allow(")

    def _security_fix_added_suppression(self, context: PipelineContext, error: Dict) -> bool:
        """O.20: patch «починил» security-код добавлением suppression-комментария
        (косметика, не ремедиация). Считаем маркеры до/после — рост = косметика.

        Только для security-кодов (по коду, error_type или классу SECURITY);
        semgrep-коды с '.security.' в id тоже ловим. Fail-safe: любое
        исключение → False (не блокируем сомнением, это advisory-страж; но
        добавленный suppression детерминирован и ловится надёжно)."""
        try:
            code = str(error.get("code") or "")
            etype = str(error.get("error_type") or "")
            eclass = str(error.get("error_class") or "")
            is_sec = (
                code in self._SECURITY_CODES
                or etype == "security"
                or eclass == "SECURITY"
                or ".security." in code
                or "injection" in code.lower()
            )
            if not is_sec:
                return False
            file_name = str(error.get("file") or "")
            if not file_name:
                return False
            pre_map = (context.metadata or {}).get("_pre_patch_content") or {}
            before = pre_map.get(file_name)
            if before is None:
                before = pre_map.get(file_name.replace("\\", "/"))
            if before is None:
                return False  # нет базы сравнения — не блокируем
            work_path = getattr(context, "working_path", None) or context.project_path
            try:
                after = (work_path / file_name).read_text(encoding="utf-8", errors="replace")
            except Exception:
                return False

            def _count(text: str) -> int:
                low = text.lower()
                return sum(low.count(m.lower()) for m in self._SUPPRESSION_MARKERS)

            return _count(after) > _count(before)
        except Exception:
            return False

    def _rollback_file_from_snapshot(self, context: PipelineContext, error: Dict) -> PipelineContext:
        """Откат непринятого патча к pre-patch состоянию.

        C4/C5/C6 (аудит 2026-07-01): канонический источник pre-patch
        состояния — `metadata["_pre_patch_content"]` (его пишет
        ApplyPatchStage для ВСЕХ файлов патча, включая многофайловые).
        Раньше откатывался только `error["file"]` из patch_snapshots —
        побочные файлы многофайлового патча оставались с непринятыми
        правками (C5), а при отсутствии снапшота откат ТИХО пропускался
        целиком: REJECT записывался с rollback=True, файл оставался
        патченным на диске (C4). Снапшот остаётся вторичным источником;
        если нет ни того, ни другого — файл помечается в
        `_rollback_failed_files` (финальный аудит увидит расхождение
        символов, а не «пройдёт мимо»). Карта _pre_patch_content после
        отката очищается (C6) — оставленная, она могла быть записана
        повторно при разрешении СЛЕДУЮЩЕЙ ошибки, стирая принятую работу.
        """
        file_name = (error or {}).get("file", "")
        work_path = getattr(context, "working_path", None) or context.project_path

        pre_map = dict((context.metadata or {}).get("_pre_patch_content") or {})
        restored: list = []
        failed: list = []

        if pre_map:
            for rel, orig in pre_map.items():
                target = work_path / rel
                try:
                    target.write_text(orig, encoding="utf-8", newline="")
                    restored.append(rel)
                except Exception as e:
                    logger.warning("  rollback: cannot write %s: %s", target, e)
                    failed.append(rel)
            if restored:
                logger.info(
                    "  rollback: восстановлено из _pre_patch_content: %s", restored
                )
        elif file_name:
            snapshots = context.metadata.get("patch_snapshots", [])
            relevant = [s for s in snapshots if s.get("file") == file_name]
            original = relevant[-1].get("original_content") if relevant else None
            if original is None:
                # C4: раньше — молчаливый return (патч оставался на диске).
                logger.error(
                    "  rollback: нет ни _pre_patch_content, ни снапшота для %s — "
                    "откат НЕВОЗМОЖЕН, файл помечен в _rollback_failed_files",
                    file_name,
                )
                failed.append(file_name)
            else:
                target = work_path / file_name
                try:
                    target.write_text(original, encoding="utf-8", newline="")
                    restored.append(file_name)
                    logger.info("  rollback: file %s restored from snapshot", file_name)
                except Exception as e:
                    logger.warning("  rollback: cannot write %s: %s", target, e)
                    failed.append(file_name)

        # Бухгалтерия: снапшоты откаченных файлов больше не нужны; карта
        # pre-patch потреблена (C6); провалы отката фиксируются для финального
        # аудита и отчёта.
        for rel in restored:
            context = context.remove_patch_snapshot(rel)
        _m = dict(context.metadata)
        _m.pop("_pre_patch_content", None)
        # Конверсия-2 (2026-07-02): baseline-счётчики валидации ставятся в
        # ValidateStage ПОСЛЕ применения патча (len(new_errors_primary) — с
        # уже применённым патчем). Откат возвращает диск к состоянию с
        # бОльшим числом ошибок, но счётчики оставались от патченного
        # состояния — на retry той же ошибки before оказывался занижен ровно
        # на число исправленного, и honest-фикс получал
        # «error_count_not_decreased» (51→51 в learning_cases на attempt>=2,
        # E704/tenacity/pluggy). Сбрасываем — следующий validate возьмёт
        # before из len(current_errors) свежего пост-rollback скана.
        _m.pop("_primary_error_count_before", None)
        _m.pop("_ruff_count_before", None)
        if failed:
            _prev_failed = list(_m.get("_rollback_failed_files") or [])
            _m["_rollback_failed_files"] = sorted(set(_prev_failed) | set(failed))
        context = context.update(metadata=_m)

        if not restored:
            return context

        if self.analyzer is not None:
            try:
                # Инкрементальный режим: после rollback файлы восстановлены к
                # ОРИГИНАЛЬНОМУ содержимому — остальные файлы проекта не
                # тронуты, достаточно пересканировать только их и заменить в
                # current_errors только их записи, вместо полного повторного
                # анализа всего проекта на каждый REJECT-rollback
                # (одна из самых частых точек полного скана).
                _pcfg = (context.config or {}).get("pipeline", {})
                _lang = (context.language or "").lower()
                _tu_suf = (".cpp", ".cxx", ".cc", ".c")
                # Python — per-file под флагом (default False). C/C++ — инкрементал
                # ВКЛЮЧЁН ПО УМОЛЧАНИЮ (перф-хвост полного скана), но ТОЛЬКО если
                # ВСЕ восстановленные файлы — единицы трансляции: откат заголовка
                # затрагивает включающие его TU → нужен полный скан (fail-closed).
                # Симметрично ValidateStage._incremental_enabled_for.
                if _lang in ("python", "py"):
                    _incremental = bool(_pcfg.get("incremental_analysis", False))
                elif _lang in ("cpp", "c++", "cxx", "cc", "c"):
                    _incremental = bool(_pcfg.get("incremental_analysis", True)) and bool(restored) and all(
                        str(r).lower().endswith(_tu_suf) for r in restored
                    )
                else:
                    _incremental = False
                if _incremental:
                    for rel in restored:
                        file_errors = self.analyzer.analyze(work_path, files=[rel])
                        context = context.merge_file_errors(rel, file_errors)
                else:
                    new_errors = self.analyzer.analyze(work_path)
                    context = context.set_errors(new_errors)
            except Exception as e:
                logger.warning("  REJECT rollback re-analyze failed: %s", e)
        return context

    _BASE_ACCEPT_THRESHOLD = 0.85
    _AI_ACCEPT_THRESHOLD_BUMP = 0.10
    _AI_FINGERPRINT_TRIGGER = 0.5

    # IMP-5: per-code adaptive threshold.
    # Style/whitespace codes that carry no semantic risk: lowered to 0.70 so
    # structured_llm patches (confidence=0.70) can reach ACCEPT when validation
    # already confirmed the target error disappeared AND no new errors appeared.
    _SAFE_LLM_THRESHOLD = 0.70
    _SAFE_LLM_PATCH_SOURCES = frozenset({"structured_llm", "llm_critical", "llm"})
    _SAFE_LLM_CODES = frozenset({
        # indentation / whitespace
        "E101", "E111", "E114", "E115", "E116", "E117",
        # brackets / operators / spacing
        "E201", "E202", "E203", "E211",
        "E221", "E225", "E226", "E228",
        "E231", "E241", "E251",
        # comments
        "E261", "E262", "E265", "E266",
        # keywords
        "E271", "E272",
        # blank lines
        "E301", "E302", "E303", "E304", "E305", "E306",
        # imports / statements
        "E401", "E701", "E702", "E703",
        # continuation lines
        "E121", "E122", "E123", "E124", "E125", "E126", "E127", "E128", "E131",
        # trailing whitespace / newlines
        "W191", "W291", "W292", "W293", "W391",
        # unused import (delete-only — no logic change)
        "F401",
    })

    def _effective_accept_threshold(self, context: PipelineContext) -> float:
        error = context.selected_error or {}
        code = (error.get("code") or "").strip()
        patch_source = (context.metadata.get(MetadataKeys.PATCH_SOURCE) or "").strip()

        # IMP-5: safe formatting codes from LLM accept at lower confidence.
        # Validation guards (target gone, no new errors) already ran before
        # _dispatch_after_success is called, so this is safe to lower.
        if code in self._SAFE_LLM_CODES and patch_source in self._SAFE_LLM_PATCH_SOURCES:
            return self._SAFE_LLM_THRESHOLD

        file_rel = (error.get("file") or "").strip()
        if not file_rel:
            return self._BASE_ACCEPT_THRESHOLD
        work_dir = (getattr(context, "working_path", None)
                    or getattr(context, "project_path", None))
        if work_dir is None:
            return self._BASE_ACCEPT_THRESHOLD
        try:
            text = (work_dir / file_rel).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return self._BASE_ACCEPT_THRESHOLD
        try:
            ai_score = float(ai_authored_score(text) or 0.0)
        except Exception:
            return self._BASE_ACCEPT_THRESHOLD
        if ai_score > self._AI_FINGERPRINT_TRIGGER:
            return self._BASE_ACCEPT_THRESHOLD + self._AI_ACCEPT_THRESHOLD_BUMP
        return self._BASE_ACCEPT_THRESHOLD

    def _classify_decision(self, context: PipelineContext) -> str:
        confidence = float(context.metadata.get(MetadataKeys.CONFIDENCE, 0.5) or 0.5)
        review = context.metadata.get("review") or {}
        verdict = str(review.get("verdict") or "ok").strip().lower()
        if verdict == "wrong":
            return "REJECT"
        if confidence < 0.5:
            return "REJECT"
        accept_threshold = self._effective_accept_threshold(context)
        if confidence >= accept_threshold and verdict != "noisy":
            return "ACCEPT"
        return "NEEDS_REVIEW"

    def _dispatch_after_success(self, context, error, target_sig, reason):
        confidence = float(context.metadata.get(MetadataKeys.CONFIDENCE, 0.5) or 0.5)
        review = context.metadata.get("review") or {}
        verdict = str(review.get("verdict") or "ok")
        decision = "NEEDS_REVIEW" if verdict.strip().lower() == "wrong" else "ACCEPT"

        # O.14: anti symbol-regression.
        # 2026-06-23: missing_imports добавлен в check_symbol_regression (см.
        # analysis/symbol_regression.py — control series, malinkang/toggl2notion:
        # F401 в RuffAutoFixStage терял имена из import-list) — но этот gate
        # проверял только missing_defs/missing_classes, так что потерянные
        # импорты попадали в metadata, но НЕ уводили патч в NEEDS_REVIEW.
        # 2026-06-24: missing_overloads добавлен в check_symbol_regression
        # (control series на 10 проектах, agronholm/anyio — "retry с
        # full-file" дописал @overload-декораторы БЕЗ их сигнатур, потеряв
        # 4 из 5 перегрузок connect_tcp; имя как def НЕ пропадает — set-based
        # missing_defs этого не видит вовсе) — тот же gap, что был с
        # missing_imports до 2026-06-23: метрика в metadata, но без этой
        # строки в условии патч ушёл бы в ACCEPT.
        sym_reg = context.metadata.get("symbol_regression")
        if isinstance(sym_reg, dict) and (
            sym_reg.get("missing_defs") or sym_reg.get("missing_classes")
            or sym_reg.get("missing_imports") or sym_reg.get("missing_overloads")
        ):
            logger.warning(
                "  O.14: NEEDS_REVIEW due to symbol regression in %s "
                "(missing_defs=%s, missing_classes=%s, missing_imports=%s, missing_overloads=%s)",
                sym_reg.get("file"), sym_reg.get("missing_defs"),
                sym_reg.get("missing_classes"), sym_reg.get("missing_imports"),
                sym_reg.get("missing_overloads"),
            )
            decision = "NEEDS_REVIEW"

        # C4/H3 (аудит 2026-07-01): снапшот патча не создан (нет правдивого
        # pre-patch содержимого или сбой в _save_patch_snapshot) — значит
        # O.14/O.17/O.18-проверки НЕ выполнялись и откатить патч в случае чего
        # нечем. Такой патч не может быть принят автоматически (fail-closed).
        snap_failed = context.metadata.get("snapshot_failed")
        if isinstance(snap_failed, dict) and snap_failed:
            logger.warning(
                "  C4/H3: NEEDS_REVIEW — снапшот патча не создан (%s: %s), "
                "символьные проверки не выполнялись",
                snap_failed.get("file"), snap_failed.get("reason"),
            )
            decision = "NEEDS_REVIEW"

        # O.18: anti type-erosion. Control series 2026-06-23
        # (Skyscanner/pycfmodel): LLM «решала» union-attr/operator/assignment/
        # return-value добавлением `Any`/`cast(Any, ...)` вместо содержательной
        # правки — ошибка формально уходила (mypy замолкал), но типобезопасность
        # снижалась, а не повышалась. Не относится к `_UNFIXABLE_MYPY_CODES`
        # (import-not-found/import-untyped/untyped-decorator) — там `# type:
        # ignore` детерминированный и единственно доступный фикс (см.
        # GeneratePatchStage._make_type_ignore_patch), это НЕ эрозия.
        type_erosion = context.metadata.get("type_erosion")
        if isinstance(type_erosion, dict) and type_erosion.get("markers"):
            logger.warning(
                "  O.18: NEEDS_REVIEW due to type erosion in %s (markers=%s) for code=%s",
                type_erosion.get("file"), type_erosion.get("markers"), error.get("code"),
            )
            decision = "NEEDS_REVIEW"

        # O.19: anti data-mangling. Живой прогон pyca/bcrypt (2026-07-02):
        # LLM «чинила» mypy list-item/arg-type/assignment подменой тестовых
        # векторов (b"salt"→"salt", 4→"4", hex-bytes→plaintext) — mypy
        # замолкал честно, новой flake8-ошибки не появлялось, net_delta_check
        # не запускался (счётчик не рос), а target_recheck подтверждал
        # «исправлено». Ни один прежний гейт этого не видел. Флаг ставит
        # ValidateStage при уменьшении числа bytes-/числовых литералов файла.
        data_mangling = context.metadata.get("data_literal_mangling")
        if isinstance(data_mangling, dict) and data_mangling.get("markers"):
            logger.warning(
                "  O.19: NEEDS_REVIEW due to data-literal mangling in %s (markers=%s) for code=%s",
                data_mangling.get("file"), data_mangling.get("markers"), error.get("code"),
            )
            decision = "NEEDS_REVIEW"

        # O.17: anti symbol-duplication. Если после патча какое-то имя
        # `def`/`class` стало встречаться чаще, чем до — это сильный признак,
        # что патч приклеил полную копию файла рядом со старой (баг pure-insert,
        # покрытый O.16 на уровне PatchEngine; O.17 — страховка на уровне
        # содержимого, на случай если LLM выдал «полный новый файл» через
        # другой источник, который мы не отсекли).
        sym_dup = context.metadata.get("symbol_duplication")
        if isinstance(sym_dup, dict) and (
            sym_dup.get("duplicated_defs") or sym_dup.get("duplicated_classes")
        ):
            logger.warning(
                "  O.17: NEEDS_REVIEW due to symbol duplication in %s (defs=%s, classes=%s)",
                sym_dup.get("file"), sym_dup.get("duplicated_defs"),
                sym_dup.get("duplicated_classes"),
            )
            decision = "NEEDS_REVIEW"

        # F821 literal guard: неизвестная переменная, заменённая голым литералом —
        # слишком рискованный патч для авто-принятия.
        if decision == "ACCEPT" and self._is_literal_variable_substitution(context, error):
            logger.warning(
                "  F821 literal guard: патч заменяет неизвестную переменную литералом "
                "(%s:%s) — понижаем до NEEDS_REVIEW",
                error.get("file", ""), error.get("line", ""),
            )
            decision = "NEEDS_REVIEW"

        # O.20: security-suppression guard (2026-07-09, идея Алекса + wxVk).
        # Security-фиксы генерятся LLM по курируемому примеру (SECURITY FIX
        # EXAMPLE в промпте). Опасность: LLM может «починить» уязвимость
        # КОСМЕТИЧЕСКИ — добавить suppression (# nosec / # noqa / nosemgrep /
        # #[allow(...)]), из-за чего security-сканер замолкает, а дыра
        # остаётся. Для security «флаг исчез» != «уязвимость закрыта» — это
        # ложная безопасность, ХУЖЕ честного флага. Если патч ДОБАВИЛ
        # suppression, которого не было — понижаем в NEEDS_REVIEW (§4:
        # ложный NR лучше ложного «безопасно»).
        if decision == "ACCEPT" and self._security_fix_added_suppression(context, error):
            logger.warning(
                "  O.20: NEEDS_REVIEW — security-фикс %s добавил suppression-комментарий "
                "(косметика, не настоящая ремедиация) в %s",
                error.get("code", ""), error.get("file", ""),
            )
            decision = "NEEDS_REVIEW"

        # Q.1/Q.3: Logic Guard — HIGH violations indicate broken contract.
        _lg = context.metadata.get("logic_guard") or {}
        _lg_high = [v for v in (_lg.get("violations") or []) if v.get("severity") == "high"]

        # Q.5: опциональный InvariantGuard-ревью поверх HIGH Logic Guard-нарушений.
        # Если LLM-гард принял патч — считаем HIGH-нарушения снятыми (LLM-знание
        # о контексте важнее детерминированных AST-эвристик).
        if _lg_high and self._lg_llm_review_enabled(context):
            try:
                invariant_guard = context.metadata.get("invariant_guard")
                if invariant_guard:
                    snapshots = context.metadata.get("patch_snapshots", [])
                    file_name = error.get("file", "")
                    relevant_snap = None
                    for snap in snapshots:
                        if snap.get("file") == file_name:
                            relevant_snap = snap
                            break
                    if relevant_snap:
                        original = relevant_snap.get("original_content", "")
                        patched = relevant_snap.get("patched_content", "")
                        if original and patched:
                            result = invariant_guard.verify_patch(original, patched, error)
                            if result.get("accepted", True):
                                logger.info(
                                    "  Q.5: InvariantGuard принял патч — "
                                    "Logic Guard HIGH-нарушения сняты (%d шт.)",
                                    len(_lg_high),
                                )
                                _lg_high = []
                            else:
                                logger.info(
                                    "  Q.5: InvariantGuard отклонил патч: %s",
                                    result.get("reason", "unknown"),
                                )
            except Exception as e:
                logger.debug("  Q.5: InvariantGuard ревью упал: %s", e)

        if _lg_high:
            logger.warning(
                "  Q.1/Q.3: NEEDS_REVIEW due to logic_guard (%s)",
                ", ".join(v["type"] for v in _lg_high),
            )
            decision = "NEEDS_REVIEW"

        new_metadata = dict(context.metadata)
        new_metadata["last_decision"] = decision

        # Структурный рефакторинг (2026-06-21): логирование decisions[]
        # больше НЕ вызывается здесь вручную. ACCEPT/REJECT логируются
        # автоматически внутри context.add_accepted_patch/add_rejected_patch
        # (единственная точка добавления в эти списки == единственная точка
        # логирования). NEEDS_REVIEW логируется внутри NeedsReviewStage.
        # execute() — для этого здесь устанавливается
        # metadata["_needs_review_pending_reason"] перед переходом в
        # State.NEEDS_REVIEW. Раньше один общий вызов ДО ветвления давал
        # "призрачные" записи на retry-попытках (см. историю находок #1).

        if decision == "ACCEPT":
            logger.info("  Decision: ACCEPT (conf=%.2f, verdict=%s), reason: %s",
                        confidence, verdict, reason)
            self._emit_patch_record(context, error, "ACCEPT", reason, rollback=False)
            context = context.update(metadata=new_metadata)
            context = context.add_accepted_patch({"error": error, "reason": reason})
            context = context.record_processed_error(_process_key(error))
            self._record_memory_on_accept(context, target_sig)
            # P1: after ACCEPT the engine loop breaks before NextErrorStage runs,
            # so per-patch fields persist into the next error's context.
            # Clear them here, after _record_memory_on_accept already read them.
            _clean = dict(context.metadata)
            _clean.pop("structured_edit", None)
            _clean.pop("intent", None)
            # C6 (аудит 2026-07-01): pre-patch карта относится к ТОЛЬКО ЧТО
            # принятому патчу — оставленная в metadata, она могла быть
            # записана на диск net-delta-откатом СЛЕДУЮЩЕЙ ошибки (например,
            # HEALED-пути, не устанавливающего свою карту), стирая только
            # что принятую работу.
            _clean.pop("_pre_patch_content", None)
            # Perf-1 (2026-07-07): ACCEPT того же (file, code) — класс оказался
            # fixable, бан LLM-генерации снимается (счётчик и exhausted-флаг
            # относились к серии безрезультатных попыток, которая прервалась
            # успехом). Без сброса случайный ACCEPT после 2 REJECT навсегда
            # держал бы класс на грани бана, а после бана — забаненным
            # даже когда LLM явно способна его решить.
            _noeff_code = str((error or {}).get("code") or "")
            _noeff_file = str((error or {}).get("file") or "").replace("\\", "/")
            if _noeff_code and _noeff_file:
                _noeff_key = f"{_noeff_file}::{_noeff_code}"
                _noeff_counts = dict(_clean.get("_llm_noeffect_counts") or {})
                if _noeff_key in _noeff_counts:
                    _noeff_counts.pop(_noeff_key, None)
                    _clean["_llm_noeffect_counts"] = _noeff_counts
                _noeff_exhausted = list(_clean.get("_llm_budget_exhausted") or [])
                if _noeff_key in _noeff_exhausted:
                    _noeff_exhausted.remove(_noeff_key)
                    _clean["_llm_budget_exhausted"] = _noeff_exhausted
                    logger.info(
                        "  perf-1: ACCEPT снял LLM-бюджет-бан с класса %s", _noeff_key,
                    )
            # IMP-F: if we just fixed an E999, unblock cascade-blocked errors for that file
            _e999_codes = ("E999", "invalid-syntax", "E902")
            if (isinstance(error, dict)
                    and error.get("error_class") == "CRITICAL_SYNTAX"
                    and error.get("code", "") in _e999_codes):
                _fixed_file = error.get("file", "") or ""
                if _fixed_file:
                    _blocked_sigs_map: dict = dict(_clean.get("_e999_cascade_blocked_sigs") or {})
                    _unblock = _blocked_sigs_map.pop(_fixed_file, [])
                    if _unblock:
                        _new_proc = dict(context.processed_errors)
                        for _psig in _unblock:
                            if _new_proc.get(_psig, 0) >= 3:
                                _new_proc.pop(_psig, None)
                        context = context.update(processed_errors=MappingProxyType(_new_proc))
                        logger.info(
                            "  IMP-F: разблокировано %d ошибок файла %s (E999 ACCEPT)",
                            len(_unblock), _fixed_file,
                        )
                        _clean["_e999_cascade_blocked_sigs"] = _blocked_sigs_map
                        _e999_blocked_files = set(_clean.get("_e999_cascade_blocked_files") or [])
                        _e999_blocked_files.discard(_fixed_file)
                        _clean["_e999_cascade_blocked_files"] = list(_e999_blocked_files)
            context = context.update(metadata=_clean)
            context = context.increment_iteration()
            return context.add_state_to_history(State.NEXT_ERROR)

        if decision == "NEEDS_REVIEW":
            # NR feedback retry: первый раз → даём LLM конкретную причину вместо NEEDS_REVIEW
            _nr_key = f"_nr_retry_{target_sig}"
            _nr_retries = new_metadata.get(_nr_key) or 0

            # Q2 (2026-07-02, замер №3 bcrypt): если ЕДИНСТВЕННАЯ причина NR —
            # O.19 data-literal mangling, retry с фидбеком пропускаем — сразу
            # NEEDS_REVIEW. Данные deep-reasoner по логу замера: 100% O.19-
            # ретраев воспроизвели ту же порчу, 0 честных фиксов; фидбек
            # «чини аннотацию, не данные» логически неисполним на malformed-
            # блоке (настоящего фикса аннотацией там нет), FileAntiLoop
            # добивал файл после 3-4 одинаковых причин, retry-петли съели
            # project_timeout при 0 LLM-таймаутов. Направление безопасное:
            # остаёмся в NR, просто не жжём LLM-вызов. При O.19 ВМЕСТЕ с
            # другим триггером retry сохраняется (другая причина может быть
            # исправима).
            _dm = context.metadata.get("data_literal_mangling") or {}
            _sr = context.metadata.get("symbol_regression") or {}
            _sd = context.metadata.get("symbol_duplication") or {}
            _te = context.metadata.get("type_erosion") or {}
            _sf = context.metadata.get("snapshot_failed") or {}
            _o19_sole_cause = bool(_dm.get("markers")) and not (
                verdict.strip().lower() in ("wrong", "noisy")
                or _sr.get("missing_defs") or _sr.get("missing_classes")
                or _sr.get("missing_imports") or _sr.get("missing_overloads")
                or _te.get("markers")
                or _sd.get("duplicated_defs") or _sd.get("duplicated_classes")
                or _lg_high
                or _sf
            )
            if _o19_sole_cause and _nr_retries == 0:
                logger.info(
                    "  Q2: O.19 — единственная причина NR (%s) — retry пропущен, "
                    "сразу NEEDS_REVIEW (по данным замера №3: 100%% ретраев "
                    "повторяли порчу)", _dm.get("markers"),
                )
                _nr_retries = 1  # ветка retry ниже не войдёт

            if _nr_retries == 0:
                _nr_reason_parts = []
                if verdict.strip().lower() in ("wrong", "noisy"):
                    _review_reasons = (context.metadata.get("review") or {}).get("reasons") or []
                    _review_reasons = [str(r) for r in _review_reasons if r]
                    _nr_reason_parts.append(
                        f"reviewer verdict='{verdict}'"
                        + (f": {'; '.join(_review_reasons[:2])}" if _review_reasons else "")
                    )
                sym_reg = context.metadata.get("symbol_regression") or {}
                if (sym_reg.get("missing_defs") or sym_reg.get("missing_classes")
                        or sym_reg.get("missing_imports") or sym_reg.get("missing_overloads")):
                    _nr_reason_parts.append(
                        f"O.14 symbol regression: your patch removed definitions "
                        f"{sym_reg.get('missing_defs',[])}, classes {sym_reg.get('missing_classes',[])}, "
                        f"imports {sym_reg.get('missing_imports',[])}, or @overload signatures "
                        f"{sym_reg.get('missing_overloads',[])} — "
                        "do not delete or rename existing symbols/imports/overload signatures"
                    )
                type_erosion = context.metadata.get("type_erosion") or {}
                if type_erosion.get("markers"):
                    _nr_reason_parts.append(
                        f"O.18 type erosion: your patch used {type_erosion.get('markers',[])} "
                        "to silence the type checker instead of fixing the actual type mismatch — "
                        "narrow the type or fix the logic instead of widening to Any/object or adding "
                        "type: ignore"
                    )
                data_mangling = context.metadata.get("data_literal_mangling") or {}
                if data_mangling.get("markers"):
                    _nr_reason_parts.append(
                        f"O.19 data mangling: your patch changed the type/content of existing "
                        f"data literals ({data_mangling.get('markers',[])}) — e.g. turning a bytes "
                        "literal into str or a number into a quoted string. Do NOT rewrite the test "
                        "data to satisfy the type checker; fix the type annotation, cast, or the "
                        "function signature instead, and keep every b\"...\"/numeric literal intact"
                    )
                sym_dup = context.metadata.get("symbol_duplication") or {}
                if sym_dup.get("duplicated_defs") or sym_dup.get("duplicated_classes"):
                    _nr_reason_parts.append(
                        f"O.17 symbol duplication: your patch duplicated "
                        f"defs {sym_dup.get('duplicated_defs',[])} — do not paste full copies of code blocks"
                    )
                if _lg_high:
                    _nr_reason_parts.append(
                        f"Q.1/Q.3 logic guard violations: {[v['type'] for v in _lg_high[:3]]} — "
                        "make a more minimal change that preserves existing logic"
                    )
                if not _nr_reason_parts:
                    _nr_reason_parts.append(
                        f"confidence too low ({confidence:.2f} < threshold) — "
                        "generate a simpler, more targeted patch"
                    )
                new_metadata[_nr_key] = 1
                new_metadata[MetadataKeys.LAST_PATCH_FAILURE] = (
                    f"NEEDS_REVIEW (not auto-accepted): {'; '.join(_nr_reason_parts)}. "
                    "Fix only the exact expression causing the lint error — minimal surgical change."
                )
                _nr_before = (context.validation_results or {}).get("error_count_before")
                _nr_after = (context.validation_results or {}).get("error_count_after")
                new_metadata[MetadataKeys.LAST_ATTEMPT_DATA] = self._build_attempt_data(
                    context, "NEEDS_REVIEW",
                    "; ".join(_nr_reason_parts)[:160],
                    _nr_before, _nr_after,
                )
                context = context.update(metadata=new_metadata)
                context = self._rollback_file_from_snapshot(context, error)
                logger.info(
                    "  NR feedback retry: даём LLM второй шанс для %s (reason: %s)",
                    target_sig, _nr_reason_parts,
                )
                return context.add_state_to_history(State.GENERATING_PATCH)

            new_metadata["_needs_review_pending_reason"] = reason
            logger.info("  Decision: NEEDS_REVIEW (conf=%.2f, verdict=%s), reason: %s",
                        confidence, verdict, reason)
            self._emit_patch_record(context, error, "NEEDS_REVIEW", reason, rollback=True)
            context = context.update(metadata=new_metadata)
            # Q3 (2026-07-07): NR-исход того же неверифицируемого mypy-класса
            # тоже кормит toxic-счётчик (см. _track_toxic_signature) — но
            # ТОЛЬКО когда этот путь достигнут не из-за content-guard'ов
            # (O.14/O.17/O.18/O.19/Q.1/Q.3), которые уже отработали выше и
            # перевели decision в NEEDS_REVIEW независимо от `reason`. Guard-
            # NR — сигнал о порче патчем, а не о неверифицируемости класса.
            if reason == "target_fixed_reviewer_ok_count_unchanged" and not (
                _lg_high
                or (isinstance(sym_reg, dict) and (
                    sym_reg.get("missing_defs") or sym_reg.get("missing_classes")
                    or sym_reg.get("missing_imports") or sym_reg.get("missing_overloads")
                ))
                or (isinstance(type_erosion, dict) and type_erosion.get("markers"))
                or (isinstance(data_mangling, dict) and data_mangling.get("markers"))
                or (isinstance(sym_dup, dict) and (
                    sym_dup.get("duplicated_defs") or sym_dup.get("duplicated_classes")
                ))
            ):
                context = self._track_toxic_signature(context, error, reason)
            context = self._rollback_file_from_snapshot(context, error)
            return context.add_state_to_history(State.NEEDS_REVIEW)

        # decision == "REJECT" via wrong reviewer verdict.
        logger.info("  Decision: REJECT (verdict=wrong), reason: %s", reason)
        self._emit_patch_record(context, error, "REJECT", f"reviewer_verdict_wrong:{reason}", rollback=True)
        context = context.update(metadata=new_metadata)
        context = context.add_rejected_patch({
            "error": error, "reason": f"reviewer_verdict_wrong:{reason}",
        })
        context = context.record_processed_error(_process_key(error))
        context = self._rollback_file_from_snapshot(context, error)
        return context.add_state_to_history(State.NEXT_ERROR)

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Stage DECIDE: making decision...")
        error = context.selected_error
        if not error:
            return context.add_state_to_history(State.NEXT_ERROR)

        if MetadataKeys.CONFIDENCE not in context.metadata:
            patch_source = context.metadata.get(MetadataKeys.PATCH_SOURCE, "")
            default_conf = default_confidence_for(patch_source)
            new_metadata = dict(context.metadata)
            new_metadata[MetadataKeys.CONFIDENCE] = default_conf
            context = context.update(metadata=new_metadata)

        target_sig = self._error_signature(error)
        error_type = error.get("error_type", "unknown")
        error_class = error.get("error_class", "")

        before = context.validation_results.get("error_count_before")
        after = context.validation_results.get("error_count_after")

        target_still_present = any(
            self._error_signature(e) == target_sig
            for e in context.current_errors
        )

        # Конверсия-1 (2026-07-02): для mypy-кодов сигнатурный поиск по
        # current_errors слеп — after-скан ValidateStage flake8-only, mypy-
        # ошибок там нет НИКОГДА (target «исчезал» даже у патча-пустышки, а
        # счётчик не падал даже у идеального фикса). Точечный mypy-re-check
        # файла цели (validation_results["target_recheck"]) — истина в обе
        # стороны: реально исправлен → False, реально остался → True.
        _recheck = (context.validation_results or {}).get("target_recheck")
        if isinstance(_recheck, dict) and _recheck.get("performed"):
            target_still_present = bool(_recheck.get("present"))
            logger.info(
                "  target_recheck (%s): target_still_present=%s",
                _recheck.get("tool"), target_still_present,
            )

        # Count-based override: if the same code appeared multiple times in the
        # same file and at least one instance was removed, treat as fixed.
        # Prevents false REJECT when E501/W503/etc. are fixed one-by-one.
        if target_still_present:
            _t_file = (error or {}).get("file", "")
            _t_code = (error or {}).get("code", "")
            if _t_file and _t_code:
                _errors_before = context.validation_results.get("current_errors_before") or []
                _cnt_before = sum(
                    1 for e in _errors_before
                    if e.get("file") == _t_file and e.get("code") == _t_code
                )
                _cnt_after = sum(
                    1 for e in context.current_errors
                    if e.get("file") == _t_file and e.get("code") == _t_code
                )
                if _cnt_before > 1 and _cnt_after < _cnt_before:
                    logger.info(
                        "  count-based fix: %s %s %d→%d — treating as fixed",
                        _t_file, _t_code, _cnt_before, _cnt_after,
                    )
                    target_still_present = False

        if context.metadata.get("broken_file_mode") and not target_still_present:
            _bfm_crit_codes = frozenset({"E999", "E902", "invalid-syntax"})
            critical_after = sum(1 for e in context.current_errors
                                if e.get("error_class") == "CRITICAL_SYNTAX" or e.get("code") in _bfm_crit_codes)
            critical_before = sum(1 for e in context.validation_results.get("current_errors_before", [])
                                  if e.get("error_class") == "CRITICAL_SYNTAX" or e.get("code") in _bfm_crit_codes)
            if critical_after <= critical_before:
                if self._should_check_invariant(context, error):
                    inv_ok, context = self._check_invariant(context, error)
                    if not inv_ok:
                        context = context.add_rejected_patch({"error": error, "reason": "invariant_violation_in_broken_mode"})
                        context = context.record_processed_error(_process_key(error))
                        context = self._rollback_file_from_snapshot(context, error)
                        return context.add_state_to_history(State.NEXT_ERROR)
                return self._dispatch_after_success(context, error, target_sig,
                                                    reason="target_error_fixed_in_broken_mode")
            else:
                context = context.add_rejected_patch({"error": error, "reason": "new_critical_syntax_in_broken_mode"})
                context = context.record_processed_error(_process_key(error))
                context = self._rollback_file_from_snapshot(context, error)
                return context.add_state_to_history(State.NEXT_ERROR)

        # Post-validate errors lack error_class (classify_stage hasn't run yet).
        # Check both error_class and code to detect newly introduced syntax errors.
        _CRIT_CODES = frozenset({"E999", "E902", "invalid-syntax"})

        def _is_crit(e: dict) -> bool:
            return (e.get("error_class") == "CRITICAL_SYNTAX"
                    or e.get("code") in _CRIT_CODES)

        if before is not None and after is not None:
            if after < before and not target_still_present:
                # Guard: reject if patch introduced new CRITICAL_SYNTAX errors
                # (e.g. memory/rule patch broke a string literal while fixing E501).
                _errors_before = context.validation_results.get("current_errors_before") or []
                _crit_before_files = {
                    e.get("file") for e in _errors_before
                    if _is_crit(e)
                }
                _new_crit = [
                    e for e in context.current_errors
                    if _is_crit(e)
                    and e.get("file") not in _crit_before_files
                ]
                if _new_crit:
                    _crit_files = list({e.get("file") for e in _new_crit})
                    logger.warning(
                        "  REJECT: патч ввёл новые CRITICAL_SYNTAX в %s — откатываем",
                        _crit_files,
                    )
                    _fb_meta = dict(context.metadata)
                    _fb_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                        f"REJECT: patch introduced new CRITICAL_SYNTAX errors in {_crit_files}. "
                        "Your change broke string literals or introduced a syntax error. "
                        "Fix ONLY the target line; do not split string literals across lines."
                    )
                    context = context.update(metadata=_fb_meta)
                    self._emit_patch_record(context, error, "REJECT",
                                            "new_critical_syntax_introduced", rollback=True)
                    context = context.add_rejected_patch(
                        {"error": error, "reason": "new_critical_syntax_introduced"}
                    )
                    context = context.record_processed_error(_process_key(error))
                    context = self._rollback_file_from_snapshot(context, error)
                    return context.add_state_to_history(State.NEXT_ERROR)
                # Guard: reject non-critical patches when target file still has CRITICAL_SYNTAX.
                # Prevents accepting E501/W503 fixes for files that remain syntactically broken.
                if error_class not in ("CRITICAL_SYNTAX",):
                    _target_file_rc2 = (error or {}).get("file", "")
                    if _target_file_rc2:
                        _crit_in_tgt_before = sum(
                            1 for e in _errors_before
                            if _is_crit(e) and e.get("file") == _target_file_rc2
                        )
                        if _crit_in_tgt_before > 0:
                            _crit_in_tgt_after = sum(
                                1 for e in context.current_errors
                                if _is_crit(e) and e.get("file") == _target_file_rc2
                            )
                            if _crit_in_tgt_after >= _crit_in_tgt_before:
                                logger.warning(
                                    "  REJECT: файл %s имел CRITICAL_SYNTAX до патча и всё ещё имеет (%d→%d) — откатываем",
                                    _target_file_rc2, _crit_in_tgt_before, _crit_in_tgt_after,
                                )
                                _rc2_meta = dict(context.metadata)
                                _rc2_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                                    f"REJECT: target file {_target_file_rc2!r} still has syntax errors "
                                    f"({_crit_in_tgt_after} CRITICAL_SYNTAX). "
                                    "Fix the CRITICAL_SYNTAX (E999/invalid-syntax) in this file first, "
                                    "before attempting other error types."
                                )
                                context = context.update(metadata=_rc2_meta)
                                self._emit_patch_record(context, error, "REJECT",
                                                        "target_file_still_critical", rollback=True)
                                context = context.add_rejected_patch(
                                    {"error": error, "reason": "target_file_still_critical"}
                                )
                                context = context.record_processed_error(_process_key(error))
                                context = self._rollback_file_from_snapshot(context, error)
                                return context.add_state_to_history(State.NEXT_ERROR)
                if self._should_check_invariant(context, error):
                    inv_ok, context = self._check_invariant(context, error)
                    if not inv_ok:
                        context = context.add_rejected_patch({"error": error, "reason": "invariant_violation"})
                        context = context.record_processed_error(_process_key(error))
                        context = self._rollback_file_from_snapshot(context, error)
                        return context.add_state_to_history(State.NEXT_ERROR)
                return self._dispatch_after_success(context, error, target_sig,
                                                    reason="error_count_decreased_and_target_fixed")

            if not target_still_present and after >= before:
                review = context.metadata.get("review") or {}
                review_verdict = str(review.get("verdict") or "").strip().lower()
                conf = float(context.metadata.get(MetadataKeys.CONFIDENCE) or 0.0)
                if review_verdict in ("ok", "skipped") and conf >= 0.85:
                    # Guard: reject if patch introduced new CRITICAL_SYNTAX errors
                    _eb2 = context.validation_results.get("current_errors_before") or []
                    _cf2 = {e.get("file") for e in _eb2 if _is_crit(e)}
                    _nc2 = [
                        e for e in context.current_errors
                        if _is_crit(e) and e.get("file") not in _cf2
                    ]
                    if _nc2:
                        _nf2 = list({e.get("file") for e in _nc2})
                        logger.warning("  REJECT: патч ввёл новые CRITICAL_SYNTAX в %s", _nf2)
                        _m2 = dict(context.metadata)
                        _m2[MetadataKeys.LAST_PATCH_FAILURE] = (
                            f"REJECT: patch introduced new CRITICAL_SYNTAX in {_nf2}. "
                            "Do not split string literals without proper closing quotes."
                        )
                        context = context.update(metadata=_m2)
                        self._emit_patch_record(context, error, "REJECT",
                                                "new_critical_syntax_introduced", rollback=True)
                        context = context.add_rejected_patch(
                            {"error": error, "reason": "new_critical_syntax_introduced"}
                        )
                        context = context.record_processed_error(_process_key(error))
                        context = self._rollback_file_from_snapshot(context, error)
                        return context.add_state_to_history(State.NEXT_ERROR)
                    # Guard: reject non-critical patches when target file still has CRITICAL_SYNTAX.
                    if error_class not in ("CRITICAL_SYNTAX",):
                        _target_file_rc2 = (error or {}).get("file", "")
                        if _target_file_rc2:
                            _crit_in_tgt_before = sum(
                                1 for e in _eb2
                                if _is_crit(e) and e.get("file") == _target_file_rc2
                            )
                            if _crit_in_tgt_before > 0:
                                _crit_in_tgt_after = sum(
                                    1 for e in context.current_errors
                                    if _is_crit(e) and e.get("file") == _target_file_rc2
                                )
                                if _crit_in_tgt_after >= _crit_in_tgt_before:
                                    logger.warning(
                                        "  REJECT: файл %s имел CRITICAL_SYNTAX до патча и всё ещё имеет (%d→%d) — откатываем",
                                        _target_file_rc2, _crit_in_tgt_before, _crit_in_tgt_after,
                                    )
                                    _rc2_meta = dict(context.metadata)
                                    _rc2_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                                        f"REJECT: target file {_target_file_rc2!r} still has syntax errors "
                                        f"({_crit_in_tgt_after} CRITICAL_SYNTAX). "
                                        "Fix the CRITICAL_SYNTAX (E999/invalid-syntax) in this file first, "
                                        "before attempting other error types."
                                    )
                                    context = context.update(metadata=_rc2_meta)
                                    self._emit_patch_record(context, error, "REJECT",
                                                            "target_file_still_critical", rollback=True)
                                    context = context.add_rejected_patch(
                                        {"error": error, "reason": "target_file_still_critical"}
                                    )
                                    context = context.record_processed_error(_process_key(error))
                                    context = self._rollback_file_from_snapshot(context, error)
                                    return context.add_state_to_history(State.NEXT_ERROR)
                    if self._should_check_invariant(context, error):
                        inv_ok, context = self._check_invariant(context, error)
                        if not inv_ok:
                            context = context.add_rejected_patch({"error": error, "reason": "invariant_violation"})
                            context = context.record_processed_error(_process_key(error))
                            context = self._rollback_file_from_snapshot(context, error)
                            return context.add_state_to_history(State.NEXT_ERROR)
                    return self._dispatch_after_success(
                        context, error, target_sig,
                        reason="target_fixed_reviewer_ok_count_unchanged",
                    )

            reason = "error_count_not_decreased" if after >= before else "target_error_still_present"
            logger.info("  Decision: REJECT, reason: %s", reason)
            # Feedback retry: при первом TESP-отказе — даём LLM обратную связь вместо немедленного NEXT_ERROR
            if reason == "target_error_still_present":
                _tesp_key = f"_tesp_retry_{target_sig}"
                _tesp_retries = (context.metadata.get(_tesp_key) or 0)
                if _tesp_retries == 0:
                    _fb_meta = dict(context.metadata)
                    _fb_meta[_tesp_key] = 1
                    _fb_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                        f"REJECT: your patch did NOT remove the target error "
                        f"'{error.get('code','')}' at line {error.get('line','')} "
                        f"in {error.get('file','')}. "
                        f"Message: {str(error.get('message',''))[:120]}. "
                        "The error is still present after your change. "
                        "Locate the exact expression that triggers this lint rule and rewrite only that expression."
                    )
                    _fb_meta[MetadataKeys.LAST_ATTEMPT_DATA] = self._build_attempt_data(
                        context, "REJECT", "target_error_still_present", before, after,
                    )
                    context = context.update(metadata=_fb_meta)
                    context = self._rollback_file_from_snapshot(context, error)
                    logger.info(
                        "  TESP feedback retry: даём LLM второй шанс для %s", target_sig
                    )
                    return context.add_state_to_history(State.GENERATING_PATCH)

            self._emit_patch_record(context, error, "REJECT", reason, rollback=True)
            context = context.add_rejected_patch({"error": error, "reason": reason})
            context = context.record_processed_error(_process_key(error))
            if error_class not in ("BUILD_SCRIPT", "MANIFEST") and error_type != "security":
                context = context.add_unfixable_error(error)
            context = self._track_toxic_signature(context, error, reason)
            context = self._track_llm_noeffect(context, error, reason)
            if reason == "target_error_still_present":
                context = self._cap_file_if_tesp_exceeded(context, error)
            context = self._rollback_file_from_snapshot(context, error)
            return context.add_state_to_history(State.NEXT_ERROR)

        if not target_still_present:
            if self._should_check_invariant(context, error):
                inv_ok, context = self._check_invariant(context, error)
                if not inv_ok:
                    self._emit_patch_record(context, error, "REJECT", "invariant_violation_fallback", rollback=True)
                    context = context.add_rejected_patch({"error": error, "reason": "invariant_violation"})
                    context = context.record_processed_error(_process_key(error))
                    context = self._rollback_file_from_snapshot(context, error)
                    return context.add_state_to_history(State.NEXT_ERROR)
            return self._dispatch_after_success(context, error, target_sig,
                                                reason="target_error_fixed_fallback")
        else:
            # Feedback retry: первый fallback TESP → даём LLM обратную связь
            _tesp_key = f"_tesp_retry_{target_sig}"
            _tesp_retries = (context.metadata.get(_tesp_key) or 0)
            if _tesp_retries == 0:
                _fb_meta = dict(context.metadata)
                _fb_meta[_tesp_key] = 1
                _fb_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                    f"REJECT: your patch did NOT remove the target error "
                    f"'{error.get('code','')}' at line {error.get('line','')} "
                    f"in {error.get('file','')}. "
                    f"Message: {str(error.get('message',''))[:120]}. "
                    "The error is still present after your change. "
                    "Locate the exact expression that triggers this lint rule and rewrite only that expression."
                )
                _fb_meta[MetadataKeys.LAST_ATTEMPT_DATA] = self._build_attempt_data(
                    context, "REJECT", "target_error_still_present", before, after,
                )
                context = context.update(metadata=_fb_meta)
                context = self._rollback_file_from_snapshot(context, error)
                logger.info(
                    "  TESP feedback retry (fallback): даём LLM второй шанс для %s", target_sig
                )
                return context.add_state_to_history(State.GENERATING_PATCH)

            self._emit_patch_record(context, error, "REJECT", "target_error_still_present_fallback", rollback=True)
            context = context.add_rejected_patch({"error": error, "reason": "target_error_still_present"})
            context = context.record_processed_error(_process_key(error))
            context = self._track_toxic_signature(context, error, "target_error_still_present")
            context = self._track_llm_noeffect(context, error, "target_error_still_present")
            context = self._cap_file_if_tesp_exceeded(context, error)
            context = self._rollback_file_from_snapshot(context, error)
            return context.add_state_to_history(State.NEXT_ERROR)

    def _should_check_invariant(self, context, error):
        error_type = error.get("error_type", "")
        error_code = error.get("code", "")
        invariant_guard = context.metadata.get("invariant_guard")
        invariant_enabled = context.config.get("pipeline", {}).get("invariant_check", True)
        if not invariant_guard or not invariant_enabled:
            return False
        semantic_types = {
            "trait_not_satisfied", "type_mismatch", "type_mismatch_argument",
            "ownership_error", "mutability_error", "unresolved_function", "unresolved_method"
        }
        semantic_codes = {"E0277", "E0308", "E0382", "E0502", "E0599", "E0425"}
        return error_type in semantic_types or error_code in semantic_codes

    def _check_invariant(self, context, error):
        try:
            invariant_guard = context.metadata.get("invariant_guard")
            if not invariant_guard:
                return True, context
            snapshots = context.metadata.get("patch_snapshots", [])
            if not snapshots:
                return True, context
            file_name = error.get("file", "")
            relevant_snap = None
            for snap in snapshots:
                if snap.get("file") == file_name:
                    relevant_snap = snap
                    break
            if not relevant_snap:
                return True, context
            original = relevant_snap.get("original_content", "")
            patched = relevant_snap.get("patched_content", "")
            if not original or not patched:
                return True, context
            result = invariant_guard.verify_patch(original, patched, error)
            is_accepted = result.get("accepted", True)
            if not is_accepted:
                reason = result.get("reason", "unknown")
                new_metadata = dict(context.metadata)
                violations = list(new_metadata.get("invariant_violations", []))
                violations.append({
                    "file": file_name, "line": error.get("line", 0), "reason": reason,
                })
                new_metadata["invariant_violations"] = violations
                context = context.update(metadata=new_metadata)
                return False, context
            return True, context
        except Exception as e:
            logger.warning("  invariant check failed: %s", e)
            return True, context

    # Паттерн: строка вида `+    name = 42`, `+    name = "x"`, `+    name = None` — голый литерал.
    _LITERAL_ASSIGN_RE = re.compile(
        r'^\+[ \t]*\w+[ \t]*=[ \t]*(?:\d[\d_\.]*|"[^"]*"|\'[^\']*\'|None|True|False|\[\]|\{\}|\(\))[ \t]*(?:#.*)?$'
    )

    @classmethod
    def _is_literal_variable_substitution(cls, context: PipelineContext, error: Dict) -> bool:
        """True, если патч для F821/define_variable заменяет неизвестное имя голым литералом.

        Срабатывает только для BLOCKING-ошибок с allowed_actions=['define_variable', ...].
        Проверяет добавленные строки diff-а на паттерн `name = <int|float|str-literal>`.
        """
        code = error.get("code", "")
        actions = error.get("allowed_actions") or []
        error_class = error.get("error_class", "")
        if code != "F821" and "define_variable" not in actions:
            return False
        if error_class not in ("BLOCKING", ""):
            return False
        patch = context.generated_patch
        if not isinstance(patch, str) or not patch:
            return False
        for line in patch.splitlines():
            if cls._LITERAL_ASSIGN_RE.match(line):
                return True
        return False

    @staticmethod
    def _error_signature(error):
        return PipelineStage._static_signature(error)

    # classmethod (2026-07-07): метод использует только атрибуты класса, а
    # путь net_delta_uncertain живёт в ValidateStage и минует DecideStage
    # (прямой вызов NeedsReviewStage) — классметод позволяет кормить счётчик
    # оттуда без инстанцирования DecideStage и без копии логики (§3: один
    # источник истины).
    @classmethod
    def _track_toxic_signature(cls, context: PipelineContext, error: dict, reason: str) -> PipelineContext:
        """Q3 (2026-07-03): считает честные отказы одной сигнатуры по mypy-кодам.

        Вызывается из REJECT-точек с причинами target_error_still_present и
        error_count_not_decreased, а также (2026-07-07) из NR-точек с
        причинами net_delta_uncertain и target_fixed_reviewer_ok_count_unchanged
        — см. `_TOXIC_SIG_REASONS`. После MAX_TOXIC_SIG_REJECTS отказов КЛАСС
        ошибки (file::code, НЕ файл целиком — вариант (i)) добавляется в
        metadata["_toxic_signatures"] и PrioritizeStage перестаёт ставить его
        ошибки в очередь до конца прогона. Не-mypy коды (flake8/ruff/E999) не
        считаются — для них уже есть свой containment (MAX_TESP_PER_FILE / Q1).

        2026-07-04 (замер №9, диагностика ключа): агрегируем по (file, code),
        НЕ по полной сигнатуре file::code::message. mypy-сообщение list-item
        содержит номер элемента («List item 0/3…») и тип — полная сигнатура
        РАЗНАЯ у каждой ошибки одного класса, поэтому порог 4 был недостижим
        (learning_cases №9: 8 REJECT list-item = 4 разных сигнатуры, max
        повтор 3). Ключ (file, code) агрегирует весь неверифицируемый класс
        одного файла.

        2026-07-07 (дыра в NR-путях): раньше считались только REJECT-причины —
        NR-исход того же неверифицируемого класса (net_delta_uncertain,
        target_fixed_reviewer_ok_count_unchanged) не инкрементил счётчик, и
        LLM продолжала жечься на классе до конца прогона (bcrypt
        20260703-121449z: 11 решений, real_fix_impact=0). Вызывающая сторона
        обязана НЕ звать этот метод для guard-NR (O.14/O.17/O.18/O.19/Q.1/Q.3
        logic_guard) и НЕ звать на ACCEPT-исходах — это другой сигнал (порча
        контентом / успешный фикс), не неверифицируемость класса."""
        if reason not in cls._TOXIC_SIG_REASONS:
            return context
        code = str(error.get("code") or "")
        if not code or code in cls._MYPY_SIG_CODE_EXCLUDE or not cls._MYPY_SIG_CODE_RE.match(code):
            return context
        file = str(error.get("file") or "").replace("\\", "/")
        if not file:
            return context
        class_key = f"{file}::{code}"
        counts = dict(context.metadata.get("_mypy_reject_sig_counts", {}))
        counts[class_key] = counts.get(class_key, 0) + 1
        m = dict(context.metadata)
        m["_mypy_reject_sig_counts"] = counts
        if counts[class_key] >= cls.MAX_TOXIC_SIG_REJECTS:
            toxic = list(m.get("_toxic_signatures") or [])
            if class_key not in toxic:
                toxic.append(class_key)
                m["_toxic_signatures"] = toxic
                logger.warning(
                    "  класс %s помечен toxic после %d отказов (REJECT/NR) — "
                    "неверифицируемый в тулчейне класс, исключается из очереди",
                    class_key, counts[class_key],
                )
        context = context.update(metadata=m)
        return context

    # classmethod по тем же причинам, что и _track_toxic_signature выше —
    # используются только атрибуты класса, вызывается строго из REJECT-точек
    # execute() (НЕ из NR-веток — там иной сигнал, порча содержимым/логика,
    # не «LLM впустую жжёт бюджет»).
    @classmethod
    def _track_llm_noeffect(cls, context: PipelineContext, error: dict, reason: str) -> PipelineContext:
        """Perf-1 (2026-07-07): считает REJECT, где LLM вернула непустой
        патч, но он не дал эффекта (error_count_not_decreased /
        target_error_still_present). В отличие от _track_toxic_signature
        (только mypy-коды, Q3) — считает ЛЮБОЙ код ошибки, но ТОЛЬКО когда
        патч сгенерирован LLM-backed источником (_LLM_PATCH_SOURCES);
        детерминированные пути (rule_based/memory/golden/type_ignore/...)
        дёшевы и не жгут LLM-бюджет — их REJECT сюда не считается.

        После MAX_LLM_NOEFFECT_PER_CLASS безрезультатных LLM-REJECT класс
        (file, code) добавляется в metadata["_llm_budget_exhausted"] —
        GeneratePatchStage.execute() режет для него дорогую LLM-генерацию
        без вызова llm_client, отправляя NEEDS_REVIEW (гейт сразу после
        structured-unanchorable fast-fail). Счётчик копится независимо от
        Q3 toxic — оба гейта могут сработать на одном классе, это не баг:
        они режут в разных стадиях и не мешают друг другу.

        Сброс — при ACCEPT того же (file, code) в _dispatch_after_success:
        класс оказался fixable, бан снят (см. комментарий там же)."""
        if reason not in cls._LLM_NOEFFECT_REASONS:
            return context
        patch_source = str(context.metadata.get(MetadataKeys.PATCH_SOURCE) or "")
        if patch_source not in cls._LLM_PATCH_SOURCES:
            return context
        code = str(error.get("code") or "")
        file = str(error.get("file") or "").replace("\\", "/")
        if not code or not file:
            return context
        class_key = f"{file}::{code}"
        counts = dict(context.metadata.get("_llm_noeffect_counts", {}))
        counts[class_key] = counts.get(class_key, 0) + 1
        m = dict(context.metadata)
        m["_llm_noeffect_counts"] = counts
        if counts[class_key] >= cls.MAX_LLM_NOEFFECT_PER_CLASS:
            exhausted = list(m.get("_llm_budget_exhausted") or [])
            if class_key not in exhausted:
                exhausted.append(class_key)
                m["_llm_budget_exhausted"] = exhausted
                logger.warning(
                    "  perf-1: класс %s исчерпал LLM-бюджет после %d безрезультатных "
                    "LLM-REJECT (source=%s) — дальнейшая генерация для него без LLM",
                    class_key, counts[class_key], patch_source,
                )
        context = context.update(metadata=m)
        return context

    def _cap_file_if_tesp_exceeded(self, context: PipelineContext, error: dict) -> PipelineContext:
        """Track target_error_still_present per file. After MAX_TESP_PER_FILE, bulk-skip all
        remaining errors from that file by raising their processed_errors count to 3."""
        error_file = error.get("file", "")
        if not error_file:
            return context
        counts = dict(context.metadata.get("_tesp_file_counts", {}))
        counts[error_file] = counts.get(error_file, 0) + 1
        m = dict(context.metadata)
        m["_tesp_file_counts"] = counts
        context = context.update(metadata=m)
        if counts[error_file] >= self.MAX_TESP_PER_FILE:
            logger.warning(
                "  target_error_still_present ×%d для '%s' — bulk-skip всех ошибок файла",
                counts[error_file], error_file,
            )
            new_proc = dict(context.processed_errors)
            for pending in context.current_errors:
                if pending.get("file", "") == error_file:
                    # Codex-5 (2026-07-02): ключ processed_errors ДОЛЖЕН быть
                    # process_key (= signature + "::L{line}"). Селектор
                    # (root_cause_stage.py) гейтит по _process_key; раньше здесь
                    # писался _error_signature (base, БЕЗ строки) — селектор такой
                    # ключ НИКОГДА не читал, и bulk-skip после 5 TESP-откатов был
                    # no-op: безнадёжный файл продолжал жечь циклы.
                    pkey = _process_key(pending)
                    if new_proc.get(pkey, 0) < 3:
                        new_proc[pkey] = 3
            context = context.update(processed_errors=MappingProxyType(new_proc))
        return context

    @staticmethod
    def _lg_llm_review_enabled(context) -> bool:
        """Q.5 флаг: InvariantGuard-ревью поверх Logic Guard HIGH-нарушений.
        Config: config["pipeline"]["logic_guard_llm_review"] (default False).
        Opt-in, так как вызывает дополнительный LLM-запрос.
        """
        try:
            cfg = (getattr(context, "config", None) or {})
            return bool(cfg.get("pipeline", {}).get("logic_guard_llm_review", False))
        except Exception:
            return False

    @staticmethod
    def _append_decision_log(metadata, error, decision, reason):
        """Append DecisionRecord-as-dict into `metadata['decisions']` for RunStatistics.

        Тонкая обёртка над analysis.run_statistics.append_decision_log —
        централизовано там 2026-06-21 (находка #1 контрольной серии), чтобы
        NeedsReviewStage/ValidateStage могли логировать решения, минующие
        DecideStage.execute(), той же функцией."""
        from analysis.run_statistics import append_decision_log
        append_decision_log(metadata, error, decision, reason)
