"""
Стадия валидации изменений после применения патча.
Вынесена из pipeline_stage.py для модульности.
Содержит подробные DEBUG-логи.
Для Rust проверяется целевой файл, а затем весь проект через cargo check,
чтобы предотвратить каскадный рост ошибок.
Добавлен вызов HypothesisValidator для property‑based тестирования.
"""

import ast
import logging
import os
import re
import subprocess
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from core.timeout_utils import run_with_timeout
from core.utils import process_key as _process_key

# ---------- Адаптер Hypothesis ----------
from tools.hypothesis_validator import HypothesisValidator
# ---------- Tests as error source (Stage G) ----------
from validation.test_runner import TestRunner

logger = logging.getLogger(__name__)


class ValidateStage(PipelineStage):
    MAX_NET_DELTA_ROLLBACKS_PER_FILE = 3  # bulk-skip file after this many regression rollbacks

    def __init__(self, compiler, linter, security, analyzer, degradation):
        self.compiler = compiler
        self.linter = linter
        self.security = security
        self.analyzer = analyzer
        self.degradation = degradation
        # Советчик Hypothesis (включается/отключается через конфиг)
        self.hypothesis = HypothesisValidator()
        # Test-pass валидация (Stage G.5): под флагом run_tests после патча
        # перепрогоняем тесты и добавляем оставшиеся падения в набор ошибок,
        # чтобы существующая логика accept/reject/rollback увидела, что тест
        # всё ещё падает (счётчик ошибок не уменьшился).
        self.test_runner = TestRunner()
        logger.debug("ValidateStage инициализирован (Hypothesis + TestRunner)")

    @staticmethod
    def _run_tests_enabled(context: PipelineContext) -> bool:
        """Stage G флаг: config["pipeline"]["run_tests"] (default False)."""
        try:
            return bool((context.config or {}).get("pipeline", {}).get("run_tests", False))
        except Exception:
            return False

    @staticmethod
    def _incremental_analysis_enabled(context: PipelineContext) -> bool:
        """pipeline.incremental_analysis (default False) — см. AnalyzeStage/
        PipelineContext.merge_file_errors. Здесь определяет, должна ли
        повторная проверка после патча сканировать ТОЛЬКО изменённый файл
        вместо всего проекта (самая частая точка полного скана — выполняется
        на КАЖДУЮ попытку патча, не только принятые)."""
        try:
            return bool((context.config or {}).get("pipeline", {}).get("incremental_analysis", False))
        except Exception:
            return False

    # Языки/файлы, для которых инкрементальный пере-анализ безопасен.
    # Python/py — per-file lint (правка файла X не меняет ошибок соседей).
    # C/C++ — ТОЛЬКО единицы трансляции (.cc/.cpp/.cxx/.c): правка заголовка
    # (.h/.hpp) меняет диагностику всех включающих его TU → инкрементал по
    # заголовку неверен, для него нужен полный скан (см. CppAnalyzer.analyze
    # docstring, safety-фильтр). Не-TU цель → полный скан (fail-closed).
    _CPP_TU_SUFFIXES = (".cpp", ".cxx", ".cc", ".c")
    # Все C/C++-исходники (TU + заголовки). Патч файла ВНЕ этого набора
    # (.py/.yml/.md/...) физически не может изменить вывод CppAnalyzer —
    # значит полный C++ пере-скан на такой патч не нужен (см. №1, 2026-07-10).
    _CPP_SRC_SUFFIXES = (".cpp", ".cxx", ".cc", ".c",
                         ".h", ".hpp", ".hxx", ".hh", ".h++")
    _CPP_LANGS = ("cpp", "c++", "cxx", "cc", "c")

    @classmethod
    def _incremental_lang_ok(cls, language: Optional[str], target_file: str) -> bool:
        """Допустим ли инкрементал для (язык, целевой файл). Python — всегда;
        C/C++ — только когда цель патча является единицей трансляции."""
        lang = (language or "").lower()
        if lang in ("python", "py"):
            return True
        if lang in cls._CPP_LANGS:
            return (target_file or "").lower().endswith(cls._CPP_TU_SUFFIXES)
        return False

    @classmethod
    def _incremental_enabled_for(cls, context: PipelineContext) -> bool:
        """Мастер-переключатель инкрементала. Python — под флагом
        pipeline.incremental_analysis (default False, opt-in). C/C++ —
        ВКЛЮЧЁН ПО УМОЛЧАНИЮ (2026-07-10): полный пере-скан C++ за цикл
        (cmake+cppcheck+clang-tidy по всему дереву) нежизнеспособен по
        времени, а инкрементал по TU доказуемо ЭКВИВАЛЕНТЕН полному по учёту
        (правка .cc не меняет диагностику других TU; заголовки уходят на
        полный скан через _incremental_lang_ok). Отключить для C++ можно
        явным pipeline.incremental_analysis=false в конфиге."""
        lang = (context.language or "").lower()
        if lang in cls._CPP_LANGS:
            try:
                cfg = (context.config or {}).get("pipeline", {})
                # явный false в конфиге отключает; отсутствие ключа = вкл для C++
                return bool(cfg.get("incremental_analysis", True))
            except Exception:
                return True
        return cls._incremental_analysis_enabled(context)

    @staticmethod
    def _use_ruff_enabled(context: PipelineContext) -> bool:
        """P2.7 Fix #3: config["pipeline"]["use_ruff"] (default False)."""
        try:
            return bool((context.config or {}).get("pipeline", {}).get("use_ruff", False))
        except Exception:
            return False

    @staticmethod
    def _logic_guard_enabled(context: PipelineContext) -> bool:
        """Q.1: config["pipeline"]["logic_guard"] (default True)."""
        try:
            return bool(
                (context.config or {}).get("pipeline", {}).get("logic_guard", True)
            )
        except Exception:
            return True

    @staticmethod
    def _skip_legacy_lint_check(context: PipelineContext) -> bool:
        """O2: LintChecks._run_flake8 гоняет flake8 по ВСЕМУ project_path на каждую
        попытку патча — это ВСЕГДА дублирует self.analyzer.analyze() (PythonAnalyzer),
        который вызывается ниже в этой же стадии независимо от use_mypy/use_bandit.
        Пропускаем безусловно для Python (не зависит от других флагов — раньше
        зависело через общий AND с use_mypy/use_bandit, из-за чего почти никогда не
        срабатывало в реальном конфиге)."""
        return (context.language or "").lower() in ("python", "py")

    @staticmethod
    def _skip_legacy_compile_check(context: PipelineContext) -> bool:
        """CompilerChecks._run_pyright/_run_mypy дублирует MypyAnalyzer ТОЛЬКО когда
        use_mypy=True (тогда type-checking уже покрыт в AnalyzeStage). Если
        use_mypy=False — это единственная проверка типов, пропускать нельзя."""
        if (context.language or "").lower() not in ("python", "py"):
            return False
        try:
            return bool((context.config or {}).get("pipeline", {}).get("use_mypy", False))
        except Exception:
            return False

    @staticmethod
    def _skip_legacy_security_check(context: PipelineContext) -> bool:
        """SecurityChecks._run_bandit дублирует BanditAnalyzer ТОЛЬКО когда
        use_bandit=True. Если use_bandit=False — это единственная security-проверка
        такого рода, пропускать нельзя."""
        if (context.language or "").lower() not in ("python", "py"):
            return False
        try:
            return bool((context.config or {}).get("pipeline", {}).get("use_bandit", False))
        except Exception:
            return False

    @staticmethod
    def _is_project_scoped_error(error) -> bool:
        """G.5×Rust fix: TEST_FAILURE/SEMANTIC_MISMATCH локализованы НЕ в целевом
        файле (паника в тесте / несоответствие интенту), поэтому rust file-scoped
        fast-path их недосчитывает (after≈before≈0 → ложный REJECT в DecideStage).
        Для таких ошибок используем generic project-wide путь (after=len(new_errors)).
        """
        if not error:
            return False
        tags = {
            str(error.get("code", "")).upper(),
            str(error.get("error_class", "")).upper(),
        }
        return bool(tags & {"TEST_FAILURE", "SEMANTIC_MISMATCH"})

    def _merge_test_failures(self, errors, context, work_path):
        """Stage G.5: добавляет к `errors` текущие падения тестов (под флагом).

        Возвращает (errors, test_pass_ok):
          * test_pass_ok=True  — тесты прошли / тул недоступен / флаг выключен;
          * test_pass_ok=False — есть оставшиеся падения (они добавлены в errors).
        Любой сбой — graceful: считаем тесты «не мешающими» (True, errors).
        """
        if not self._run_tests_enabled(context):
            return errors, True
        try:
            if not self.test_runner.available(context.language):
                return errors, True
            passed, failures = self.test_runner.run(work_path, context.language)
            if not passed and failures:
                logger.warning("  G.5: после патча всё ещё падает тестов: %d", len(failures))
                return list(errors) + list(failures), False
            logger.info("  G.5: тесты проходят после патча")
            return errors, True
        except Exception as e:
            logger.debug("  G.5: перепрогон тестов не удался: %s", e)
            return errors, True

    @staticmethod
    def _count_file_errors(file_path: Path) -> int:
        """Подсчитывает количество ошибок компиляции в одном файле (без всего проекта)."""
        logger.debug("Подсчёт ошибок в файле: %s", file_path)
        try:
            result = subprocess.run(
                ["rustc", "--edition", "2021", "--crate-type", "lib",
                 str(file_path), "-o", os.devnull],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace"
            )
            errors = result.stderr.count('error:') if result.stderr else 0
            logger.debug("Найдено %d ошибок в %s", errors, file_path.name)
            return errors
        except Exception as e:
            logger.warning("Ошибка подсчёта ошибок в %s: %s", file_path, e)
            return 999

    @staticmethod
    def _count_project_errors(project_path: Path) -> int:
        """Подсчитывает общее количество ошибок через cargo check."""
        try:
            result = subprocess.run(
                ["cargo", "check"],
                cwd=str(project_path),
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace"
            )
            return result.stderr.count('error:') if result.stderr else 0
        except Exception as e:
            logger.warning("Ошибка cargo check: %s", e)
            return 999

    def _save_patch_snapshot(self, context: PipelineContext) -> PipelineContext:
        """Сохраняет снапшот оригинального и исправленного кода для проверки инвариантов."""
        error = context.selected_error
        if not error:
            return context

        file_name = error.get("file", "")
        if not file_name:
            return context

        work_path = getattr(context, 'working_path', None) or context.project_path
        file_path = work_path / file_name
        if not file_path.exists():
            return context

        try:
            # Читаем текущий (уже исправленный) файл.
            # H3 (аудит 2026-07-01): errors="replace" — раньше UnicodeDecodeError
            # на не-UTF8 файле тихо выключал ВЕСЬ блок (снапшот + O.14/O.17/
            # O.18/Q.1) через общий except ниже.
            patched_content = file_path.read_text(encoding="utf-8", errors="replace")

            # P2.7 Fix #1: use pre-patch content saved by apply_patch_stage.
            # M3 (аудит 2026-07-01): матчим ТОЛЬКО нормализованным полным
            # относительным путём — basename-fallback (Path(file_name).name)
            # на проектах с несколькими `__init__.py` подставлял «оригинал»
            # ЧУЖОГО файла, и rollback записывал чужое содержимое.
            pre_patch_map = context.metadata.get("_pre_patch_content") or {}
            _norm = file_name.replace("\\", "/")
            orig_from_pre = None
            for _k, _v in pre_patch_map.items():
                if str(_k).replace("\\", "/") == _norm:
                    orig_from_pre = _v
                    break
            if orig_from_pre is not None:
                original_content = orig_from_pre
            else:
                backup_path = context.metadata.get("last_backup_path")
                original_content = None
                if backup_path:
                    backup_file = Path(backup_path)
                    if backup_file.exists():
                        original_content = backup_file.read_text(encoding="utf-8", errors="replace")
                if original_content is None:
                    # C4 (аудит 2026-07-01): раньше здесь был fallback
                    # `original_content = patched_content` — снапшот с
                    # original==patched превращал последующий rollback в
                    # запись ПАТЧЕННОГО содержимого («откат», который ничего
                    # не откатывает), а symbol/erosion guard-ы сравнивали
                    # файл сам с собой. Лживый снапшот вреднее отсутствующего:
                    # не создаём его и ставим snapshot_failed — DecideStage
                    # не даст такому патчу ACCEPT (fail-closed).
                    logger.warning(
                        "  Снапшот для %s НЕ создан: нет pre-patch содержимого "
                        "(_pre_patch_content/backup) — snapshot_failed, ACCEPT будет заблокирован",
                        file_name,
                    )
                    _sf_meta = dict(context.metadata)
                    _sf_meta["snapshot_failed"] = {
                        "file": file_name, "reason": "no_pre_patch_content",
                    }
                    return context.update(metadata=_sf_meta)

            # Сохраняем снапшот
            error_line = error.get("line", 0)
            new_metadata = dict(context.metadata)
            # Снапшот создан с правдивым оригиналом — снимаем возможный флаг
            # прошлой неудачи (C4/H3).
            new_metadata.pop("snapshot_failed", None)
            # M5 (аудит 2026-07-01): копия списка — раньше append мутировал
            # список, разделяемый со старыми контекстами (ломает copy-on-write).
            snapshots = list(new_metadata.get("patch_snapshots", []))
            snapshots.append({
                "file": file_name,
                "error_line": error_line,
                "original_content": original_content,
                "patched_content": patched_content,
                "error_code": error.get("code", ""),
                "error_message": error.get("message", ""),
            })
            new_metadata["patch_snapshots"] = snapshots

            # O.14: анти-регрессия символов. На реальном прогоне 2026-06-02
            # движок потерял `def hash_password` из `auth.py` (LLM «выпрямил»
            # файл). Аудит этого не видел, потому что синтаксис остался
            # валиден. Здесь сравниваем множества def/class в before/after
            # и при пропадаемых именах поднимаем флаг — DecideStage уведёт
            # такой патч в NEEDS_REVIEW вместо ACCEPT.
            try:
                from analysis.symbol_regression import (
                    check_symbol_regression,
                    check_symbol_duplication,
                )
                reg = check_symbol_regression(
                    original_content, patched_content,
                    context.language or "",
                )
                if not reg.get("ok", True):
                    missing_defs = list(reg.get("missing_defs", []))
                    missing_classes = list(reg.get("missing_classes", []))
                    missing_imports = list(reg.get("missing_imports", []))
                    missing_overloads = list(reg.get("missing_overloads", []))
                    new_metadata["symbol_regression"] = {
                        "file": file_name,
                        "missing_defs": missing_defs,
                        "missing_classes": missing_classes,
                        "missing_imports": missing_imports,
                        "missing_overloads": missing_overloads,
                    }
                    logger.warning(
                        "  O.14: после патча из %s исчезли символы "
                        "(defs=%s, classes=%s, imports=%s, overloads=%s) — пометим NEEDS_REVIEW",
                        file_name, missing_defs, missing_classes, missing_imports, missing_overloads,
                    )
                else:
                    new_metadata.pop("symbol_regression", None)

                # O.17: анти-дубль. Кейс 2026-06-04 — в main.py появились
                # ДВЕ `def main` и дубли импортов, потому что PatchEngine
                # сделал pure-insert вместо replace, и старый файл остался
                # рядом с новым.
                dup = check_symbol_duplication(
                    original_content, patched_content,
                    context.language or "",
                )
                if not dup.get("ok", True):
                    new_metadata["symbol_duplication"] = {
                        "file": file_name,
                        "duplicated_defs": list(dup.get("duplicated_defs", [])),
                        "duplicated_classes": list(dup.get("duplicated_classes", [])),
                    }
                    logger.warning(
                        "  O.17: после патча в %s дублируются символы "
                        "(defs=%s, classes=%s) — пометим NEEDS_REVIEW",
                        file_name, dup.get("duplicated_defs"),
                        dup.get("duplicated_classes"),
                    )
                else:
                    new_metadata.pop("symbol_duplication", None)
            except Exception as e:
                logger.debug("  O.14/O.17: проверка символов пропущена: %s", e)

            # O.18: анти-эрозия типов. Control series 2026-06-23
            # (Skyscanner/pycfmodel): LLM «решала» mypy-ошибки добавлением
            # Any/cast(Any, ...) вместо содержательной правки — ошибка
            # формально уходила, типобезопасность снижалась. Не относится к
            # import-not-found/import-untyped/untyped-decorator — там
            # `# type: ignore` детерминированный и единственно доступный фикс.
            if (context.language or "").lower() in ("python", "py"):
                try:
                    from analysis.type_erosion_guard import check_type_erosion
                    erosion = check_type_erosion(
                        original_content, patched_content, error.get("code", ""),
                    )
                    if not erosion.get("ok", True):
                        new_metadata["type_erosion"] = {
                            "file": file_name,
                            "markers": erosion.get("markers", []),
                        }
                        logger.warning(
                            "  O.18: после патча из %s обнаружена типовая эрозия "
                            "(markers=%s) для кода %s — пометим NEEDS_REVIEW",
                            file_name, erosion.get("markers"), error.get("code", ""),
                        )
                    else:
                        new_metadata.pop("type_erosion", None)
                except Exception as e:
                    logger.debug("  O.18: проверка типовой эрозии пропущена: %s", e)

                # O.19: анти-порча данных. Живой прогон pyca/bcrypt (2026-07-02):
                # LLM «чинила» mypy list-item/arg-type/assignment в
                # tests/test_bcrypt.py, понижая тип тестовых векторов
                # (b"salt"→"salt", 4→"4", hex-bytes→plaintext-str) — mypy
                # замолкал честно, новой flake8-ошибки не было, все прежние
                # гейты молчали. Ловим по уменьшению числа bytes-/числовых
                # литералов в файле (см. analysis/data_literal_guard.py).
                try:
                    from analysis.data_literal_guard import check_data_literal_mangling
                    mangling = check_data_literal_mangling(
                        original_content, patched_content, error.get("code", ""),
                    )
                    if not mangling.get("ok", True):
                        new_metadata["data_literal_mangling"] = {
                            "file": file_name,
                            "markers": mangling.get("markers", []),
                            "detail": mangling.get("detail", {}),
                        }
                        logger.warning(
                            "  O.19: после патча из %s обнаружена порча данных "
                            "(markers=%s) для кода %s — пометим NEEDS_REVIEW",
                            file_name, mangling.get("markers"), error.get("code", ""),
                        )
                    else:
                        new_metadata.pop("data_literal_mangling", None)
                except Exception as e:
                    logger.debug("  O.19: проверка порчи данных пропущена: %s", e)

            # Q.1: Logic Guard — AST contract diff (Python only).
            if self._logic_guard_enabled(context):
                try:
                    from analysis.logic_guard import LogicGuard
                    lg_result = LogicGuard.check(
                        original_content, patched_content,
                        context.language or "",
                    )
                    if lg_result.violations:
                        high_count = sum(
                            1 for v in lg_result.violations if v.severity == "high"
                        )
                        new_metadata["logic_guard"] = {
                            "violations": [
                                {
                                    "function_name": v.function_name,
                                    "type": v.type,
                                    "severity": v.severity,
                                    "detail": v.detail,
                                }
                                for v in lg_result.violations
                            ],
                            "checked_functions": lg_result.checked_functions,
                            "skipped_reason": lg_result.skipped_reason,
                        }
                        logger.warning(
                            "  Q.1: logic guard: %d нарушений (%d высокого) в %s",
                            len(lg_result.violations), high_count, file_name,
                        )
                    else:
                        new_metadata.pop("logic_guard", None)
                except Exception as e:
                    logger.debug("  Q.1: logic guard пропущен: %s", e)

            context = context.update(metadata=new_metadata)
            logger.debug("  Сохранён снапшот для файла %s (строка %d)", file_name, error_line)

        except Exception as e:
            # H3 (аудит 2026-07-01): fail-closed. Раньше сбой здесь означал
            # «нет снапшота и нет O.14/O.17/O.18-флагов» → DecideStage мог
            # выдать ACCEPT без какой-либо символьной защиты и без
            # возможности отката (гипотеза «silent exception» из P0
            # unsafe_accept). Теперь патч с проваленным снапшотом не может
            # быть принят.
            logger.warning("  Не удалось сохранить снапшот: %s — snapshot_failed", e)
            _sf_meta = dict(context.metadata)
            _sf_meta["snapshot_failed"] = {
                "file": file_name, "reason": f"exception: {e}",
            }
            context = context.update(metadata=_sf_meta)

        return context

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия VALIDATE: валидация изменений...")
        work_path = getattr(context, 'working_path', None) or context.project_path
        logger.debug("  Запуск валидации: проект %s, язык %s", work_path, context.language)

        # --- ЛОКАЛЬНАЯ ПРОВЕРКА ЦЕЛЕВОГО ФАЙЛА ---
        # G.5×Rust: для TEST_FAILURE/SEMANTIC_MISMATCH пропускаем rust file-scoped
        # fast-path и идём в generic-путь с проектным счётчиком (after=len(new_errors)).
        if (context.language in ("rust", "rs") and context.selected_error
                and not self._is_project_scoped_error(context.selected_error)):
            file_path_str = context.selected_error.get("file", "")
            if file_path_str:
                file_path = work_path / file_path_str
                if file_path.exists():
                    # before считаем ТОЛЬКО по текущему файлу, чтобы сравнение с
                    # after (rustc по одному файлу) было корректным по масштабу
                    before = sum(1 for e in context.current_errors
                                 if e.get("file") == file_path_str)
                    after = self._count_file_errors(file_path)
                    logger.info("  Ошибок в файле %s до: %d, после: %d", file_path_str, before, after)

                    # --- ПРОВЕРКА ВСЕГО ПРОЕКТА ---
                    project_before = len(context.current_errors)
                    project_after = self._count_project_errors(work_path)
                    logger.info("  Ошибок в проекте до: %d, после: %d", project_before, project_after)

                    results = {
                        "compile": {"success": project_after <= project_before, "output": []},
                        "lint": {"success": True, "output": []},
                        "security": {"success": True, "output": []},
                        "error_count_before": before,
                        "error_count_after": after,
                        "project_error_count_before": project_before,
                        "project_error_count_after": project_after,
                        "current_errors_before": list(context.current_errors),
                    }
                    context = context.set_validation_results(results)

                    # Сохраняем снапшот для проверки инвариантов
                    context = self._save_patch_snapshot(context)

                    # === НОВЫЙ БЛОК: Hypothesis property‑based проверка ===
                    if not self._run_hypothesis_check(context, file_path):
                        logger.warning("  Hypothesis обнаружил потенциальный дефект – патч отклоняется")
                        results["hypothesis"] = {"success": False, "reason": "property_violation"}
                        context = context.set_validation_results(results)
                        return context.add_state_to_history(State.NEXT_ERROR)
                    else:
                        results["hypothesis"] = {"success": True}
                        context = context.set_validation_results(results)

                    try:
                        new_errors = run_with_timeout(self.analyzer.analyze, 1200, work_path)
                        # G.5: до-бавляем оставшиеся падения тестов (под флагом),
                        # чтобы счётчик after отразил «тест всё ещё падает».
                        new_errors, _tp_ok = self._merge_test_failures(new_errors, context, work_path)
                        context = context.set_errors(new_errors)
                        self.degradation.update(new_errors)
                        # Перезаписываем error_count_after файл-скопированной суммой
                        # из ПОЛНОГО ре-анализа (cargo check + build + clippy).
                        # Иначе single-file rustc shortcut занижает счётчик, когда
                        # патч ломает файл синтаксически сильнее (rustc стопится
                        # на первых ошибках) и DecideStage ACCEPT'ит порчу.
                        file_after_full = sum(1 for e in new_errors
                                              if e.get("file") == file_path_str)
                        if file_after_full != after:
                            logger.info(
                                "  error_count_after скорректирован полным анализом: %d -> %d",
                                after, file_after_full,
                            )
                        new_results = dict(results)
                        new_results["error_count_after"] = file_after_full
                        new_results["project_error_count_after"] = len(new_errors)
                        context = context.set_validation_results(new_results)
                    except Exception as e:
                        logger.warning("  Повторный анализ не удался: %s", e)

                    logger.info("  Переход к стадии ревью (D.3)")
                    return context.add_state_to_history(State.REVIEWING)

        # --- ДЛЯ ДРУГИХ ЯЗЫКОВ (или если нет выбранной ошибки) ---
        # P0-фикс (tech debt audit 2026-06-21, #1): раньше единая AND-связка
        # (use_mypy AND use_bandit) почти никогда не выполнялась в реальном конфиге
        # (use_bandit=False по умолчанию) — все три легаси-чекера гоняли full-project
        # скан на КАЖДУЮ попытку патча. Теперь skip решается НЕЗАВИСИМО для каждого
        # инструмента: lint всегда (дублирует analyzer.analyze() безусловно), compile/
        # security — только если соответствующий АНАЛИЗАТОР включён.
        results = {}
        _skip_map = {
            "compile": self._skip_legacy_compile_check(context),
            "lint": self._skip_legacy_lint_check(context),
            "security": self._skip_legacy_security_check(context),
        }
        for name, tool, timeout in [("compile", self.compiler, 300),
                                    ("lint", self.linter, 120),
                                    ("security", self.security, 120)]:
            if _skip_map[name]:
                results[name] = {"success": True, "output": []}
                logger.debug("  O2: legacy %s skipped (covered by main analyzer)", name)
                continue
            logger.debug("  Запуск инструмента: %s (таймаут %ds)", name, timeout)
            try:
                ok, out = run_with_timeout(tool.run, timeout, work_path, context.language)
                results[name] = {"success": ok, "output": out}
                logger.info("  %s: %s", name, "успех" if ok else "провал")
            except Exception as e:
                results[name] = {"success": False, "output": [str(e)]}
                logger.warning("  %s: ошибка - %s", name, e)

        # Если mypy сообщил [syntax] в целевом файле патча, проверяем через ast.parse().
        # Mypy иногда атрибутирует [syntax] соседнего файла (e.g. auth.py: if x = ...)
        # на целевой файл при проверке всей директории. ast.parse() — авторитетный
        # арбитр: если Python-парсер не видит SyntaxError, ошибка mypy ложная.
        if (not results.get("compile", {}).get("success")
                and context.selected_error
                and (context.language or "").lower() in ("python", "py")):
            target_file = context.selected_error.get("file", "")
            compile_out = results.get("compile", {}).get("output", [])
            target_has_syntax = any(
                target_file in line and "[syntax]" in line for line in compile_out
            )
            if target_has_syntax and target_file:
                fpath = work_path / target_file
                if fpath.exists():
                    try:
                        ast.parse(
                            fpath.read_text(encoding="utf-8", errors="replace"),
                            filename=str(fpath),
                        )
                        results["compile"]["success"] = True
                        logger.info(
                            "  compile: mypy [syntax] в %s опровергнут ast.parse() "
                            "— bleed-through от соседнего файла, считаем pre-existing",
                            target_file,
                        )
                    except SyntaxError:
                        logger.debug(
                            "  compile: ast.parse() подтвердил SyntaxError в %s",
                            target_file,
                        )

        context = context.set_before_validation(context.validation_results)

        # Инкрементальная валидация (pipeline.incremental_analysis): вместо
        # полного пересканирования всего проекта на КАЖДУЮ попытку патча
        # (accept И reject — это самая частая точка полного скана во всём
        # пайплайне) — пересканируем только файл, который только что
        # патчили. Ошибки остальных файлов берём из context.current_errors
        # (последний известный снимок — полный или инкрементальный) и
        # заменяем в нём только записи целевого файла через merge_file_errors,
        # что даёт корректный «полно-эквивалентный» after-список без вызова
        # анализаторов по всему проекту.
        _incr_target_file = (
            context.selected_error.get("file", "") if context.selected_error else ""
        )
        _incremental = bool(
            self._incremental_enabled_for(context)
            and _incr_target_file
            and self._incremental_lang_ok(context.language, _incr_target_file)
        )

        # №1 (2026-07-10): в C++-прогоне патч НЕ-C++-файла (.py/.yml/.md — напр.
        # semgrep-находка в python-тулинге проекта) НЕ может изменить вывод
        # CppAnalyzer (тот читает только .c/.cpp/.cc/.h). Полный C++ пере-скан
        # (~100с на fmt) здесь чистая трата — переиспользуем снимок последнего
        # C++-скана. Вывод БАЙТ-В-БАЙТ равен analyze(), поэтому ни одно решение
        # не меняется, только скорость. Снимок (`_cpp_full_snapshot`) — чистый
        # вывод CppAnalyzer, обновляется при полном скане и TU-инкрементале ниже.
        _cpp_run = (context.language or "").lower() in self._CPP_LANGS
        _tgt_lower = (_incr_target_file or "").lower()
        _target_is_cpp_src = _tgt_lower.endswith(self._CPP_SRC_SUFFIXES)
        _cpp_snapshot = context.metadata.get("_cpp_full_snapshot")
        _reuse_cpp_snapshot = bool(
            _cpp_run and _incr_target_file and not _target_is_cpp_src
            and _cpp_snapshot is not None
        )

        try:
            if _reuse_cpp_snapshot:
                new_errors_primary = list(_cpp_snapshot)
                logger.info(
                    "  C++ primary re-scan пропущен (№1): цель %s — не C++-исходник, "
                    "вывод анализатора неизменен, снимок %d ошибок",
                    _incr_target_file, len(new_errors_primary),
                )
            elif _incremental:
                new_errors_primary = run_with_timeout(
                    self.analyzer.analyze, 1200, work_path, files=[_incr_target_file]
                )
            else:
                new_errors_primary = run_with_timeout(self.analyzer.analyze, 1200, work_path)
            # G.5: до-бавляем оставшиеся падения тестов (под флагом). Если тест,
            # ради которого правили, всё ещё падает — счётчик after не упадёт и
            # патч будет отклонён существующей логикой DecideStage (+rollback).
            new_errors_primary, _tp_ok = self._merge_test_failures(new_errors_primary, context, work_path)
            # P2.7 Fix #3: run ruff (if enabled, same conditions as analyze_stage)
            ruff_new_errors = []
            if self._use_ruff_enabled(context) and (context.language or "").lower() in ("python", "py"):
                try:
                    from analyzers.ruff_analyzer import RuffAnalyzer
                    _ruff = RuffAnalyzer()
                    if _incremental:
                        ruff_new_errors = _ruff.analyze(work_path, files=[_incr_target_file]) or []
                    else:
                        ruff_new_errors = _ruff.analyze(work_path) or []
                    if ruff_new_errors:
                        logger.info("  validate ruff: %d findings", len(ruff_new_errors))
                except Exception as _exc:
                    logger.warning("  validate ruff failed: %s", _exc)
                    ruff_new_errors = []
            # Merge via dedup (file, line, code) — different codes (E999 vs invalid-syntax)
            # are kept even if message is identical, same-code duplicates are dropped.
            new_errors = list(new_errors_primary)
            _seen_keys = {
                (e.get("file", ""), e.get("line", 0), e.get("code", ""))
                for e in new_errors
            }
            for _err in ruff_new_errors:
                _k = (_err.get("file", ""), _err.get("line", 0), _err.get("code", ""))
                if _k not in _seen_keys:
                    new_errors.append(_err)
                    _seen_keys.add(_k)

            if _incremental:
                # new_errors сейчас — только записи ИЗМЕНЁННОГО файла. Достраиваем
                # до полно-эквивалентного списка через слияние с current_errors.
                _file_only_new_errors = new_errors
                _merged_ctx = context.merge_file_errors(_incr_target_file, _file_only_new_errors)
                _before_total = len(context.current_errors)
                after = len(_merged_ctx.current_errors)
                before = _before_total
                results["error_count_before"] = before
                results["error_count_after"] = after
                results["current_errors_before"] = list(context.current_errors)
                logger.info(
                    "  Ошибок до: %d, после: %d (инкрементально, файл %s: %d новых записей)",
                    before, after, _incr_target_file, len(_file_only_new_errors),
                )
                context = _merged_ctx
                # ВАЖНО: net_delta_check и код ниже (file-level before/after,
                # degradation.update) читают переменную `new_errors`, ожидая
                # ПОЛНЫЙ список по всему проекту (как в неинкрементальной ветке) —
                # переприсваиваем на полно-эквивалентный merged-список.
                new_errors = list(context.current_errors)
                # Простой комбинированный счётчик вместо раздельного primary/ruff —
                # в инкрементальном режиме не пытаемся декомпозировать "сколько из
                # общего числа было от flake8 vs ruff для других файлов", это
                # неизвлекаемо из уже смешанного current_errors. Следующий вызов
                # validate увидит корректный total через before=len(current_errors).
                _vm = dict(context.metadata)
                _vm["_primary_error_count_before"] = after
                _vm["_ruff_count_before"] = 0
                context = context.update(metadata=_vm)
                self.degradation.update(list(context.current_errors))
            else:
                # P2.7 Fix #2+#3: combined flake8+ruff baseline for symmetric before/after comparison
                _pb = context.metadata.get("_primary_error_count_before")
                _rb = int(context.metadata.get("_ruff_count_before") or 0)
                before = (int(_pb) + _rb) if _pb is not None else len(context.current_errors)
                after = len(new_errors)
                results["error_count_before"] = before
                results["error_count_after"] = after
                results["current_errors_before"] = list(context.current_errors)
                logger.info("  Ошибок до: %d, после: %d", before, after)
                context = context.set_errors(new_errors)
                # P2.7 Fix #2+#3: update both baselines for subsequent errors in the same run
                _vm = dict(context.metadata)
                _vm["_primary_error_count_before"] = len(new_errors_primary)
                _vm["_ruff_count_before"] = len(ruff_new_errors)
                context = context.update(metadata=_vm)
                self.degradation.update(new_errors)

            # №1: поддержка снимка чистого вывода CppAnalyzer (`_cpp_full_snapshot`)
            # для переиспользования при патче не-C++ файла. Обновляем ТОЛЬКО из
            # реального C++-скана (полного или TU-инкрементального); при reuse
            # снимок и так актуален. Заголовок/полный скан → снимок = свежий
            # полный вывод; TU-инкрементал → заменяем слайс целевого файла.
            if _cpp_run and not _reuse_cpp_snapshot:
                if _incremental and _cpp_snapshot is not None:
                    _tf_norm = str(_incr_target_file).replace("\\", "/").lstrip("/")
                    _kept = [
                        e for e in _cpp_snapshot
                        if str(e.get("file", "")).replace("\\", "/").lstrip("/") != _tf_norm
                    ]
                    _snap = _kept + list(new_errors_primary)
                elif not _incremental:
                    # полный C++ скан (заголовок/сид/фолбэк) — new_errors_primary
                    # это чистый вывод CppAnalyzer (ruff отключён для C++)
                    _snap = list(new_errors_primary)
                else:
                    _snap = None  # TU-инкрементал без сид-снимка — не строим частичный
                if _snap is not None:
                    _sm = dict(context.metadata)
                    _sm["_cpp_full_snapshot"] = _snap
                    context = context.update(metadata=_sm)

            # --- NET-DELTA SMART CLASSIFICATION ---
            # Distinguish regression (new errors caused by the patch) from
            # diagnostic unmasking (pre-existing errors now visible) and
            # uncertain cases. See core/stages/net_delta_check.py for logic.
            _nd_error = context.selected_error or {}
            _nd_file = _nd_error.get("file", "")
            if _nd_file:
                from core.stages.net_delta_check import classify_net_delta as _nd_classify
                _nd_patch = (
                    context.generated_patch
                    if isinstance(context.generated_patch, str) else ""
                )
                _nd_cls = _nd_classify(
                    before_errors=list(results.get("current_errors_before") or []),
                    after_errors=new_errors,
                    patched_file=_nd_file,
                    patch_text=_nd_patch,
                    original_error=_nd_error,
                )
                if _nd_cls is not None:
                    _nd_meta = _nd_cls.to_metadata()
                    results["net_delta_classification"] = _nd_meta
                    results["net_delta_file"] = _nd_file
                    # Always record file-level delta for reporting
                    _nd_file_norm = _nd_file.replace("\\", "/").lstrip("/")
                    _nd_file_before = sum(
                        1 for e in (results.get("current_errors_before") or [])
                        if (e.get("file") or "").replace("\\", "/").lstrip("/") == _nd_file_norm
                    )
                    _nd_file_after = sum(
                        1 for e in new_errors
                        if (e.get("file") or "").replace("\\", "/").lstrip("/") == _nd_file_norm
                    )
                    results["net_delta"] = _nd_file_after - _nd_file_before

                    _nd_decision = _nd_cls.decision
                    _nd_src = context.metadata.get("patch_source", "?")

                    if _nd_decision == "rollback":
                        from core.pipeline_stage import PipelineStage as _PS
                        # EDGE-5: if this file was already capped in a prior rollback,
                        # skip immediately without performing another rollback or patch.
                        _nd_capped = set(context.metadata.get("_net_delta_capped_files") or [])
                        if _nd_file in _nd_capped:
                            logger.warning(
                                "  NET_DELTA per-file cap (cached): '%s' уже заблокирован — пропуск без отката",
                                _nd_file,
                            )
                            context = context.record_processed_error(
                                _process_key(_nd_error)
                            )
                            context = context.add_rejected_patch(
                                {"error": _nd_error, "reason": "net_delta_per_file_cap"}
                            )
                            return context.add_state_to_history(State.NEXT_ERROR)

                        logger.warning(
                            "  NET_DELTA regression: +%d новых ошибок у "
                            "изменённых строк в %s (class=%s src=%s) — откат",
                            len(_nd_cls.regression), _nd_file,
                            _nd_error.get("error_class"), _nd_src,
                        )
                        _pre = (context.metadata or {}).get("_pre_patch_content") or {}
                        for _rel, _orig in _pre.items():
                            try:
                                (work_path / _rel).write_text(_orig, encoding="utf-8")
                            except Exception as _re:
                                logger.warning("  NET_DELTA rollback %s: %s", _rel, _re)
                        _ndm = dict(context.metadata)
                        # C6 (аудит 2026-07-01): карта потреблена откатом —
                        # оставленная, она могла быть записана ПОВТОРНО при
                        # net-delta следующей ошибки (например HEALED-пути,
                        # не устанавливающего свою карту), стирая уже
                        # ПРИНЯТУЮ работу предыдущего патча.
                        _ndm.pop("_pre_patch_content", None)
                        # Конверсия-2 (2026-07-02): baseline-счётчики после
                        # отката stale (см. decide_stage._rollback_file_from_snapshot).
                        _ndm.pop("_primary_error_count_before", None)
                        _ndm.pop("_ruff_count_before", None)
                        _ndm["net_delta_rollback"] = True
                        _ndm["net_delta_classification"] = "regression"
                        _ndm["net_delta"] = len(_nd_cls.regression)
                        _ndm["net_delta_rollback_count"] = int(_ndm.get("net_delta_rollback_count", 0)) + 1
                        # Per-file rollback cap: after MAX_NET_DELTA_ROLLBACKS_PER_FILE,
                        # bulk-skip all remaining errors from the same file.
                        _nd_file_counts = dict(_ndm.get("_net_delta_file_counts", {}))
                        _nd_file_counts[_nd_file] = _nd_file_counts.get(_nd_file, 0) + 1
                        _ndm["_net_delta_file_counts"] = _nd_file_counts

                        # IMP-E: first rollback for this error → retry with regression feedback.
                        _nd_error_sig = _PS._static_signature(_nd_error)
                        _nd_err_retries = dict(_ndm.get("_net_delta_error_retries") or {})
                        if _nd_err_retries.get(_nd_error_sig, 0) == 0:
                            _nd_err_retries[_nd_error_sig] = 1
                            _ndm["_net_delta_error_retries"] = _nd_err_retries
                            _reg_lines = sorted({e.get("line", "?") for e in _nd_cls.regression})
                            _reg_codes = sorted({e.get("code", "?") for e in _nd_cls.regression})
                            from core.contract import MetadataKeys as _MK
                            _ndm[_MK.LAST_PATCH_FAILURE] = (
                                f"NET_DELTA regression: your patch introduced "
                                f"{len(_nd_cls.regression)} new error(s) near line(s) "
                                f"{_reg_lines} in {_nd_file} (codes: {_reg_codes}). "
                                "Rewrite the fix to avoid touching those surrounding lines — "
                                "use a more surgical, minimal change."
                            )
                            context = context.update(metadata=_ndm)
                            logger.info(
                                "  NET_DELTA retry (IMP-E): %d regression(s) — "
                                "перепробуем с feedback для %s",
                                len(_nd_cls.regression), _nd_error_sig,
                            )
                            return context.add_state_to_history(State.GENERATING_PATCH)

                        # Second+ rollback for same error → NEXT_ERROR as before.
                        _ndm["_net_delta_error_retries"] = _nd_err_retries
                        context = context.update(metadata=_ndm)
                        context = context.record_processed_error(_process_key(_nd_error))
                        context = context.add_rejected_patch(
                            {"error": _nd_error, "reason": "net_delta_regression"}
                        )
                        if _nd_file_counts[_nd_file] >= self.MAX_NET_DELTA_ROLLBACKS_PER_FILE:
                            logger.warning(
                                "  NET_DELTA per-file cap: %d rollbacks для '%s' — bulk-skip всех ошибок файла",
                                _nd_file_counts[_nd_file], _nd_file,
                            )
                            # EDGE-5: persist capped file so future iterations skip immediately.
                            _nd_capped.add(_nd_file)
                            _ndm2 = dict(context.metadata)
                            _ndm2["_net_delta_capped_files"] = list(_nd_capped)
                            context = context.update(metadata=_ndm2)
                            _new_proc = dict(context.processed_errors)
                            for _pending in context.current_errors:
                                if _pending.get("file", "") == _nd_file:
                                    # Codex-5 (2026-07-02): ключ processed_errors
                                    # ДОЛЖЕН быть process_key (signature + "::L{line}").
                                    # Селектор гейтит по _process_key; раньше писался
                                    # _static_signature (base, без строки) — селектор
                                    # его не читал, и bulk-skip после 3 net-delta
                                    # откатов был no-op.
                                    _pkey = _process_key(_pending)
                                    if _new_proc.get(_pkey, 0) < 3:
                                        _new_proc[_pkey] = 3
                            context = context.update(processed_errors=MappingProxyType(_new_proc))
                        return context.add_state_to_history(State.NEXT_ERROR)

                    elif _nd_decision == "needs_review":
                        logger.info(
                            "  NET_DELTA uncertain: +%d новых ошибок "
                            "(uncertain=%d unmasked=%d) в %s — "
                            "откат + NEEDS_REVIEW",
                            len(_nd_cls.uncertain) + len(_nd_cls.unmasked),
                            len(_nd_cls.uncertain), len(_nd_cls.unmasked),
                            _nd_file,
                        )
                        _pre = (context.metadata or {}).get("_pre_patch_content") or {}
                        for _rel, _orig in _pre.items():
                            try:
                                (work_path / _rel).write_text(_orig, encoding="utf-8")
                            except Exception as _re:
                                logger.warning(
                                    "  NET_DELTA NR rollback %s: %s", _rel, _re
                                )
                        _ndm = dict(context.metadata)
                        # C6: карта потреблена откатом (см. rollback-ветку выше).
                        _ndm.pop("_pre_patch_content", None)
                        # Конверсия-2: baseline-счётчики после отката stale.
                        _ndm.pop("_primary_error_count_before", None)
                        _ndm.pop("_ruff_count_before", None)
                        _ndm["net_delta_classification"] = "uncertain"
                        _ndm["net_delta_uncertain"] = True
                        _ndm["net_delta"] = len(_nd_cls.uncertain)
                        _ndm["net_delta_uncertain_count"] = int(_ndm.get("net_delta_uncertain_count", 0)) + 1
                        _ndm["_needs_review_pending_reason"] = "net_delta_uncertain"
                        context = context.update(metadata=_ndm)
                        # Q3 (2026-07-07): net_delta_uncertain — NR-исход того же
                        # неверифицируемого mypy-класса, кормит toxic-счётчик
                        # DecideStage (classmethod — один источник истины, §3).
                        # Этот путь минует DecideStage (прямой вызов
                        # NeedsReviewStage ниже), поэтому без явного вызова здесь
                        # класс продолжал жечь LLM до конца прогона (bcrypt
                        # 20260703-121449z: 11 решений, real_fix_impact=0).
                        from core.stages.decide_stage import DecideStage as _DS
                        context = _DS._track_toxic_signature(
                            context, _nd_error, "net_delta_uncertain"
                        )
                        from core.stages.needs_review_stage import NeedsReviewStage as _NRS
                        return _NRS().execute(context)

                    else:  # keep — only unmasked errors
                        logger.info(
                            "  NET_DELTA unmasked: +%d ошибок сняты маской "
                            "(class=%s) — патч сохраняется",
                            len(_nd_cls.unmasked), _nd_error.get("error_class"),
                        )
                        _ndm = dict(context.metadata)
                        _ndm["net_delta_classification"] = "unmasked"
                        _ndm["net_delta_unmasked_count"] = len(_nd_cls.unmasked)
                        context = context.update(metadata=_ndm)
            # --- end net-delta ---

        except Exception as e:
            logger.error("  Повторный анализ не удался: %s", e)
            results["error_count_after"] = len(context.current_errors)
            results["current_errors_before"] = list(context.current_errors)

        # Конверсия-1 (2026-07-02): точечный mypy-re-check целевой ошибки.
        # PythonAnalyzer выше — flake8-only: mypy-ошибки (import-not-found/
        # attr-defined/arg-type/...) в after-скане ОТСУТСТВУЮТ в принципе,
        # поэтому «error_count_decreased» для них невыполним, а
        # target_still_present по flake8-списку всегда False — даже для
        # патча, который ничего не исправил. Здесь mypy запускается ТОЛЬКО
        # на файле цели и только для mypy-кодов; DecideStage использует
        # результат как истину о target_still_present (в обе стороны).
        self._recheck_mypy_target(context, results, work_path)

        # Сохраняем снапшот для инвариантов
        context = self._save_patch_snapshot(context)

        # === Hypothesis проверка для не‑Rust языков ===
        if context.selected_error:
            error_file = context.selected_error.get("file", "")
            if error_file:
                fpath = work_path / error_file
                if fpath.exists():
                    if not self._run_hypothesis_check(context, fpath):
                        logger.warning("  Hypothesis обнаружил дефект")
                        results["hypothesis"] = {"success": False, "reason": "property_violation"}
                    else:
                        results["hypothesis"] = {"success": True}

        context = context.set_validation_results(results)
        logger.info("  Переход к стадии ревью (D.3)")
        return context.add_state_to_history(State.REVIEWING)

    # Конверсия-1 (2026-07-02): mypy-коды — lowercase-слова через дефис
    # (import-not-found, attr-defined, arg-type, ...). flake8/ruff — буква+
    # цифры (E501/W291/F821), semgrep — с точками, bandit — B###. syntax/
    # invalid-syntax исключены: это E999-домен, обрабатывается своим путём.
    _MYPY_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
    _MYPY_RECHECK_EXCLUDE = frozenset({"syntax", "invalid-syntax"})

    def _recheck_mypy_target(self, context: PipelineContext, results: dict, work_path: Path) -> None:
        """Точечный mypy-прогон файла целевой ошибки → results["target_recheck"].

        Кладёт {"performed": True, "tool": "mypy", "present": bool} — DecideStage
        использует `present` как истину о target_still_present для mypy-кодов.
        Любой сбой — «recheck не выполнялся» (ключ не ставится): поведение
        деградирует до прежнего, а не до ложного вердикта."""
        error = context.selected_error or {}
        code = str(error.get("code") or "")
        file_rel = str(error.get("file") or "")
        if not code or not file_rel:
            return
        if (context.language or "").lower() not in ("python", "py"):
            return
        if code in self._MYPY_RECHECK_EXCLUDE or not self._MYPY_CODE_RE.match(code):
            return
        try:
            from analyzers.mypy_analyzer import MypyAnalyzer
            mypy = MypyAnalyzer()
            if not mypy.available():
                return
            file_errors = mypy.analyze(work_path, files=[file_rel]) or []
        except Exception as e:
            logger.debug("  mypy target recheck пропущен: %s", e)
            return
        target_sig = PipelineStage._static_signature(error)
        _norm_target = target_sig.replace("\\", "/")
        present = any(
            PipelineStage._static_signature(e).replace("\\", "/") == _norm_target
            for e in file_errors
        )
        results["target_recheck"] = {
            "performed": True, "tool": "mypy", "present": present,
            "file_error_count": len(file_errors),
        }
        logger.info(
            "  mypy target recheck: %s %s — %s (%d mypy-ошибок в файле)",
            file_rel, code, "ВСЁ ЕЩЁ ПРИСУТСТВУЕТ" if present else "исправлена",
            len(file_errors),
        )

    def _run_hypothesis_check(self, context: PipelineContext, file_path: Path) -> bool:
        """Запускает HypothesisValidator, возвращает True если проверка пройдена."""
        if not self.hypothesis.enabled:
            return True  # Если отключён — считаем, что всё хорошо

        patched_content = file_path.read_text(encoding="utf-8")
        # Советчик сам решит, можно ли проверить
        result = self.hypothesis.safe_run(file_path=file_path, patched_content=patched_content)
        # Если вернул None (инструмент недоступен) — не блокируем
        if result is None:
            return True
        return result
