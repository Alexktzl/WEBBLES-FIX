"""
Стадия предварительного анализа артефактов (read-only).
Больше НЕ изменяет файлы на диске.
Собирает подсказки для LLM и сохраняет их в context.metadata.
"""

import hashlib
import logging
from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage, _get_source_patterns
from core.state_machine import State
from fixers.syntax_repair import SyntaxRepair

logger = logging.getLogger(__name__)


class PreCleanupStage(PipelineStage):
    """
    Анализирует файлы проекта на наличие артефактов (дубликаты строк и т.п.)
    без их модификации. Результаты сохраняются в метаданных контекста
    для дальнейшего использования в подсказках LLM.
    """

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия PRE_CLEANUP: анализ артефактов и очистка...")

        patterns = _get_source_patterns(context.language)
        suggestions = []
        cleaned_count = 0

        # --- Шаг 1: Сбор артефактов (read-only) ---
        SKIP_CLEANUP_DIRS = frozenset({
            ".webbles_backups", ".webles_sandbox", ".webbles_sandbox",
            ".webbles", ".webbles_fix",
            ".git", "__pycache__", "venv", ".venv", "env", ".env",
            "node_modules", "target", "dist", "build",
            ".idea", ".vscode", ".tox", ".pytest_cache",
            ".next", ".cache", ".gradle", ".mvn",
            "statistic",
        })
        for pattern in patterns:
            for file_path in context.project_path.rglob(pattern):
                if "_clean" in file_path.name:
                    continue
                if any(part in SKIP_CLEANUP_DIRS for part in file_path.parts):
                    continue
                try:
                    result = SyntaxRepair.analyze_file_artifacts(file_path, context.language)
                    if result.get("has_artifacts"):
                        suggestions.append({
                            "file": str(file_path.relative_to(context.project_path)),
                            "message": result.get("suggestion"),
                            "cleaned_content": result.get("cleaned_content"),
                        })
                except Exception as e:
                    logger.debug("  Ошибка анализа %s: %s", file_path, e)

        # --- Шаг 2: Активная очистка для Python (если есть рабочая копия) ---
        # Аналог `cargo clippy --fix` для Rust: пробегаем ВСЕ python-файлы в
        # working_path, прогоняем детерминированный конвейер (LLM-noise →
        # слипшиеся строки → несмежные дубли → мёртвый код после return),
        # без LLM. Это закрывает большую часть «мусора», который иначе
        # уезжал бы в LLM и порождал бесконечные O.15-отказы.
        if context.language in ("python", "py") and context.working_path:
            from fixers.rule_based_fixer import RuleBasedFixer
            fixer = RuleBasedFixer()
            work_root = context.working_path

            cleanup_pipeline = [
                ("LLM_NOISE", fixer._py_remove_llm_noise),
                ("SLIPPED_LINE", fixer._py_separate_slipped_lines),
                ("DUPLICATE_LINES", fixer._py_remove_duplicate_lines),
                ("DEAD_CODE", fixer._py_remove_dead_code_after_return),
            ]

            # control series 12 (2026-06-20, semiprime/pygenda): PreCleanup
            # эти же детерминированные правила на тех же файлах пересчитывает
            # КАЖДЫЙ global cycle (PreCleanupStage входит в _single_run каждого
            # цикла AdaptiveCycleController). Если на файле они стабильно дают
            # синтаксически невалидный результат, повтор на НЕИЗМЕНИВШЕМСЯ
            # содержимом всегда даст тот же невалидный результат — чистая
            # трата времени/budget без шанса на иной исход. Запоминаем
            # (rel_path, hash содержимого) в context.metadata, которое
            # переживает между циклами (PipelineEngine._global_fix_loop
            # сливает metadata, не пересоздаёт), и пропускаем повтор для
            # неизменившегося файла.
            _known_bad = set(context.metadata.get(MetadataKeys.PRECLEANUP_FAILED_RULES) or [])
            _newly_bad: set = set()
            _skipped_known_bad = 0

            visited = 0
            for py_file in work_root.rglob("*.py"):
                if any(part in SKIP_CLEANUP_DIRS for part in py_file.parts):
                    continue
                if "_clean" in py_file.name:
                    continue
                visited += 1
                try:
                    rel_path = str(py_file.relative_to(work_root)).replace("\\", "/")
                except ValueError:
                    rel_path = py_file.name
                try:
                    original = py_file.read_text(encoding="utf-8")
                except Exception as e:
                    logger.debug("  PreCleanup: не прочитал %s: %s", py_file, e)
                    continue

                _content_hash = hashlib.sha256(original.encode("utf-8", errors="ignore")).hexdigest()[:16]
                _bad_key = f"{rel_path}::{_content_hash}"
                if _bad_key in _known_bad:
                    _skipped_known_bad += 1
                    continue

                current = original

                for code, fn in cleanup_pipeline:
                    try:
                        edit_set = fn(
                            {"file": rel_path, "line": 0, "code": code, "message": ""},
                            current,
                        )
                    except Exception as _e:
                        logger.debug("  PreCleanup: правило %s упало для %s: %s",
                                     code, rel_path, _e)
                        continue
                    if edit_set is not None and edit_set.edits:
                        current = edit_set.edits[0].new

                if current != original:
                    # Эти правила детерминированные (без LLM), но пишут прямо на
                    # диск МИМО ApplyPatchStage — без этой проверки баг в любом
                    # из них (например в _semicolon_in_string_or_comment) молча
                    # портит файл, и ни одна из защит ApplyPatchStage этого
                    # никогда не увидит (DecideStage сюда вообще не вызывается).
                    try:
                        import ast as _ast_precheck
                        _ast_precheck.parse(current)
                    except SyntaxError as _se:
                        logger.warning(
                            "  PreCleanup: правило сделало %s синтаксически "
                            "невалидным (%s) — пропускаем запись, не повторим "
                            "для этого содержимого в следующих циклах",
                            rel_path, _se,
                        )
                        _newly_bad.add(_bad_key)
                        continue
                    try:
                        py_file.write_text(current, encoding="utf-8")
                        cleaned_count += 1
                        logger.info(
                            "  PreCleanup: исправлен %s (noise/slipped/dup/dead-code)",
                            rel_path,
                        )
                    except Exception as e:
                        logger.debug("  PreCleanup: запись %s упала: %s", rel_path, e)
            logger.debug("  PreCleanup: просмотрено .py файлов: %d", visited)
            if _skipped_known_bad:
                logger.info(
                    "  PreCleanup: пропущено %d файлов с известным невалидным "
                    "результатом (не пересчитываем на неизменившемся содержимом)",
                    _skipped_known_bad,
                )

        if cleaned_count:
            logger.info("  PreCleanup: всего исправлено файлов: %d", cleaned_count)

        # --- Шаг 4: Сохраняем подсказки в метаданные ---
        new_metadata = dict(context.metadata)
        if context.language in ("python", "py") and context.working_path and _newly_bad:
            new_metadata[MetadataKeys.PRECLEANUP_FAILED_RULES] = sorted(_known_bad | _newly_bad)
        if suggestions:
            new_metadata["pre_cleanup_suggestions"] = suggestions
            logger.info("  Всего найдено файлов с артефактами: %d", len(suggestions))
            logger.debug("  Список файлов с артефактами: %s",
                         [s["file"] for s in suggestions])
        else:
            new_metadata.pop("pre_cleanup_suggestions", None)
            logger.info("  Артефакты не обнаружены")

        context = context.update(metadata=new_metadata)
        return context.add_state_to_history(State.PRIORITIZING)