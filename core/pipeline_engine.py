"""
Главный оркестратор конвейера Webbles Fix.
Поддерживает гранулированный аудит: при провале аудита откатываются только проблемные сегменты,
успешные файлы копируются в оригинал.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from analysis.dependency_graph import DependencyGraph
from analysis.error_classifier import ErrorClassifier
from analysis.health_baseline import HealthBaseline
from analysis.invariant_guard import InvariantGuard
from analysis.run_statistics import RunStatistics, decision_from_metadata
from core.anti_loop import AntiLoop
from core.circuit_breaker import CircuitBreaker
from core.engine.audit import AuditManager
from core.engine.cleanup import CleanupManager
from core.engine.parallel import ParallelExecutor
from core.engine.state import StateManager
from core.error_context_validator import ErrorContextValidator
from core.language_support import get_language_provider, LanguageSupport
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage, _get_source_patterns
from core.stages.analyze_stage import AnalyzeStage
from core.stages.apply_patch_stage import ApplyPatchStage
from core.stages.classify_stage import ClassifyStage
from core.stages.cleanup_stage import CleanupStage
from core.stages.decide_stage import DecideStage
from core.stages.needs_review_stage import NeedsReviewStage
from core.stages.review_stage import ReviewStage
from core.stages.execute_plan_stage import ExecutePlanStage
from core.stages.final_resolve_stage import FinalResolveStage
from core.stages.generate_patch_stage import GeneratePatchStage
from core.stages.idle_stage import IdleStage
from core.stages.next_error_stage import NextErrorStage
from core.stages.planning_stage import PlanningStage
from core.stages.pre_cleanup_stage import PreCleanupStage
from core.stages.prioritize_stage import PrioritizeStage
from core.stages.rollback_stage import RollbackStage
from core.stages.root_cause_stage import RootCauseStage
from core.stages.validate_stage import ValidateStage
from core.planning_factory import PlanningFactory
from core.quality_evaluator import QualityEvaluator
from core.state_machine import State
from core.stability_governor import StabilityGovernor
from core.system_health import SystemHealthEvaluator
from core.utils import process_key as _process_key
from fixers.llm_client import LLMClient
from fixers.patch_engine import PatchEngine
from fixers.syntax_disaster_recovery import SyntaxDisasterRecovery
from memory.learning import MemoryLearning
from safety.degradation_tracker import DegradationTracker
from safety.error_priority_engine import ErrorPriorityEngine
from validation.compiler_checks import CompilerChecks
from validation.lint_checks import LintChecks
from validation.rust_audit_checks import RustAuditChecks
from validation.security_checks import SecurityChecks

from core.contract import ErrorSignature, MetadataKeys

import core.languages

logger = logging.getLogger(__name__)


def compute_progress_metrics(
    *,
    scan_before: Optional[Dict[str, Any]],
    scan_after: Optional[Dict[str, Any]],
    accepted_patches: list,
    rejected_patches: list,
    needs_review_meta: Dict[str, Any],
    initial_error_count: int,
    baseline_remaining: int,
    total_current_errors: Optional[int],
) -> Dict[str, Any]:
    """Метрики прогресса (2026-06-24, расследование 5909 vs 6055 на pytils).

    Точка истины для "сколько реально исправлено" — fresh full scan
    before/after (project_path, один и тот же `AnalyzeStage.run_full_scan`),
    а НЕ baseline_remaining: это внутренняя инкрементальная модель очереди
    `context.current_errors`, обновляемая по ходу прогона per-file, которая
    может разойтись с реальным состоянием диска (на pytils разрыв был
    ~1187 ошибок при 5 ACCEPT).

    Возвращает:
      independent_scan_before/after/delta — scan_before/after["total"] и их
        разница: НОВАЯ точка истины.
      queue_delta — старая метрика (initial_error_count - baseline_remaining),
        оставлена для обратной совместимости, но больше НЕ заголовочный
        показатель прогресса.
      real_fix_impact — independent_scan_delta, ОГРАНИЧЕННЫЙ файлами,
        тронутыми хотя бы одним ACCEPT — сколько ошибок реально исчезло там,
        где система отчиталась об исправлении (включая каскадный эффект:
        1 ACCEPT может убрать много зависимых mypy-ошибок в одном файле —
        см. tarjan_bridges_cut_vertices.py на pytils: 1 фикс убрал 14 ошибок).
      attempted_errors_count — сколько ошибок дошли до генерации патча
        (ACCEPT + REJECT + needs_review с патчем), в отличие от
        needs_review_no_patch_unprocessed — ошибок, до которых LLM не дошла.
      needs_review_with_patch / needs_review_no_patch_unprocessed — из
        needs_review_meta (см. NeedsReviewStage.has_patch).
      accept_cancelled_count / accept_cancelled_items — ACCEPT, впоследствии
        отменённые осциллирующим патчем на той же file::line::code сигнатуре
        (см. PipelineEngine._ban_oscillating_signatures, 2026-06-24,
        Bluetooth-Devices/dbus-fast: 2 ACCEPT взаимно отменили друг друга,
        итоговый файл байт-в-байт идентичен исходному). Эти accepted_patches
        помечены `oscillation_cancelled=True` и ИСКЛЮЧЕНЫ из touched_files
        для real_fix_impact — иначе файл с нулевым net-изменением ложно
        попал бы в "файлы, тронутые ACCEPT".
    """
    out: Dict[str, Any] = {}

    out["independent_scan_before"] = scan_before["total"] if scan_before else None
    out["independent_scan_after"] = total_current_errors
    if out["independent_scan_before"] is not None and out["independent_scan_after"] is not None:
        out["independent_scan_delta"] = out["independent_scan_before"] - out["independent_scan_after"]
    else:
        out["independent_scan_delta"] = None

    out["queue_delta"] = initial_error_count - baseline_remaining

    # audit_reverted (2026-07-02, deep-reasoner, pyca/bcrypt): ACCEPT-патчи в
    # файле, ПОЛНОСТЬЮ откачённом финальным аудитом (failed_segments, end==0),
    # не доживают до доставки — файл байт-в-байт равен оригиналу
    # (verify_accepts: verdict=UNCHANGED_FILE). Исключаем их из «живых» ACCEPT
    # ровно как oscillation_cancelled: иначе файл с нулевым net-изменением
    # ложно попадает в touched_files и раздувает впечатление от прогона.
    _real_accepted = [
        p for p in accepted_patches
        if not p.get("oscillation_cancelled") and not p.get("audit_reverted")
    ]
    _cancelled_accepted = [p for p in accepted_patches if p.get("oscillation_cancelled")]
    out["accept_cancelled_count"] = len(_cancelled_accepted)

    if scan_before is not None and scan_after is not None:
        touched_files = {
            str(p.get("file", "")).replace("\\", "/").lstrip("/")
            for p in _real_accepted
            if p.get("file")
        }

        def _count_for_files(errors: list) -> int:
            return sum(
                1 for e in errors
                if str(e.get("file", "")).replace("\\", "/").lstrip("/") in touched_files
            )

        before_touched = _count_for_files(scan_before["errors"])
        after_touched = _count_for_files(scan_after["errors"])
        out["real_fix_impact"] = before_touched - after_touched
    else:
        out["real_fix_impact"] = None

    needs_review_with_patch = int(needs_review_meta.get("needs_review_with_patch_count", 0))
    needs_review_no_patch = int(needs_review_meta.get("needs_review_no_patch_count", 0))
    out["needs_review_with_patch"] = needs_review_with_patch
    out["needs_review_no_patch_unprocessed"] = needs_review_no_patch
    out["attempted_errors_count"] = (
        len(accepted_patches) + len(rejected_patches) + needs_review_with_patch
    )
    out["accept_cancelled_items"] = list(needs_review_meta.get("accept_cancelled_items", []) or [])
    # ACCEPT, откачённые финальным аудитом (полный откат файла) — см.
    # PipelineEngine._mark_audit_reverted_accepts. Отдельный счётчик от
    # oscillation, чтобы не смешивать два разных механизма потери ACCEPT.
    out["accept_reverted_count"] = int(needs_review_meta.get("accept_reverted_count", 0) or 0)
    out["accept_reverted_items"] = list(needs_review_meta.get("accept_reverted_items", []) or [])
    _ambiguous = list(needs_review_meta.get("oscillation_ambiguous_items", []) or [])
    out["oscillation_ambiguous_items"] = _ambiguous
    out["oscillation_ambiguous_count"] = len(_ambiguous)
    return out


class PipelineEngine:
    # P0.6: ВСЕ runtime-файлы движка теперь живут в СИСТЕМНОЙ папке webbles_fix
    # (`<webbles_fix>/runtime/<project_name>/{state,memory,baseline}.json`),
    # а НЕ в ремонтируемом проекте. В папке проекта остаётся ТОЛЬКО
    # `.webbles_backups/` (по требованию пользователя).
    #
    # P0.2 (предыдущая итерация) использовал `<project>/.webbles_fix/` — но
    # пользователь явно сказал «ничего не должно появляться в этой папке
    # кроме папки бэкапов». Теперь даже эта служебная папка вынесена в систему.
    #
    # Legacy-имена нужны только для cleanup'а старых установок (если эти
    # файлы остались с прежних версий, их нужно вычистить из проекта).
    STATE_FILE = "state.json"
    MEMORY_FILE = "memory.json"
    BASELINE_FILE = "baseline.json"
    LEGACY_STATE_FILE = ".webbles_fix_state.json"
    LEGACY_MEMORY_FILE = ".webbles_fix_memory.json"
    LEGACY_BASELINE_FILE = ".webbles_baseline.json"
    LEGACY_WEBBLES_FIX_DIR = ".webbles_fix"  # P0.2 промежуточный вариант
    MAX_PATCHES_PER_FILE = 30

    @staticmethod
    def _system_runtime_root() -> Path:
        """Корень для runtime-артефактов: `<webbles_fix>/runtime/`.
        Файлы группируются по basename проекта."""
        return Path(__file__).resolve().parents[1] / "runtime"

    @classmethod
    def _project_runtime_dir(cls, project_path) -> Path:
        name = Path(project_path).name or "unknown_project"
        return cls._system_runtime_root() / name

    @classmethod
    def _project_needs_review_dir(cls, project_path) -> Path:
        """Очередь ручного просмотра тоже в системе, не в проекте."""
        return cls._project_runtime_dir(project_path) / "needs_review"

    @classmethod
    def _project_llm_debug_dir(cls, project_path) -> Path:
        """Сырые ответы LLM при категоризированных сбоях (empty_response
        диагностика, 2026-06-22) — отдельная диагностика, не в проекте
        (см. _project_needs_review_dir)."""
        return cls._project_runtime_dir(project_path) / "llm_debug"

    def _migrate_needs_review(self, legacy_dir: Path) -> None:
        """Переносит содержимое `<project>/.webbles_fix/needs_review/` в
        `<webbles_fix>/runtime/<project>/needs_review/`."""
        src = legacy_dir / "needs_review"
        if not src.is_dir():
            return
        dst = self._project_needs_review_dir(self.project_path)
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            try:
                target = dst / item.name
                if not target.exists():
                    item.replace(target)
                else:
                    item.unlink(missing_ok=True)
            except Exception as e:
                logger.debug("needs_review migration of %s failed: %s", item, e)
        try:
            src.rmdir()
        except Exception:
            pass

    def __init__(
        self,
        project_path: Path,
        language: str,
        llm_client: Optional[LLMClient] = None,
        config: Optional[Dict[str, Any]] = None,
        resume: bool = False,
        dry_run: bool = False,
        reporter=None,
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.project_path = project_path.resolve()
        self.language = language.lower()
        self.config = config or {}
        pipeline_config = self.config.get("pipeline", {})
        self.dry_run = dry_run
        self.reporter = reporter

        # P0.6: runtime-папка в системе webbles_fix, НЕ в проекте.
        runtime_dir = self._project_runtime_dir(self.project_path)
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug("system runtime dir create failed: %s", e)

        self.state_file = runtime_dir / self.STATE_FILE
        self.memory_file = runtime_dir / self.MEMORY_FILE
        self.baseline_file = runtime_dir / self.BASELINE_FILE

        # Миграция со СТАРЫХ расположений (P0.2 .webbles_fix/ + legacy в корне).
        # Цель: ВЫТАЩИТЬ остатки из проекта, чтобы там стало чисто.
        legacy_locations = [
            # (legacy path, dst, kind)
            (self.project_path / self.LEGACY_STATE_FILE, self.state_file, "state"),
            (self.project_path / self.LEGACY_MEMORY_FILE, self.memory_file, "memory"),
            (self.project_path / self.LEGACY_BASELINE_FILE, self.baseline_file, "baseline"),
            # P0.2 промежуточный вариант:
            (self.project_path / self.LEGACY_WEBBLES_FIX_DIR / "state.json", self.state_file, "state"),
            (self.project_path / self.LEGACY_WEBBLES_FIX_DIR / "memory.json", self.memory_file, "memory"),
            (self.project_path / self.LEGACY_WEBBLES_FIX_DIR / "baseline.json", self.baseline_file, "baseline"),
        ]
        for legacy_path, new_path, kind in legacy_locations:
            try:
                if legacy_path.exists():
                    if not new_path.exists():
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        legacy_path.replace(new_path)
                        logger.info("Migrated %s → %s", legacy_path, new_path)
                    else:
                        # Уже есть актуальная версия в системе — старую удаляем.
                        legacy_path.unlink(missing_ok=True)
                        logger.info("Removed legacy %s (already migrated)", legacy_path)
            except Exception as e:
                logger.debug("Legacy migration %s skipped: %s", kind, e)

        # Дочистим пустые `.webbles_fix/` и `needs_review/` в проекте, если
        # они остались. По требованию пользователя в папке проекта должна
        # быть ТОЛЬКО `.webbles_backups/`.
        try:
            legacy_dir = self.project_path / self.LEGACY_WEBBLES_FIX_DIR
            if legacy_dir.is_dir():
                # Содержимое уже перенесено выше; нужно решить — удалять или нет.
                # `needs_review/` тоже переезжает в системную папку (см. ниже).
                self._migrate_needs_review(legacy_dir)
                # Если папка опустела — удаляем её.
                try:
                    next(legacy_dir.iterdir())
                except StopIteration:
                    legacy_dir.rmdir()
                    logger.info("Removed empty %s", legacy_dir)
        except Exception as e:
            logger.debug("legacy .webbles_fix cleanup skipped: %s", e)

        if pipeline_config.get("reset_baseline", False) and self.baseline_file.exists():
            self.baseline_file.unlink()
            logger.info("Старый baseline удалён (reset_baseline=true)")

        self.language_provider: Optional[LanguageSupport] = get_language_provider(self.language)
        if self.language_provider is None:
            logger.warning(f"Языковой провайдер не найден для '{self.language}'. Используется fallback.")

        if llm_client is None:
            self.llm_client = LLMClient(config=self.config, language_provider=self.language_provider)
        else:
            self.llm_client = llm_client
            if not getattr(self.llm_client, 'language_provider', None):
                self.llm_client.language_provider = self.language_provider

        # 2026-06-24: пул LLMClient с разными api_key (WEBBLES_LLM_API_KEY_POOL)
        # для parallel_workers>1 — каждый поток получает СВОЙ ключ и СВОЙ
        # изолированный circuit breaker (round-robin, см. ParallelExecutor).
        # Без переменной окружения — пул из одного клиента (self.llm_client),
        # поведение не меняется.
        from fixers.llm_client import build_llm_client_pool
        self.llm_client_pool = build_llm_client_pool(
            self.config, self.language_provider, primary_client=self.llm_client,
        )

        self.patch_engine = PatchEngine()
        self.analyzer = self.language_provider.create_analyzer() if self.language_provider else self._get_analyzer()
        self.compiler = CompilerChecks()
        self.linter = LintChecks()
        self.security = SecurityChecks()
        self.rust_audit = RustAuditChecks() if self.language == "rust" else None

        self.priority_engine = ErrorPriorityEngine()
        self.degradation_tracker = DegradationTracker()
        self.circuit_breaker = CircuitBreaker()
        self.anti_loop = AntiLoop()
        _mem_ttl = pipeline_config.get("memory", {}).get("ttl_days", 90)
        self.memory = MemoryLearning(self.memory_file, ttl_days=_mem_ttl)

        if pipeline_config.get("invariant_check", True):
            self.invariant_guard = InvariantGuard(self.llm_client)
        else:
            self.invariant_guard = None

        if self.invariant_guard and self.reporter:
            self.reporter.send("⚠️ Проверка инвариантов включена — повышенный расход токенов LLM.")

        self.classifier = ErrorClassifier(language_provider=self.language_provider)
        self.quality_evaluator = QualityEvaluator(self.classifier)

        self.health_baseline = HealthBaseline.load_or_create(self.baseline_file, self._compute_current_baseline)
        self.health_evaluator = SystemHealthEvaluator(window_size=10, classifier=self.classifier)
        self.health_evaluator.set_baseline(self.health_baseline)
        self.stability_governor = StabilityGovernor(
            health_evaluator=self.health_evaluator,
            base_params={
                "accept_threshold": 0.3, "retry_threshold": -0.3,
                "exploration_rate": 0.5, "max_patch_size": 100,
                "max_iterations": 10, "rollback_limit": 3,
            },
        )
        self.dynamic_params = self.stability_governor.get_params()

        self.dep_graph = DependencyGraph(self.project_path, self.language)

        self.use_planning = pipeline_config.get("use_planning", False)
        self.planning_components = None
        if self.use_planning:
            factory = PlanningFactory(
                project_path=self.project_path, language=self.language,
                analyzer=self.analyzer, compiler=self.compiler,
                linter=self.linter, security=self.security,
                memory=self.memory, health_evaluator=self.health_evaluator,
            )
            self.planning_components = factory.create(pipeline_config)
            if self.planning_components is None:
                logger.warning("Планирование отключено – не удалось загрузить модули.")
                self.use_planning = False

        self._state_lock = threading.Lock()

        from fixers.file_segmenter import FileSegmenter as FS
        self.disaster_recovery = SyntaxDisasterRecovery(
            llm_client=self.llm_client,
            segmenter=FS(self.language),
            language_provider=self.language_provider
        )

        self.audit_manager = AuditManager(
            self.project_path, self.language, self.analyzer,
            self.classifier, self.compiler, self.config,
            context_getter=lambda: self.context,
            invariant_guard=self.invariant_guard,
        )
        self.cleanup_manager = CleanupManager(self.project_path, self.language, self.patch_engine, self.config)
        self.state_manager = StateManager(
        self.project_path, self.memory, self._state_lock,
        runtime_dir=runtime_dir,
        )
        # Структурированная запись patch-событий (schema v1.2)
        _project_id = self.project_path.name
        _patch_events_path = runtime_dir / "patch_events.jsonl"
        _learning_cases_path = runtime_dir.parent / "learning_cases.jsonl"
        # Извлекаем model/profile из конфига LLM
        _llm_providers = (self.config.get("llm") or {}).get("providers") or []
        _llm_model = (_llm_providers[0].get("model") if _llm_providers else None) or ""
        _llm_profile = (_llm_providers[0].get("profile") if _llm_providers else None) or ""
        from core.patch_recorder import PatchRecorder
        self._patch_recorder = PatchRecorder(
            _patch_events_path,
            learning_cases_path=_learning_cases_path,
            language=self.language,
            llm_model=_llm_model,
            prompt_profile=_llm_profile,
            project_id=_project_id,
        )
        self._patch_recorder_project_id = _project_id

        # 2026-06-24: compiler/linter/security/quality_evaluator/recorder
        # передаются, чтобы параллельный мини-цикл мог прогнать ПОЛНУЮ
        # цепочку ValidateStage/ReviewStage/DecideStage (см.
        # ParallelExecutor._resolve_patch) — раньше эти стадии в параллельном
        # режиме не вызывались вовсе.
        self.parallel_executor = ParallelExecutor(
            self.llm_client, self.memory, self.patch_engine,
            self._state_lock,
            get_context=lambda: self.context,
            set_context=lambda ctx: setattr(self, 'context', ctx),
            analyzer=self.analyzer,
            classifier=self.classifier,
            compiler=self.compiler,
            linter=self.linter,
            security=self.security,
            quality_evaluator=self.quality_evaluator,
            recorder=self._patch_recorder,
            circuit_breaker=self.circuit_breaker,
            anti_loop=self.anti_loop,
            llm_client_pool=self.llm_client_pool,
        )

        self.stages = {
            State.IDLE: IdleStage(),
            State.ANALYZING: AnalyzeStage(self.analyzer, llm_client=self.llm_client),
            State.CLASSIFY: ClassifyStage(self.classifier),
            State.PRE_CLEANUP: PreCleanupStage(),
            State.PRIORITIZING: PrioritizeStage(self.priority_engine),
            State.ROOT_CAUSE: RootCauseStage(self.dep_graph),
            State.GENERATING_PATCH: GeneratePatchStage(
                self.llm_client, self.memory, self.patch_engine,
                self.invariant_guard, self.language_provider,
                self.disaster_recovery,
                syntax_orchestrator=None,
            ),
            State.APPLYING_PATCH: ApplyPatchStage(self.patch_engine, self.analyzer, self.classifier),
            State.VALIDATING: ValidateStage(
                self.compiler, self.linter, self.security, self.analyzer, self.degradation_tracker
            ),
            State.REVIEWING: ReviewStage(self.llm_client),
            State.DECIDING: DecideStage(self.quality_evaluator, self.analyzer,
                                        recorder=self._patch_recorder),
            State.ROLLING_BACK: RollbackStage(self.patch_engine, self.analyzer),
            State.NEEDS_REVIEW: NeedsReviewStage(),
            State.NEXT_ERROR: NextErrorStage(),
            State.EXECUTE_PLAN: ExecutePlanStage(),
            State.FINAL_RESOLVE: FinalResolveStage(),
        }

        if self.use_planning and self.planning_components:
            self.stages[State.PLANNING] = PlanningStage(
                simulator=self.planning_components["simulator"],
                planner=self.planning_components["planner"],
                memory=self.memory, llm_client=self.llm_client,
                sandbox=self.planning_components["sandbox"],
                state_cache=self.planning_components["state_cache"],
                state_hasher=self.planning_components["state_hasher"],
                patch_scorer=self.planning_components["patch_scorer"],
            )

        if resume and self.state_file.exists():
            self.context = self.state_manager.load_state()
            if self.context is None:
                self.context = self._create_fresh_context()
            else:
                # state.json хранит config зачищенным (см.
                # StateManager._sanitize_for_persist) — там лежит строка-плейсхолдер,
                # а не dict. Восстанавливаем живой config, иначе любой код вида
                # context.config.get(...) (например NextErrorStage) падает с
                # AttributeError на резюмированном запуске.
                self.context = self.context.update(config=self.config)
            self.initial_errors = self.context.initial_errors
        else:
            self.context = self._create_fresh_context()
            self.initial_errors = self.context.initial_errors

        if self.invariant_guard:
            new_metadata = dict(self.context.metadata)
            new_metadata["invariant_guard"] = self.invariant_guard
            self.context = self.context.update(metadata=new_metadata)

        self.step_times: Dict[str, float] = {}
        self.start_time = time.time()

        # Stage L.5+ — пер-ошибочный live-стриминг событий из движка.
        # `event_emitter`: callable(event_type:str, data:dict) -> None. Если None
        # — никаких эмиссий не делаем. Любое исключение в эмиттере глушим, чтобы
        # внешний слушатель никогда не валил конвейер.
        self._event_emitter = event_emitter
        # Сигнатуры ошибок, для которых уже отправлено `error_dequeued`. Нужно
        # чтобы не плодить дубли на повторных проходах одного `_single_run`.
        self._emitted_error_sigs: set = set()

    def _create_fresh_context(self) -> PipelineContext:
        return PipelineContext(
            project_path=self.project_path, language=self.language,
            config=self.config, dry_run=self.dry_run,
            memory=self.memory, reporter=self.reporter,
        )

    def _get_analyzer(self):
        if self.language == "rust":
            from analyzers.rust_analyzer import RustAnalyzer
            return RustAnalyzer()
        elif self.language == "python":
            from analyzers.python_analyzer import PythonAnalyzer
            return PythonAnalyzer()
        elif self.language in ("javascript", "typescript", "js", "ts"):
            from analyzers.js_analyzer import JSAnalyzer
            return JSAnalyzer()
        elif self.language == "csharp":
            from analyzers.csharp_analyzer import CsharpAnalyzer
            return CsharpAnalyzer()
        elif self.language in ("cpp", "c++", "cxx", "cc", "c"):
            # 2026-07-09: раньше только "cpp" → c/c++/cxx падали в ValueError,
            # хотя dependency-recovery/manifest их принимали (рассинхрон).
            # CppAnalyzer теперь сканирует и .c (gcc), и .cpp (g++).
            from analyzers.cpp_analyzer import CppAnalyzer
            return CppAnalyzer()
        raise ValueError(f"Unsupported language: {self.language}")

    def _compute_current_baseline(self) -> Dict[str, Any]:
        errors = self.analyzer.analyze(self.project_path)
        total_weight = self.classifier.total_weight(errors)
        return {
            "target_error_weight": total_weight,
            "target_compile_success": True,
            "target_security_ok": True,
            "target_lint_count": 0,
            "created_at": datetime.utcnow().isoformat(),
        }

    def _save_state(self) -> None:
        self.state_manager.save_state(
            self.context, self.anti_loop, self.circuit_breaker,
            self.dynamic_params, self.step_times, self.start_time
        )

    # ------------------------------------------------------------------
    # Stage L.5+ — пер-ошибочные события движка (хелпер в core.engine)
    # ------------------------------------------------------------------
    def _emit_after_stage(self, stage_state: "State") -> None:
        """Тонкая обёртка над `core.engine.event_emission.emit_after_stage`.

        Хелпер вынесен отдельно, чтобы тесты могли гонять его без подъёма
        самого движка. PipelineEngine лишь хранит emitter+set отправленных
        сигнатур и делегирует.
        """
        from core.engine.event_emission import emit_after_stage
        try:
            emit_after_stage(
                self._event_emitter,
                stage_state.name if hasattr(stage_state, "name") else str(stage_state),
                self.context,
                error_signature_fn=self._error_signature,
                emitted_sigs=self._emitted_error_sigs,
            )
        except Exception as e:  # pragma: no cover
            logger.debug("emit_after_stage упал: %s", e)

    # Состояния, в которых на диске уже лежит применённый, но ещё не
    # разрешённый (validated/decided/rolled back) патч. Прерывать цикл
    # (по таймауту или circuit breaker) внутри этого окна нельзя — иначе
    # файл останется повреждённым навсегда, потому что откат происходит
    # именно в DECIDING/VALIDATING. Инцидент: PROJECT_TIMEOUT срабатывал
    # ровно между APPLYING_PATCH и DECIDING, оставляя pyclean/cli.py и
    # pyclean/debris.py с разорванными строковыми литералами между прогонами.
    _PATCH_RESOLUTION_STATES = frozenset({
        State.VALIDATING, State.REVIEWING, State.DECIDING, State.ROLLING_BACK,
        State.NEEDS_REVIEW, State.FINAL_RESOLVE, State.CLEANUP_ANALYSIS,
    })

    # Плато применённых патчей внутри ОДНОГО цикла (2026-07-08, loguru №7):
    # _single_run завершается только на ACCEPT, поэтому проект, где патчи
    # систематически применяются-и-умирают (~50 BLOCKING-генераций за цикл,
    # каждая ~15-90s: Apply→full-scan→rollback), крутил ПЕРВЫЙ цикл до
    # project_timeout (cycles_run=0, 1873s впустую). После порога цикл мягко
    # завершается: AdaptiveCycleController видит «0 ACCEPT» и глушит прогон
    # своим plateau-детектом, либо следующий цикл стартует с re-prioritize
    # и сдвинутой очередью (processed_errors вырос) — ошибки не теряются.
    MAX_APPLIED_FAILURES_PER_CYCLE = 10

    def _single_run(self, *, _deadline: Optional[float] = None) -> Dict[str, Any]:
        _pending_circuit_open = False
        _applied_without_accept = 0
        _pending_apply_plateau = False
        while self.context.current_state not in {State.COMPLETED, State.FAILED, State.CIRCUIT_OPEN}:
            state = self.context.current_state
            in_resolution = state in self._PATCH_RESOLUTION_STATES

            if not in_resolution:
                if _pending_circuit_open:
                    logger.error("Anti-loop: открываем circuit breaker (текущий патч уже разрешён)")
                    self.context = self.context.add_state_to_history(State.CIRCUIT_OPEN)
                    break
                if _pending_apply_plateau:
                    logger.warning(
                        "Плато цикла: %d применённых патчей без единого ACCEPT — "
                        "мягко завершаем цикл (очередь продолжится со следующего)",
                        _applied_without_accept,
                    )
                    self.context = self.context.add_state_to_history(State.COMPLETED)
                    break
                if _deadline is not None and time.monotonic() >= _deadline:
                    logger.warning("PROJECT_TIMEOUT: внутри _single_run — досрочное завершение")
                    # Явно фиксируем таймаут в metadata, чтобы _build_result
                    # вернул project_timeout_triggered=True и stop_reason="project_timeout"
                    # независимо от того, сработал ли аналогичный флаг во внешнем
                    # _global_fix_loop (outer-loop проверяет бюджет только в начале
                    # итерации, inner-guard — в конце каждого шага внутри _single_run).
                    _tm = dict(self.context.metadata)
                    _tm["project_timeout_triggered"] = True
                    _tm.setdefault("stop_reason_override", "project_timeout")
                    self.context = self.context.update(metadata=_tm)
                    self.context = self.context.add_state_to_history(State.FAILED)
                    break
                if self.circuit_breaker.is_open():
                    logger.error("Circuit breaker open, halting")
                    self.context = self.context.add_state_to_history(State.CIRCUIT_OPEN)
                    break

            step_start = time.time()

            logger.info("▶️ Вход в стадию: %s", state.name)
            self.context.notify(f"▶️ Стадия: {state.name}")

            self.dynamic_params = self.stability_governor.get_params()
            self.context = self.context.update(
                max_iterations=int(self.dynamic_params.get("max_iterations", 10))
            )

            stage = self.stages.get(state)
            if stage is None:
                logger.error("No stage defined for state %s", state)
                self.context = self.context.add_state_to_history(State.FAILED)
                break

            self.context = stage.execute(self.context)
            # Stage L.5+ — пер-ошибочные события (error_dequeued / patch_proposed
            # / verdict / applied|needs_review|failed). Глушит сам себя при сбое.
            self._emit_after_stage(state)

            if self.context.metadata.get(MetadataKeys.LAST_DECISION) == "ACCEPT":
                logger.info("Достигнут ACCEPT — завершаем одиночный прогон для пересканирования")
                break

            if state == State.APPLYING_PATCH and self.context.generated_patch:
                # Плато цикла (см. MAX_APPLIED_FAILURES_PER_CYCLE): ACCEPT
                # завершил бы _single_run break-ом выше — счётчик считает
                # именно применённые-и-НЕ-принятые.
                _applied_without_accept += 1
                if _applied_without_accept >= self.MAX_APPLIED_FAILURES_PER_CYCLE:
                    _pending_apply_plateau = True
                error = self.context.selected_error
                if error:
                    sig = self._error_signature(error)
                    patch_hash = hashlib.sha256(self.context.generated_patch.encode()).hexdigest()
                    file_path = str(self.context.project_path / error.get("file", ""))
                    self.anti_loop.record_attempt(sig, patch_hash, file_path)
                    if self.anti_loop.should_break():
                        logger.error("Anti-loop triggered — circuit откроется после разрешения текущего патча")
                        self.circuit_breaker.record_failure()
                        _pending_circuit_open = True

            self.step_times[state.name] = time.time() - step_start
            self.health_evaluator.update(self.context)
            self.dynamic_params = self.stability_governor.step()
            self.context = self.context.update(
                metadata=dict(self.context.metadata) | {MetadataKeys.DYNAMIC_PARAMS: self.dynamic_params}
            )
            self._save_state()

        final_status = self.context.current_state.name if self.context.current_state in {State.COMPLETED, State.FAILED, State.CIRCUIT_OPEN} else "FIXED"
        logger.info("Одиночный прогон завершён со статусом: %s", final_status)
        return self._build_result(status_override=final_status)

    # -----------------------------------------------------------------
    # Разбитый метод run()
    # -----------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        logger.info("Starting pipeline for %s (%s) dry_run=%s", self.project_path, self.language, self.dry_run)
        self.start_time = time.time()

        # 2026-06-24 (метрики прогресса, расследование 5909 vs 6055 на pytils):
        # независимый fresh full scan на project_path ДО какого-либо изменения
        # файлов — точка истины "before" для independent_scan_delta. В отличие
        # от baseline/initial_errors (который AnalyzeStage.execute() считает на
        # working_path — временной копии из _prepare_environment), этот скан
        # идёт ТЕМ ЖЕ методом (`run_full_scan`), что и финальный пересчёт "after"
        # в _finalize_and_audit — apples-to-apples, без посредников вроде
        # внутренней очереди current_errors, которая инкрементально обновляется
        # по ходу прогона и может расходиться с реальным состоянием диска.
        self._independent_scan_before: Optional[Dict[str, Any]] = None
        try:
            _analyze_stage_pre = self.stages.get(State.ANALYZING)
            self._independent_scan_before = _analyze_stage_pre.run_full_scan(
                self.context, self.project_path,
            )
            logger.info(
                "independent_scan_before: %d ошибок (fresh full scan, project_path)",
                self._independent_scan_before["total"],
            )
        except Exception as e:
            logger.warning("independent_scan_before не удалось посчитать: %s", e)

        # 1. Подготовка изолированной среды
        tmp_dir = self._prepare_environment()
        if tmp_dir is None:
            return {
                "status": "FAILED",
                "error": "Failed to create temporary directory for sandbox",
                "language": self.language,
                "project_path": str(self.project_path),
                "iterations": 0,
                "rollbacks": 0,
                "initial_error_count": 0,
                "final_error_count": 0,
                "baseline_remaining": 0,
                "current_total_errors": 0,
                "accepted_patches": 0,
                "rejected_patches": 0,
                "total_runtime_seconds": time.time() - self.start_time,
                "system_health": {},
                "dynamic_params": {},
                "independent_scan_before": (
                    self._independent_scan_before["total"]
                    if self._independent_scan_before else None
                ),
                "independent_scan_after": None,
                "independent_scan_delta": None,
                "queue_delta": 0,
                "real_fix_impact": None,
                "attempted_errors_count": 0,
                "needs_review_with_patch": 0,
                "needs_review_no_patch_unprocessed": 0,
            }

        # 2026-07-02 (решение Алекса, вариант «в»): dependency recovery — в
        # SANDBOX, не в проекте пользователя. Раньше controller писал
        # requirements.txt/Cargo.toml прямо в project_path ДО sandbox, мимо
        # accept-цикла (контрольная серия 07-02: «# auto-added by Webbles
        # Fix» появился в корне клона tenacity) — вопреки правилу «в проекте
        # пользователя только .webbles_backups/». Теперь правка делается в
        # tmp_dir, изменённые манифесты регистрируются в accepted_patches и
        # доставляются обычным потоком: аудит → копирование с бэкапом.
        try:
            self._recover_dependencies_sandbox(tmp_dir)
        except Exception as e:
            logger.warning("Dependency recovery (sandbox) пропущен: %s", e)

        try:
            # Старт сбора статистики: snapshot ошибок до прогона.
            try:
                self.run_stats = RunStatistics()
                self.run_stats.start(str(self.project_path), self.language)
                self.run_stats.record_initial_errors(list(self.context.current_errors or []))
            except Exception as e:
                logger.debug("RunStatistics start failed: %s", e)
                self.run_stats = None

            # 2. Глобальный цикл исправления
            accepted_patches, rejected_patches = self._global_fix_loop(tmp_dir)

            # 3. Финальное разрешение и аудит (гранулированный)
            result = self._finalize_and_audit(tmp_dir, accepted_patches, rejected_patches)
            return result
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._cleanup_artifacts()
            logger.info(f"Временная копия {tmp_dir} удалена")

    def _prepare_environment(self) -> Optional[Path]:
        """Создаёт временную копию проекта для изолированного выполнения."""
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="webbles_isolated_"))
        except Exception as e:
            logger.error("Не удалось создать временную директорию: %s", e)
            return None

        # Исключаем служебные директории Webbles и стандартные build/venv.
        # .webles_sandbox/.webbles_sandbox содержат копии файлов для IncrementalSandbox —
        # они не должны попадать в temp copy, иначе semgrep сканирует их и
        # возвращает абсолютные sandbox-пути как ошибки (F4).
        _COPYTREE_IGNORE = shutil.ignore_patterns(
            "target", ".git", "__pycache__", ".venv",
            ".webles_sandbox", ".webbles_sandbox",
            ".webbles_fix", ".webbles_backups", ".webbles",
        )
        try:
            shutil.copytree(
                self.project_path, tmp_dir,
                symlinks=True, dirs_exist_ok=True,
                ignore=_COPYTREE_IGNORE,
            )
        except (shutil.Error, OSError) as _copy_err:
            # BUG-8: on Windows, shutil.copy2 fails with [Errno 22] on certain binary
            # files (e.g. woff2 fonts) due to metadata copy failure. Retry without
            # metadata preservation using shutil.copy.
            logger.warning(
                "copytree с copy2 не удался (%s) — повтор с copy (без метаданных)", _copy_err
            )
            shutil.copytree(
                self.project_path, tmp_dir,
                symlinks=True, dirs_exist_ok=True,
                ignore=_COPYTREE_IGNORE,
                copy_function=shutil.copy,
            )
        self.context = self.context.update(working_path=tmp_dir)
        logger.info("Работаем с изолированной копией проекта: %s", tmp_dir)

        self.audit_manager.restore_golden_source()

        if self.language_provider:
            sig_provider = self.language_provider.get_initial_signatures_provider()
            if sig_provider:
                sigs = sig_provider(tmp_dir, self.context)
                if sigs:
                    self.context = self.context.update(
                        metadata=dict(self.context.metadata, **{MetadataKeys.FILE_ERROR_SIGNATURES_BEFORE: sigs})
                    )
        return tmp_dir

    # Манифесты зависимостей per-language — для снапшота до/после recovery
    # (csharp правит *.csproj в произвольных подкаталогах, остальные — файл
    # с фиксированным именем в корне).
    _DEP_MANIFEST_PATTERNS = {
        "python": ["requirements.txt"], "py": ["requirements.txt"],
        "rust": ["Cargo.toml"], "rs": ["Cargo.toml"],
        "cpp": ["CMakeLists.txt"], "c++": ["CMakeLists.txt"],
        "cxx": ["CMakeLists.txt"], "c": ["CMakeLists.txt"],
        "csharp": ["**/*.csproj"], "cs": ["**/*.csproj"],
    }

    def _manifest_snapshot(self, tmp_dir: Path, patterns: list) -> Dict[str, bytes]:
        snap: Dict[str, bytes] = {}
        for pat in patterns:
            if pat.startswith("**/"):
                candidates = tmp_dir.rglob(pat[3:])
            else:
                candidates = [tmp_dir / pat]
            for p in candidates:
                try:
                    if p.is_file():
                        snap[str(p.relative_to(tmp_dir)).replace("\\", "/")] = p.read_bytes()
                except Exception:
                    continue
        return snap

    def _recover_dependencies_sandbox(self, tmp_dir: Path) -> None:
        """Детерминированная починка манифестов зависимостей — В SANDBOX.

        2026-07-02 (решение Алекса, вариант «в»; перенос из
        Controller._recover_dependencies): та же схема infer() → apply →
        recover() с валидацией и откатом из analysis/*_dependency_inference,
        но применённая к tmp_dir. Изменённые/созданные манифесты
        регистрируются через context.add_accepted_patch — дальше они идут
        ОБЫЧНЫМ accept-потоком: финальный аудит (M1 включает accepted-файлы)
        и _copy_passed_files_to_original (копия с бэкапом оригинала в
        .webbles_backups/). Проект пользователя до доставки не трогается.
        Никогда не роняет пайплайн."""
        cfg = self.config.get("pipeline", {}) or {}
        if not cfg.get("dependency_recovery", True):
            return
        lang = (self.language or "").lower()
        patterns = self._DEP_MANIFEST_PATTERNS.get(lang)
        if not patterns:
            return  # язык пока без dep-inference

        before = self._manifest_snapshot(tmp_dir, patterns)

        res = None
        tool = ""
        if lang in ("rust", "rs"):
            from analysis.dependency_inference import DependencyInference
            res = DependencyInference().recover(
                tmp_dir, run_cargo_check=bool(cfg.get("dependency_recovery_cargo_check", True)),
            )
            tool = "Cargo.toml"
        elif lang in ("csharp", "cs"):
            from analysis.csharp_dependency_inference import CsharpDependencyInference
            res = CsharpDependencyInference().recover(
                tmp_dir, run_build_check=bool(cfg.get("dependency_recovery_build_check", True)),
            )
            tool = ".csproj"
        elif lang in ("cpp", "c++", "cxx", "c"):
            from analysis.cpp_dependency_inference import CppDependencyInference
            res = CppDependencyInference().recover(
                tmp_dir, run_cmake_check=bool(cfg.get("dependency_recovery_cmake_check", True)),
            )
            tool = "CMakeLists.txt"
        elif lang in ("python", "py"):
            from analysis.python_dependency_inference import PythonDependencyInference
            res = PythonDependencyInference().recover(
                tmp_dir, run_pip_check=bool(cfg.get("dependency_recovery_pip_check", False)),
            )
            tool = "requirements.txt"

        if res is None:
            return
        for line in getattr(res, "explanations", []) or []:
            logger.info("  dep-recovery: %s", line)
        if not getattr(res, "has_changes", False):
            return

        after = self._manifest_snapshot(tmp_dir, patterns)
        changed = sorted(
            rel for rel in after
            if before.get(rel) != after[rel]
        )
        added = ", ".join(getattr(res, "missing", {}) or {})
        logger.info("Dependency recovery (%s, sandbox): добавлены зависимости — %s; "
                    "изменённые манифесты: %s", tool, added, changed)
        # Codex-2 (2026-07-02): dep-recovery регистрировал манифест в
        # accepted_patches БЕЗ собственной проверки — предполагалось, что
        # инференс только ДОБАВЛЯет строки. Но при баге/некорректном инференсе
        # он мог удалить/переписать существующие зависимости, и такой манифест
        # доехал бы до оригинала обычным accept-потоком без символьной защиты
        # (это не .py — O.14 к нему неприменима). Additive-guard для
        # python-манифеста: непустые строки снапшота ДО обязаны быть
        # ПОДМНОЖЕСТВОМ строк ПОСЛЕ (разрешены только добавления). Если нет —
        # восстанавливаем содержимое из снапшота и НЕ регистрируем (fail-closed).
        # Для не-python манифестов (toml/csproj/cmake) построчная additive-
        # проверка неприменима — формат сложнее (секции, вложенность), их
        # оставляем как есть (constraint: если добавится язык с легко
        # разбираемым построчным манифестом — расширить проверку сюда).
        _is_python_manifest = lang in ("python", "py")
        registered = []
        for rel in changed:
            if _is_python_manifest:
                _before_bytes = before.get(rel)
                _after_bytes = after.get(rel)
                _before_lines = {
                    ln.strip()
                    for ln in (_before_bytes or b"").decode("utf-8", errors="replace").splitlines()
                    if ln.strip()
                }
                _after_lines = {
                    ln.strip()
                    for ln in (_after_bytes or b"").decode("utf-8", errors="replace").splitlines()
                    if ln.strip()
                }
                if not _before_lines.issubset(_after_lines):
                    _removed = sorted(_before_lines - _after_lines)
                    logger.warning(
                        "Dependency recovery (%s): манифест %s НЕ additive "
                        "(исчезли/изменены строки: %s) — восстанавливаем из снапшота, "
                        "не регистрируем", tool, rel, _removed[:10],
                    )
                    try:
                        (tmp_dir / rel).write_bytes(_before_bytes if _before_bytes is not None else b"")
                    except Exception as _re:
                        logger.error(
                            "Dependency recovery: не удалось восстановить %s из снапшота: %s",
                            rel, _re,
                        )
                    continue
            self.context = self.context.add_accepted_patch({
                "error": {
                    "file": rel, "line": 0, "code": "dependency_recovery",
                    "message": f"dep-inference ({tool}): added {added}"[:200],
                    "error_class": "MANIFEST",
                },
                "reason": "dependency_recovery_sandbox",
            })
            registered.append(rel)
        changed = registered
        if self.reporter and changed:
            try:
                self.reporter.send(f"📦 Зависимости ({tool}): добавлено {added}")
            except Exception:
                pass

    def _cleanup_artifacts(self) -> None:
        """Удаляет только по-настоящему временные файлы.

        Память (.webbles_fix_memory.json) и базлайн (.webbles_baseline.json)
        СОХРАНЯЕМ между запусками — это и есть «накопительное знание»
        (MemoryLearning хранит здесь успешные патчи для повторного использования
        в few-shot и rule-based fast-path). Без этого Phase 2/3 теряют смысл.
        """
        # Чистим только реально временные артефакты. state/memory/baseline теперь
        # лежат в `.webbles_fix/` и переживают запуски (накопительное знание).
        artifacts = [
            self.project_path / ".webles_sandbox",
            self.project_path / "target",
            self.project_path / self.LEGACY_STATE_FILE,  # вытираем legacy если ещё остался
        ]
        for path in artifacts:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    logger.debug("Удалена временная директория: %s", path)
                elif path.is_file():
                    path.unlink(missing_ok=True)
                    logger.debug("Удалён временный файл: %s", path)
            except Exception as e:
                logger.debug("Не удалось удалить %s: %s", path, e)

    def _global_fix_loop(self, tmp_dir: Path) -> tuple:
        """Выполняет цикл глобального исправления с адаптивным контроллером.

        AdaptiveCycleController заменяет фиксированный max_global_cycles:
        продолжает циклы пока есть прогресс, останавливается при plateau /
        error_growth / reject_spike / runtime_budget / global_invariant, и
        динамически расширяет лимит для проектов с устойчивым улучшением.
        """
        import time as _time
        from core.adaptive_cycle_controller import AdaptiveCycleController
        from core.stages.syntax_repair_stage import SyntaxRepairStage
        from core.stages.ruff_autofix_stage import RuffAutoFixStage

        # Pre-pipeline: ruff --fix on safe codes.
        ruff_stage = RuffAutoFixStage()
        self.context, _ruff_accepted = ruff_stage.execute(self.context, tmp_dir)

        # Pre-pipeline: collapse cascading E999 → 1 attempt per file.
        syntax_stage = SyntaxRepairStage()
        self.context, syntax_accepted, _syntax_nr = syntax_stage.execute(
            self.context, tmp_dir, getattr(self, "llm_client", None)
        )

        # Начальный счёт для контроллера. РАНЬШЕ брали len(initial_errors), но
        # на этот момент initial_errors ЕЩЁ ПУСТ (начальный AnalyzeStage.execute
        # идёт позже, в FSM-цикле) → контроллер получал initial=0. Следствие
        # (2026-07-10, spdlog): global_invariant (runaway-защита, `if initial>0`)
        # НИКОГДА не срабатывал, а error_growth-guard `count>initial` вырождался
        # в `count>0` (всегда true). Берём уже посчитанный независимый full-scan
        # (_independent_scan_before, строка ~737) — это реальный старт (напр. 21).
        _isb = getattr(self, "_independent_scan_before", None)
        initial_error_count = (
            int(_isb["total"]) if _isb and _isb.get("total") is not None
            else (len(self.context.initial_errors or [])
                  or len(self.context.current_errors or []))
        )
        controller = AdaptiveCycleController(initial_error_count, self.config)
        logger.info("AdaptiveCC initial_error_count=%d (источник=%s)",
                    initial_error_count, "independent_scan" if _isb else "context")
        accepted_patches: list = []
        rejected_patches: list = []

        _loop_start = _time.monotonic()
        _project_budget = float(
            (self.config.get("pipeline") or {}).get("project_timeout", 300)
        )
        _project_timeout_fired = False
        # B-fix: сигнатуры ошибок, уже принятых в предыдущих циклах
        _outer_accepted_sigs: set = set()
        # 2026-07-07 (httpx/bcrypt UNCHANGED-разбор): счётчик срабатываний
        # карваута «ошибка вернулась в current_errors» per-signature. Один
        # возврат — легитимный доразбор (сосед задел файл); второй возврат
        # той же принятой сигнатуры — паттерн «ACCEPT корёжит структуру,
        # ошибка мигает» (bcrypt test_bcrypt.py::370::arg-type: ACCEPT×2 →
        # осцилляция → аудит откатил файл целиком). Со второго возврата
        # карваут не применяется — сигнатура уходит в oscillation-ban сразу,
        # экономя LLM-заход и порчу (ложный бан лучше ложного ACCEPT, §4).
        _returned_sig_counts: dict = {}
        # B-fix: счётчики «до» для взятия только дельты из context-списков
        _ctx_accepted_before: int = 0
        _ctx_rejected_before: int = 0

        while True:
            _elapsed = _time.monotonic() - _loop_start
            if _elapsed >= _project_budget:
                logger.warning(
                    "PROJECT_TIMEOUT: %.0fs elapsed >= budget %.0fs — завершаем",
                    _elapsed, _project_budget,
                )
                _project_timeout_fired = True
                break
            logger.info(
                "=== Глобальный цикл %d (лимит %d) ===",
                controller.current_cycle + 1,
                controller.max_cycles,
            )
            _deadline = _loop_start + _project_budget
            self.context = self.context.update(
                metadata=dict(self.context.metadata, **{
                    MetadataKeys.LAST_DECISION: None,
                    "_project_id": self._patch_recorder_project_id,
                    "_current_global_cycle": controller.current_cycle + 1,
                    # Дедлайн в context.metadata, а не только в локальной
                    # переменной _single_run — внутренние retry-петли стадий
                    # (GeneratePatchStage segmented/blocking strategies, LLM
                    # client) читают его и бросают работу досрочно. Без этого
                    # project_timeout проверяется только МЕЖДУ переходами
                    # состояний в _single_run, а не внутри одного долгого
                    # stage.execute() — см. control series 12 (investdaytip).
                    MetadataKeys.PROJECT_DEADLINE: _deadline,
                })
            )
            # Защитная очистка от прошлого цикла: если _single_run не дошёл до
            # ANALYZING (circuit breaker/FAILED раньше) и ключ остался
            # непотреблённым — не должен просочиться в этот цикл.
            if "_incremental_target_files" in self.context.metadata or "_parallel_touched_files" in self.context.metadata:
                _clean_meta = dict(self.context.metadata)
                _clean_meta.pop("_incremental_target_files", None)
                _clean_meta.pop("_parallel_touched_files", None)
                self.context = self.context.update(metadata=_clean_meta)
            self._e999_triage()
            # 2026-06-25: self.dep_graph строился ОДИН раз в __init__ от
            # self.project_path (снимок ДО первого патча) и никогда не
            # обновлялся — RootCauseStage.cascade_score со временем считался
            # по всё более устаревшей структуре импортов, хотя реальные
            # правки идут в tmp_dir (working_path), а не в project_path.
            # Пересобираем раз в макро-цикл (не на каждый pick ошибки внутри
            # цикла — RootCauseStage вызывается много раз за один цикл,
            # полный rglob+AST на каждый вызов был бы расточительным).
            self.dep_graph.rebuild(tmp_dir)
            self.parallel_executor.execute(self.config, self.project_path)
            # Tech debt audit 2026-06-21, #2: health_evaluator.update() обычно
            # вызывается только внутри _single_run() (после каждой стадии
            # последовательного пути) — патчи, обработанные ParallelExecutor,
            # никогда туда не попадали, оставляя слепое пятно в health-метриках
            # (error_counts/stability не видели изменений, сделанных параллельно).
            # Вызываем явно здесь, ДО того как _parallel_touched_files может
            # быть преобразован в _incremental_target_files ниже — иначе
            # _infer_decision() не успеет увидеть исходный маркер.
            self.health_evaluator.update(self.context)
            # ParallelExecutor (см. core/engine/parallel.py) теперь всегда
            # возвращает current_state=ANALYZING после merge и кладёт список
            # реально изменённых на диске файлов в metadata, если таковые есть.
            # Если incremental_analysis включён — отдаём AnalyzeStage именно
            # этот список (один комбинированный re-scan вместо полного), а не
            # дожидаемся ACCEPT внутри _single_run (которого здесь не будет —
            # ParallelExecutor не вызывает DecideStage и не считается за accept).
            _parallel_touched = self.context.metadata.get("_parallel_touched_files")
            if _parallel_touched and (self.config.get("pipeline") or {}).get("incremental_analysis", False):
                _pim = dict(self.context.metadata)
                _pim["_incremental_target_files"] = list(_parallel_touched)
                _pim.pop("_parallel_touched_files", None)
                self.context = self.context.update(metadata=_pim)
            result = self._single_run(_deadline=_deadline)
            status = result.get("status", "FAILED")

            # B-fix: берём только НОВЫЕ патчи этого цикла (дельта, не весь список)
            _new_accepted = self.context.accepted_patches[_ctx_accepted_before:]
            _new_rejected = self.context.rejected_patches[_ctx_rejected_before:]
            _ctx_accepted_before = len(self.context.accepted_patches)
            _ctx_rejected_before = len(self.context.rejected_patches)

            # B-fix: проверяем дубликаты ДО добавления в local список
            _cycle_sigs: set = set()
            for patch_info in _new_accepted:
                err = patch_info.get("error") or {}
                _sig = f"{err.get('file','')}::{err.get('line',0)}::{err.get('code','')}"
                _cycle_sigs.add(_sig)

            _real_dupes: set = set()
            # Конверсия-4 (2026-07-02): раньше дубликаты искались только когда
            # ВЕСЬ цикл состоял из повторов (`_cycle_sigs.issubset(...)`) —
            # в смешанном цикле (повтор + новый ACCEPT) повторная сигнатура
            # не банилась вовсе. Контрольная серия 07-02 (httpx
            # _models.py::999::attr-defined): ТРИ ACCEPT на одну сигнатуру
            # подряд, каждый следующий перезаписывал предыдущий — два цикла
            # потрачены впустую. Теперь проверка per-signature: сигнатура
            # банится при ПЕРВОМ повторном ACCEPT.
            _repeated = _cycle_sigs & _outer_accepted_sigs
            if _repeated:
                # B-fix v2: если ошибка вернулась в current_errors (другой патч задел файл) —
                # убираем её из _outer_accepted_sigs и не считаем дублем
                _current_sigs = {
                    f"{e.get('file','')}::{e.get('line',0)}::{e.get('code','')}"
                    for e in (self.context.current_errors or [])
                }
                _allowed_returns, _real_dupes = self._filter_returned_sigs(
                    _repeated, _current_sigs, _returned_sig_counts,
                )
                if _allowed_returns:
                    logger.info(
                        "Outer loop: ошибки %s вернулись в current_errors — не дубликаты, продолжаем",
                        _allowed_returns,
                    )
                    _outer_accepted_sigs -= _allowed_returns
            _outer_accepted_sigs |= _cycle_sigs

            for patch_info in _new_accepted:
                accepted_patches.append({
                    "file": patch_info.get("error", {}).get("file", "unknown"),
                    "line": patch_info.get("error", {}).get("line", 0),
                    "message": patch_info.get("error", {}).get("message", "")[:100],
                    "code": patch_info.get("error", {}).get("code", ""),
                })

            # 2026-06-24 (Bluetooth-Devices/dbus-fast benchmark): раньше
            # _real_dupes (ACCEPT той же file::line::code сигнатуры, что уже
            # ОДНАЖДЫ была принята и больше не в current_errors — т.е. два
            # патча подряд взаимно отменили друг друга на одной строке)
            # останавливал ВЕСЬ прогон ("break"). Одна осциллирующая ошибка
            # глушила оставшиеся сотни/тысячи валидных ошибок проекта.
            # Теперь — баним ТОЛЬКО эту сигнатуру (PrioritizeStage больше не
            # выберет её в этом прогоне), помечаем затронутые accepted_patches
            # как cancelled (исключаются из real_fix_impact — см.
            # compute_progress_metrics), и продолжаем со следующими ошибками.
            if _real_dupes:
                self._ban_oscillating_signatures(_real_dupes, tmp_dir, accepted_patches)
            for patch_info in _new_rejected:
                rejected_patches.append({
                    "file": patch_info.get("error", {}).get("file", "unknown"),
                    "message": patch_info.get("error", {}).get("message", "")[:100],
                    "code": patch_info.get("error", {}).get("code", ""),
                })

            file_counts: dict = {}
            for acc in accepted_patches:
                file_counts[acc["file"]] = file_counts.get(acc["file"], 0) + 1
            if any(c >= self.MAX_PATCHES_PER_FILE for c in file_counts.values()):
                logger.warning("Достигнут лимит правок для файлов")
                break

            # Terminal pipeline failures short-circuit the adaptive check.
            if status in ("FAILED", "CIRCUIT_OPEN"):
                logger.info("Цикл завершён с %s, останавливаемся", status)
                break

            # COMPLETED with all remaining errors invalid — nothing more to do.
            if status == "COMPLETED" and self.context.current_errors and all(
                not ErrorContextValidator.is_valid(e, self.context.project_path)
                for e in self.context.current_errors
            ):
                logger.info("Все оставшиеся ошибки невалидны — завершаем глобальные циклы")
                break

            current_error_count = len(self.context.current_errors or [])
            total_accepted = len(accepted_patches)
            total_rejected = len(rejected_patches)
            total_nr = int((self.context.metadata or {}).get("needs_review_count", 0) or 0)

            # Диагностика error_growth (2026-07-10): разбивка счётчика,
            # который кормит AdaptiveCC, по источникам — видно осцилляцию,
            # когда ValidateStage пере-скан меняет состав current_errors
            # (напр. primary-only скан временно вытесняет semgrep/security).
            try:
                _by_src: dict = {}
                for _e in (self.context.current_errors or []):
                    _et = _e.get("error_type") or "?"
                    _by_src[_et] = _by_src.get(_et, 0) + 1
                logger.info(
                    "AdaptiveCC feed: cycle_error_count=%d (acc=%d rej=%d nr=%d) breakdown=%s",
                    current_error_count, total_accepted, total_rejected, total_nr, _by_src,
                )
            except Exception:
                pass

            # LLM-недоступность (2026-07-10): если провайдер накопил порог
            # подряд идущих transport_error/empty (DeepSeek лежит/рвёт связь) —
            # прекращаем макро-циклы с ЧЕСТНОЙ причиной вместо выжигания всего
            # project_timeout впустую (tinyxml2: 30 мин, 272 empty, 0 delivery,
            # маскировалось под project_timeout). Отличает инфра-сбой LLM от
            # реального «нечего чинить».
            _pool = getattr(self, "llm_client_pool", None) or [getattr(self, "llm_client", None)]
            if any(getattr(c, "is_unresponsive", lambda: False)() for c in _pool if c is not None):
                controller.stop_reason = "llm_unavailable"
                logger.warning(
                    "PipelineEngine: LLM недоступен (порог подряд-неответов) — "
                    "останавливаем прогон с причиной llm_unavailable"
                )
                break

            if not controller.should_continue(
                current_error_count, total_accepted, total_rejected, total_nr
            ):
                logger.info(
                    "AdaptiveCycleController: стоп, причина=%s", controller.stop_reason
                )
                break

            # Инкрементальный анализ (pipeline.incremental_analysis, default
            # False): вместо полного пересканирования всего проекта на каждый
            # принятый патч — AnalyzeStage пересканирует ТОЛЬКО файл только что
            # принятого патча, замещая в current_errors лишь его записи (см.
            # AnalyzeStage._execute_incremental, PipelineContext.merge_file_errors).
            # Полный скан остаётся: в начале прогона (этот флаг тут ещё не
            # установлен на первой итерации) и в конце (_finalize_and_audit
            # делает отдельный полный self.analyzer.analyze() для финального
            # отчёта, не через эту стадию).
            _incremental_meta = dict(self.context.metadata)
            # Safety-триггер: переименование/удаление публичного символа может
            # сломать ИМПОРТЫ/ссылки в ДРУГИХ файлах, которые partial-rescan
            # одного файла никогда не увидит (см. отчёт исследования —
            # межфайловые зависимости — главный риск инкрементального режима).
            # symbol_regression/symbol_duplication уже детектируются ValidateStage
            # для каждого патча — если сработали, форсируем полный скан вместо
            # инкрементального именно на этом цикле.
            _risky_patch = bool(
                self.context.metadata.get("symbol_regression")
                or self.context.metadata.get("symbol_duplication")
            )
            # Мастер-переключатель: Python — под флагом (default False); C/C++ —
            # ВКЛЮЧЁН ПО УМОЛЧАНИЮ (перф-хвост полного скана), симметрично
            # ValidateStage._incremental_enabled_for. Для C/C++ дополнительно
            # требуем, чтобы принятый файл был единицей трансляции (.cc/.cpp/.c):
            # ACCEPT на заголовке мог сломать включающие его TU — их partial-
            # rescan одного заголовка не увидит, нужен полный скан (fail-closed).
            _pcfg = (self.config.get("pipeline") or {})
            _lang = (self.language or "").lower()
            _cpp_langs = ("cpp", "c++", "cxx", "cc", "c")
            _tu_suf = (".cpp", ".cxx", ".cc", ".c")
            if _lang in _cpp_langs:
                _incr_on = bool(_pcfg.get("incremental_analysis", True))
            else:
                _incr_on = bool(_pcfg.get("incremental_analysis", False))
            _target_file = (
                _new_accepted[-1].get("error", {}).get("file", "") if _new_accepted else ""
            )
            _cpp_tu_ok = (_lang not in _cpp_langs) or (
                _target_file and _target_file.lower().endswith(_tu_suf)
            )
            if (
                _incr_on and _new_accepted and not _risky_patch
                and _target_file and _cpp_tu_ok
            ):
                _incremental_meta["_incremental_target_file"] = _target_file
            else:
                _incremental_meta.pop("_incremental_target_file", None)

            self.context = (
                self.context
                .update(metadata=_incremental_meta)
                .set_selected_error(None)
                .set_patch(None)
                .add_state_to_history(State.ANALYZING)
            )

        ctrl_summary = controller.summary()
        _loop_meta: dict = dict(self.context.metadata)
        _loop_meta["adaptive_cycle_summary"] = ctrl_summary
        if _project_timeout_fired:
            _loop_meta["project_timeout_triggered"] = True
            _loop_meta.setdefault("stop_reason_override", "project_timeout")
        # Collect LLM hard-timeout count from client (if wired).
        # 2026-06-24 (живой бенчмарк dbus-fast с пулом ключей): раньше
        # считался ТОЛЬКО self.llm_client — при parallel_workers>1 с
        # WEBBLES_LLM_API_KEY_POOL большинство таймаутов происходит на
        # ДРУГИХ клиентах пула (round-robin по файлам), и метрика молча
        # показывала 0 при реальных 29 hard timeout + 159 fail-fast после
        # срабатывания circuit breaker на одном из клиентов пула. Суммируем
        # по всем клиентам пула (self.llm_client всегда pool[0] — см.
        # build_llm_client_pool, без дублирования).
        _llm_pool = getattr(self, "llm_client_pool", None) or [getattr(self, "llm_client", None)]
        _loop_meta["llm_timeout_count"] = sum(
            int(getattr(c, "hard_timeout_count", 0) or 0) for c in _llm_pool if c is not None
        )
        # 2026-07-07 (фаза Performance): суммарные исходящие LLM-вызовы —
        # главная метрика фазы; та же pool-логика, что и у timeout-счётчика.
        _loop_meta["llm_call_count"] = sum(
            int(getattr(c, "llm_call_count", 0) or 0) for c in _llm_pool if c is not None
        )
        self.context = self.context.update(metadata=_loop_meta)
        logger.info("AdaptiveCycleController итог: %s", ctrl_summary)

        return accepted_patches, rejected_patches

    def _net_change_bytes(self, tmp_dir: Path, rel_file: str) -> Optional[int]:
        """Сравнивает текущее содержимое файла (в tmp_dir) с `git show HEAD`
        в project_path (репозиторий есть только там — tmp_dir копируется без
        .git, см. _prepare_environment). Возвращает разницу в байтах (0 —
        длины совпали; используется только как индикатор, точное равенство
        проверяет вызывающий код отдельно) или None, если сравнение
        невозможно (не git-репозиторий, файл не отслеживается и т.п.)."""
        try:
            fp = tmp_dir / rel_file
            current = fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""
        except Exception:
            return None
        try:
            posix_rel = str(rel_file).replace("\\", "/")
            proc = subprocess.run(
                ["git", "show", f"HEAD:{posix_rel}"],
                cwd=str(self.project_path), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
            )
            if proc.returncode != 0:
                return None
            original = proc.stdout
        except Exception:
            return None
        return len(current) - len(original)

    @staticmethod
    def _filter_returned_sigs(
        repeated: set, current_sigs: set, returned_sig_counts: dict,
    ) -> tuple:
        """Делит повторно-принятые сигнатуры на «легитимный доразбор» и
        осцилляционные дубли. Мутирует returned_sig_counts (счётчик живёт
        у вызывающего на весь прогон).

        2026-07-07 (httpx/bcrypt UNCHANGED-разбор): карваут «ошибка вернулась
        в current_errors → не дубль» был безусловным, и паттерн «ACCEPT
        корёжит структуру, ошибка мигает» гонял одну сигнатуру по кругу
        (bcrypt test_bcrypt.py::370::arg-type: ACCEPT×2 → осцилляция →
        аудит откатил файл целиком; httpx _models.py::999::attr-defined
        02.07: ACCEPT×3). Теперь карваут одноразовый per-signature: первый
        возврат — легитимен (сосед задел файл), со второго сигнатура идёт
        в oscillation-ban без нового LLM-захода (ложный бан лучше ложного
        ACCEPT, §4).

        Возвращает (allowed_returns, real_dupes): allowed_returns вычесть
        из _outer_accepted_sigs (разбан), real_dupes — в
        _ban_oscillating_signatures."""
        returned = repeated & current_sigs
        allowed_returns = set()
        for sig in returned:
            returned_sig_counts[sig] = returned_sig_counts.get(sig, 0) + 1
            if returned_sig_counts[sig] <= 1:
                allowed_returns.add(sig)
            else:
                logger.warning(
                    "Outer loop: сигнатура %s вернулась в current_errors "
                    "ПОВТОРНО после re-ACCEPT (возврат №%d) — карваут "
                    "исчерпан, уходит в oscillation-ban",
                    sig, returned_sig_counts[sig],
                )
        return allowed_returns, repeated - allowed_returns

    def _ban_oscillating_signatures(self, sigs: set, tmp_dir: Path, accepted_patches: list) -> None:
        """Сигнатура (file::line::code) осциллирует: цикл N принял патч,
        цикл N+k принял ДРУГОЙ патч на ТОЙ ЖЕ строке/коде, который убрал её
        из current_errors уже во второй раз — обычно значит, что второй
        патч откатил первый (см. Bluetooth-Devices/dbus-fast: обёртка
        BytesIO в BufferedRWPair, затем следующий патч развернул её обратно
        — итоговый файл побайтово идентичен исходному, несмотря на 2 ACCEPT).

        Раньше это останавливало ВЕСЬ прогон. Теперь: баним сигнатуру для
        этого прогона (PrioritizeStage отфильтрует её из current_errors) и
        продолжаем со следующими ошибками вместо остановки всего проекта.

        2026-06-24, второй прогон Bluetooth-Devices/dbus-fast (после первой
        версии этого фикса): дубликат-сигнатура срабатывает и тогда, когда
        ВТОРОЙ патч НЕ откатывает первый, а решает ДРУГУЮ проблему, которая
        случайно легла на тот же file::line::code (например, две разные
        arg-type ошибки на соседних строках теста после сдвига кода первым
        патчем). verify_accepts.py на этом прогоне подтвердил: все 16 ACCEPT
        были REAL_FIX strong, хотя 3 сигнатуры сработали как "дубликат".
        net_change_bytes у них был 8/8/39 — НЕ 0, то есть файл реально
        изменился, это не откат. Поэтому accepted_patches помечаются как
        oscillation_cancelled (и исключаются из real_fix_impact) ТОЛЬКО при
        net_change_bytes == 0 — то есть когда у нас есть прямое доказательство
        отката, а не просто структурное совпадение сигнатур."""
        meta = dict(self.context.metadata)
        banned = set(meta.get("_oscillating_signatures", []) or [])
        cancelled_items = list(meta.get("accept_cancelled_items", []) or [])
        ambiguous_items = list(meta.get("oscillation_ambiguous_items", []) or [])

        for sig in sigs:
            parts = sig.split("::", 2)
            file_part = parts[0] if len(parts) > 0 else ""
            line_part = parts[1] if len(parts) > 1 else "0"
            code_part = parts[2] if len(parts) > 2 else ""
            try:
                line_num = int(line_part)
            except ValueError:
                line_num = 0

            net_change = self._net_change_bytes(tmp_dir, file_part)
            is_confirmed_revert = net_change == 0
            banned.add(sig)

            if is_confirmed_revert:
                logger.warning(
                    "OSCILLATION: %s — два ACCEPT взаимно отменили друг друга "
                    "(net_change_bytes=0, файл байт-в-байт идентичен исходному) "
                    "— баним сигнатуру для этого прогона, продолжаем со "
                    "следующими ошибками вместо остановки всего проекта",
                    sig,
                )
                cancelled_items.append({
                    "file": file_part, "line": line_num, "code": code_part,
                    "oscillation_reason": "duplicate_signature_after_revert",
                    "net_change_bytes": net_change,
                })
                nr_reason = "oscillation_cancelled"
            else:
                logger.warning(
                    "OSCILLATION (неоднозначно): %s — повторная сигнатура, "
                    "но net_change_bytes=%s (не 0) — файл реально изменился, "
                    "это НЕ подтверждённый откат. Баним сигнатуру на будущее "
                    "(защита от возможного зацикливания), но НЕ исключаем уже "
                    "принятые патчи из real_fix_impact — нет доказательств,"
                    " что они отменяют друг друга",
                    sig, net_change,
                )
                ambiguous_items.append({
                    "file": file_part, "line": line_num, "code": code_part,
                    "oscillation_reason": "duplicate_signature_unconfirmed",
                    "net_change_bytes": net_change,
                })
                nr_reason = "oscillation_ambiguous_duplicate"
                for ap in accepted_patches:
                    ap_sig = f"{ap.get('file', '')}::{ap.get('line', 0)}::{ap.get('code', '')}"
                    if ap_sig == sig:
                        ap["oscillation_ambiguous"] = True

            try:
                from analysis.run_statistics import append_decision_log
                # Конверсия-4 (2026-07-02): раньше логировалось как
                # NEEDS_REVIEW — но NR-item при этом НЕ создавался
                # (NeedsReviewStage здесь не вызывается), и
                # _check_decision_integrity гарантированно расходился
                # (контрольная серия 07-02, httpx: decisions[]=13 vs
                # items=12). Отдельный тип решения сохраняет аудит-след,
                # не ломая сверку ACCEPT/REJECT/NEEDS_REVIEW.
                append_decision_log(
                    meta, {"file": file_part, "line": line_num, "code": code_part},
                    "OSCILLATION_BAN", nr_reason,
                )
            except Exception as e:
                logger.debug("append_decision_log (oscillation) упал: %s", e)

            if is_confirmed_revert:
                for ap in accepted_patches:
                    ap_sig = f"{ap.get('file', '')}::{ap.get('line', 0)}::{ap.get('code', '')}"
                    if ap_sig == sig:
                        ap["oscillation_cancelled"] = True

        meta["_oscillating_signatures"] = sorted(banned)
        meta["accept_cancelled_items"] = cancelled_items
        meta["accept_cancelled_count"] = len(cancelled_items)
        meta["oscillation_ambiguous_items"] = ambiguous_items
        self.context = self.context.update(metadata=meta)

        # Убираем забаненные сигнатуры из текущей очереди немедленно — иначе
        # они могут быть выбраны ещё раз в этом же цикле до следующего
        # PrioritizeStage (который дальше фильтрует по _oscillating_signatures
        # на каждый последующий цикл).
        if self.context.current_errors:
            _filtered = [
                e for e in self.context.current_errors
                if f"{e.get('file', '')}::{e.get('line', 0)}::{e.get('code', '')}" not in banned
            ]
            if len(_filtered) != len(self.context.current_errors):
                self.context = self.context.update(current_errors=tuple(_filtered))

    def _finalize_and_audit(self, tmp_dir: Path, accepted_patches: list, rejected_patches: list) -> Dict[str, Any]:
        """Завершает работу: финальное разрешение, гранулированный аудит и копирование в оригинал."""
        if self.config.get("pipeline", {}).get("final_resolve", True):
            self.context = self.stages[State.FINAL_RESOLVE].execute(self.context)

        audit_result = self.audit_manager.audit_run(self.context)

        # Всегда собираем оставшиеся ошибки
        remaining_errors = [
            {"file": e.get("file", ""), "line": e.get("line", 0),
             "code": e.get("code", ""), "message": e.get("message", "")}
            for e in self.context.current_errors
        ]

        result = self._build_result(status_override=self.context.current_state.name)
        # D.5: также прокидываем счётчик очереди ручного просмотра в API-ответ,
        # чтобы вызывающая сторона могла отобразить «K на ручной просмотр»
        # без копания в metadata.
        nr_items = list(self.context.metadata.get("needs_review_items", []) or [])
        result.update({
            "remaining_errors": remaining_errors,
            "unfixable_errors": [
                {"file": e.get("file", ""), "line": e.get("line", 0),
                 "code": e.get("code", ""), "message": e.get("message", "")}
                for e in self.context.unfixable_errors
            ],
            "needs_review_count": int(self.context.metadata.get("needs_review_count", 0) or 0),
            "needs_review_items": nr_items,
            "unsupported_python_version_count": int(
                self.context.metadata.get("unsupported_python_version_count", 0) or 0
            ),
            "unsupported_python_version_items": list(
                self.context.metadata.get("unsupported_python_version_items", []) or []
            ),
            # empty_response диагностика (2026-06-22) — инфраструктурные
            # сбои LLM (timeout/transport_error/empty/bad_format/parser_fail),
            # исключённые из rejected_patches — см. GeneratePatchStage._record_llm_failure.
            "llm_infra_failure_count": int(
                self.context.metadata.get("llm_infra_failure_count", 0) or 0
            ),
            "llm_infra_failure_items": list(
                self.context.metadata.get("llm_infra_failure_items", []) or []
            ),
        })

        # Гранулированный аудит: восстанавливаем только проблемные сегменты
        restore_status: Dict[str, bool] = {}
        if not audit_result.get("audit_ok"):
            failed_segments = audit_result.get("failed_segments", {})
            if failed_segments:
                logger.info("Частичный провал аудита: восстанавливаем проблемные сегменты")
                # C3 (аудит 2026-07-01): статус restore теперь проверяется —
                # файл с проваленным откатом не может быть скопирован в
                # оригинал ни одним из путей ниже.
                restore_status = self.audit_manager.restore_failed_segments(tmp_dir, failed_segments)
                _restore_failed = sorted(f for f, ok in restore_status.items() if not ok)
                if _restore_failed:
                    logger.error(
                        "Restore провален для %d файла(ов): %s — они исключены из доставки",
                        len(_restore_failed), _restore_failed,
                    )
                result["restore_failed_files"] = _restore_failed
            else:
                logger.warning("Аудит провален, но failed_segments пуст — смотрим passed_files")
        else:
            logger.info("Аудит пройден полностью")
        result.setdefault("restore_failed_files", [])
        result["missing_in_sandbox"] = list(audit_result.get("missing_in_sandbox", []) or [])
        # Codex-1 (2026-07-02): файлы с проваленным REJECT/NR-откатом (audit_run
        # принудительно вернул их в failed_segments) — прокидываем в результат.
        result["rollback_failed"] = list(audit_result.get("rollback_failed", []) or [])

        # Честный учёт ACCEPT vs финальный аудит (2026-07-02, deep-reasoner,
        # pyca/bcrypt): файл, ПОЛНОСТЬЮ откачённый аудитом, не доставляется, но
        # его ACCEPT-патчи оставались «живыми» в accepted_patches — прогон
        # отчитывался «принято N», хотя файл байт-в-байт равен оригиналу
        # (verify_accepts: UNCHANGED_FILE). Помечаем такие патчи audit_reverted
        # (по образцу oscillation_cancelled) ДО метрик/отчёта. Это НЕ ослабляет
        # guard: доставка не меняется, откат остаётся — правится только учёт.
        self._mark_audit_reverted_accepts(accepted_patches, audit_result)

        # Копируем успешные файлы в оригинал (даже при частичном провале)
        self._copy_passed_files_to_original(tmp_dir, audit_result, restore_status)

        # baseline_remaining (= старое final_error_count) считает только ошибки
        # ИЗ исходного baseline-набора, которые остались незакрытыми — он не видит
        # новые ошибки вне этого набора (другие файлы/строки, задетые патчами,
        # де-маскированные ошибки и т.п.), поэтому может разойтись со
        # самостоятельным прогоном линтера.
        #
        # current_total_errors — независимый свежий пересчёт по финальному коду
        # на диске (после копирования в оригинал). До 2026-06-23 это был ТОЛЬКО
        # `self.analyzer.analyze()` (flake8-only) — но baseline/initial_errors
        # считается AnalyzeStage'ем через flake8+semgrep(+bandit/mypy/ruff если
        # включены) — разный скоуп инструментов делал current_total_errors
        # структурно ниже baseline везде, где semgrep что-то находил, и это
        # выглядело как "ошибки исчезли без единого ACCEPT" (контрольная серия
        # 2026-06-22, errors_removed-расследование: archinfo/dodola/roomba —
        # 43/21/1 "исчезнувших" ошибки на самом деле были semgrep-находками,
        # невидимыми старому flake8-only пересчёту). Теперь финальный пересчёт
        # идёт через ТОТ ЖЕ `AnalyzeStage._collect_multi_tool_errors`, что и
        # baseline — apples-to-apples. `flake8_current_errors`/
        # `semgrep_current_errors`/`bandit_current_errors` — разбивка по
        # инструментам (сырые находки, до dedup/cascade/version-filter, как и
        # раньше у current_total_errors); `total_current_errors` — то же самое
        # число, что и `current_total_errors` (алиас), после полной
        # пост-обработки — явный, несокращаемый сосед, чтобы поле с узким
        # flake8-only смыслом больше нельзя было перепутать с полным скоупом.
        result["baseline_remaining"] = result["final_error_count"]
        try:
            _analyze_stage = self.stages.get(State.ANALYZING)
            _scan = _analyze_stage.run_full_scan(self.context, self.project_path)
            _breakdown = _scan["breakdown"]
            result["total_current_errors"] = _scan["total"]
            result["current_total_errors"] = _scan["total"]
            result["flake8_current_errors"] = _breakdown.get("flake8", 0)
            result["semgrep_current_errors"] = _breakdown.get("semgrep", 0)
            result["bandit_current_errors"] = _breakdown.get("bandit", 0)
            result["current_errors_breakdown"] = _breakdown
        except Exception as e:
            logger.warning("Не удалось выполнить финальный пересчёт ошибок: %s", e)
            result["current_total_errors"] = None
            result["total_current_errors"] = None
            result["flake8_current_errors"] = None
            result["semgrep_current_errors"] = None
            result["bandit_current_errors"] = None
            result["current_errors_breakdown"] = {}
            _scan = None

        # 2026-06-24 (метрики прогресса): точка истины для "сколько реально
        # исправлено" — fresh full scan before/after (project_path, тот же
        # `run_full_scan`), а НЕ baseline_remaining (внутренняя инкрементальная
        # модель очереди current_errors — обновляется по ходу прогона per-file
        # и может разойтись с реальным состоянием диска; расследование на
        # pytils показало разрыв ~1187 ошибок между ними при 5 ACCEPT). См.
        # compute_progress_metrics() — чистая функция, юнит-тесты в
        # tests/test_phase_progress_metrics.py.
        _scan_before = getattr(self, "_independent_scan_before", None)
        result.update(compute_progress_metrics(
            scan_before=_scan_before,
            scan_after=_scan,
            accepted_patches=accepted_patches,
            rejected_patches=rejected_patches,
            needs_review_meta=self.context.metadata or {},
            initial_error_count=result["initial_error_count"],
            baseline_remaining=result["baseline_remaining"],
            total_current_errors=result.get("total_current_errors"),
        ))

        if self.reporter:
            self._send_report(accepted_patches, rejected_patches, audit_result,
                              remaining_errors, result)

        # Финальная статистика → `<project>/statistic/`. Записываем здесь
        # (после `_copy_passed_files_to_original`), потому что:
        #  - именно тут известны audit_result + accepted/rejected + needs_review,
        #  - оригинал-проект уже обновлён, и `statistic/` ляжет в актуальную картину.
        try:
            stats = getattr(self, "run_stats", None)
            if stats is not None:
                if self.context.initial_errors:
                    stats.record_initial_errors(list(self.context.initial_errors))
                _rf_meta = self.context.metadata or {}
                stats.record_final(
                    final_error_count=len(self.context.current_errors or []),
                    accepted_patches=accepted_patches,
                    rejected_patches=rejected_patches,
                    needs_review_items=list(_rf_meta.get("needs_review_items", []) or []),
                    audit_result=audit_result or {},
                    iterations=int(self.context.iteration_count or 0),
                    rollbacks=int(self.context.rollback_count or 0),
                    llm_timeout_count=int(_rf_meta.get("llm_timeout_count", 0)),
                    project_timeout_triggered=bool(_rf_meta.get("project_timeout_triggered", False)),
                    stop_reason=str(_rf_meta.get("stop_reason_override", "") or ""),
                    net_delta_rollback_count=int(_rf_meta.get("net_delta_rollback_count", 0)),
                    net_delta_uncertain_count=int(_rf_meta.get("net_delta_uncertain_count", 0)),
                    ruff_autofix_total_fixed=int(_rf_meta.get("ruff_autofix_total_fixed", 0)),
                    syntax_reconstruction_attempts=int(_rf_meta.get("syntax_reconstruction_attempts", 0)),
                    syntax_reconstruction_failed_count=int(_rf_meta.get("syntax_reconstruction_failed_count", 0)),
                    unsupported_python_version_count=int(_rf_meta.get("unsupported_python_version_count", 0)),
                    unsupported_python_version_items=list(_rf_meta.get("unsupported_python_version_items", []) or []),
                    llm_infra_failure_count=int(_rf_meta.get("llm_infra_failure_count", 0)),
                    llm_infra_failure_items=list(_rf_meta.get("llm_infra_failure_items", []) or []),
                    # bcrypt 2026-07-03: заголовок отчёта должен строиться от
                    # независимого full scan (тот же источник, что и
                    # compute_progress_metrics), а не от internal-очереди
                    # current_errors — см. комментарий в RunStatistics.record_final.
                    independent_scan_before=(_scan_before["total"] if _scan_before else None),
                    independent_scan_after=result.get("total_current_errors"),
                )
                # Журнал решений: DecideStage кладёт записи в metadata["decisions"].
                # Здесь переносим их в stats как DecisionRecord (без потерь).
                decisions_raw = list(self.context.metadata.get("decisions", []) or [])
                _decisions_merge_dropped = 0
                for rec in decisions_raw:
                    if isinstance(rec, dict):
                        # Поддержка двух форматов: «уже dataclass-словарь»
                        # (file/line/code/decision/...) и «{error, decision, reason}».
                        if "decision" in rec and "file" in rec:
                            try:
                                from analysis.run_statistics import DecisionRecord
                                stats.decisions.append(DecisionRecord(**{
                                    k: v for k, v in rec.items()
                                    if k in DecisionRecord.__dataclass_fields__
                                }))
                            except Exception as _e_rec:
                                _decisions_merge_dropped += 1
                                logger.warning(
                                    "decisions[] merge: запись потеряна (%r): %s", rec, _e_rec,
                                )
                        else:
                            err = rec.get("error") or {}
                            stats.decisions.append(
                                decision_from_metadata(
                                    err, rec.get("metadata") or {},
                                    rec.get("decision", ""),
                                    rec.get("reason", ""),
                                )
                            )

                # Control series 2026-06-21, находка #1 — встроенная сверка
                # целостности decisions[] vs независимых счётчиков
                # (accepted_patches/rejected_patches/needs_review_items).
                # Логирует на WARNING (видно в стандартном выводе run_agent.py
                # БЕЗ принудительной настройки логирования — через lastResort
                # handler) и сохраняет диагностику в notes, чтобы расхождение
                # было видно в КАЖДОМ будущем прогоне без ручной диагностики.
                self._check_decision_integrity(
                    stats, accepted_patches, rejected_patches,
                    list(_rf_meta.get("needs_review_items", []) or []),
                    _decisions_merge_dropped,
                )

                stats.write_to_disk(str(self.project_path))
                logger.info("Статистика прогона записана в %s",
                            RunStatistics.statistics_dir(self.project_path))
        except Exception as e:
            logger.warning("RunStatistics write failed: %s", e)

        return result

    def _check_decision_integrity(
        self, stats, accepted_patches: list, rejected_patches: list,
        needs_review_items: list, decisions_merge_dropped: int,
    ) -> None:
        """Сверка decisions[] vs независимых счётчиков (control series
        2026-06-21, находка #1).

        Три величины должны соответствовать друг другу:
        - len(accepted_patches)       — счётчик из context.add_accepted_patch
        - len(rejected_patches)       — счётчик из context.add_rejected_patch
        - len(needs_review_items)     — счётчик из NeedsReviewStage.execute()
        против decisions[] (granular audit-лог из append_decision_log).

        ACCEPT и NEEDS_REVIEW должны совпадать ТОЧНО — известных bypass-путей
        для них не осталось после фикса находки #1. REJECT может быть МЕНЬШЕ
        в decisions[], чем в rejected_patches — известный, неисправленный в
        этой сессии путь: `GeneratePatchStage`'s `invalid_context`-reject
        (core/stages/generate_patch_stage.py) вызывает `add_rejected_patch`
        напрямую, минуя append_decision_log, ДО того как патч даже
        сгенерирован — не находка #1, отдельный (известный, но не
        приоритетный) гэп. Любое ДРУГОЕ расхождение — неожиданное, логируем
        на WARNING с полной диагностикой.
        """
        try:
            decisions = list(getattr(stats, "decisions", []) or [])
            by_decision: Dict[str, int] = {}
            for d in decisions:
                key = str(getattr(d, "decision", "") or "")
                by_decision[key] = by_decision.get(key, 0) + 1

            n_accept_dec = by_decision.get("ACCEPT", 0)
            n_reject_dec = by_decision.get("REJECT", 0)
            n_nr_dec = by_decision.get("NEEDS_REVIEW", 0)
            n_accept_exp = len(accepted_patches or [])
            n_reject_exp = len(rejected_patches or [])
            n_nr_exp = len(needs_review_items or [])

            n_invalid_context = sum(
                1 for r in (rejected_patches or [])
                if isinstance(r, dict) and r.get("reason") == "invalid_context"
            )
            reject_gap = n_reject_exp - n_reject_dec
            reject_gap_explained = max(0, reject_gap) <= n_invalid_context

            issues = []
            if n_accept_dec != n_accept_exp:
                issues.append(
                    f"ACCEPT: decisions[]={n_accept_dec} != accepted_patches={n_accept_exp}"
                )
            if n_nr_dec != n_nr_exp:
                issues.append(
                    f"NEEDS_REVIEW: decisions[]={n_nr_dec} != needs_review_items={n_nr_exp}"
                )
            if reject_gap < 0 or not reject_gap_explained:
                issues.append(
                    f"REJECT: decisions[]={n_reject_dec} vs rejected_patches={n_reject_exp} "
                    f"(gap={reject_gap}, invalid_context-известных={n_invalid_context}) — "
                    f"необъяснённое расхождение"
                )
            if decisions_merge_dropped:
                issues.append(
                    f"{decisions_merge_dropped} запись(ей) потеряна при merge в "
                    f"_finalize_and_audit (см. WARNING выше в логе)"
                )

            integrity = {
                "accept_decisions": n_accept_dec, "accept_expected": n_accept_exp,
                "reject_decisions": n_reject_dec, "reject_expected": n_reject_exp,
                "reject_gap_explained_by_invalid_context": n_invalid_context,
                "needs_review_decisions": n_nr_dec, "needs_review_expected": n_nr_exp,
                "decisions_merge_dropped": decisions_merge_dropped,
                "ok": not issues,
                "issues": issues,
            }
            # M5 (аудит 2026-07-01): через update, а не прямую мутацию —
            # на MappingProxyType прямое присваивание кидало TypeError
            # (глотался общим except ниже, диагностика терялась), на dict —
            # мутировало разделяемый со старыми контекстами объект.
            self.context = self.context.update(
                metadata=dict(self.context.metadata) | {"_decision_integrity": integrity}
            )
            if issues:
                logger.warning(
                    "DECISION INTEGRITY MISMATCH: %s | accepted_patches=%r "
                    "rejected_patches=%r needs_review_items=%r decisions=%r",
                    "; ".join(issues), accepted_patches, rejected_patches,
                    needs_review_items, decisions,
                )
                stats.add_note(f"decision_integrity_mismatch: {'; '.join(issues)}")
            else:
                logger.info("Decision integrity OK: %s", integrity)
        except Exception as e:
            logger.warning("_check_decision_integrity сам упал: %s", e)

    @staticmethod
    def _norm_deliver_key(path) -> str:
        """Ключ для сравнения путей доставки между собой.

        Codex-4 (2026-07-02): раньше сравнение шло только через
        replace("\\","/") — на регистронезависимой ФС (Windows/macOS default)
        один физический файл мог фигурировать под РАЗНЫМ регистром в
        failed_segments и в accepted_patches ("Foo.py" vs "foo.py"), из-за чего
        отклонённый аудитом файл не исключался из accepted-fallback и уезжал в
        оригинал. os.path.normcase лоуэркейсит на Windows (и меняет прямые
        слэши на обратные), поэтому после него ещё раз нормализуем слэши.
        На POSIX normcase — тождество (регистр значим), поведение не меняется."""
        return os.path.normcase(str(path)).replace("\\", "/")

    def _mark_audit_reverted_accepts(self, accepted_patches: list, audit_result: dict) -> None:
        """Сверяет accepted_patches с вердиктом финального аудита и помечает
        `audit_reverted=True` те принятые патчи, чей файл был ПОЛНОСТЬЮ откачён
        (`failed_segments` содержит сегмент с end==0). Такой файл доставкой не
        трогается (см. _copy_passed_files_to_original: full-rollback → «не
        копируем» + failed_norm-исключение accept-fallback), поэтому его ACCEPT
        не дожили до диска — файл байт-в-байт равен оригиналу.

        Мотивация (2026-07-02, deep-reasoner, pyca/bcrypt): before_sigs для
        Python — flake8-only (python_support.get_initial_signatures_provider).
        5 ACCEPT заменили байт-строки на строки и расплющили parametrize-кейс,
        укоротив одну длинную строку (−1 E501) и внеся новый E131 → число
        уникальных flake8-сигнатур файла выросло 1→2 → audit_run: count_grew →
        полный откат. Патчи оставались «принятыми» → verify_accepts показывал
        ACCEPT+UNCHANGED_FILE. Механизм-близнец уже есть для осцилляции
        (`oscillation_cancelled`), но НЕ покрывал откат аудитом.

        Осознанно помечаем ТОЛЬКО полный откат (end==0). Частичный откат
        (end != 0) с подтверждённым restore доставляет выжившие сегменты —
        часть ACCEPT там может дожить, огульно помечать их нельзя. На текущей
        кодовой базе продюсеров сегментных снапшотов нет (M6), поэтому
        `_find_problem_segments` практически всегда возвращает [(1,0,"")] —
        полный откат покрывает 100% реальных случаев.
        """
        failed_segments = dict((audit_result or {}).get("failed_segments", {}) or {})
        if not failed_segments:
            return
        fully_reverted = {
            self._norm_deliver_key(f)
            for f, segs in failed_segments.items()
            if any(end == 0 for _, end, _ in (segs or []))
        }
        if not fully_reverted:
            return
        meta = dict(self.context.metadata)
        reverted_items = list(meta.get("accept_reverted_items", []) or [])
        seen = {(it.get("file"), it.get("line"), it.get("code")) for it in reverted_items}
        marked = 0
        for ap in (accepted_patches or []):
            if ap.get("audit_reverted"):
                continue
            fkey = self._norm_deliver_key(str(ap.get("file", "")))
            if fkey in fully_reverted:
                ap["audit_reverted"] = True
                marked += 1
                key = (ap.get("file"), ap.get("line"), ap.get("code"))
                if key not in seen:
                    reverted_items.append({
                        "file": ap.get("file"),
                        "line": ap.get("line"),
                        "code": ap.get("code"),
                        "reason": "final_audit_full_rollback",
                    })
                    seen.add(key)
        if marked:
            meta["accept_reverted_items"] = reverted_items
            meta["accept_reverted_count"] = len(reverted_items)
            self.context = self.context.update(metadata=meta)
            logger.warning(
                "Учёт ACCEPT: %d принятых патч(ей) откачены финальным аудитом "
                "(полный откат %d файла(ов), не доставлены) — помечены "
                "audit_reverted, исключены из real_fix_impact",
                marked, len(fully_reverted),
            )

    def _copy_passed_files_to_original(
        self, tmp_dir: Path, audit_result: dict,
        restore_status: Optional[Dict[str, bool]] = None,
    ) -> None:
        """Копирует файлы, прошедшие аудит, из временной копии в оригинал.

        Дополнительно копирует файлы, по которым DECIDE поставил ACCEPT, но
        которые НЕ оказались в `audit_result.passed_files`. Такое бывает, когда
        исходный анализатор увидел только часть файлов (например, Python без
        flake8 → AST-фоллбэк отдаёт только ПЕРВУЮ синтаксическую ошибку, поэтому
        `file_error_signatures_before` пуст для остальных). Без этой страховки
        принятый патч на `config.py`/`database.py`/... остаётся в sandbox и
        теряется вместе с temp-каталогом.

        C3 (аудит 2026-07-01): вердикт аудита приоритетнее ACCEPT — файлы из
        `failed_segments` НИКОГДА не копируются ACCEPT-fallback-ом (шаг 3), а
        частично откачённые копируются только при подтверждённом restore.
        """
        passed_files = list(audit_result.get("passed_files", []) or [])
        failed_segments = dict(audit_result.get("failed_segments", {}) or {})
        restore_status = restore_status or {}

        backup_dir = self.project_path / ".webbles_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        copied: set = set()
        passed_files = list(dict.fromkeys(passed_files))
        # Нормализованное множество файлов, проваливших аудит, — для
        # исключения из ЛЮБЫХ fallback-путей копирования ниже. Codex-4
        # (2026-07-02): ключ регистронезависим на Windows/macOS (_norm_deliver_key).
        failed_norm = {self._norm_deliver_key(f) for f in failed_segments}


        def _copy_one(fname: str, why: str) -> None:
            # O.13: anti backup-of-backup. Если путь под .webbles_backups/
            # .webbles/.webbles_fix попал в passed_files/failed_segments/
            # accepted_patches (бывает на старых данных или если analyzer
            # ещё не skip-ает бэкапы), копировать нельзя — иначе будем
            # плодить `.webbles_backups/.webbles_backups_<file>.py` →
            # `.webbles_backups/.webbles_backups/.webbles_backups_<file>.py`
            # рекурсивно из прогона в прогон.
            parts = Path(str(fname).replace("\\", "/")).parts
            if any(p in (".webbles_backups", ".webbles", ".webbles_fix")
                   for p in parts):
                logger.info(
                    "Файл %s лежит в служебной/бэкап-директории — "
                    "пропускаем копирование в оригинал (O.13)", fname
                )
                return
            original_file = self.project_path / fname
            fixed_file = tmp_dir / fname
            if not fixed_file.exists():
                return
            safe_name = fname.replace("/", "_").replace("\\", "_")
            try:
                if original_file.exists():
                    shutil.copy2(original_file, backup_dir / safe_name)
                shutil.copy2(fixed_file, original_file)
                copied.add(fname)
                logger.info("Файл %s %s и скопирован в оригинал", fname, why)
            except Exception as e:
                logger.warning("Не удалось скопировать %s в оригинал: %s", fname, e)

        # 1. Файлы, прошедшие аудит.
        for fname in passed_files:
            _copy_one(fname, "прошёл аудит")

        # 2. Частично откаченные сегменты — копируем то, что осталось,
        #    ТОЛЬКО если restore для файла подтверждён (C3).
        for fname, segments in failed_segments.items():
            if any(end == 0 for _, end, _ in segments):
                logger.info("Файл %s полностью откачен, не копируем", fname)
                continue
            if not restore_status.get(fname, False):
                logger.error(
                    "Файл %s: частичный откат НЕ подтверждён (restore_status=%s) — "
                    "не копируем в оригинал", fname, restore_status.get(fname),
                )
                continue
            _copy_one(fname, "частично откачен")

        # 3. Страховка: файлы, принятые DECIDE, но НЕ упомянутые ни в
        #    passed_files, ни в failed_segments. Их аудит не видел (исходный
        #    анализатор не выдал по ним сигнатур ошибок), но решение по ним —
        #    ACCEPT, и патч уже физически лежит в sandbox.
        #    C3 (аудит 2026-07-01): файлы из failed_segments исключаются
        #    ЯВНО — раньше полностью откачённый файл не попадал в `copied`
        #    (шаг 2 его пропускает) и этот fallback копировал его «как
        #    accepted», полагаясь на то, что restore уже сработал; при
        #    провале restore повреждённый файл уезжал в оригинал ПОСЛЕ
        #    корректной детекции аудитом.
        try:
            # Codex-4 (2026-07-02): ключ сравнения — регистронезависимый
            # (_norm_deliver_key), но для фактического копирования сохраняем
            # ОРИГИНАЛЬНЫЙ путь (регистр как в accepted_patches).
            accepted_map: Dict[str, str] = {}
            for pinfo in (self.context.accepted_patches or []):
                err = (pinfo or {}).get("error") or {}
                f = err.get("file")
                if isinstance(f, str) and f:
                    accepted_map.setdefault(self._norm_deliver_key(f), f.replace("\\", "/"))
            # Сравниваем нормализованными (регистронезависимыми) ключами.
            seen_norm = {self._norm_deliver_key(x) for x in copied}
            for _key in sorted(set(accepted_map) - seen_norm - failed_norm):
                _copy_one(accepted_map[_key], "accepted by DECIDE (not in audit.passed_files)")
        except Exception as e:
            logger.debug("accepted_patches fallback failed: %s", e)

    def _send_report(self, accepted_patches, rejected_patches, audit_result,
                     remaining_errors, result=None):
        # D.5: needs_review-сводка. Источник копит NeedsReviewStage (D.1),
        # маршрутизирует туда DecideStage (D.4).
        nr_count = int(self.context.metadata.get("needs_review_count", 0) or 0)
        nr_items = list(self.context.metadata.get("needs_review_items", []) or [])

        msg = (
            f"🏁 Итоговый отчёт Webbles Fix\n"
            f"Принято патчей: {len(accepted_patches)}\n"
            f"Отклонено патчей: {len(rejected_patches)}\n"
            f"На ручной просмотр: {nr_count}\n"
        )
        # Конверсия (2026-07-02): заголовочная метрика — ДОСТАВЛЕННЫЙ эффект,
        # а не ACCEPT-счётчик. Контрольная серия 07-02: 10 из 19 ACCEPT не
        # дожили до доставки (oscillation-бан, fail-closed откаты аудита,
        # net-delta) — «Принято патчей» без этой строки систематически
        # завышает впечатление от прогона.
        if isinstance(result, dict):
            _rfi = result.get("real_fix_impact")
            _delta = result.get("independent_scan_delta")
            if _rfi is not None or _delta is not None:
                msg += (
                    f"Реально исправлено (real_fix_impact): "
                    f"{_rfi if _rfi is not None else '—'}"
                    f" | независимый скан: Δ{_delta if _delta is not None else '—'}\n"
                )
            _cancelled = int(result.get("accept_cancelled_count", 0) or 0)
            if _cancelled:
                msg += f"ACCEPT отменено осцилляцией: {_cancelled}\n"
            _reverted = int(result.get("accept_reverted_count", 0) or 0)
            if _reverted:
                msg += f"ACCEPT откачено финальным аудитом: {_reverted}\n"
        if nr_items:
            # Топ-5 файлов с error_code, без захламления.
            preview = "; ".join(
                f"{(it.get('file') or '?')}:{it.get('line', 0)} [{it.get('error_code') or '?'}]"
                for it in nr_items[:5]
            )
            tail = f" (+{len(nr_items) - 5} ещё)" if len(nr_items) > 5 else ""
            msg += f"  очередь: {preview}{tail}\n"
            msg += f"  файлы лежат в .webbles_fix/needs_review/\n"
        if audit_result.get("audit_ok"):
            msg += "✅ Аудит: все файлы прошли проверку\n"
        else:
            failed = len(audit_result.get("failed_segments", {}))
            passed = len(audit_result.get("passed_files", []))
            msg += f"⚠️ Аудит частичный: {passed} файлов принято, {failed} файлов частично откачены\n"
        # Q1 containment (2026-07-02): предупреждение о data-toxic файлах.
        if isinstance(result, dict):
            _dtf = list(result.get("data_toxic_files") or [])
            if _dtf:
                _dts = dict(result.get("data_toxic_skipped") or {})
                _total_skipped = sum(_dts.values())
                msg += (
                    f"⚠️ Файл(ы) исключены как data-toxic: "
                    f"{', '.join(_dtf)} (пропущено {_total_skipped} ошибок)\n"
                )
            # Q3 signature containment (2026-07-03): предупреждение о toxic-сигнатурах.
            _tsig = list(result.get("toxic_signatures") or [])
            if _tsig:
                _tsk = dict(result.get("toxic_sig_skipped") or {})
                _total_sig_skipped = sum(_tsk.values())
                msg += (
                    f"⚠️ Сигнатуры исключены как неверифицируемые: {len(_tsig)} "
                    f"(пропущено {_total_sig_skipped} ошибок)\n"
                )
        msg += f"Общее время: {time.time() - self.start_time:.1f} сек"
        self.reporter.send(msg)

    def _e999_triage(self) -> None:
        """Pre-loop pass: cascade-block E999 files that deterministic repair can't fix.

        For each file with E999/invalid-syntax, tries python_ast_repair and
        language_syntax_healer. On failure, marks ALL errors in that file as
        processed (count=3) so the main loop skips them without wasting iterations.
        Successfully repaired files are written to disk; AnalyzeStage picks them up.
        """
        from types import MappingProxyType
        _E999_CODES = frozenset(("E999", "invalid-syntax", "E902"))
        errors = list(self.context.current_errors)
        # Collect unique E999 files not yet cascade-blocked
        e999_files: Dict[str, Any] = {}
        for e in errors:
            if e.get("code", "") not in _E999_CODES:
                continue
            file_key = e.get("file", "") or ""
            if not file_key or file_key in e999_files:
                continue
            pkey = _process_key(e)
            if self.context.processed_errors.get(pkey, 0) >= 3:
                continue  # already cascade-blocked from a prior cycle
            e999_files[file_key] = e

        if not e999_files:
            return

        logger.info("E999 triage: %d файлов с синтаксическими ошибками", len(e999_files))
        new_proc = dict(self.context.processed_errors)
        repaired = 0
        blocked = 0

        for file_key, error in e999_files.items():
            file_path = self.project_path / file_key
            if not file_path.exists():
                continue
            try:
                file_content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            fixed = False

            # 1. Python AST repair
            try:
                from fixers.python_ast_repair import try_repair as _ast_repair
                new_content = _ast_repair(file_content)
                if new_content and new_content != file_content:
                    file_path.write_text(new_content, encoding="utf-8")
                    logger.info("  E999 triage: python_ast_repair исправил %s", file_key)
                    fixed = True
            except Exception as _e:
                logger.debug("  E999 triage: python_ast_repair недоступен/не сработал: %s", _e)

            # 2. Language syntax healer
            if not fixed:
                try:
                    if self.language_provider and hasattr(self.language_provider, "get_syntax_healer"):
                        lang_healer = self.language_provider.get_syntax_healer()
                        if lang_healer:
                            result = lang_healer.heal(error, file_path)
                            if result == "HEALED":
                                logger.info("  E999 triage: lang_healer восстановил %s", file_key)
                                fixed = True
                            elif isinstance(result, str) and result and not self.patch_engine.is_empty_patch(result):
                                self.patch_engine.apply(file_path, result)
                                logger.info("  E999 triage: lang_healer патч применён к %s", file_key)
                                fixed = True
                except Exception as _e:
                    logger.debug("  E999 triage: lang_healer не сработал для %s: %s", file_key, _e)

            if fixed:
                repaired += 1
            else:
                # Cascade-block all errors in this file
                for e in errors:
                    if e.get("file", "") == file_key:
                        pkey = _process_key(e)
                        if new_proc.get(pkey, 0) < 3:
                            new_proc[pkey] = 3
                blocked += 1
                logger.info("  E999 triage: файл %s заблокирован (детерминированный ремонт не сработал)", file_key)

        if new_proc != dict(self.context.processed_errors):
            self.context = self.context.update(processed_errors=MappingProxyType(new_proc))

        if repaired or blocked:
            logger.info("E999 triage: исправлено=%d, заблокировано=%d", repaired, blocked)

    def _error_signature(self, error: Dict[str, Any]) -> str:
        # Делегируем в общий core.utils.error_signature — раньше здесь была
        # своя копия с расходящимся форматом.
        from core.utils import error_signature
        return error_signature(error)

    def _build_result(self, error: Optional[str] = None, status_override: Optional[str] = None) -> Dict[str, Any]:
        try:
            self._patch_recorder.close()
        except Exception:
            pass
        total_time = time.time() - self.start_time
        health = self.health_evaluator.evaluate()
        _meta = self.context.metadata or {}
        ctrl = _meta.get("adaptive_cycle_summary") or {}
        _stop_reason = (
            _meta.get("stop_reason_override")
            or ctrl.get("stop_reason", "")
        )
        return {
            "status": status_override or self.context.current_state.name,
            "language": self.language,
            "project_path": str(self.project_path),
            "iterations": self.context.iteration_count,
            "rollbacks": self.context.rollback_count,
            "initial_error_count": len(self.context.initial_errors),
            "final_error_count": len(self.context.current_errors),
            "validation_results": dict(self.context.validation_results),
            "error": error,
            "state_history": [s.name for s in self.context.state_history],
            "accepted_patches": len(self.context.accepted_patches),
            "rejected_patches": len(self.context.rejected_patches),
            "step_times": self.step_times,
            "total_runtime_seconds": total_time,
            "dry_run": self.dry_run,
            "system_health": health.to_dict(),
            "dynamic_params": self.dynamic_params,
            "stop_reason": _stop_reason,
            "cycles_run": ctrl.get("cycles_run", 0),
            # Timeout / net-delta / ruff-autofix telemetry
            "project_timeout_triggered": bool(_meta.get("project_timeout_triggered", False)),
            "llm_timeout_count": int(_meta.get("llm_timeout_count", 0)),
            # Фаза Performance (2026-07-07): исходящие LLM-вызовы за прогон.
            "llm_call_count": int(_meta.get("llm_call_count", 0)),
            "net_delta_rollback_count": int(_meta.get("net_delta_rollback_count", 0)),
            "net_delta_uncertain_count": int(_meta.get("net_delta_uncertain_count", 0)),
            "ruff_autofix_total_fixed": int(_meta.get("ruff_autofix_total_fixed", 0)),
            # Q1 containment (2026-07-02): файлы, заблокированные FileAntiLoop
            # из-за детерминированной порчи данных guard-ом O.19 — см.
            # GeneratePatchStage._mark_data_toxic_if_o19 / prioritize_stage.py.
            "data_toxic_files": list(_meta.get("_data_toxic_files") or []),
            "data_toxic_skipped": dict(_meta.get("_data_toxic_skipped") or {}),
            # Dir-карантин fixture-корпусов (2026-07-07, loguru): каталоги с
            # >=3 файлами, заблокированными FileAntiLoop, и сколько ошибок
            # из них пропущено без LLM.
            "dir_quarantine_skipped": dict(_meta.get("_dir_quarantine_skipped") or {}),
            # Q3 signature containment (2026-07-03): сигнатуры (file::line::code),
            # исключённые DecideStage._track_toxic_signature / PrioritizeStage.execute
            # после MAX_TOXIC_SIG_REJECTS честных REJECT по mypy-кодам — см.
            # core/stages/decide_stage.py и core/stages/prioritize_stage.py.
            "toxic_signatures": list(_meta.get("_toxic_signatures") or []),
            "toxic_sig_skipped": dict(_meta.get("_toxic_sig_skipped") or {}),
        }
