"""
Stage D.1 — NeedsReviewStage.

Сохраняет «подозрительный» патч (EditSet + сопутствующая информация)
в очередь ручного просмотра `.webbles_fix/needs_review/<sig>.json`
и переходит к следующей ошибке БЕЗ применения.

Решение «класть в очередь, а не ACCEPT/REJECT» принимает DecideStage
(см. D.4): для трёх-вариантного исхода нужны confidence-thresholds и
verdict от ReviewStage (D.3). Здесь — только инфраструктура: новый
стейт + стейдж, который умеет сохранить EditSet на диск и
гарантированно вернуть пайплайн к NEXT_ERROR.

Откат файла из snapshot (как REJECT делает) намеренно НЕ выполняется
здесь: соответствующий вызов добавит DecideStage в D.4, единообразно
с REJECT-веткой. Иначе откат раскидался бы по двум стейджам.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from core.utils import process_key as _process_key

logger = logging.getLogger(__name__)

# Priority tiers (lower number = higher priority in UI sort)
_NR_PRIORITY: Dict[str, int] = {
    "security":   1,
    "logic":      2,
    "type":       3,
    "import":     4,
    "syntax":     5,
    "formatting": 6,
    "unknown":    7,
}

# Map error_class → category
_CLASS_TO_CATEGORY: Dict[str, str] = {
    "SECURITY":       "security",
    "CRITICAL_SYNTAX": "syntax",
    "BLOCKING":       "logic",
    "CLEANUP":        "formatting",
    "UNKNOWN":        "unknown",
}

# Map specific codes → category overrides
_CODE_TO_CATEGORY: Dict[str, str] = {
    # security
    "hardcoded_secret": "security", "hardcoded_password": "security",
    "sql_injection": "security", "command_injection": "security",
    "dangerous_eval": "security", "unsafe_deserialization": "security",
    # logic
    "B904": "logic", "B006": "logic", "B008": "logic",
    "W503": "logic", "W504": "logic",
    # type
    "import-untyped": "type", "no-untyped-def": "type",
    "ANN001": "type", "ANN201": "type", "ANN202": "type",
    # import
    "F401": "import", "F811": "import", "I001": "import",
    # syntax
    "E999": "syntax", "invalid-syntax": "syntax", "E902": "syntax",
}


def _nr_category_and_priority(error: Dict[str, Any]) -> Tuple[str, int]:
    """Return (category, priority_int) for a given error dict."""
    code = str(error.get("code") or "").strip()
    ec = str(error.get("error_class") or "").strip().upper()

    # Code-level override first
    cat = _CODE_TO_CATEGORY.get(code)
    if cat is None:
        cat = _CLASS_TO_CATEGORY.get(ec, "unknown")

    prio = _NR_PRIORITY.get(cat, 7)
    return cat, prio


class NeedsReviewStage(PipelineStage):
    """Кладёт текущий патч в `.webbles_fix/needs_review/<sig>.json` и
    переходит в `NEXT_ERROR`. Поведение полностью идемпотентное: если
    EditSet'а нет (например, патч пришёл из legacy generate_fix) —
    сохраняем то, что есть (raw `patch`-строку и метаданные).
    """

    QUEUE_DIRNAME = ".webbles_fix/needs_review"

    # Какие безопасные символы оставляем в имени файла очереди.
    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

    def execute(self, context: PipelineContext) -> PipelineContext:
        """Структурный рефакторинг (2026-06-21, после decision-integrity
        серии из 10 проектов): эта стадия — ЕДИНСТВЕННЫЙ executor для
        State.NEEDS_REVIEW (см. dispatch table в PipelineEngine) и
        ЕДИНСТВЕННОЕ место, добавляющее в needs_review_items — поэтому
        теперь ЕДИНСТВЕННОЕ место, логирующее NEEDS_REVIEW в decisions[].
        Раньше каждый вызывающий код (DecideStage, GeneratePatchStage,
        ValidateStage net-delta) сам обязан был не забыть отдельно вызвать
        append_decision_log — две из пяти живых находок контрольной серии
        были именно такими забытыми вызовами. Теперь физически невозможно
        попасть в NEEDS_REVIEW без лога.

        Вызывающий код обязан установить
        metadata["_needs_review_pending_reason"] ПЕРЕД переходом в этот
        стейт (или прямым вызовом этой стадии) — иначе используется
        дефолтный "needs_review" (защитный fallback, не должен срабатывать
        на корректно написанном вызывающем коде)."""
        logger.info("Стадия NEEDS_REVIEW: откладываем патч на ручной просмотр...")

        error = context.selected_error or {}
        error_sig = PipelineStage._static_signature(error) if error else ""

        # Каскадное схлопывание F821-групп (2026-07-02, вариант A): если
        # GeneratePatchStage забанил группу siblings разом (см. no_llm_codes/
        # cascade_collapse_codes), список их строк лежит в
        # metadata["_nr_group_occurrences"] — потребляем его здесь (pop) и
        # переносим в поле `occurrences` NR-записи. `line` записи остаётся
        # строкой ошибки-представителя (реально дошедшей до этой стадии),
        # occurrences — все строки root-cause группы, нужны ревьюеру для
        # контекста. Для не-каскадных ошибок используем синглтон [line], чтобы
        # схема JSON/dict была одинаковой независимо от cascade_collapse_codes.
        _group_occurrences = list(context.metadata.get("_nr_group_occurrences") or [])
        if _group_occurrences:
            _popped_meta = dict(context.metadata)
            _popped_meta.pop("_nr_group_occurrences", None)
            context = context.update(metadata=_popped_meta)
        _this_occurrences = sorted(set(_group_occurrences)) if _group_occurrences else [error.get("line", 0)]

        new_metadata = dict(context.metadata)

        # Конверсия-5 (2026-07-02): дедуп очереди по (error_sig, line).
        # Одна и та же ошибка, повторно дошедшая до NR (retry в следующем
        # цикле), раньше добавляла ДУБЛИРУЮЩУЮ запись в needs_review_items и
        # инкрементила счётчики (httpx: httpx\_models.py::F821::undefined
        # name encoding — ТРИ идентичные записи на одну строку). Обновляем
        # существующую запись на месте, без дублей в счётчиках и без второго
        # NEEDS_REVIEW в decisions[] (иначе разошлась бы decision-integrity
        # сверка «decisions == items»).
        #
        # Конверсия-6 (2026-07-02): каскадные записи (occurrences длиннее
        # одной строки — своя группа ИЛИ уже сохранённая) матчятся по
        # error_sig БЕЗ учёта line и СЛИВАЮТ occurrences (объединение
        # множеств), а не создают новую запись — иначе повторный проход по
        # той же root-cause сигнатуре от ДРУГОГО представителя строки плодил
        # бы второй item вместо расширения первого.
        _existing_items = list(new_metadata.get("needs_review_items", []))
        _dup_idx = None
        if error_sig:
            for i, it in enumerate(_existing_items):
                if it.get("error_sig") != error_sig:
                    continue
                _it_occurrences = it.get("occurrences") or []
                if len(_it_occurrences) > 1 or len(_this_occurrences) > 1:
                    _dup_idx = i
                    break
                if it.get("line", 0) == error.get("line", 0):
                    _dup_idx = i
                    break

        _merged_occurrences = _this_occurrences
        if _dup_idx is not None:
            _prev_occurrences = _existing_items[_dup_idx].get("occurrences") or []
            _merged_occurrences = sorted(set(list(_prev_occurrences) + list(_this_occurrences)))

        saved_path, has_patch = self._save_review_item(context, error, error_sig, _merged_occurrences)

        if _dup_idx is not None:
            _existing_items[_dup_idx] = {
                **_existing_items[_dup_idx],
                "saved_path": str(saved_path) if saved_path else _existing_items[_dup_idx].get("saved_path", ""),
                "intent": new_metadata.get("intent", ""),
                "confidence": new_metadata.get("confidence"),
                "has_patch": has_patch or _existing_items[_dup_idx].get("has_patch", False),
                "occurrences": _merged_occurrences,
            }
            new_metadata["needs_review_items"] = _existing_items
            new_metadata["last_decision"] = "NEEDS_REVIEW"
            new_metadata.pop("_needs_review_pending_reason", None)
            new_metadata.pop("patch_attempt", None)
            new_metadata.pop("use_full_file", None)
            new_metadata.pop("broken_file_mode", None)
            logger.info(
                "  needs_review: %s уже в очереди — запись обновлена"
                " (occurrences=%s), счётчики не дублируем",
                error_sig, _merged_occurrences,
            )
            context = context.update(metadata=new_metadata)
            if error_sig:
                context = context.record_processed_error(_process_key(error))
            context = context.set_selected_error(None)
            context = context.set_patch(None)
            return context.add_state_to_history(State.NEXT_ERROR)

        new_metadata["last_decision"] = "NEEDS_REVIEW"
        # Счётчик для финального отчёта (D.5).
        new_metadata["needs_review_count"] = int(
            new_metadata.get("needs_review_count", 0)
        ) + 1
        # 2026-06-24: раздельные счётчики with_patch/no_patch_unprocessed —
        # см. docstring _save_review_item про has_patch.
        if has_patch:
            new_metadata["needs_review_with_patch_count"] = int(
                new_metadata.get("needs_review_with_patch_count", 0)
            ) + 1
        else:
            new_metadata["needs_review_no_patch_count"] = int(
                new_metadata.get("needs_review_no_patch_count", 0)
            ) + 1
        review_items = list(new_metadata.get("needs_review_items", []))
        review_items.append({
            "error_sig": error_sig,
            "error_code": error.get("code", ""),
            "file": error.get("file", ""),
            "line": error.get("line", 0),
            "saved_path": str(saved_path) if saved_path else "",
            "intent": new_metadata.get("intent", ""),
            "confidence": new_metadata.get("confidence"),
            "has_patch": has_patch,
            "occurrences": _merged_occurrences,
        })
        new_metadata["needs_review_items"] = review_items

        _nr_reason = new_metadata.pop("_needs_review_pending_reason", None) or "needs_review"
        from analysis.run_statistics import append_decision_log
        append_decision_log(new_metadata, error, "NEEDS_REVIEW", _nr_reason)

        # Гасим артефакты текущей попытки, чтобы следующий цикл стартовал
        # чистым (по аналогии с NextErrorStage).
        new_metadata.pop("patch_attempt", None)
        new_metadata.pop("use_full_file", None)
        new_metadata.pop("broken_file_mode", None)

        context = context.update(metadata=new_metadata)
        # Фиксируем ошибку как обработанную и снимаем patch/selected_error.
        if error_sig:
            context = context.record_processed_error(_process_key(error))
        context = context.set_selected_error(None)
        context = context.set_patch(None)

        return context.add_state_to_history(State.NEXT_ERROR)

    # ---- internals --------------------------------------------------

    def _save_review_item(
        self,
        context: PipelineContext,
        error: Dict[str, Any],
        error_sig: str,
        occurrences: Optional[List[int]] = None,
    ) -> Tuple[Optional[Path], bool]:
        """Пишет JSON в `.webbles_fix/needs_review/<safe_sig>.json`.
        Возвращает (путь записанного файла или `None` при отказе, has_patch).
        Любой фейл записи логируется и не ломает переход в NEXT_ERROR.

        `occurrences` — список номеров строк root-cause группы (2026-07-02,
        каскадное схлопывание F821-групп); для не-каскадных ошибок вызывающий
        код передаёт синглтон `[line]`, чтобы схема payload не зависела от
        cascade_collapse_codes.
        """
        # has_patch вычисляем ДО возможного раннего return по ошибке каталога,
        # чтобы execute() всегда получал корректный счётчик даже при сбое записи.
        meta = context.metadata or {}
        _patch = context.generated_patch if isinstance(context.generated_patch, str) else ""
        _structured_edit = meta.get("structured_edit")
        has_patch = bool(_patch.strip()) or bool(_structured_edit)

        try:
            root = self._resolve_queue_root(context)
            root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("  needs_review: не удалось создать каталог: %s", e)
            return None, has_patch

        safe_sig = self._SAFE_NAME_RE.sub("_", error_sig)[:120] or "unknown"
        target = root / f"{safe_sig}.json"

        _cat, _prio = _nr_category_and_priority(error)
        # 2026-06-24 (метрики прогресса): has_patch различает записи, для
        # которых хоть раз сгенерировался реальный патч (есть что показать
        # ревьюеру), от записей, до которых LLM не дошла вовсе (например,
        # каскадные F821, упёршиеся в project_timeout раньше содержательной
        # попытки) — см. queue_delta/independent_scan_delta/real_fix_impact/
        # attempted_errors_count в PipelineEngine._finalize_and_audit.
        payload: Dict[str, Any] = {
            "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "error_sig": error_sig,
            "error": error,
            "patch_source": meta.get("patch_source", ""),
            "patch": _patch,
            "intent": meta.get("intent", ""),
            "confidence": meta.get("confidence"),
            "risks": list(meta.get("risks", []) or []),
            "structured_edit": _structured_edit,
            "category": _cat,
            "priority": _prio,
            "has_patch": has_patch,
            "occurrences": list(occurrences) if occurrences else [error.get("line", 0)],
        }
        try:
            target.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("  needs_review: запись %s упала: %s", target, e)
            return None, has_patch
        logger.info("  needs_review: сохранено → %s", target)
        return target, has_patch

    @classmethod
    def _resolve_queue_root(cls, context: PipelineContext) -> Path:
        """P0.6: каталог очереди вынесен в СИСТЕМНУЮ папку webbles_fix —
        `<webbles_fix>/runtime/<project_name>/needs_review/`. В папке
        ремонтируемого проекта НИЧЕГО НЕ ДОЛЖНО создаваться кроме
        `.webbles_backups/` (требование пользователя).

        Группируем по basename проекта (а не по working_path/sandbox),
        чтобы повторные прогоны на одном проекте видели одну и ту же очередь.
        """
        # PipelineEngine знает реальный project_path и системный путь.
        # Используем `project_path` (а не sandbox/working_path).
        project_path = getattr(context, "project_path", None) or Path.cwd()
        try:
            from core.pipeline_engine import PipelineEngine
            return PipelineEngine._project_needs_review_dir(project_path)
        except Exception:
            # Fallback: рядом с системным `core/` (если что-то сломалось).
            sys_root = Path(__file__).resolve().parents[2] / "runtime"
            return sys_root / Path(project_path).name / "needs_review"
