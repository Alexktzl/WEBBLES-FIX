"""
Стадия анализа проекта.
Вынесена из pipeline_stage.py для модульности.
Интегрирован Semgrep как дополнительный анализатор.
"""

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List

from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State

# ---------- Адаптер Semgrep ----------
from tools.semgrep_analyzer import SemgrepAnalyzer
# ---------- Tests as error source (Stage G) ----------
from validation.test_runner import TestRunner
# ---------- Semantic audit: docstring vs code (Stage H) ----------
from analysis.semantic_auditor import SemanticAuditor
# ---------- Security pattern scanner (Stage K) ----------
from analysis.security_scanner import SecurityScanner

logger = logging.getLogger(__name__)

# Каталоги, которые НЕ обходим при семантическом аудите (H audit-mode).
_AUDIT_SKIP_DIRS = {"target", "node_modules", ".git", "venv", ".venv",
                    "__pycache__", ".webbles_fix", ".webbles", ".webbles_backups",
                    ".webles_sandbox", ".webles_sandbox",
                    "dist", "build", ".idea", ".vscode", ".tox", ".pytest_cache",
                    ".next", ".cache", ".gradle", ".mvn", "statistic"}
# Расширения исходников по языку.
_AUDIT_EXTS = {
    "rust": (".rs",), "rs": (".rs",),
    "python": (".py",), "py": (".py",),
    "javascript": (".js", ".jsx"), "js": (".js", ".jsx"),
    "typescript": (".ts", ".tsx"), "ts": (".ts", ".tsx"),
}
# Верхняя граница на число файлов за прогон.
_AUDIT_MAX_FILES = 200


# Semgrep --config=auto не участвует в ACCEPT/REJECT-решении (ValidateStage его
# не вызывает) — он только пополняет backlog ошибок на будущие циклы. Но
# `_global_fix_loop` сбрасывает состояние в ANALYZING после КАЖДОГО принятого
# патча, так что без throttling Semgrep гонял полный project-rescan на каждом
# global_cycle: ~9.7с на demo_project, измерено напрямую (semgrep --config=auto
# тратит это время в основном на загрузку/компиляцию правил, не на размер
# проекта). На прогоне с 10 принятыми патчами это ~97с чистого повтора.
_DEFAULT_SEMGREP_SCAN_INTERVAL = 3


class _MainAnalysisFailed(Exception):
    """Внутренний маркер: первичный анализатор (flake8/cargo/tsc) упал.

    `execute()` ловит её и переводит контекст в FAILED — отдельный тип
    нужен, чтобы отличить этот случай от любых других исключений внутри
    `_collect_multi_tool_errors` (вторичные анализаторы там уже сами глушат
    свои ошибки `try/except` и не должны валить весь скан)."""


class AnalyzeStage(PipelineStage):
    def __init__(self, analyzer, llm_client=None):
        self.analyzer = analyzer
        self.semgrep = SemgrepAnalyzer()
        self.test_runner = TestRunner()
        self.llm_client = llm_client
        self._semantic_auditor = None
        self.security_scanner = SecurityScanner()
        # Semgrep throttling state (см. _get_semgrep_errors). Кэш переживает
        # все global_cycle одного прогона, т.к. AnalyzeStage — синглтон на run.
        self._semgrep_cache: list = []
        self._semgrep_cache_cycle: int = 0
        self._semgrep_file_hashes: Dict[str, str] = {}
        logger.debug("AnalyzeStage initialised with %s, Semgrep, TestRunner, SemanticAuditor(lazy), SecurityScanner",
                     type(analyzer).__name__)

    @staticmethod
    def _file_hash(path: Path) -> str:
        try:
            return hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            return ""

    def _get_semgrep_errors(self, context: PipelineContext, work_dir: Path) -> list:
        """Throttled Semgrep: полный rescan на первом цикле и далее раз в N
        циклов; на промежуточных циклах переиспользует прошлый результат, но
        предварительно сбрасывает находки по файлам, которые изменились с
        момента скана (не несём вперёд потенциально устаревшие/съехавшие
        строки — лучше на пару циклов позже найти, чем поймать stale-находку).
        """
        cfg = (context.config or {}).get("pipeline", {})
        try:
            interval = max(1, int(cfg.get("semgrep_scan_interval", _DEFAULT_SEMGREP_SCAN_INTERVAL)))
        except Exception:
            interval = _DEFAULT_SEMGREP_SCAN_INTERVAL
        cycle = int(context.metadata.get("_current_global_cycle") or 0)

        due_for_rescan = (
            self._semgrep_cache_cycle == 0
            or cycle <= 1
            or (cycle - self._semgrep_cache_cycle) >= interval
        )

        if due_for_rescan:
            semgrep_errors = self.semgrep.safe_run(project_path=work_dir) or []
            self._semgrep_cache = semgrep_errors
            self._semgrep_cache_cycle = cycle
            self._semgrep_file_hashes = {}
            for finding in semgrep_errors:
                rel = finding.get("path", "")
                if rel and rel not in self._semgrep_file_hashes:
                    self._semgrep_file_hashes[rel] = self._file_hash(work_dir / rel)
            logger.debug("Semgrep: полный rescan (global_cycle=%d)", cycle)
            return semgrep_errors

        # Переиспользуем кэш, но отфильтровываем находки в изменившихся файлах.
        fresh = []
        for finding in self._semgrep_cache:
            rel = finding.get("path", "")
            cached_hash = self._semgrep_file_hashes.get(rel, "")
            if rel and cached_hash and self._file_hash(work_dir / rel) != cached_hash:
                continue  # файл изменился с момента скана — не несём находку дальше
            fresh.append(finding)
        logger.debug(
            "Semgrep: переиспользуем кэш цикла %d (global_cycle=%d, %d/%d находок валидны)",
            self._semgrep_cache_cycle, cycle, len(fresh), len(self._semgrep_cache),
        )
        return fresh

    def _execute_incremental(self, context: PipelineContext, work_dir: Path,
                             target_files: List[str]) -> PipelineContext:
        """Инкрементальный re-scan СПИСКА файлов (один или несколько) вместо
        всего проекта.

        Вызывается из execute() когда `context.metadata["_incremental_target_file"]`
        (один файл, после ACCEPT в последовательном пути) или
        `_incremental_target_files` (несколько, после ParallelExecutor —
        см. core/engine/parallel.py) установлены PipelineEngine. Полный скан
        остаётся в начале прогона и в финальном отчёте; safety-триггер
        symbol_regression/symbol_duplication форсирует полный путь вместо этого.

        Покрывает: основной анализатор (flake8) + ruff + bandit — все три не
        делают межмодульный анализ, сужение для них безопасно (см. research
        отчёт control series 13). НЕ покрывает: semgrep (свой throttling),
        mypy (зависит от контекста ИМПОРТИРУЕМЫХ модулей — сужение даёт другой
        результат), security_scanner/pip_audit/python_runtime/tests/
        semantic_audit (редкие/опциональные, остаются на полном/периодическом
        скане). Это намеренное, документированное ограничение, не пробел.
        """
        logger.info("Stage ANALYZE: инкрементальный re-scan %d файл(ов): %s",
                    len(target_files), target_files)
        file_errors: List[Dict] = []

        try:
            try:
                file_errors = self.analyzer.analyze(work_dir, files=target_files, clean_before_each=False)
            except TypeError as te:
                msg = str(te)
                if "unexpected keyword argument" in msg and "clean_before_each" in msg:
                    file_errors = self.analyzer.analyze(work_dir, files=target_files)
                else:
                    raise
            logger.info("Инкрементальный анализ %s: найдено %d ошибок", target_files, len(file_errors))
        except Exception as e:
            logger.error("Инкрементальный анализ %s упал: %s — fallback на полный скан", target_files, e)
            new_metadata = dict(context.metadata)
            new_metadata.pop("_incremental_target_file", None)
            new_metadata.pop("_incremental_target_files", None)
            return self.execute(context.update(metadata=new_metadata))

        if self._use_ruff_enabled(context):
            try:
                from analyzers.ruff_analyzer import RuffAnalyzer
                ruff_errors = RuffAnalyzer().analyze(work_dir, files=target_files) or []
                if ruff_errors:
                    logger.info("ruff (инкрементально): %d находок", len(ruff_errors))
                    file_errors = list(file_errors) + ruff_errors
            except Exception as e:
                logger.warning("ruff (инкрементально) упал: %s", e)

        if self._use_bandit_enabled(context):
            try:
                from analyzers.bandit_analyzer import BanditAnalyzer
                bandit_errors = BanditAnalyzer().analyze(work_dir, files=target_files) or []
                if bandit_errors:
                    logger.info("bandit (инкрементально): %d находок", len(bandit_errors))
                    file_errors = list(file_errors) + bandit_errors
            except Exception as e:
                logger.warning("bandit (инкрементально) упал: %s", e)

        # Дедуп (file, line, message) — та же логика, что в полном пути.
        seen = set()
        unique_file_errors = []
        for err in file_errors:
            key = (err.get("file", ""), err.get("line", 0), err.get("message", ""))
            if key not in seen:
                seen.add(key)
                unique_file_errors.append(err)

        # Python version-aware analysis — та же фильтрация, что в полном
        # пути (см. execute()), применена и здесь для консистентности.
        if (context.language or "").lower() in ("python", "py") and (
            (context.config or {}).get("pipeline", {}).get("python_version_detection", True)
        ):
            unique_file_errors, _version_skipped = self._filter_version_incompatible_errors(
                unique_file_errors, work_dir,
            )
            if _version_skipped:
                _vvm = dict(context.metadata)
                _vvm["unsupported_python_version_items"] = list(
                    _vvm.get("unsupported_python_version_items", []) or []
                ) + _version_skipped
                _vvm["unsupported_python_version_count"] = int(
                    _vvm.get("unsupported_python_version_count", 0)
                ) + len(_version_skipped)
                context = context.update(metadata=_vvm)

        # merge_file_errors принимает один файл — для списка применяем
        # последовательно, каждый раз отдавая только записи СВОЕГО файла
        # (единый combined-скан выше уже вернул ошибки всех файлов разом,
        # экономия именно в ОДНОМ вызове анализатора, а не в N вызовах).
        for tf in target_files:
            tf_norm = str(tf).replace("\\", "/").lstrip("/")
            tf_errors = [
                e for e in unique_file_errors
                if str(e.get("file", "")).replace("\\", "/").lstrip("/") == tf_norm
            ]
            context = context.merge_file_errors(tf, tf_errors)

        # cascade_collapse — чистая in-memory пост-обработка (не внешний
        # вызов), пересчитываем на полном слитом списке для консистентности
        # с полным путём.
        cfg = getattr(context, "config", {}) or {}
        if cfg.get("pipeline", {}).get("cascade_collapse", True):
            try:
                from analysis.error_cascade import collapse_cascades
                context = context.set_errors(collapse_cascades(list(context.current_errors)))
            except Exception as e:
                logger.warning("cascade_collapse (инкрементально) пропущен: %s", e)

        new_metadata = dict(context.metadata)
        new_metadata.pop("_incremental_target_file", None)
        new_metadata.pop("_incremental_target_files", None)
        # Та же простая консолидация, что и в ValidateStage: не пытаемся
        # декомпозировать смешанный список на "сколько было от primary vs ruff".
        new_metadata["_primary_error_count_before"] = len(context.current_errors)
        new_metadata["_ruff_count_before"] = 0
        context = context.update(metadata=new_metadata)
        return context.add_state_to_history(State.CLASSIFY)

    def execute(self, context: PipelineContext) -> PipelineContext:
        work_dir = getattr(context, "working_path", context.project_path)
        logger.debug("Project: %s (lang: %s)", work_dir, context.language)

        _target_files: List[str] = []
        _single_target = context.metadata.get("_incremental_target_file")
        if _single_target:
            _target_files.append(_single_target)
        _target_files.extend(context.metadata.get("_incremental_target_files") or [])
        if _target_files and (context.language or "").lower() in ("python", "py"):
            return self._execute_incremental(context, work_dir, _target_files)

        logger.info("Stage ANALYZE: running project analysis...")

        try:
            unique_errors, breakdown, version_skipped, meta_updates = (
                self._collect_multi_tool_errors(context, work_dir)
            )
        except _MainAnalysisFailed as e:
            logger.error("Main analysis failed: %s", e.__cause__, exc_info=True)
            return context.add_state_to_history(State.FAILED)

        if version_skipped:
            _vvm = dict(context.metadata)
            _vvm["unsupported_python_version_items"] = list(
                _vvm.get("unsupported_python_version_items", []) or []
            ) + version_skipped
            _vvm["unsupported_python_version_count"] = int(
                _vvm.get("unsupported_python_version_count", 0)
            ) + len(version_skipped)
            context = context.update(metadata=_vvm)

        # P2.7 Fix #2+#3: persist counts so validate_stage uses symmetric baseline
        try:
            _pm2 = dict(context.metadata or {})
            _pm2.update(meta_updates)
            context = context.update(metadata=_pm2)
        except Exception as _e2:
            logger.debug("_primary_error_count_before not saved: %s", _e2)

        context = context.set_errors(unique_errors)
        return context.add_state_to_history(State.CLASSIFY)

    def _collect_multi_tool_errors(
        self, context: PipelineContext, work_dir: Path,
        *, force_fresh_semgrep: bool = False,
    ) -> tuple:
        """Единая multi-tool сборка ошибок: flake8/cargo/tsc (primary) +
        semgrep + SecurityScanner + pip-audit + mypy + ruff + bandit +
        python_runtime + tests + semantic_audit, с тем же dedup/
        cascade_collapse/version-filter, что у `execute()`.

        Используется И основным FSM-путём (`execute()`, baseline/initial-скан
        на `working_path`), И финальным пересчётом `*_current_errors` в
        `PipelineEngine._finalize_and_audit` (на `project_path`) — это
        ЕДИНСТВЕННЫЙ источник правды для обоих, чтобы они не могли разойтись
        по скоупу инструментов (см. 2026-06-23: baseline считал
        flake8+semgrep, а финальный пересчёт раньше дёргал ТОЛЬКО
        `self.analyzer.analyze()` — flake8-only — отчего current_total_errors
        был структурно занижен относительно baseline везде, где semgrep что-то
        находил, выглядя как "ошибки исчезли без патчей").

        Возвращает (unique_errors, breakdown_by_tool, version_skipped,
        meta_updates). `breakdown_by_tool` — счётчики СЫРЫХ (до dedup/
        cascade/version-filter) находок каждого инструмента, по тем же
        ключам, что top-level `*_current_errors`-поля результата движка
        (flake8/semgrep/bandit/...).

        `force_fresh_semgrep=True` — игнорирует throttling-кэш Semgrep
        (`_get_semgrep_errors` иначе может отдать устаревший снимок с
        середины прогона) и форсирует свежий `safe_run` — для финального
        пересчёта, где точность важнее экономии ~10с на повторном вызове.
        """
        all_errors: List[Dict[str, Any]] = []
        breakdown: Dict[str, int] = {}

        # 1. Main analyzer (cargo check / flake8 / tsc / etc.).
        try:
            try:
                errors = self.analyzer.analyze(work_dir, clean_before_each=False)
            except TypeError as te:
                msg = str(te)
                if ("unexpected keyword argument" in msg
                        and "clean_before_each" in msg):
                    errors = self.analyzer.analyze(work_dir)
                else:
                    raise
            logger.info("Main analysis: found %d errors", len(errors))
            all_errors.extend(errors)
        except Exception as e:
            raise _MainAnalysisFailed() from e

        # P2.7 Fix #2: primary-analyzer count before secondary analyzers inflate the list
        _primary_count = len(errors)
        breakdown["flake8"] = _primary_count

        # 2. Semgrep (optional).
        # O1: SemgrepAnalyzer is instantiated without config in __init__, so its
        # self.enabled defaults to True regardless of webles_config.json.
        # Guard via context.config here so "enabled: false" is actually honoured.
        _semgrep_cfg_enabled = (
            (context.config or {}).get("tools", {}).get("semgrep", {}).get("enabled", True)
        )
        breakdown["semgrep"] = 0
        if _semgrep_cfg_enabled:
            if force_fresh_semgrep:
                semgrep_errors = self.semgrep.safe_run(project_path=work_dir) or []
            else:
                semgrep_errors = self._get_semgrep_errors(context, work_dir)
            if semgrep_errors:
                logger.info("Semgrep: %d findings", len(semgrep_errors))
                _skipped_sandbox = 0
                _normalized = []
                for finding in semgrep_errors:
                    raw_path = finding.get("path", "")
                    # Нормализуем абсолютные пути → относительные к work_dir.
                    # Фильтруем находки внутри sandbox-директорий (F4).
                    file_path = raw_path
                    if raw_path:
                        from pathlib import Path as _Path
                        _p = _Path(raw_path)
                        if _p.is_absolute():
                            try:
                                file_path = str(_p.relative_to(work_dir))
                            except ValueError:
                                # Путь вне work_dir — sandbox или посторонний
                                _skipped_sandbox += 1
                                continue
                        _SANDBOX_MARKERS = ("webbles_isolated_", ".webles_sandbox",
                                            ".webles_sandbox", ".webbles_fix")
                        if any(m in file_path for m in _SANDBOX_MARKERS):
                            _skipped_sandbox += 1
                            continue
                    _normalized.append({
                        "file": file_path,
                        "line": finding.get("start", {}).get("line", 0),
                        "column": finding.get("start", {}).get("col", 0),
                        "message": finding.get("extra", {}).get("message", ""),
                        "code": finding.get("check_id", "semgrep"),
                        "severity": finding.get("extra", {}).get("severity", "warning"),
                        "error_type": "lint",
                    })
                all_errors.extend(_normalized)
                breakdown["semgrep"] = len(_normalized)
                if _skipped_sandbox:
                    logger.info("Semgrep: пропущено %d sandbox-находок (F4)", _skipped_sandbox)
            elif self.semgrep.enabled and self.semgrep.is_available():
                logger.info("Semgrep: no new findings")
        else:
            logger.debug("Semgrep: disabled by config (tools.semgrep.enabled=false)")

        # 2b. SecurityScanner (Stage K), default on.
        breakdown["security_scanner"] = 0
        if self._security_scan_enabled(context):
            try:
                sec_errors = self.security_scanner.scan_project(work_dir, context.language)
                if sec_errors:
                    logger.info("SecurityScanner: %d vulns", len(sec_errors))
                    all_errors.extend(sec_errors)
                    breakdown["security_scanner"] = len(sec_errors)
            except Exception as e:
                logger.warning("SecurityScanner failed: %s", e)

        # 2c. pip-audit (Stage K.8), Python only, default off.
        breakdown["pip_audit"] = 0
        if self._pip_audit_enabled(context) and (context.language or "").lower() in ("python", "py"):
            try:
                from analysis.pip_audit_scan import PipAuditScanner
                pa = PipAuditScanner()
                pa_errors = pa.scan(work_dir)
                if pa_errors:
                    logger.info("pip-audit: %d CVEs", len(pa_errors))
                    all_errors.extend(pa_errors)
                    breakdown["pip_audit"] = len(pa_errors)
            except Exception as e:
                logger.warning("pip-audit failed: %s", e)

        # 2d. mypy (Stage P.0), Python only, default off.
        breakdown["mypy"] = 0
        if self._use_mypy_enabled(context) and (context.language or "").lower() in ("python", "py"):
            try:
                from analyzers.mypy_analyzer import MypyAnalyzer
                mypy = MypyAnalyzer()
                mypy_errors = mypy.analyze(work_dir)
                if mypy_errors:
                    logger.info("mypy: %d type errors", len(mypy_errors))
                    all_errors.extend(mypy_errors)
                    breakdown["mypy"] = len(mypy_errors)
            except Exception as e:
                logger.warning("mypy failed: %s", e)

        _ruff_count_for_meta = 0  # P2.7 Fix #3: validate_stage reads this for symmetric baseline

        # 2e. ruff (Stage P.1), Python only, default off.
        breakdown["ruff"] = 0
        if self._use_ruff_enabled(context) and (context.language or "").lower() in ("python", "py"):
            try:
                from analyzers.ruff_analyzer import RuffAnalyzer
                ruff = RuffAnalyzer()
                ruff_errors = ruff.analyze(work_dir)
                if ruff_errors:
                    logger.info("ruff: %d findings", len(ruff_errors))
                    all_errors.extend(ruff_errors)
                _ruff_count_for_meta = len(ruff_errors or [])
                breakdown["ruff"] = _ruff_count_for_meta
            except Exception as e:
                logger.warning("ruff failed: %s", e)

        # 2f. bandit (Stage P.2), Python only, default off.
        breakdown["bandit"] = 0
        if self._use_bandit_enabled(context) and (context.language or "").lower() in ("python", "py"):
            try:
                from analyzers.bandit_analyzer import BanditAnalyzer
                bandit = BanditAnalyzer()
                bandit_errors = bandit.analyze(work_dir)
                if bandit_errors:
                    logger.info("bandit: %d security findings", len(bandit_errors))
                    all_errors.extend(bandit_errors)
                    breakdown["bandit"] = len(bandit_errors)
            except Exception as e:
                logger.warning("bandit failed: %s", e)

        # 2g. PythonRuntimeAnalyzer (Stage P.5), Python only, default off.
        # Runtime-errors (NameError/TypeError/ImportError/AssertionError)
        # parity with Rust runtime path. Requires explicit opt-in.
        breakdown["python_runtime"] = 0
        if self._use_python_runtime_enabled(context) and (context.language or "").lower() in ("python", "py"):
            try:
                from analyzers.python_runtime_analyzer import PythonRuntimeAnalyzer
                runtime = PythonRuntimeAnalyzer(timeout=int(
                    (context.config or {}).get("pipeline", {}).get("python_runtime_timeout", 20)
                ))
                runtime_errors = runtime.analyze(work_dir)
                if runtime_errors:
                    logger.info("python_runtime: %d runtime errors", len(runtime_errors))
                    all_errors.extend(runtime_errors)
                    breakdown["python_runtime"] = len(runtime_errors)
            except Exception as e:
                logger.warning("python_runtime failed: %s", e)

        # 3. Tests as error source (Stage G).
        breakdown["tests"] = 0
        if self._run_tests_enabled(context):
            try:
                passed, test_errors = self.test_runner.run(work_dir, context.language)
                if not passed and test_errors:
                    logger.info("TestRunner: %d test failures", len(test_errors))
                    all_errors.extend(test_errors)
                    breakdown["tests"] = len(test_errors)
            except Exception as e:
                logger.warning("TestRunner failed: %s", e)

        # 4. Semantic audit (Stage H), default off.
        breakdown["semantic_audit"] = 0
        if self._semantic_audit_enabled(context):
            try:
                audit_errors = self._audit_project(work_dir, context.language)
                if audit_errors:
                    logger.info("SemanticAuditor: %d mismatches", len(audit_errors))
                    all_errors.extend(audit_errors)
                    breakdown["semantic_audit"] = len(audit_errors)
            except Exception as e:
                logger.warning("SemanticAuditor failed: %s", e)

        # Дедуп по (file, line, message).
        unique_errors = []
        seen = set()
        for err in all_errors:
            key = (err.get("file", ""), err.get("line", 0), err.get("message", ""))
            if key not in seen:
                seen.add(key)
                unique_errors.append(err)
        logger.info("Unique errors after merge: %d", len(unique_errors))

        # Cascade collapse pre-filter.
        cfg = getattr(context, "config", {}) or {}
        if cfg.get("pipeline", {}).get("cascade_collapse", True):
            try:
                from analysis.error_cascade import collapse_cascades
                unique_errors = collapse_cascades(unique_errors)
            except Exception as e:
                logger.warning("cascade_collapse skipped: %s", e)

        # Python version-aware analysis (2026-06-22): часть E999/invalid-syntax
        # на самом деле не баг, а несовместимость версий Python (PEP 695 и
        # др. конструкции, валидные в новых версиях, но не парсящиеся в
        # окружении пайплайна) — см. CONVERSION_ANALYSIS_2026-06-22_
        # FULL_ARCHIVE.md (33% всего объёма решений, accept-доля 1.8-17.3%)
        # и core/python_version_detector.py. Исключаем такие "ошибки" из
        # current_errors/initial_errors ДО классификации, целиком — не
        # REJECT, не NEEDS_REVIEW, отдельная категория, не портящая метрики
        # конверсии.
        version_skipped: List[Dict] = []
        if (context.language or "").lower() in ("python", "py") and (
            (context.config or {}).get("pipeline", {}).get("python_version_detection", True)
        ):
            unique_errors, version_skipped = self._filter_version_incompatible_errors(
                unique_errors, work_dir,
            )

        meta_updates = {
            "_primary_error_count_before": _primary_count,
            "_ruff_count_before": _ruff_count_for_meta,
        }
        return unique_errors, breakdown, version_skipped, meta_updates

    def run_full_scan(
        self, context: PipelineContext, scan_dir: Path,
        *, force_fresh_semgrep: bool = True,
    ) -> Dict[str, Any]:
        """Публичный вход для пересчёта ошибок ВНЕ обычного FSM-цикла —
        тот же multi-tool скоуп, что у `execute()` (initial/baseline-скан),
        применённый к произвольной директории (например `project_path` для
        финального `*_current_errors`, см. PipelineEngine._finalize_and_audit).

        Возвращает {"errors": [...], "breakdown": {tool: count, ...},
        "total": int} — `total` — счётчик ПОСЛЕ dedup/cascade_collapse/
        version-filter (сравним с `initial_error_count`/`baseline_remaining`
        по смыслу); `breakdown[tool]` — счётчики СЫРЫХ находок каждого
        инструмента до этой постобработки (как и у `*_current_errors`-полей).
        """
        unique_errors, breakdown, _version_skipped, _meta = self._collect_multi_tool_errors(
            context, scan_dir, force_fresh_semgrep=force_fresh_semgrep,
        )
        return {
            "errors": unique_errors,
            "breakdown": breakdown,
            "total": len(unique_errors),
        }

    # Коды конкретных анализаторов, означающие "файл не парсится" — НЕ единый
    # стандарт между ними (flake8/ruff: E999/invalid-syntax/E902; mypy:
    # буквально "syntax" — см. analyzers/mypy_analyzer.py _CODE_TO_TYPE).
    # error_type == "syntax" — НОРМАЛИЗОВАННОЕ поле, которое выставляют ВСЕ
    # анализаторы для этого класса ошибок (см. python_analyzer.py
    # _classify_flake8_error/AST-фоллбек) — основной, более надёжный
    # критерий; коды — additional safety net для путей, где error_type ещё
    # не выставлен (до ClassifyStage).
    _SYNTAX_CODES = frozenset({"E999", "invalid-syntax", "E902", "syntax"})

    @classmethod
    def _is_syntax_error(cls, err: Dict) -> bool:
        return (
            err.get("error_type") == "syntax"
            or err.get("code") in cls._SYNTAX_CODES
        )

    @classmethod
    def _filter_version_incompatible_errors(cls, errors: List[Dict], work_dir) -> tuple:
        """Отделяет синтаксические ошибки (любого анализатора), которые на
        самом деле — несовместимость версий Python, от остальных. Возвращает
        (оставшиеся_ошибки, пропущенные_как_version_incompatible)."""
        from core.python_version_detector import (
            classify_version_incompatibility, detect_project_python_version,
        )

        if not any(cls._is_syntax_error(e) for e in errors):
            return errors, []

        try:
            project_required = detect_project_python_version(work_dir)
        except Exception as e:
            logger.debug("python_version_detector: project-level detect failed: %s", e)
            project_required = None

        kept: List[Dict] = []
        skipped: List[Dict] = []
        _content_cache: Dict[str, str] = {}
        for err in errors:
            if not cls._is_syntax_error(err):
                kept.append(err)
                continue
            file_rel = err.get("file", "") or ""
            if file_rel not in _content_cache:
                try:
                    _content_cache[file_rel] = (Path(work_dir) / file_rel).read_text(
                        encoding="utf-8", errors="replace",
                    )
                except Exception:
                    _content_cache[file_rel] = ""
            content = _content_cache[file_rel]
            try:
                result = classify_version_incompatibility(
                    work_dir, content, project_required_version=project_required,
                )
            except Exception as e:
                logger.debug("python_version_detector: classify failed for %s: %s", file_rel, e)
                result = None
            if result:
                logger.info(
                    "  Python version-aware: %s:%s (%s) требует Python %s, "
                    "окружение %s (обнаружено через %s) — не E999, исключаем из анализа",
                    file_rel, err.get("line", 0), err.get("code", ""),
                    result["required_version"], result["current_version"], result["detected_via"],
                )
                skipped.append({
                    "file": file_rel, "line": err.get("line", 0),
                    "code": err.get("code", ""), "message": err.get("message", ""),
                    **result,
                })
            else:
                kept.append(err)
        return kept, skipped

    @staticmethod
    def _run_tests_enabled(context: PipelineContext) -> bool:
        try:
            return bool((context.config or {}).get("pipeline", {}).get("run_tests", False))
        except Exception as e:
            logger.debug("_run_tests_enabled: чтение конфига упало (%s), default=False", e)
            return False

    @staticmethod
    def _semantic_audit_enabled(context: PipelineContext) -> bool:
        try:
            return bool((context.config or {}).get("pipeline", {}).get("semantic_audit", False))
        except Exception as e:
            logger.debug("_semantic_audit_enabled: чтение конфига упало (%s), default=False", e)
            return False

    @staticmethod
    def _pip_audit_enabled(context: PipelineContext) -> bool:
        try:
            return bool((context.config or {}).get("pipeline", {}).get("pip_audit", False))
        except Exception as e:
            logger.debug("_pip_audit_enabled: чтение конфига упало (%s), default=False", e)
            return False

    @staticmethod
    def _security_scan_enabled(context: PipelineContext) -> bool:
        try:
            return bool((context.config or {}).get("pipeline", {}).get("security_scan", True))
        except Exception as e:
            logger.debug("_security_scan_enabled: чтение конфига упало (%s), default=True", e)
            return True

    @staticmethod
    def _use_mypy_enabled(context: PipelineContext) -> bool:
        """Stage P.0 flag: pipeline.use_mypy (default False)."""
        try:
            return bool((context.config or {}).get("pipeline", {}).get("use_mypy", False))
        except Exception as e:
            logger.debug("_use_mypy_enabled: чтение конфига упало (%s), default=False", e)
            return False

    @staticmethod
    def _use_ruff_enabled(context: PipelineContext) -> bool:
        """Stage P.1 flag: pipeline.use_ruff (default False)."""
        try:
            return bool((context.config or {}).get("pipeline", {}).get("use_ruff", False))
        except Exception as e:
            logger.debug("_use_ruff_enabled: чтение конфига упало (%s), default=False", e)
            return False

    @staticmethod
    def _use_bandit_enabled(context: PipelineContext) -> bool:
        """Stage P.2 flag: pipeline.use_bandit (default False)."""
        try:
            return bool((context.config or {}).get("pipeline", {}).get("use_bandit", False))
        except Exception as e:
            logger.debug("_use_bandit_enabled: чтение конфига упало (%s), default=False", e)
            return False

    @staticmethod
    def _use_python_runtime_enabled(context: PipelineContext) -> bool:
        """Stage P.5 flag: pipeline.use_python_runtime (default False).

        Runs project code in a subprocess (pytest -> unittest -> entrypoints),
        so requires explicit opt-in. When on, catches runtime errors
        (NameError/TypeError/ImportError/AssertionError) that static analysis
        misses.
        """
        try:
            return bool((context.config or {}).get("pipeline", {}).get("use_python_runtime", False))
        except Exception as e:
            logger.debug("_use_python_runtime_enabled: чтение конфига упало (%s), default=False", e)
            return False

    def _get_semantic_auditor(self, work_dir):
        """Lazy SemanticAuditor with AuditCache (Stage H wiring)."""
        if self._semantic_auditor is not None:
            return self._semantic_auditor
        if self.llm_client is None:
            return None
        cache = None
        try:
            from analysis.audit_cache import AuditCache
            cache = AuditCache(Path(work_dir) / ".webbles_fix" / "audit_cache")
        except Exception as e:
            logger.debug("SemanticAuditor cache not created: %s", e)
        try:
            self._semantic_auditor = SemanticAuditor(
                llm_client=self.llm_client,
                cache=cache,
            )
        except Exception as e:
            logger.warning("SemanticAuditor init failed: %s", e)
            return None
        return self._semantic_auditor

    def _audit_project(self, work_dir, language):
        """Stage H audit-mode: docstring↔code per project file."""
        auditor = self._get_semantic_auditor(work_dir)
        if auditor is None:
            return []
        lang = (language or "").lower()
        exts = _AUDIT_EXTS.get(lang)
        if not exts:
            return []
        root = Path(work_dir)
        results = []
        count = 0
        try:
            for path in root.rglob("*"):
                if count >= _AUDIT_MAX_FILES:
                    break
                if not path.is_file() or path.suffix not in exts:
                    continue
                if any(part in _AUDIT_SKIP_DIRS for part in path.parts):
                    continue
                count += 1
                try:
                    errs = auditor.audit_file(path, lang)
                    if errs:
                        results.extend(errs)
                except Exception as e:
                    logger.debug("SemanticAuditor.audit_file failed for %s: %s", path, e)
        except Exception as e:
            logger.warning("SemanticAuditor scan failed: %s", e)
        return results
