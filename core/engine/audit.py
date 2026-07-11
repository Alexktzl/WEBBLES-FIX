"""
Модуль финального аудита с гранулированным откатом сегментов.
Если в отдельном файле появляются новые ошибки, аудит определяет проблемные сегменты
и восстанавливает только их, а не весь файл или проект.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from analysis.invariant_guard import InvariantGuard
from analysis.symbol_regression import check_symbol_duplication, check_symbol_regression
from fixers.file_segmenter import FileSegmenter

logger = logging.getLogger(__name__)

NON_BLOCKING_CODES = {
    "unused_imports", "unused_variables", "unused_mut",
    "dead_code", "clippy", "RUSTSEC",
}

# L2 (аудит 2026-07-01): "warning:"/"help:"/"note:" вынесены в ПРЕФИКСНЫЕ
# маркеры — contains-совпадение исключало из аудита любую ошибку, у которой
# эти слова встречались ВНУТРИ сообщения (например, в строковом литерале,
# попавшем в message). "= note:"/"= help:" остаются contains — это формат
# продолжения rustc-диагностики, он всегда внутри строки.
NON_BLOCKING_MESSAGE_KEYWORDS = [
    "unused import", "unused variable", "unused mut",
    "dead code", "never used", "= note:", "= help:",
]

NON_BLOCKING_MESSAGE_PREFIXES = (
    "warning:", "help:", "note:",
)


class AuditManager:
    """Управляет финальным аудитом проекта и восстановлением проблемных сегментов."""

    def __init__(
        self,
        project_path: Path,
        language: str,
        analyzer,
        classifier,
        compiler,
        config: dict,
        context_getter,
        invariant_guard: Optional[InvariantGuard] = None,
    ):
        self.project_path = project_path
        self.language = language
        self.analyzer = analyzer
        self.classifier = classifier
        self.compiler = compiler
        self.config = config
        self._get_context = context_getter
        self.invariant_guard = invariant_guard
        self.segmenter = FileSegmenter(language)

    # ----------------------------------------------------------------
    # Публичный метод — гранулированный аудит
    # ----------------------------------------------------------------
    def audit_run(self, context) -> Dict[str, Any]:
        """
        Проверяет каждый изменённый файл и для проблемных файлов
        определяет, какие сегменты нужно откатить.
        Возвращает:
        {
            "audit_ok": bool,
            "failed_segments": {file_name: [(start, end, original_content)]},
            "passed_files": [file_name]
        }
        """
        logger.info("Запуск гранулированного аудита...")
        audit_path = self._get_audit_path()

        file_sigs_before = context.metadata.get("file_error_signatures_before", {}) if context else {}
        snapshots = context.metadata.get("patch_snapshots", []) if context else []

        if not file_sigs_before:
            logger.warning("file_error_signatures_before пуст – переключаемся на fallback по весам")
            return self._fallback_audit(context)

        try:
            all_current_errors = self.analyzer.analyze(audit_path)
        except Exception as e:
            logger.error("Ошибка анализа при аудите: %s", e)
            return {"audit_ok": False, "failed_segments": {}, "passed_files": [], "error": str(e)}

        passed_files = []
        failed_segments = {}

        # M1 (аудит 2026-07-01): аудит проверял ТОЛЬКО файлы с ошибками в
        # baseline (ключи file_error_signatures_before). Файл БЕЗ ошибок в
        # baseline, изменённый прогоном (побочный файл многофайлового патча,
        # parallel touched, accepted-файл вне sigs-карты), не проходил ни
        # lint-, ни символьную проверку — правка доезжала до оригинала
        # (ACCEPT-fallback) невалидированной. Расширяем множество аудируемых
        # файлов; для новых ключей before_sigs = пустой set.
        audit_targets: Dict[str, Any] = dict(file_sigs_before)
        extra_files = set()
        for snap in snapshots:
            f = snap.get("file")
            if isinstance(f, str) and f:
                extra_files.add(f)
        for f in (context.metadata.get("_parallel_touched_files", []) or []) if context else []:
            if isinstance(f, str) and f:
                extra_files.add(f)
        try:
            for pinfo in list(getattr(context, "accepted_patches", []) or []):
                f = ((pinfo or {}).get("error") or {}).get("file")
                if isinstance(f, str) and f:
                    extra_files.add(f)
        except Exception:
            pass  # контекст-заглушка без accepted_patches
        # Codex-1 (2026-07-02): файлы, для которых REJECT/NR-откат ПРОВАЛИЛСЯ
        # (DecideStage._rollback_file_from_snapshot записал их в
        # metadata["_rollback_failed_files"]), тоже обязаны попасть в аудит —
        # раньше audit_run их не читал вовсе (см. принудительный полный откат
        # ниже, после основного цикла).
        rollback_failed_files = [
            f for f in (context.metadata.get("_rollback_failed_files", []) or [])
            if isinstance(f, str) and f
        ] if context else []
        for f in rollback_failed_files:
            extra_files.add(f)
        for f in sorted(extra_files):
            if f not in audit_targets and (audit_path / f).exists():
                audit_targets[f] = set()
                logger.info("Аудит: файл %s добавлен в проверку (изменён прогоном, "
                            "но без ошибок в baseline)", f)

        missing_in_sandbox = []
        for fname, before_data in audit_targets.items():
            fpath = audit_path / fname
            if not fpath.exists():
                # H2 (аудит 2026-07-01): раньше исчезнувший из sandbox файл
                # молча пропускался (только warning) — его ошибки исчезали из
                # финального скана, метрики показывали «исправлено», хотя файл
                # просто потерян. Теперь: audit_ok=False, полный «откат»
                # (restore_failed_segments пересоздаст файл из оригинала).
                logger.error(
                    "Файл %s исчез из sandbox — помечаем failed (не passed, не skip)", fname
                )
                missing_in_sandbox.append(fname)
                failed_segments[fname] = [(1, 0, "")]
                continue

            file_errors = [e for e in all_current_errors if e.get("file") == fname]
            after_sigs = self._extract_error_signatures(file_errors)

            # C1 (аудит 2026-07-01): раньше `before_data` (set объектов
            # ErrorSignature) использовался КАК ЕСТЬ, а after_sigs были
            # кортежами с другой нормализацией — вычитание множеств было
            # мёртвым. Теперь обе стороны приводятся к to_tuple().
            try:
                if isinstance(before_data, list) and before_data and isinstance(before_data[0], dict):
                    before_sigs = self._extract_error_signatures(before_data)
                else:
                    before_sigs = self._normalize_before_sigs(before_data)
            except Exception:
                before_sigs = set()

            new_sigs = after_sigs - before_sigs
            lost_sigs = before_sigs - after_sigs

            logger.info("Файл %s: сигнатур до %d, после %d; новых %d, исчезло %d",
                        fname, len(before_sigs), len(after_sigs), len(new_sigs), len(lost_sigs))

            # O.14/O.17 на уровне финального аудита: lint-сигнатуры выше видят
            # только НОВЫЕ ошибки на конкретных строках. Патч, стирающий целую
            # функцию/класс (или дублирующий её), может не породить ни одной
            # новой ошибки на затронутых строках — такой файл раньше уходил в
            # passed_files необнаруженным (control series 2026-06-25: bcrypt,
            # httpx — unsafe_accept, десятки def/dunder-методов пропали без
            # единой новой ошибки). per-patch guard в validate_stage/decide_stage
            # это ловит на уровне ОДНОГО патча, но финальный аудит — последний
            # рубеж на уровне всего прогона — этой проверки не делал вовсе.
            original_path = self.project_path / fname
            symbol_issue = None
            original_content = ""
            if original_path.exists() and original_path.resolve() != fpath.resolve():
                try:
                    # H3 (аудит 2026-07-01): errors="replace" — раньше один
                    # UnicodeDecodeError на не-UTF8 файле тихо выключал
                    # символьную проверку именно для этого файла.
                    original_content = original_path.read_text(encoding="utf-8", errors="replace")
                    current_content = fpath.read_text(encoding="utf-8", errors="replace")
                    reg = check_symbol_regression(original_content, current_content, self.language or "")
                    if not reg.get("ok", True):
                        symbol_issue = {
                            "kind": "regression",
                            "missing_defs": reg.get("missing_defs", []),
                            "missing_classes": reg.get("missing_classes", []),
                            "missing_imports": reg.get("missing_imports", []),
                            "missing_overloads": reg.get("missing_overloads", []),
                        }
                    else:
                        dup = check_symbol_duplication(original_content, current_content, self.language or "")
                        if not dup.get("ok", True):
                            symbol_issue = {
                                "kind": "duplication",
                                "duplicated_defs": dup.get("duplicated_defs", []),
                                "duplicated_classes": dup.get("duplicated_classes", []),
                            }
                except Exception as e:
                    # H3 (аудит 2026-07-01): fail-closed. Раньше исключение
                    # означало «файл не проверен → passed» — детерминированная
                    # защита выключалась любым сбоем чтения/парсинга (класс
                    # «silent exception» из P0 unsafe_accept). Непроверенный
                    # файл не может считаться прошедшим последний рубеж.
                    logger.error(
                        "Аудит: символьная проверка %s не удалась (%s) — "
                        "fail-closed, файл уходит в failed_segments", fname, e,
                    )
                    symbol_issue = {"kind": "check_failed", "error": str(e)}

            if symbol_issue is not None:
                logger.warning(
                    "Аудит: файл %s помечен повреждённым по символьной проверке (%s) — %s "
                    "— полный откат вместо passed",
                    fname, symbol_issue["kind"], symbol_issue,
                )
                failed_segments[fname] = [(1, 0, original_content)]
                continue

            if new_sigs:
                # Появились новые ошибки — regression, если:
                #   а) общее число ошибок в файле выросло (фикс не окупился), ИЛИ
                #   б) среди новых есть CRITICAL_SYNTAX (синтаксис сломали), ИЛИ
                #   в) M4 (аудит 2026-07-01): счётчик НЕ уменьшился и в before
                #      не было masking-кодов (E999/E902/invalid-syntax) —
                #      «демаскирование» физически возможно только когда фикс
                #      снял блокер анализа; подмена одной ошибки другой при
                #      равном счёте без блокера — это регрессия, а не демаск
                #      (тот же критерий, что в net_delta_check._MASKING_CODES).
                # Иначе — «демаскирование»/чистое улучшение: счётчик упал,
                # per-patch guard-ы (symbol/net-delta) каждый патч уже провели.
                count_grew = len(after_sigs) > len(before_sigs)
                count_decreased = len(after_sigs) < len(before_sigs)
                has_critical_syntax = any(
                    e.get("error_class") == "CRITICAL_SYNTAX" for e in file_errors
                )
                # Python: E999/E902/invalid-syntax (стоп анализа файла);
                # Rust: E0432/E0433 (unresolved import/failed to resolve) —
                # блокируют type-check, их фикс легитимно «обнажает» E0609 и
                # прочие (см. tests/test_phase_audit_demask.py).
                _MASKING_CODES = (
                    "E999", "E902", "invalid-syntax", "E0432", "E0433",
                )
                had_masking_before = any(t[1] in _MASKING_CODES for t in before_sigs)
                if count_grew or has_critical_syntax or (
                    not count_decreased and not had_masking_before
                ):
                    problem_segments = self._find_problem_segments(
                        fname, new_sigs, file_errors, snapshots
                    )
                    if problem_segments:
                        failed_segments[fname] = problem_segments
                    else:
                        # если не удалось вычислить сегменты, откатываем весь файл
                        original_path = self.project_path / fname
                        if original_path.exists():
                            failed_segments[fname] = [(1, 0, original_path.read_text(encoding="utf-8"))]
                else:
                    logger.info(
                        "Файл %s: новые ошибки появились, но счётчик не вырос (%d -> %d) "
                        "и нет CRITICAL_SYNTAX — демаскирование импорт-фиксом, не regression",
                        fname, len(before_sigs), len(after_sigs),
                    )
                    passed_files.append(fname)
            else:
                passed_files.append(fname)

        # Codex-1 (2026-07-02): если REJECT/NR-откат патча не удался
        # (нет ни _pre_patch_content, ни снапшота, либо запись на диск упала),
        # DecideStage помечает файл в metadata["_rollback_failed_files"], но на
        # диске sandbox остаётся НЕПРИНЯТОЕ изменение. Раньше финальный аудит
        # этот список игнорировал: если такой файл не терял символов и не давал
        # новых lint-ошибок, он уходил в passed_files и ДОСТАВЛЯЛСЯ в оригинал,
        # хотя его откат провалился. Последний рубеж обязан перекрыть это
        # НЕЗАВИСИМО от lint/символьной проверки — принудительный полный откат
        # (fail-closed; направление ошибки по §3/§4: ложный откат безопаснее
        # ложной доставки повреждения). Побочный эффект: если тот же файл позже
        # получил легитимный ACCEPT, он тоже будет отброшен — это осознанный
        # безопасный размен (провал отката = недоверие ко всему файлу).
        for f in rollback_failed_files:
            failed_segments[f] = [(1, 0, "")]
            if f in passed_files:
                passed_files.remove(f)
            logger.error(
                "Аудит: файл %s имел ПРОВАЛЕННЫЙ откат (_rollback_failed_files) — "
                "принудительный полный откат, из доставки исключён", f,
            )

        audit_ok = len(failed_segments) == 0
        if audit_ok:
            logger.info("Аудит пройден: все изменённые файлы чисты")
        else:
            logger.warning("Аудит частичный: проблемные сегменты будут откачены, остальное сохранено")

        return {
            "audit_ok": audit_ok,
            "failed_segments": failed_segments,
            "passed_files": passed_files,
            "missing_in_sandbox": missing_in_sandbox,
            "rollback_failed": list(rollback_failed_files),
        }

    # ----------------------------------------------------------------
    # Восстановление проблемных сегментов
    # ----------------------------------------------------------------
    def restore_failed_segments(
        self, tmp_dir: Path, failed_segments: Dict[str, List[Tuple[int, int, str]]],
    ) -> Dict[str, bool]:
        """
        Восстанавливает проблемные сегменты в файлах из оригиналов.
        Если end=0, восстанавливается весь файл целиком.

        C3 (аудит 2026-07-01): возвращает per-file статус `{fname: ok}` —
        раньше ЛЮБОЕ исключение только логировалось, вызывающий код
        (_finalize_and_audit) не знал о провале отката, и повреждённый файл
        мог уехать в оригинал через ACCEPT-fallback копирования. Полный
        откат дополнительно верифицируется байтовым сравнением с оригиналом.
        """
        status: Dict[str, bool] = {}
        for fname, segments in failed_segments.items():
            tmp_file = tmp_dir / fname
            orig_file = self.project_path / fname
            if not orig_file.exists():
                # Нечем восстанавливать — файл считается НЕ восстановленным,
                # копировать его в оригинал нельзя ни при каких условиях.
                logger.error("Restore %s: оригинал отсутствует — статус failed", fname)
                status[fname] = False
                continue
            wants_full = any(end == 0 for _, end, _ in segments)
            if not tmp_file.exists():
                # H2: файл исчез из sandbox. Для полного отката пересоздаём
                # его из оригинала (оригинал и так не тронут — важно лишь,
                # чтобы дальнейшие шаги не считали файл «обработанным»).
                if wants_full:
                    try:
                        tmp_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(orig_file, tmp_file)
                        status[fname] = tmp_file.read_bytes() == orig_file.read_bytes()
                    except Exception as e:
                        logger.error("Restore %s: пересоздание из оригинала упало: %s", fname, e)
                        status[fname] = False
                else:
                    status[fname] = False
                continue
            try:
                current_lines = tmp_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                original_lines = orig_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

                # Применяем откаты от последнего к первому, чтобы не сбить индексы
                full_rollback = False
                for start, end, orig_content in sorted(segments, reverse=True):
                    if end == 0:
                        # откат всего файла
                        shutil.copy2(orig_file, tmp_file)
                        logger.info("Полный откат файла %s", fname)
                        full_rollback = True
                        break
                    if start <= len(current_lines):
                        seg_lines = orig_content.splitlines(keepends=True) if orig_content else original_lines[start-1:end]
                        current_lines[start-1:end] = seg_lines
                        logger.info("Откачен сегмент в %s: строки %d-%d", fname, start, end)
                    else:
                        # C3: сегмент вне диапазона — «частичный» откат ничего
                        # не откатил; молча продолжать нельзя.
                        raise ValueError(
                            f"segment start={start} вне диапазона файла ({len(current_lines)} строк)"
                        )

                # 2026-07-01 (control series verify-run, httpx — unsafe_accept
                # ВСЁ ЕЩЁ воспроизводился ПОСЛЕ фикса детекции в audit_run()):
                # полный откат делает shutil.copy2() напрямую на диске, но
                # раньше код падал СКВОЗЬ break к этой записи ниже безусловно —
                # она перезаписывала tmp_file УСТАРЕВШИМ `current_lines`,
                # прочитанным ДО restore, сводя полный откат на нет. Файл
                # оставался повреждённым несмотря на то, что audit_run()
                # корректно его детектировал.
                if not full_rollback:
                    tmp_file.write_text("".join(current_lines), encoding="utf-8")
                    status[fname] = True
                else:
                    # C3: верификация результата полного отката — байт-в-байт.
                    status[fname] = tmp_file.read_bytes() == orig_file.read_bytes()
                    if not status[fname]:
                        logger.error(
                            "Restore %s: полный откат НЕ подтверждён байтовым сравнением", fname
                        )
            except Exception as e:
                logger.error("Ошибка восстановления сегментов в %s: %s", fname, e)
                status[fname] = False
        return status

    # ----------------------------------------------------------------
    # Поиск проблемных сегментов
    # ----------------------------------------------------------------
    def _find_problem_segments(
        self,
        file_name: str,
        new_sigs: Set[Tuple[str, str, str]],
        file_errors: List[Dict],
        snapshots: List[Dict],
    ) -> List[Tuple[int, int, str]]:
        """
        По новым сигнатурам ошибок и снапшотам определяет,
        какие сегменты нужно откатить.
        Возвращает список (start_line, end_line, original_content).
        """
        # Если нет снапшотов, вернём весь файл
        file_snaps = [s for s in snapshots if s.get("file") == file_name]
        if not file_snaps:
            return [(1, 0, "")]   # сигнал к полному откату

        affected_lines = set()
        for err in file_errors:
            sig = self._signature(err)
            if sig in new_sigs:
                line = err.get("line", 0)
                if line:
                    affected_lines.add(line)

        if not affected_lines:
            return [(1, 0, "")]

        # M6 (аудит 2026-07-01): снапшоты БЕЗ явных segment_start/segment_end
        # хранят в original_content ПОЛНЫЙ текст файла (см.
        # validate_stage._save_patch_snapshot) — restore_failed_segments
        # вставлял его в диапазон строк `current_lines[start-1:end]`, получая
        # мусорный файл (полный текст внутри одной строки). Пока производителей
        # сегментных снапшотов нет (grep 2026-07-01: segment_start никто не
        # пишет), безопасный вариант для такого файла — только полный откат.
        if any("segment_start" not in snap or "segment_end" not in snap
               for snap in file_snaps):
            return [(1, 0, "")]

        # Собираем все сегменты, которые пересекаются с проблемными строками
        problem_segments = []
        for snap in file_snaps:
            seg_start = snap["segment_start"]
            seg_end = snap["segment_end"]
            if any(seg_start <= line <= seg_end for line in affected_lines):
                problem_segments.append((seg_start, seg_end, snap.get("original_content", "")))

        return problem_segments if problem_segments else [(1, 0, "")]

    # ----------------------------------------------------------------
    # Вспомогательные методы
    # ----------------------------------------------------------------
    @staticmethod
    def _is_non_blocking(code: str, message: str = "") -> bool:
        if code and isinstance(code, str):
            if code in NON_BLOCKING_CODES:
                return True
            if code.startswith("clippy"):
                return True
        if message:
            msg_lower = message.lower()
            if msg_lower.lstrip().startswith(NON_BLOCKING_MESSAGE_PREFIXES):
                return True
            for keyword in NON_BLOCKING_MESSAGE_KEYWORDS:
                if keyword in msg_lower:
                    return True
        return False

    @staticmethod
    def _count_file_errors(file_path: Path) -> int:
        try:
            result = subprocess.run(
                ["rustc", "--edition", "2021", "--crate-type", "lib",
                 str(file_path), "-o", os.devnull],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace"
            )
            return result.stderr.count('error:')
        except Exception as e:
            logger.warning("Ошибка подсчёта ошибок в %s: %s", file_path, e)
            return 999

    @staticmethod
    def _extract_error_signatures(errors: List[Dict]) -> Set[Tuple[str, str, str]]:
        """C1 (аудит 2026-07-01): единое представление сигнатуры — кортеж
        `ErrorSignature.to_tuple()`. Раньше здесь была СВОЯ нормализация
        (вырезание цифр/пунктуации + lower), а before-сигнатуры приходили от
        провайдера объектами ErrorSignature с ДРУГОЙ нормализацией —
        `after_sigs - before_sigs` никогда ничего не вычитал (разные типы,
        __eq__ через NotImplemented), и «новыми» считались ВСЕ текущие ошибки
        файла. Теперь обе стороны конвертируются одной функцией."""
        from analysis.error_intelligence.error_signature import ErrorSignature
        sigs = set()
        for err in errors:
            code = err.get("code", "")
            message = err.get("message", "")
            if AuditManager._is_non_blocking(code, message):
                continue
            sigs.add(ErrorSignature.from_error(err).to_tuple())
        return sigs

    @staticmethod
    def _normalize_before_sigs(before_data) -> Set[Tuple[str, str, str]]:
        """Приводит before-сигнатуры к тому же представлению, что
        `_extract_error_signatures` (кортежи `ErrorSignature.to_tuple()`).

        Поддерживаемые входы (C1/H6, аудит 2026-07-01):
          * set/list объектов ErrorSignature — живой прогон (sig provider);
          * list словарей ошибок — legacy-формат;
          * set/list кортежей/списков (file, code, msg) — restored state.json;
          * строки (испорченный default=str-персист старых версий) — пропускаются.
        Non-blocking фильтруется той же _is_non_blocking, что и after-сторона —
        иначе счётчики before/after несимметричны."""
        from analysis.error_intelligence.error_signature import ErrorSignature
        sigs: Set[Tuple[str, str, str]] = set()
        if before_data is None:
            return sigs
        if isinstance(before_data, (set, frozenset, list, tuple)):
            for item in before_data:
                if hasattr(item, "to_tuple"):
                    t = item.to_tuple()
                elif isinstance(item, dict):
                    t = ErrorSignature.from_error(item).to_tuple()
                elif isinstance(item, (list, tuple)) and len(item) >= 3:
                    t = (str(item[0]), str(item[1]), str(item[2]))
                else:
                    continue  # строка/мусор из legacy-персиста
                if AuditManager._is_non_blocking(t[1], t[2]):
                    continue
                sigs.add(t)
        return sigs

    def _get_audit_path(self) -> Path:
        ctx = self._get_context()
        if ctx and getattr(ctx, 'working_path', None):
            return ctx.working_path
        return self.project_path

    def restore_golden_source(self) -> None:
        cfg = self.config.get("pipeline", {})
        golden_src = cfg.get("golden_source_path")
        if not golden_src:
            return
        src = Path(golden_src)
        if not src.is_file():
            logger.warning("Эталонный файл не найден: %s", src)
            return
        lang = self.language
        default_targets = {"rust": "src/main.rs", "python": "main.py"}
        target_rel = cfg.get("golden_target", default_targets.get(lang, ""))
        if not target_rel:
            logger.warning("Не указан golden_target и язык %s не поддерживается", lang)
            return
        audit_path = self._get_audit_path()
        dst = audit_path / target_rel
        if dst.exists():
            try:
                current = dst.read_text(encoding="utf-8")
            except Exception as e:
                logger.error("Не удалось прочитать %s: %s", dst, e)
                return
            if current == src.read_text(encoding="utf-8"):
                return
            logger.info("Восстановление эталонного файла %s из %s", dst.name, src)
            shutil.copy2(src, dst)
        else:
            logger.info("Файл %s не существует, копирование из эталона", dst.relative_to(audit_path))
            shutil.copy2(src, dst)

    @staticmethod
    def _signature(error: Dict) -> Tuple[str, str, str]:
        # C1/L3 (аудит 2026-07-01): та же нормализация, что в
        # _extract_error_signatures — раньше здесь была третья своя копия,
        # из-за чего сигнатуры из _find_problem_segments не матчились с new_sigs.
        from analysis.error_intelligence.error_signature import ErrorSignature
        return ErrorSignature.from_error(error).to_tuple()

    def _fallback_audit(self, context) -> Dict[str, Any]:
        """
        Запасной аудит по взвешенным суммам ошибок (если нет file_error_signatures_before).
        """
        logger.info("Запуск запасного аудита (по весам)...")
        audit_path = self._get_audit_path()

        try:
            current_errors = self.analyzer.analyze(audit_path)
        except Exception as e:
            logger.error("Аудиторский прогон не удался: %s", e)
            return {"audit_ok": False, "failed_segments": {}, "passed_files": [], "error": str(e)}

        current_weight = self.classifier.total_weight(current_errors)
        initial_weight = self.classifier.total_weight(context.initial_errors if context else [])
        compile_ok, _ = self.compiler.run(audit_path, self.language)

        audit_ok = (current_weight == 0) or (current_weight < initial_weight)
        logger.info("Результат запасного аудита: %s", "успех" if audit_ok else "провал")

        file_sigs = context.metadata.get("file_error_signatures_before", {}) if context else {}

        if not audit_ok:
            # При провале возвращаем все изменённые файлы на полный откат
            failed = {}
            for fname in file_sigs:
                original_path = self.project_path / fname
                if original_path.exists():
                    failed[fname] = [(1, 0, original_path.read_text(encoding="utf-8"))]
            return {"audit_ok": False, "failed_segments": failed, "passed_files": []}

        return {"audit_ok": True, "failed_segments": {}, "passed_files": list(file_sigs.keys()) if context else []}