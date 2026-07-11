"""
Стадия приоритизации ошибок.
Вынесена из pipeline_stage.py для модульности.
Содержит подробные DEBUG-логи.
Ошибкам с кодом E0765 принудительно назначается максимальный приоритет.
"""

import logging
from pathlib import Path

from analysis.error_classifier import ErrorClassifier
from core.error_context_validator import ErrorContextValidator
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from safety.error_priority_engine import ErrorPriorityEngine

logger = logging.getLogger(__name__)


class PrioritizeStage(PipelineStage):
    def __init__(self, priority_engine: ErrorPriorityEngine):
        self.priority_engine = priority_engine
        logger.debug("PrioritizeStage инициализирован")

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия PRIORITIZE: приоритизация ошибок...")
        errors = list(context.current_errors)
        if not errors:
            logger.info("  Нет ошибок – завершаем цикл")
            return context.add_state_to_history(State.COMPLETED)

        # 2026-06-24 (Bluetooth-Devices/dbus-fast benchmark): сигнатуры
        # (file::line::code), забаненные PipelineEngine._ban_oscillating_signatures
        # за осциллирующие ACCEPT (два патча взаимно отменили друг друга на
        # одной строке) — не выбираем их повторно в этом прогоне, иначе они
        # бы возвращались на каждый свежий рескан и осциллировали бесконечно.
        _banned = set(context.metadata.get("_oscillating_signatures", []) or [])
        if _banned:
            errors = [
                e for e in errors
                if f"{e.get('file', '')}::{e.get('line', 0)}::{e.get('code', '')}" not in _banned
            ]
            if not errors:
                logger.info("  Все оставшиеся ошибки забанены как осциллирующие – завершаем цикл")
                return context.add_state_to_history(State.COMPLETED)

        # Q1 containment (2026-07-02, спека Алекса): файлы, помеченные
        # GeneratePatchStage._mark_data_toxic_if_o19 как data-toxic (guard
        # O.19 детерминированно портит тестовые данные при каждой попытке,
        # FileAntiLoop заблокировал файл) — их НЕструктурные ошибки не
        # ставим в очередь до конца прогона, иначе стилевые ошибки того же
        # файла продолжают жечь циклы после сброса блока FileAntiLoop по
        # смене hash. Структурные ошибки (синтаксис) оставляем — файл должен
        # оставаться парсимым.
        _toxic = set(context.metadata.get("_data_toxic_files") or [])
        if _toxic:
            _toxic_norm = {str(f).replace("\\", "/") for f in _toxic}
            _structural_codes = {"E999", "E902", "invalid-syntax"}

            def _is_structural(e):
                return (
                    e.get("error_class") == "CRITICAL_SYNTAX"
                    or e.get("code") in _structural_codes
                )

            _skipped_now = {}
            _kept = []
            for e in errors:
                _file_norm = str(e.get("file", "")).replace("\\", "/")
                if _file_norm in _toxic_norm and not _is_structural(e):
                    _skipped_now[_file_norm] = _skipped_now.get(_file_norm, 0) + 1
                    continue
                _kept.append(e)
            errors = _kept
            if _skipped_now:
                _skip_meta = dict(context.metadata.get("_data_toxic_skipped") or {})
                for _f, _n in _skipped_now.items():
                    _skip_meta[_f] = max(_skip_meta.get(_f, 0), _n)
                new_metadata = dict(context.metadata)
                new_metadata["_data_toxic_skipped"] = _skip_meta
                context = context.update(metadata=new_metadata)
                logger.info(
                    "  containment: отфильтровано %d нестроктурных ошибок в data-toxic файлах: %s",
                    sum(_skipped_now.values()), _skipped_now,
                )

        # Q3 signature containment (2026-07-03, спека Алекса, вердикт deep-reasoner
        # по замерам №6-8 bcrypt; ключ починен 2026-07-04, замер №9): классы
        # (file::code), накопившие >=DecideStage.MAX_TOXIC_SIG_REJECTS честных
        # REJECT (target_error_still_present / error_count_not_decreased) по
        # mypy-кодам — DecideStage._track_toxic_signature пометил их в
        # metadata["_toxic_signatures"]. Класс ошибок (arg-type/list-item на
        # parametrize-блоках) неверифицируем в тулчейне: детерминированный фикс
        # не подтверждается recheck-ом, LLM плющит блок. В отличие от Q1 (весь
        # файл), здесь исключается ТОЛЬКО конкретный класс (file::code) —
        # ошибки других кодов того же файла остаются в очереди. Ключ (file,
        # code), а НЕ полная сигнатура: mypy-message list-item варьирует номер
        # элемента, полная сигнатура у каждой ошибки класса разная (см.
        # _track_toxic_signature).
        _toxic_sigs = set(context.metadata.get("_toxic_signatures") or [])
        if _toxic_sigs:
            _skipped_sig_now = {}
            _kept = []
            for e in errors:
                _ck = f"{str(e.get('file') or '').replace(chr(92), '/')}::{e.get('code') or ''}"
                if _ck in _toxic_sigs:
                    _skipped_sig_now[_ck] = _skipped_sig_now.get(_ck, 0) + 1
                    continue
                _kept.append(e)
            errors = _kept
            if _skipped_sig_now:
                _sig_skip_meta = dict(context.metadata.get("_toxic_sig_skipped") or {})
                for _s, _n in _skipped_sig_now.items():
                    _sig_skip_meta[_s] = max(_sig_skip_meta.get(_s, 0), _n)
                new_metadata = dict(context.metadata)
                new_metadata["_toxic_sig_skipped"] = _sig_skip_meta
                context = context.update(metadata=new_metadata)
                logger.info(
                    "  containment: отфильтровано %d ошибок toxic-классов: %s",
                    sum(_skipped_sig_now.values()), _skipped_sig_now,
                )

        valid_errors = [
            e for e in errors
            if ErrorContextValidator.is_valid(e, context.project_path)
        ]
        for err in errors:
            if err not in valid_errors:
                context = context.add_unfixable_error(err)
                logger.debug("  Ошибка отфильтрована как невалидная: %s:%s",
                             err.get("file", ""), err.get("message", "")[:80])

        if not valid_errors:
            logger.info("  Все ошибки невалидны – завершаем цикл")
            return context.add_state_to_history(State.COMPLETED)

        # Определяем вес каждой ошибки для сортировки
        classifier = ErrorClassifier()
        # 2026-07-08 (loguru, 6 прогонов): детерминированно-починяемый CLEANUP
        # живого кода (3×E203 в loguru/*.py) стоял в хвосте очереди за 148
        # нечинимыми корпусными/CI-ошибками и ни разу не дождался обработки
        # за 30-минутный бюджет. Ранняя полоса: rule-based-чинимые ошибки
        # файлов вне тестов/CI поднимаются до 95 — выше LLM-требующих
        # SECURITY (90; дешёвый гарантированный фикс раньше дорогих попыток),
        # но ниже BLOCKING (100) и CRITICAL_SYNTAX (200): контракт «сначала
        # чинить то, что не собирается» сохраняется. Риска ACCEPT-стороны
        # нет — меняется только порядок, все гейты/guard-ы неизменны.
        from fixers.rule_based_fixer import RuleBasedFixer  # локально, без cycle
        _rb = RuleBasedFixer()
        _NON_LIVE_MARKERS = ("tests/", "test/", "testing/", ".github/",
                             "docs/", "examples/")

        def _is_live_file(path: str) -> bool:
            p = str(path or "").replace("\\", "/").lstrip("./")
            return bool(p) and not any(
                p == m.rstrip("/") or p.startswith(m) for m in _NON_LIVE_MARKERS
            )

        # Up-front memory-pass (2026-07-08, рецепт воспроизводимости по
        # репро-разбору dotenv №1/№2): ошибка с ИЗВЕСТНЫМ фиксом в памяти
        # (runtime/<project>/memory.json переживает пере-клон) реплеится
        # мгновенно и без LLM — такие поднимаются в голову очереди (98,
        # выше SECURITY и полосы rule-based 95, ниже BLOCKING 100), чтобы
        # повторный прогон начинал с гарантированного повторения уже
        # доказанных фиксов, а не тратил усечённый бюджет на новые LLM-
        # попытки. Реплей по-прежнему проходит все guard-ы (memory-патч
        # идёт обычным пайплайном с pre-apply проверкой и O.19-карантином).
        _memory = getattr(context, "memory", None)

        def _has_known_fix(e) -> bool:
            if _memory is None:
                return False
            try:
                from core.pipeline_stage import PipelineStage as _PS
                _ext = Path(e.get("file", "")).suffix.lower() or None
                return bool(_memory.get_known_fix(
                    _PS._static_signature(e), language=context.language, file_ext=_ext,
                ))
            except Exception:
                return False

        for e in valid_errors:
            if e.get("code") == "E0765":
                e["_weight"] = 9999   # высший приоритет – незакрытая кавычка ломает всё
            else:
                e["_weight"] = classifier.get_weight(e)
                if (_rb.can_fix_deterministically(e.get("code", ""), context.language)
                        and _is_live_file(e.get("file", ""))):
                    e["_weight"] = max(float(e["_weight"]), 95.0)
                if _has_known_fix(e):
                    e["_weight"] = max(float(e["_weight"]), 98.0)
            # Пишем priority = _weight чтобы RootCauseAnalyzer видел реальный вес
            e["priority"] = float(e["_weight"])

        # 2026-07-08 (репро-разбор python-dotenv №1 vs №2): у равных весов не
        # было вторичного ключа — ничья E704↔dependabot (оба UNKNOWN=50)
        # флипалась между прогонами, dependabot-фикс появлялся то в одном,
        # то в другом (semgrep-находки выбираемы только на cycle-0 — кто не
        # успел, тот потерял). Детерминированный tie-break (file, line, code)
        # закрепляет порядок: одинаковый вход → одинаковая очередь.
        valid_errors.sort(
            key=lambda e: (
                -float(e.get("_weight", 0)),
                str(e.get("file", "")).replace("\\", "/"),
                int(e.get("line", 0) or 0),
                str(e.get("code", "")),
            )
        )

        logger.info("  Валидных ошибок для приоритизации: %d", len(valid_errors))
        logger.info("  Распределение классов: %s",
                    {cls: sum(1 for e in valid_errors if e.get("error_class") == cls)
                     for cls in set(e.get("error_class", "?") for e in valid_errors)})
        logger.info("  Первая (высший приоритет) ошибка: %s:%s [%s] %s",
                    valid_errors[0].get("file", ""),
                    valid_errors[0].get("line", 0),
                    valid_errors[0].get("error_class", ""),
                    valid_errors[0].get("message", ""))
        logger.debug("  Топ-3 ошибок по приоритету: %s",
                     [(e.get("file"), e.get("line"), e.get("error_class")) for e in valid_errors[:3]])

        prioritized = self.priority_engine.rank(valid_errors)
        context = context.update(prioritized_errors=tuple(prioritized))
        return context.add_state_to_history(State.ROOT_CAUSE)