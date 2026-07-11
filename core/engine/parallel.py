"""
Модуль параллельной обработки ошибок.
Вынесен из pipeline_engine.py для модульности.
Содержит подробные DEBUG-логи.
"""

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.generate_patch_stage import GeneratePatchStage
from core.stages.apply_patch_stage import ApplyPatchStage
from core.stages.validate_stage import ValidateStage
from core.stages.review_stage import ReviewStage
from core.stages.decide_stage import DecideStage
from core.stages.rollback_stage import RollbackStage
from core.stages.needs_review_stage import NeedsReviewStage
from core.utils import process_key as _process_key
from safety.degradation_tracker import DegradationTracker

logger = logging.getLogger(__name__)

# 2026-06-24: верхняя граница шагов внутри цикла разрешения ОДНОЙ ошибки
# (VALIDATING→REVIEWING→DECIDING→...) — защитный потолок, чтобы баг в
# какой-то стадии не мог зациклить поток навечно. В реальности обычный
# путь укладывается в 2-4 шага.
_MAX_RESOLUTION_STEPS = 8

# Состояния, в которых единственно осмысленный путь дальше — соответствующая
# стадия из этого набора; любое другое состояние (NEXT_ERROR, FAILED, или
# повторный запрос GENERATING_PATCH) завершает цикл разрешения для потока.
_RESOLUTION_STAGE_STATES = frozenset({
    State.VALIDATING, State.REVIEWING, State.DECIDING,
    State.ROLLING_BACK, State.NEEDS_REVIEW,
})


class ParallelExecutor:
    """Управляет параллельной обработкой ошибок."""

    def __init__(self, llm_client, memory, patch_engine, state_lock, get_context, set_context,
                 analyzer=None, classifier=None, compiler=None, linter=None, security=None,
                 quality_evaluator=None, recorder=None, circuit_breaker=None, anti_loop=None,
                 llm_client_pool=None):
        self.llm_client = llm_client
        # 2026-06-24: пул LLMClient с разными api_key (WEBBLES_LLM_API_KEY_POOL,
        # см. fixers.llm_client.build_llm_client_pool) — без него все потоки
        # делили ОДИН ключ/circuit breaker, конкурентные вызовы упирались в
        # общий rate-limit провайдера. round-robin по индексу файла (см.
        # execute()) — каждый поток получает свой клиент со своим ключом и
        # изолированным consecutive_hard_timeouts. Без пула — список из
        # одного self.llm_client (обратная совместимость).
        self.llm_client_pool = llm_client_pool or [llm_client]
        self.memory = memory
        self.patch_engine = patch_engine
        self._state_lock = state_lock
        self._get_context = get_context
        self._set_context = set_context
        self.analyzer = analyzer
        self.classifier = classifier
        # 2026-06-24: раньше мини-цикл потока обрывался сразу после
        # ApplyPatchStage и НИКОГДА не вызывал ValidateStage/ReviewStage/
        # DecideStage — патчи писались на диск без net_delta_check,
        # symbol_regression, type_erosion_guard и без формального
        # ACCEPT/REJECT решения. Теперь поток прогоняет ТОТ ЖЕ набор
        # стадий, что и последовательный _single_run (см. _resolve_patch).
        # compiler/linter/security/quality_evaluator/recorder — те же
        # экземпляры, что у основного движка (PatchRecorder уже
        # thread-safe внутри себя; остальные — без мутируемого состояния).
        # DegradationTracker НЕ шарим — он мутирует список без блокировки,
        # каждый поток получает свой одноразовый экземпляр (см. process_file).
        self.compiler = compiler
        self.linter = linter
        self.security = security
        self.quality_evaluator = quality_evaluator
        self.recorder = recorder
        # circuit_breaker/anti_loop — ТЕ ЖЕ глобальные экземпляры, что у
        # последовательного _single_run. Используются ТОЛЬКО read-only
        # (is_open()/should_break() — простые проверки счётчиков/словарей,
        # без мутации) перед retry-попыткой (regenerate+reapply патча) —
        # см. _resolve_patch. Не мутируем их из потока, поэтому конкурентные
        # read-only вызовы из нескольких потоков безопасны (GIL).
        self.circuit_breaker = circuit_breaker
        self.anti_loop = anti_loop
        # Стадии будут создаваться для каждого потока отдельно

    def execute(self, config: dict, project_path) -> None:
        """
        Параллельная обработка ошибок (включается при parallel_workers > 1).
        Ошибки группируются по файлам, для каждого файла в отдельном потоке
        запускается мини-цикл исправлений.
        """
        workers = int(config.get("pipeline", {}).get("parallel_workers", 1))
        if workers <= 1:
            logger.debug("Параллельный режим отключён (workers=%d)", workers)
            return

        context = self._get_context()
        errors = list(context.current_errors)
        if not errors:
            logger.debug("Нет ошибок для параллельной обработки")
            return

        # Группировка ошибок по файлам
        file_to_errors = {}
        for err in errors:
            fname = err.get("file", "")
            if not fname:
                continue
            file_to_errors.setdefault(fname, []).append(err)

        if len(file_to_errors) <= 1:
            logger.debug("Только один файл с ошибками – параллелить нечего")
            return

        logger.info("Параллельная обработка %d файлов (workers=%d)", len(file_to_errors), workers)
        logger.debug("Файлы для параллельной обработки: %s", list(file_to_errors.keys()))

        # Клонируем основной контекст для каждого потока
        base_ctx_dict = context.to_dict()

        # Запоминаем размеры списков ДО форка, чтобы при слиянии брать только
        # genuine-дельту каждого воркера (а не дублировать baseline N раз).
        _n_base_accepted = len(base_ctx_dict.get("accepted_patches", []))
        _n_base_rejected = len(base_ctx_dict.get("rejected_patches", []))
        _n_base_unfixable = len(base_ctx_dict.get("unfixable_errors", []))
        # H4 (аудит 2026-07-01): та же дельта-схема для metadata-списков —
        # раньше merge оставлял metadata ПЕРВОГО завершившегося воркера
        # целиком, и decisions[]/needs_review_items остальных воркеров
        # терялись (decision integrity гарантированно расходился).
        _base_meta = base_ctx_dict.get("metadata", {}) or {}
        _n_base_decisions = len(_base_meta.get("decisions", []) or [])
        _n_base_nr_items = len(_base_meta.get("needs_review_items", []) or [])

        touched_files: set = set()

        def process_file(file_name: str, error_list: List[Dict[str, Any]], worker_llm_client) -> Dict[str, Any]:
            """Обрабатывает все ошибки одного файла и возвращает обновлённый контекст.

            `worker_llm_client` — клиент ИЗ ПУЛА, назначенный этому потоку
            (round-robin по индексу файла, см. execute()) — свой api_key и
            свой изолированный circuit breaker, не общий на все потоки."""
            local_ctx = PipelineContext.from_dict(base_ctx_dict, memory=self.memory)
            local_ctx = local_ctx.update(current_errors=tuple(error_list))
            gen_stage = GeneratePatchStage(worker_llm_client, self.memory, self.patch_engine)
            app_stage = ApplyPatchStage(self.patch_engine, self.analyzer, self.classifier)
            # 2026-06-24: полная цепочка валидации/решения — те же стадии, что
            # у последовательного _single_run (см. _resolve_patch ниже).
            # DegradationTracker создаётся одноразово на поток (не шарим —
            # мутирует список без блокировки, см. docstring __init__).
            validate_stage = ValidateStage(
                self.compiler, self.linter, self.security, self.analyzer,
                DegradationTracker(),
            )
            review_stage = ReviewStage(worker_llm_client)
            decide_stage = DecideStage(self.quality_evaluator, self.analyzer, recorder=self.recorder)
            rollback_stage = RollbackStage(self.patch_engine, self.analyzer)
            needs_review_stage = NeedsReviewStage()

            logger.debug("Поток для файла %s: начало обработки %d ошибок", file_name, len(error_list))
            _deadline = local_ctx.metadata.get(MetadataKeys.PROJECT_DEADLINE)
            for _idx, error in enumerate(error_list):
                # control series 12 fix verification (2026-06-20, cantools/textparser):
                # этот цикл обходит ВЕСЬ error_list потока независимо от
                # project_timeout — GeneratePatchStage внутри себя дедлайн
                # уважает (см. GeneratePatchStage._deadline_exceeded), но как
                # только она возвращает управление сюда, цикл просто берёт
                # следующую ошибку. На проекте с ошибками в десятках файлов
                # (parallel_workers>1) это держало процесс far за бюджетом,
                # пока _global_fix_loop/​_single_run даже не получали слова.
                if _deadline is not None and time.monotonic() >= float(_deadline):
                    logger.warning(
                        "Поток %s: PROJECT_TIMEOUT — прекращаем обработку "
                        "оставшихся ошибок файла (%d из %d обработано)",
                        file_name, _idx, len(error_list),
                    )
                    break
                error_sig = self._error_signature(error)
                pkey = _process_key(error)
                if local_ctx.processed_errors.get(pkey, 0) >= 3:
                    logger.debug("Поток %s: ошибка %s уже обработана (≥3 раз), пропускаем", file_name, error_sig)
                    continue

                local_ctx = local_ctx.set_selected_error(error)
                local_ctx = gen_stage.execute(local_ctx)
                if not local_ctx.generated_patch and not local_ctx.metadata.get("segmented_patches"):
                    logger.debug("Поток %s: патч не сгенерирован для %s", file_name, error_sig)
                    local_ctx = local_ctx.set_selected_error(None)
                    continue

                local_ctx = app_stage.execute(local_ctx)
                if local_ctx.current_state != State.VALIDATING:
                    # ApplyPatchStage сам ушёл в NEXT_ERROR/FAILED (например,
                    # новый CRITICAL_SYNTAX) — ничего разрешать дальше не нужно.
                    local_ctx = local_ctx.set_selected_error(None)
                    continue

                # 2026-06-24: раньше ЗДЕСЬ цикл считал патч готовым и шёл к
                # следующей ошибке — ValidateStage/ReviewStage/DecideStage
                # никогда не вызывались, патч молча оставался на диске без
                # net_delta_check/symbol_regression/type_erosion_guard и без
                # формального ACCEPT/REJECT. Теперь прогоняем ТОТ ЖЕ набор
                # стадий, что и последовательный _single_run, ограниченный
                # _MAX_RESOLUTION_STEPS шагами (защита от зацикливания).
                local_ctx = self._resolve_patch(
                    local_ctx, gen_stage, app_stage, validate_stage, review_stage,
                    decide_stage, rollback_stage, needs_review_stage, file_name, error_sig,
                    circuit_breaker=self.circuit_breaker, anti_loop=self.anti_loop,
                    deadline=_deadline,
                )

                # M10 (аудит 2026-07-01): читаем из working_path (sandbox с
                # применёнными правками), а не из project_path — раньше в
                # file_cache («актуальный текст файла») попадал НЕПАТЧЕННЫЙ
                # оригинал.
                _work_root = getattr(local_ctx, "working_path", None) or local_ctx.project_path
                fpath = _work_root / file_name
                try:
                    new_content = fpath.read_text(encoding="utf-8")
                    local_ctx = local_ctx.set_file_cache(str(fpath), new_content)
                except OSError as e:
                    logger.debug("Поток %s: не удалось перечитать файл после разрешения: %s", file_name, e)
                touched_files.add(file_name)
                local_ctx = local_ctx.set_selected_error(None)

            logger.debug("Поток для файла %s: завершён", file_name)
            return local_ctx.to_dict()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for i, (fname, err_list) in enumerate(file_to_errors.items()):
                # round-robin по индексу файла — каждый поток получает свой
                # LLMClient (свой api_key/circuit breaker) из пула.
                worker_client = self.llm_client_pool[i % len(self.llm_client_pool)]
                future = executor.submit(process_file, fname, err_list, worker_client)
                futures[future] = fname

            merged_ctx = None
            for future in as_completed(futures):
                file_name = futures[future]
                try:
                    local_dict = future.result()
                    if merged_ctx is None:
                        merged_ctx = local_dict
                        # Инициализируем тройку списков: одна копия baseline +
                        # genuine-добавления этого воркера (за пределами baseline-индекса).
                        merged_ctx["accepted_patches"] = (
                            list(base_ctx_dict.get("accepted_patches", [])) +
                            local_dict.get("accepted_patches", [])[_n_base_accepted:]
                        )
                        merged_ctx["rejected_patches"] = (
                            list(base_ctx_dict.get("rejected_patches", [])) +
                            local_dict.get("rejected_patches", [])[_n_base_rejected:]
                        )
                        merged_ctx["unfixable_errors"] = (
                            list(base_ctx_dict.get("unfixable_errors", [])) +
                            local_dict.get("unfixable_errors", [])[_n_base_unfixable:]
                        )
                    else:
                        # Добавляем только genuine-дельту последующих воркеров.
                        merged_ctx["accepted_patches"] += local_dict.get("accepted_patches", [])[_n_base_accepted:]
                        merged_ctx["rejected_patches"] += local_dict.get("rejected_patches", [])[_n_base_rejected:]
                        merged_ctx["unfixable_errors"] += local_dict.get("unfixable_errors", [])[_n_base_unfixable:]
                        merged_cache = merged_ctx.get("metadata", {}).get("file_cache", {})
                        local_cache = local_dict.get("metadata", {}).get("file_cache", {})
                        merged_cache.update(local_cache)
                        merged_ctx["metadata"]["file_cache"] = merged_cache
                        # H4 (аудит 2026-07-01): decisions[]/needs_review_items
                        # последующих воркеров — той же дельта-схемой, что и
                        # accepted/rejected/unfixable выше. Раньше терялись
                        # целиком (metadata первого воркера побеждала).
                        _local_meta = local_dict.get("metadata", {}) or {}
                        _mm = merged_ctx.setdefault("metadata", {})
                        _mm["decisions"] = (
                            list(_mm.get("decisions", []) or [])
                            + list(_local_meta.get("decisions", []) or [])[_n_base_decisions:]
                        )
                        _mm["needs_review_items"] = (
                            list(_mm.get("needs_review_items", []) or [])
                            + list(_local_meta.get("needs_review_items", []) or [])[_n_base_nr_items:]
                        )
                        _mm["needs_review_count"] = len(_mm["needs_review_items"])
                        # Merge processed_errors: take max attempt count per signature
                        # so sequential _single_run skips errors already handled here.
                        local_proc = local_dict.get("processed_errors", {})
                        merged_proc = dict(merged_ctx.get("processed_errors", {}))
                        for sig, count in local_proc.items():
                            merged_proc[sig] = max(merged_proc.get(sig, 0), count)
                        merged_ctx["processed_errors"] = merged_proc
                except Exception as e:
                    # H4 (аудит 2026-07-01): контекст воркера потерян, но его
                    # дисковые правки в sandbox ОСТАЛИСЬ — применённые, без
                    # решения и невидимые для merge. Best effort: возвращаем
                    # файл воркера к оригиналу из project_path, чтобы
                    # непроверенная правка не уехала дальше по пайплайну.
                    logger.error("Параллельная обработка файла %s провалилась: %s", file_name, e)
                    try:
                        _wp = base_ctx_dict.get("working_path")
                        if _wp:
                            _orig = Path(project_path) / file_name
                            _dst = Path(_wp) / file_name
                            if _orig.exists() and _dst.exists():
                                shutil.copy2(_orig, _dst)
                                logger.warning(
                                    "Поток %s: файл восстановлен из оригинала после сбоя воркера",
                                    file_name,
                                )
                    except Exception as _re:
                        logger.error(
                            "Поток %s: восстановление после сбоя воркера не удалось: %s",
                            file_name, _re,
                        )

            if merged_ctx:
                # Каждый воркер клонирует base_ctx_dict и затем сам гоняет
                # GeneratePatchStage/ApplyPatchStage — обе стадии переводят
                # current_state куда им нужно для СВОЕГО локального потока
                # (VALIDATING/NEXT_ERROR/GENERATING_PATCH...), и какой из
                # воркеров завершится последним — тот state и "выигрывал" при
                # простом merged_ctx = local_dict. В итоге _global_fix_loop
                # выставлял ANALYZING перед вызовом execute(), а после него
                # _single_run() мог обнаружить там PLANNING/VALIDATING и
                # ПРОПУСТИТЬ повторный анализ целиком — даже если файлы на
                # диске реально изменились (см. control series 13/14:
                # incremental_analysis был architecturally недостижим именно
                # из-за этого). Форсируем возврат в ANALYZING безусловно —
                # независимо от того, что произошло в воркерах.
                merged_ctx["current_state"] = State.ANALYZING.name
                _touched = sorted(touched_files)
                if _touched:
                    _pm = dict(merged_ctx.get("metadata", {}))
                    _pm["_parallel_touched_files"] = _touched
                    merged_ctx["metadata"] = _pm
                    logger.info(
                        "Параллельная обработка изменила %d файл(ов) на диске: %s",
                        len(_touched), _touched,
                    )
                with self._state_lock:
                    new_context = PipelineContext.from_dict(merged_ctx, memory=self.memory)
                    self._set_context(new_context)
                    logger.info("Контекст обновлён после параллельной обработки — current_state=ANALYZING")

    @staticmethod
    def _error_signature(error: Dict[str, Any]) -> str:
        from core.pipeline_stage import PipelineStage
        return PipelineStage._static_signature(error)

    @staticmethod
    def _resolve_patch(
        local_ctx: PipelineContext,
        gen_stage: GeneratePatchStage,
        app_stage: ApplyPatchStage,
        validate_stage: ValidateStage,
        review_stage: ReviewStage,
        decide_stage: DecideStage,
        rollback_stage: RollbackStage,
        needs_review_stage: NeedsReviewStage,
        file_name: str,
        error_sig: str,
        *,
        circuit_breaker=None,
        anti_loop=None,
        deadline: Optional[float] = None,
    ) -> PipelineContext:
        """Прогоняет уже применённый (на диске) патч через тот же набор
        стадий разрешения, что и последовательный `_single_run`
        (VALIDATING→REVIEWING→DECIDING→[ROLLING_BACK|NEEDS_REVIEW]) —
        раньше параллельный мини-цикл этого не делал вовсе (см. 2026-06-24
        находку в docstring `ParallelExecutor.__init__`).

        2026-06-24 (вторая итерация фикса, по запросу пользователя "убрать
        обход защиты полностью"): retry-петля ТЕПЕРЬ реплицируется — если
        какая-то стадия запрашивает повторную генерацию патча
        (State.GENERATING_PATCH, например ValidateStage на net_delta
        regression — откат уже выполнен запросившей стадией ДО перехода в
        GENERATING_PATCH, см. validate_stage.py), мини-цикл сам вызывает
        gen_stage→app_stage заново и продолжает разрешение. Перед КАЖДОЙ
        retry-попыткой (но НЕ внутри самого разрешения VALIDATING/
        REVIEWING/DECIDING/... — та же защита "не прерывать пока патч не
        разрешён", что и в `_single_run._PATCH_RESOLUTION_STATES") проверяем
        circuit_breaker.is_open()/anti_loop.should_break()/deadline — если
        что-то из этого сработало, прекращаем retry для ЭТОЙ ошибки (она
        остаётся в current_errors, будет переподхвачена обычным циклом),
        а не продолжаем вслепую."""
        stage_map = {
            State.VALIDATING: validate_stage,
            State.REVIEWING: review_stage,
            State.DECIDING: decide_stage,
            State.ROLLING_BACK: rollback_stage,
            State.NEEDS_REVIEW: needs_review_stage,
        }
        for _ in range(_MAX_RESOLUTION_STEPS):
            state = local_ctx.current_state
            if state == State.GENERATING_PATCH:
                if circuit_breaker is not None and circuit_breaker.is_open():
                    logger.warning(
                        "Поток %s: circuit breaker открыт — прекращаем retry для %s",
                        file_name, error_sig,
                    )
                    break
                if anti_loop is not None and anti_loop.should_break():
                    logger.warning(
                        "Поток %s: anti-loop сработал — прекращаем retry для %s",
                        file_name, error_sig,
                    )
                    break
                if deadline is not None and time.monotonic() >= float(deadline):
                    logger.warning(
                        "Поток %s: PROJECT_TIMEOUT — прекращаем retry для %s",
                        file_name, error_sig,
                    )
                    break
                local_ctx = gen_stage.execute(local_ctx)
                if not local_ctx.generated_patch and not local_ctx.metadata.get("segmented_patches"):
                    break
                local_ctx = app_stage.execute(local_ctx)
                if local_ctx.current_state != State.VALIDATING:
                    break
                continue
            if state not in _RESOLUTION_STAGE_STATES:
                break
            stage = stage_map[state]
            local_ctx = stage.execute(local_ctx)
        else:
            # H4 (аудит 2026-07-01): раньше здесь был только warning —
            # применённый патч оставался на диске БЕЗ решения (не ACCEPT,
            # не REJECT, не откачен) и мог доехать до оригинала через
            # passed_files. Принудительный откат тем же механизмом, что
            # REJECT в DecideStage (_pre_patch_content → снапшот →
            # _rollback_failed_files).
            logger.warning(
                "Поток %s: разрешение %s не завершилось за %d шагов — "
                "принудительный откат применённого патча",
                file_name, error_sig, _MAX_RESOLUTION_STEPS,
            )
            try:
                local_ctx = decide_stage._rollback_file_from_snapshot(
                    local_ctx, local_ctx.selected_error or {"file": file_name},
                )
            except Exception as _rb_e:
                logger.error(
                    "Поток %s: принудительный откат не удался: %s", file_name, _rb_e,
                )
        return local_ctx