"""
Иммутабельный контекст конвейера для webles_conveyor.
Обеспечивает потокобезопасное обновление состояния с копированием при записи.
Добавлены: кумулятивный счётчик патчей на файл, теневое хранилище для shadow-оценки,
список нерешаемых ошибок, эталонное хранилище, счётчик неудачных попыток, режим отладки,
восстановление из резервной копии (restore_backup), кеш актуального содержимого файлов,
и сброс флагов обработанных/нерешаемых ошибок для нового глобального цикла.
Все ключевые мутации логируются для упрощения отладки.
"""

import logging
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

from core.state_machine import State

logger = logging.getLogger(__name__)


class PipelineContext:
    __slots__ = (
        'project_path', 'language', 'config', 'dry_run', 'memory',
        'current_state', 'state_history', 'iteration_count', 'rollback_count',
        'max_iterations', 'current_errors', 'initial_errors', 'prioritized_errors',
        'selected_error', 'generated_patch', 'validation_results', 'before_validation',
        'metadata', 'processed_errors', 'accepted_patches', 'rejected_patches',
        '_reporter',
        'patches_per_file',
        'shadow_log',
        'unfixable_errors',
        'golden_patches',
        'unfixable_attempts',
        'debug_mode',
        'working_path',  # путь к изолированной копии проекта (если используется)
    )

    MAX_UNFIXABLE_ATTEMPTS = 10

    def __init__(
        self,
        project_path: Path,
        language: str,
        config: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
        memory: Optional[Any] = None,
        reporter: Optional[Any] = None,
        current_state: State = State.IDLE,
        state_history: Optional[List[State]] = None,
        iteration_count: int = 0,
        rollback_count: int = 0,
        max_iterations: int = 10,
        current_errors: Optional[Tuple[Dict[str, Any], ...]] = None,
        initial_errors: Optional[Tuple[Dict[str, Any], ...]] = None,
        prioritized_errors: Optional[Tuple[Dict[str, Any], ...]] = None,
        selected_error: Optional[Dict[str, Any]] = None,
        generated_patch: Optional[str] = None,
        validation_results: Optional[Dict[str, Any]] = None,
        before_validation: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        processed_errors: Optional[Dict[str, int]] = None,
        accepted_patches: Optional[List[Dict[str, Any]]] = None,
        rejected_patches: Optional[List[Dict[str, Any]]] = None,
        patches_per_file: Optional[Dict[str, int]] = None,
        shadow_log: Optional[List[Dict[str, Any]]] = None,
        unfixable_errors: Optional[List[Dict[str, Any]]] = None,
        golden_patches: Optional[Dict[str, Dict[str, Any]]] = None,
        unfixable_attempts: Optional[Dict[str, int]] = None,
        debug_mode: bool = False,
        working_path: Optional[Path] = None,
    ):
        self.project_path = project_path
        self.language = language
        self.config = config or {}
        self.dry_run = dry_run
        self.memory = memory
        self._reporter = reporter

        self.current_state = current_state
        self.state_history = state_history or []
        self.iteration_count = iteration_count
        self.rollback_count = rollback_count
        self.max_iterations = max_iterations

        self.current_errors = current_errors or ()
        self.initial_errors = initial_errors or ()
        self.prioritized_errors = prioritized_errors or ()
        self.selected_error = selected_error
        self.generated_patch = generated_patch
        self.validation_results = validation_results or MappingProxyType({})
        self.before_validation = before_validation or MappingProxyType({})
        self.metadata = metadata or MappingProxyType({})
        self.processed_errors = processed_errors or MappingProxyType({})
        self.accepted_patches = accepted_patches or []
        self.rejected_patches = rejected_patches or []

        self.patches_per_file = patches_per_file or {}
        self.shadow_log = shadow_log or []
        self.unfixable_errors = unfixable_errors or []
        self.golden_patches = golden_patches or {}
        self.unfixable_attempts = unfixable_attempts or {}

        self.debug_mode = debug_mode or self.config.get("verbose", False)
        self.working_path = working_path or self.project_path

        logger.info("PipelineContext создан: project=%s language=%s initial_errors=%d working_path=%s",
                    project_path, language, len(self.initial_errors), self.working_path)

    # ---------- Удаление снимков для файла (откат) ----------
    def remove_patch_snapshot(self, file_path: str) -> 'PipelineContext':
        """
        Удаляет все снимки, связанные с указанным файлом, из метаданных.
        Используется при откате патча, чтобы аудит не видел отменённые изменения.
        """
        new_metadata = dict(self.metadata)
        snapshots = new_metadata.get("patch_snapshots", [])
        if not snapshots:
            return self
        new_snapshots = [s for s in snapshots if s.get("file") != file_path]
        new_metadata["patch_snapshots"] = new_snapshots
        logger.debug("Удалены снимки для файла %s (осталось %d)", file_path, len(new_snapshots))
        return self.update(metadata=new_metadata)

    # ---------- Сброс флагов обработанных/нерешаемых ошибок ----------
    def reset_error_tracking(self) -> 'PipelineContext':
        """Сбрасывает все флаги processed/unfixable для нового глобального цикла."""
        logger.info("Сброс флагов processed/unfixable для нового глобального цикла")
        return self.update(
            processed_errors=MappingProxyType({}),
            unfixable_errors=[],
            unfixable_attempts={}
        )

    # ---------- Кеш актуального состояния файлов ----------
    def get_file_cache(self, file_path: str) -> Optional[str]:
        """Возвращает актуальный текст файла из кеша (после предыдущих правок)."""
        cache = self.metadata.get("file_cache", {})
        return cache.get(file_path)

    def set_file_cache(self, file_path: str, content: str) -> 'PipelineContext':
        """Сохраняет актуальный текст файла в кеш для использования в текущем цикле."""
        new_cache = dict(self.metadata.get("file_cache", {}))
        new_cache[file_path] = content
        new_metadata = dict(self.metadata)
        new_metadata["file_cache"] = new_cache
        logger.debug("Кеш файла обновлён: %s", file_path)
        return self.update(metadata=new_metadata)

    # ---------- Восстановление из резервной копии ----------
    def restore_backup(self) -> 'PipelineContext':
        """
        Восстанавливает оригинальный файл из резервной копии, если путь сохранён в метаданных.
        """
        backup_path = self.metadata.get("last_backup_path")
        if backup_path:
            try:
                import shutil
                backup = Path(backup_path)
                if backup.exists():
                    original = backup.with_suffix("")
                    shutil.copy2(backup, original)
                    backup.unlink()
                    logger.info("✅ Файл восстановлен из резервной копии: %s", original)
                    new_metadata = dict(self.metadata)
                    new_metadata.pop("last_backup_path", None)
                    return self.update(metadata=new_metadata)
                else:
                    logger.warning("Файл резервной копии не найден: %s", backup_path)
            except Exception as e:
                logger.error("Не удалось восстановить файл из бэкапа %s: %s", backup_path, e)
        return self

    # ---------- Счётчик неудачных попыток ----------
    def increment_unfixable_attempt(self, error_sig: str) -> 'PipelineContext':
        new_attempts = dict(self.unfixable_attempts)
        new_count = new_attempts.get(error_sig, 0) + 1
        new_attempts[error_sig] = new_count
        logger.info("Счётчик unfixable для %s увеличен: %d/%d", error_sig, new_count, self.MAX_UNFIXABLE_ATTEMPTS)
        return self.update(unfixable_attempts=new_attempts)

    def is_unfixable(self, error_sig: str) -> bool:
        unfixable = self.unfixable_attempts.get(error_sig, 0) >= self.MAX_UNFIXABLE_ATTEMPTS
        if unfixable:
            logger.debug("Ошибка %s помечена как нерешаемая", error_sig)
        return unfixable

    # ---------- Уведомления ----------
    @property
    def reporter(self):
        return self._reporter

    def notify(self, message: str) -> None:
        if self._reporter and self.config.get("telegram", {}).get("notifications", False):
            try:
                self._reporter.send(message)
            except Exception:
                pass

    # ---------- Режим отладки ----------
    def debug_log(self, message: str) -> None:
        """Выводит сообщение только в режиме отладки."""
        if self.debug_mode:
            logger.debug(message)

    # ---------- Кумулятивный учёт патчей ----------
    def record_file_patch(self, file_path: str) -> 'PipelineContext':
        new_counts = dict(self.patches_per_file)
        new_count = new_counts.get(file_path, 0) + 1
        new_counts[file_path] = new_count
        logger.info("Патч записан для файла %s (всего %d)", file_path, new_count)
        return self.update(patches_per_file=new_counts)

    def check_file_threshold(self, max_patches: int) -> List[str]:
        over = [f for f, c in self.patches_per_file.items() if c >= max_patches]
        if over:
            logger.warning("Файлы с превышением порога патчей (%d): %s", max_patches, over)
        return over

    # ---------- Теневое логирование (shadow-оценка) ----------
    def log_shadow_accept(
        self,
        error: Dict[str, Any],
        before_errors: List[Dict[str, Any]],
        after_errors: List[Dict[str, Any]],
        decision: str,
        score: float
    ) -> 'PipelineContext':
        entry = {
            "error_sig": self._signature(error),
            "before_errors": before_errors,
            "after_errors": after_errors,
            "decision": decision,
            "score": score,
        }
        new_log = list(self.shadow_log) + [entry]
        if len(new_log) > 1000:
            new_log = new_log[-1000:]
            logger.debug("Лог теневого хранилища обрезан до 1000 записей")
        logger.debug("Shadow запись добавлена: sig=%s decision=%s score=%.2f", entry["error_sig"], decision, score)
        return self.update(shadow_log=new_log)

    @staticmethod
    def _signature(error: Dict[str, Any]) -> str:
        # Единый источник: core.utils.error_signature.
        # Раньше здесь была своя версия, дававшая `{file}::{code}` без сообщения,
        # тогда как PipelineStage давал `{file}::{code}::{message}` — из-за этого
        # один и тот же error лежал в processed_errors и unfixable под разными
        # ключами, и AntiLoop его не видел.
        from core.utils import error_signature
        return error_signature(error)

    # ---------- Нерешаемые ошибки ----------
    def add_unfixable_error(self, error: Dict[str, Any]) -> 'PipelineContext':
        sig = self._signature(error)
        if any(self._signature(e) == sig for e in self.unfixable_errors):
            logger.debug("Повторная попытка добавить unfixable ошибку %s – игнорируем", sig)
            return self
        new_list = list(self.unfixable_errors) + [error]
        if len(new_list) > 100:
            new_list = new_list[-100:]
            logger.debug("Список unfixable ошибок обрезан до 100")
        logger.info("Ошибка помечена как нерешаемая: %s", sig)
        return self.update(unfixable_errors=new_list)

    # ---------- Эталонное хранилище ----------
    def store_golden_patch(
        self,
        error_sig: str,
        patch: str,
        score: float,
        error_type: str = "unknown",
        source: str = "llm"
    ) -> 'PipelineContext':
        current = self.golden_patches.get(error_sig)
        if current and current.get("score", 0) >= score:
            logger.debug("Патч для %s не сохранён – существующий score выше (%.2f >= %.2f)", error_sig, current["score"], score)
            return self
        new_golden = dict(self.golden_patches)
        new_golden[error_sig] = {
            "patch": patch,
            "score": score,
            "error_type": error_type,
            "source": source,
        }
        if len(new_golden) > 500:
            worst_key = min(new_golden, key=lambda k: new_golden[k].get("score", 0))
            del new_golden[worst_key]
            logger.debug("Хранилище golden-патчей обрезано до 500 записей")
        logger.info("Золотой патч сохранён: sig=%s type=%s score=%.2f source=%s", error_sig, error_type, score, source)
        return self.update(golden_patches=new_golden)

    def get_golden_patch(self, error_sig: str) -> Optional[Dict[str, Any]]:
        patch = self.golden_patches.get(error_sig)
        if patch:
            logger.info("Найден золотой патч для %s (score=%.2f)", error_sig, patch["score"])
        return patch

    # ---------- Методы обновления ----------
    def update(self, **kwargs) -> 'PipelineContext':
        logger.debug("Контекст обновляется (%d полей)", len(kwargs))
        for k, v in kwargs.items():
            if k != "metadata" and k != "debug_mode":
                logger.debug("  %s изменено", k)
        new_dict = {
            'project_path': self.project_path,
            'language': self.language,
            'config': self.config,
            'dry_run': self.dry_run,
            'memory': self.memory,
            'reporter': self._reporter,
            'current_state': self.current_state,
            'state_history': list(self.state_history),
            'iteration_count': self.iteration_count,
            'rollback_count': self.rollback_count,
            'max_iterations': self.max_iterations,
            'current_errors': self.current_errors,
            'initial_errors': self.initial_errors,
            'prioritized_errors': self.prioritized_errors,
            'selected_error': self.selected_error,
            'generated_patch': self.generated_patch,
            'validation_results': dict(self.validation_results),
            'before_validation': dict(self.before_validation),
            'metadata': dict(self.metadata),
            'processed_errors': dict(self.processed_errors),
            'accepted_patches': list(self.accepted_patches),
            'rejected_patches': list(self.rejected_patches),
            'patches_per_file': dict(self.patches_per_file),
            'shadow_log': list(self.shadow_log),
            'unfixable_errors': list(self.unfixable_errors),
            'golden_patches': dict(self.golden_patches),
            'unfixable_attempts': dict(self.unfixable_attempts),
            'debug_mode': self.debug_mode,
            'working_path': self.working_path,
        }
        new_dict.update(kwargs)
        if 'validation_results' in kwargs:
            new_dict['validation_results'] = kwargs['validation_results']
        if 'before_validation' in kwargs:
            new_dict['before_validation'] = kwargs['before_validation']
        if 'metadata' in kwargs:
            new_dict['metadata'] = kwargs['metadata']
        if 'processed_errors' in kwargs:
            new_dict['processed_errors'] = kwargs['processed_errors']
        return PipelineContext(**new_dict)

    def add_state_to_history(self, state: State) -> 'PipelineContext':
        new_history = list(self.state_history) + [state]
        logger.info("Переход состояния: %s → %s", self.current_state.name, state.name)
        return self.update(current_state=state, state_history=new_history)

    def set_errors(self, errors: List[Dict[str, Any]]) -> 'PipelineContext':
        t = tuple(errors)
        if not self.initial_errors:
            logger.info("Начальные ошибки установлены: %d шт.", len(t))
            return self.update(current_errors=t, initial_errors=t)
        logger.info("Ошибки обновлены: было %d, стало %d", len(self.current_errors), len(t))
        return self.update(current_errors=t)

    def merge_file_errors(self, file_rel: str, new_errors_for_file: List[Dict[str, Any]]) -> 'PipelineContext':
        """Инкрементальное обновление: заменяет в `current_errors` ТОЛЬКО
        записи указанного файла, оставляя ошибки остальных файлов как были.

        Используется режимом инкрементального анализа (см. AnalyzeStage,
        ValidateStage) — вместо полного `set_errors()` после каждого патча
        пересканируется только изменённый файл, а данные по остальным файлам
        берутся из ПОСЛЕДНЕГО известного полного/инкрементального снимка.

        `file_rel` сравнивается с `error["file"]` после нормализации разделителей
        (`\\` → `/`) — разные анализаторы на Windows могут отдавать пути в
        разном стиле в зависимости от того, как резолвился относительный путь.
        """
        target_norm = str(file_rel).replace("\\", "/").lstrip("/")
        kept = [
            e for e in self.current_errors
            if str(e.get("file", "")).replace("\\", "/").lstrip("/") != target_norm
        ]
        merged = kept + list(new_errors_for_file)
        logger.info(
            "Инкрементальное обновление ошибок файла %s: было %d (всего %d), "
            "стало %d для файла (всего %d)",
            file_rel, len(self.current_errors) - len(kept), len(self.current_errors),
            len(new_errors_for_file), len(merged),
        )
        return self.update(current_errors=tuple(merged))

    def set_selected_error(self, error: Optional[Dict[str, Any]]) -> 'PipelineContext':
        if error:
            logger.debug("Выбрана ошибка для обработки: %s:%s", error.get("file", ""), error.get("line", 0))
        return self.update(selected_error=error)

    def set_patch(self, patch: Optional[str]) -> 'PipelineContext':
        if patch:
            logger.debug("Патч установлен (длина %d символов)", len(patch))
        return self.update(generated_patch=patch)

    def set_validation_results(self, results: Dict[str, Any]) -> 'PipelineContext':
        logger.debug("Результаты валидации обновлены: %s", {k: v for k, v in results.items() if k != "output"})
        return self.update(validation_results=MappingProxyType(results))

    def set_before_validation(self, results: Dict[str, Any]) -> 'PipelineContext':
        logger.debug("Предвалидационные результаты сохранены")
        return self.update(before_validation=MappingProxyType(results))

    def record_processed_error(self, sig: str) -> 'PipelineContext':
        new_processed = dict(self.processed_errors)
        new_count = new_processed.get(sig, 0) + 1
        new_processed[sig] = new_count
        logger.info("Ошибка обработана: %s (попытка %d)", sig, new_count)
        return self.update(processed_errors=MappingProxyType(new_processed))

    def add_accepted_patch(self, info: Dict[str, Any]) -> 'PipelineContext':
        """Регистрирует ACCEPT. Структурный рефакторинг (2026-06-21, после
        decision-integrity серии из 10 проектов): это ЕДИНСТВЕННАЯ точка,
        добавляющая в accepted_patches — поэтому ЕДИНСТВЕННАЯ точка,
        логирующая ACCEPT в decisions[] (через append_decision_log). Раньше
        каждый вызывающий код САМ обязан был не забыть отдельно вызвать
        append_decision_log — 5 живых находок за одну контрольную серию
        показали, что забывали регулярно (разные стадии, разные пути).
        Теперь физически невозможно зарегистрировать ACCEPT без лога:
        нет другого способа добавить запись в accepted_patches."""
        new_list = list(self.accepted_patches) + [info]
        logger.info("Патч ПРИНЯТ: %s:%s", info.get("error", {}).get("file", "?"), info.get("error", {}).get("message", "")[:80])
        from analysis.run_statistics import append_decision_log
        new_meta = dict(self.metadata)
        append_decision_log(new_meta, info.get("error") or {}, "ACCEPT", str(info.get("reason") or ""))
        return self.update(accepted_patches=new_list, metadata=new_meta)

    def add_rejected_patch(self, info: Dict[str, Any]) -> 'PipelineContext':
        """Регистрирует REJECT. См. add_accepted_patch — тот же принцип:
        единственная точка добавления в rejected_patches == единственная
        точка логирования REJECT в decisions[]."""
        new_list = list(self.rejected_patches) + [info]
        logger.warning("Патч ОТКЛОНЁН: %s (причина: %s)", info.get("error", {}).get("file", "?"), info.get("reason", "unknown"))
        from analysis.run_statistics import append_decision_log
        new_meta = dict(self.metadata)
        append_decision_log(new_meta, info.get("error") or {}, "REJECT", str(info.get("reason") or ""))
        return self.update(rejected_patches=new_list, metadata=new_meta)

    def increment_iteration(self) -> 'PipelineContext':
        new_iter = self.iteration_count + 1
        logger.info("Итерация увеличена: %d/%d", new_iter, self.max_iterations)
        return self.update(iteration_count=new_iter)

    def increment_rollback(self) -> 'PipelineContext':
        new_roll = self.rollback_count + 1
        logger.warning("Откат зарегистрирован (всего %d)", new_roll)
        return self.update(rollback_count=new_roll)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'project_path': str(self.project_path),
            'language': self.language,
            'config': self.config,
            'dry_run': self.dry_run,
            'current_state': self.current_state.name,
            'state_history': [s.name for s in self.state_history],
            'iteration_count': self.iteration_count,
            'rollback_count': self.rollback_count,
            'max_iterations': self.max_iterations,
            'current_errors': list(self.current_errors),
            'initial_errors': list(self.initial_errors),
            'prioritized_errors': list(self.prioritized_errors),
            'selected_error': self.selected_error,
            'generated_patch': self.generated_patch,
            'validation_results': dict(self.validation_results),
            'before_validation': dict(self.before_validation),
            'metadata': dict(self.metadata),
            'processed_errors': dict(self.processed_errors),
            'accepted_patches': self.accepted_patches,
            'rejected_patches': self.rejected_patches,
            'patches_per_file': self.patches_per_file,
            'shadow_log': self.shadow_log,
            'unfixable_errors': self.unfixable_errors,
            'golden_patches': self.golden_patches,
            'unfixable_attempts': self.unfixable_attempts,
            'debug_mode': self.debug_mode,
            'working_path': str(self.working_path) if self.working_path else str(self.project_path),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], memory: Optional[Any] = None) -> 'PipelineContext':
        return cls(
            project_path=Path(data.get('project_path', '.')),
            language=data.get('language', 'unknown'),
            config=data.get('config', {}),
            dry_run=data.get('dry_run', False),
            memory=memory,
            current_state=State[data.get('current_state', 'IDLE')],
            state_history=[State[s] for s in data.get('state_history', [])],
            iteration_count=data.get('iteration_count', 0),
            rollback_count=data.get('rollback_count', 0),
            max_iterations=data.get('max_iterations', 10),
            current_errors=tuple(data.get('current_errors', [])),
            initial_errors=tuple(data.get('initial_errors', [])),
            prioritized_errors=tuple(data.get('prioritized_errors', [])),
            selected_error=data.get('selected_error'),
            generated_patch=data.get('generated_patch'),
            validation_results=MappingProxyType(data.get('validation_results', {})),
            before_validation=MappingProxyType(data.get('before_validation', {})),
            metadata=MappingProxyType(data.get('metadata', {})),
            processed_errors=MappingProxyType(data.get('processed_errors', {})),
            accepted_patches=data.get('accepted_patches', []),
            rejected_patches=data.get('rejected_patches', []),
            patches_per_file=data.get('patches_per_file', {}),
            shadow_log=data.get('shadow_log', []),
            unfixable_errors=data.get('unfixable_errors', []),
            golden_patches=data.get('golden_patches', {}),
            unfixable_attempts=data.get('unfixable_attempts', {}),
            debug_mode=data.get('debug_mode', False),
            working_path=Path(data['working_path']) if data.get('working_path') else None,
        )