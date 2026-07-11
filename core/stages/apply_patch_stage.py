"""
Стадия применения патча.
Поддерживает полную замену файла и проверку баланса скобок.
Для эвристики brace_heuristic проверка баланса отключается.
Добавлена проверка структурных анкоров (impl, fn, struct) перед валидацией.
"""

import logging
import subprocess
from pathlib import Path
from typing import Dict

from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from core.state_machine import State
from core.utils import process_key as _process_key
from fixers.patch_engine import PatchEngine
from fixers.brace_utils import count_braces_safe

logger = logging.getLogger(__name__)


class ApplyPatchStage(PipelineStage):
    def __init__(self, patch_engine: PatchEngine, analyzer=None, classifier=None):
        self.patch_engine = patch_engine
        # Язык-специфичный анализатор (flake8/cargo/tsc/...), тот же что у
        # AnalyzeStage/DecideStage. Без него _get_errors не может надёжно
        # посчитать CRITICAL_SYNTAX для непустого до/после сравнения —
        # раньше здесь был захардкожен RustAnalyzer, который для не-Rust
        # проектов (нет Cargo.toml) молча возвращает [] и не ловит вообще
        # никакую регрессию (before=0, after=0 всегда проходили проверку).
        self.analyzer = analyzer
        # error_class у "сырых" ошибок анализатора не выставлен — это делает
        # отдельно ClassifyStage через тот же ErrorClassifier. Без него
        # before_critical/after_critical здесь всегда были бы 0 (поле просто
        # отсутствует), и проверка регрессии была бы такой же мёртвой, как
        # с захардкоженным RustAnalyzer.
        self.classifier = classifier

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия APPLY_PATCH: применение патча...")
        error = context.selected_error
        patch = context.generated_patch
        full_content = context.metadata.get("full_file_replacement")
        patch_source = context.metadata.get(MetadataKeys.PATCH_SOURCE, "")

        # --- Если SyntaxRepair исправил файл на месте ---
        if patch == "HEALED":
            logger.info("  Файл уже исправлен на месте (HEALED), переходим к валидации")
            context = context.set_patch(None)
            # C6 (аудит 2026-07-01): у HEALED нет своей pre-patch карты —
            # stale-карта от ПРЕДЫДУЩЕГО патча здесь особенно опасна: снапшот
            # в validate взял бы чужой «оригинал», а net-delta откат записал
            # бы на диск содержимое чужих файлов.
            _hm = dict(context.metadata)
            _hm.pop("_pre_patch_content", None)
            context = context.update(metadata=_hm)
            return context.add_state_to_history(State.VALIDATING)

        if not patch and not full_content:
            logger.warning("  Нет патча для применения – пропускаем")
            return context.add_state_to_history(State.NEXT_ERROR)

        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
        file_path = work_dir / error["file"]
        if not file_path.exists():
            logger.error("  Файл %s не существует", file_path)
            return context.add_state_to_history(State.FAILED)

        # --- Защита от зацикливания DisasterRecovery ---
        if full_content and patch_source == "disaster_recovery":
            file = error.get("file", "")
            attempt_key = f"disaster_attempts_{file}"
            attempts = context.metadata.get(attempt_key, 0) + 1
            if attempts > 2:
                logger.warning(f"  DisasterRecovery для файла {file} вызывался {attempts} раз, пропускаем")
                new_metadata = dict(context.metadata)
                new_metadata.pop("full_file_replacement", None)
                new_metadata.pop("patch_source", None)
                context = context.update(metadata=new_metadata)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.NEXT_ERROR)
            new_metadata = dict(context.metadata)
            new_metadata[attempt_key] = attempts
            context = context.update(metadata=new_metadata)

        # --- Полная замена файла ---
        if full_content:
            logger.info("  Полная замена файла (из метаданных)")
            backup = file_path.read_text(encoding="utf-8")
            # P2.7 Fix #1: save pre-patch content so validate_stage can do real rollback
            _fc_meta = dict(context.metadata)
            _fc_meta["_pre_patch_content"] = {error.get("file", ""): backup}
            context = context.update(metadata=_fc_meta)

            if patch_source == "brace_heuristic":
                logger.info("  Применяем сбалансированный код от эвристики скобок (проверка баланса отключена)")
                try:
                    file_path.write_text(full_content, encoding="utf-8")
                    if not self._python_syntax_ok(file_path, full_content):
                        logger.warning(
                            "  brace_heuristic дал синтаксически невалидный код (%s) – откат",
                            file_path.name,
                        )
                        file_path.write_text(backup, encoding="utf-8")
                        new_metadata = dict(context.metadata)
                        new_metadata.pop("full_file_replacement", None)
                        new_metadata.pop("patch_source", None)
                        context = context.update(metadata=new_metadata)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                    logger.info("  Полная замена успешно применена")
                    new_metadata = dict(context.metadata)
                    new_metadata.pop("full_file_replacement", None)
                    new_metadata.pop("patch_source", None)
                    context = context.update(metadata=new_metadata)
                    context = self._reset_file_error_tracking(context, error.get("file", ""))
                    # Проверка структурных анкоров перед валидацией
                    if not self._preserve_structural_anchors(backup, full_content):
                        logger.warning("  Полная замена нарушила структурные анкоры – откат")
                        file_path.write_text(backup, encoding="utf-8")
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                    # --- Защита от попадания отчёта об ошибках в код ---
                    if self._contains_error_report(full_content):
                        logger.warning("  Полная замена содержит отчёт об ошибках — откат")
                        file_path.write_text(backup, encoding="utf-8")
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                    return context.add_state_to_history(State.VALIDATING)
                except Exception as e:
                    logger.error("  Ошибка записи полной замены: %s", e)
                    file_path.write_text(backup, encoding="utf-8")
                    return context.add_state_to_history(State.FAILED)

            if patch_source == "syntax_reconstruction":
                # E999 Semantic Recovery (2026-06-21): файл реконструирован
                # ЦЕЛИКОМ по логике (см. fixers/semantic_recovery.py). Старый
                # контент СЛОМАН (E999 каскад) — проверка баланса скобок
                # старого/нового бессмысленна (старое не сбалансировано по
                # определению сценария), поэтому здесь её НЕТ. Структурная
                # проверка имён/сигнатур — Python-специфичная, делается ниже
                # по потоку в ReviewStage (AST-диф + socraticode), а не здесь
                # (anchors-проверка ниже — Rust-специфичная, для Python
                # бессмысленна).
                logger.info("  E999 Semantic Recovery: применяем реконструированный файл")
                try:
                    file_path.write_text(new_content := full_content, encoding="utf-8")
                    if not self._python_syntax_ok(file_path, new_content):
                        logger.warning(
                            "  syntax_reconstruction дала синтаксически невалидный код (%s) — откат",
                            file_path.name,
                        )
                        file_path.write_text(backup, encoding="utf-8")
                        new_metadata = dict(context.metadata)
                        new_metadata.pop("full_file_replacement", None)
                        new_metadata.pop("patch_source", None)
                        context = context.update(metadata=new_metadata)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                    if self._contains_error_report(new_content):
                        logger.warning("  syntax_reconstruction содержит отчёт об ошибках — откат")
                        file_path.write_text(backup, encoding="utf-8")
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                    # full_file_replacement уже применён — чистим, чтобы не
                    # "утёк" в ApplyPatchStage следующей (другой) ошибки.
                    # patch_source ОСТАВЛЯЕМ — ReviewStage должен увидеть
                    # "syntax_reconstruction" и применить усиленную проверку.
                    new_metadata = dict(context.metadata)
                    new_metadata.pop("full_file_replacement", None)
                    context = context.update(metadata=new_metadata)
                    logger.info("  E999 Semantic Recovery: реконструкция применена, переходим к валидации")
                    return context.add_state_to_history(State.VALIDATING)
                except Exception as e:
                    logger.error("  Ошибка применения syntax_reconstruction: %s", e)
                    file_path.write_text(backup, encoding="utf-8")
                    return context.add_state_to_history(State.FAILED)

            if patch_source == "disaster_recovery":
                logger.info("  Проверка DisasterRecovery...")
                try:
                    before_errors = self._get_errors(work_dir)
                    before_count = len(before_errors)
                    file_path.write_text(full_content, encoding="utf-8")
                    if not self._python_syntax_ok(file_path, full_content):
                        logger.warning(
                            "  DisasterRecovery дал синтаксически невалидный код (%s) – откат",
                            file_path.name,
                        )
                        file_path.write_text(backup, encoding="utf-8")
                        new_metadata = dict(context.metadata)
                        new_metadata.pop("full_file_replacement", None)
                        new_metadata.pop("patch_source", None)
                        context = context.update(metadata=new_metadata)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                    after_errors = self._get_errors(work_dir)
                    after_count = len(after_errors)

                    before_critical = sum(1 for e in before_errors if e.get('error_class') == 'CRITICAL_SYNTAX')
                    after_critical = sum(1 for e in after_errors if e.get('error_class') == 'CRITICAL_SYNTAX')

                    if after_critical > before_critical or after_count > before_count:
                        logger.warning("  DisasterRecovery ухудшил состояние – откат")
                        file_path.write_text(backup, encoding="utf-8")
                        new_metadata = dict(context.metadata)
                        new_metadata.pop("full_file_replacement", None)
                        new_metadata.pop("patch_source", None)
                        context = context.update(metadata=new_metadata)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)

                    logger.info("  DisasterRecovery не ухудшил состояние")
                except Exception as e:
                    logger.error("  Ошибка проверки DisasterRecovery: %s", e)
                    file_path.write_text(backup, encoding="utf-8")
                    return context.add_state_to_history(State.FAILED)

                new_metadata = dict(context.metadata)
                new_metadata.pop("full_file_replacement", None)
                new_metadata.pop("patch_source", None)
                context = context.update(metadata=new_metadata)
                return context.add_state_to_history(State.VALIDATING)

            # Обычная полная замена
            if not self._is_brace_balance_ok(file_path, full_content):
                logger.warning("  Полная замена отклонена: изменён баланс скобок")
                new_metadata = dict(context.metadata)
                new_metadata.pop("full_file_replacement", None)
                context = context.update(metadata=new_metadata)
                context = context.record_processed_error(_process_key(error))
                context = context.add_unfixable_error(error)
                return context.add_state_to_history(State.NEXT_ERROR)

            try:
                file_path.write_text(full_content, encoding="utf-8")
                if not self._python_syntax_ok(file_path, full_content):
                    logger.warning(
                        "  Полная замена дала синтаксически невалидный код (%s) – откат",
                        file_path.name,
                    )
                    file_path.write_text(backup, encoding="utf-8")
                    new_metadata = dict(context.metadata)
                    new_metadata.pop("full_file_replacement", None)
                    new_metadata.pop("patch_source", None)
                    context = context.update(metadata=new_metadata)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)
                logger.info("  Полная замена успешно применена")
                new_metadata = dict(context.metadata)
                new_metadata.pop("full_file_replacement", None)
                new_metadata.pop("patch_source", None)
                context = context.update(metadata=new_metadata)
                # Проверка структурных анкоров перед валидацией
                if not self._preserve_structural_anchors(backup, full_content):
                    logger.warning("  Полная замена нарушила структурные анкоры – откат")
                    file_path.write_text(backup, encoding="utf-8")
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)
                # --- Защита от попадания отчёта об ошибках в код ---
                if self._contains_error_report(full_content):
                    logger.warning("  Полная замена содержит отчёт об ошибках — откат")
                    file_path.write_text(backup, encoding="utf-8")
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)
                return context.add_state_to_history(State.VALIDATING)
            except Exception as e:
                logger.error("  Ошибка записи полной замены: %s", e)
                file_path.write_text(backup, encoding="utf-8")
                return context.add_state_to_history(State.FAILED)

        # --- Обычный патч (возможно МНОГО-ФАЙЛОВЫЙ) ---
        error_rel = error.get("file", "")

        # Базовая проверка: если патч не затрагивает целевой файл ошибки — отказ
        if not any(self._same_rel(f, error_rel) for f in self.patch_engine.files_in_patch(patch)):
            logger.warning(
                "  Патч не затрагивает целевой файл %s — отклоняем",
                error_rel,
            )
            return context.add_state_to_history(State.NEXT_ERROR)

        # NEW: запрещаем LLM трогать .json (кроме манифестов пакетов) и .md файлы.
        files_in = list(self.patch_engine.files_in_patch(patch))
        bad = [f for f in files_in if self._is_blocked_for_llm_edit(f, error_rel)]
        if bad:
            logger.warning(
                "  Патч содержит правки на запрещённых для LLM файлах "
                "(.md / data .json без whitelist): %s — отклоняем целиком",
                bad,
            )
            return context.add_state_to_history(State.NEXT_ERROR)

        targets = [error_rel]
        for pf in files_in:
            if not self._same_rel(pf, error_rel):
                targets.append(pf)

        # Бэкапы существующих файлов (основной первым). order сохраняет порядок.
        backups: Dict[str, tuple] = {}
        order = []
        for rel in targets:
            fp = work_dir / rel
            key = str(fp)
            if key in backups:
                continue
            if not fp.exists():
                if self._same_rel(rel, error_rel):
                    logger.error("  Файл %s не существует", fp)
                    return context.add_state_to_history(State.FAILED)
                logger.warning("  Файл секции патча не существует, пропуск: %s", rel)
                continue
            backups[key] = (fp, fp.read_text(encoding="utf-8"))
            order.append(key)

        # P2.7 Fix #1: save pre-patch content so validate_stage can do real rollback
        if order:
            _pre_patch: Dict[str, str] = {}
            for _rel in targets:
                _rkey = str(work_dir / _rel)
                if _rkey in backups:
                    _pre_patch[_rel] = backups[_rkey][1]
            if _pre_patch:
                _pm = dict(context.metadata)
                _pm["_pre_patch_content"] = _pre_patch
                context = context.update(metadata=_pm)

        def _rollback_all():
            for k in order:
                bfp, btext = backups[k]
                bfp.write_text(btext, encoding="utf-8")

        # --- Stale snapshot detection ---
        snap_hash = context.metadata.get(MetadataKeys.FILE_CONTENT_HASH)
        if snap_hash is not None and order:
            _, primary_backup = backups[order[0]]
            import hashlib as _hl
            current_hash = _hl.sha256(primary_backup.encode("utf-8", errors="ignore")).hexdigest()
            if current_hash != snap_hash:
                logger.warning(
                    "  stale file snapshot detected: %s изменён между генерацией "
                    "патча и его применением — перегенерируем с актуальным содержимым",
                    error_rel,
                )
                stale_count = context.metadata.get("_stale_retries", 0) + 1
                new_metadata = dict(context.metadata)
                new_metadata.pop(MetadataKeys.FILE_CONTENT_HASH, None)
                new_metadata["_stale_retries"] = stale_count
                context = context.update(metadata=new_metadata)
                if stale_count > 2:
                    logger.warning("  _stale_retries > 2 — считаем ошибку нефиксируемой")
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)
                return context.add_state_to_history(State.GENERATING_PATCH)

        # --- Structured direct apply (минует _locate_hunk) ---
        _se_raw = context.metadata.get("structured_edit")
        _use_structured_direct = (
            _se_raw is not None
            and patch_source in {"structured_llm_critical", "structured_llm", "structured_llm_blocking"}
        )

        try:
            # 1) Применяем каждую секцию к своему файлу.
            applied_any = False
            for key in order:
                bfp, _bk = backups[key]
                applied = False

                if _use_structured_direct and key == order[0]:
                    try:
                        from fixers.structured_edit import EditSet as _EditSet
                        _es = _EditSet.from_dict(_se_raw)
                        if _es is not None:
                            _result = _es.apply({error_rel: _bk})
                            if _result and error_rel in _result:
                                bfp.write_text(_result[error_rel], encoding="utf-8")
                                applied = True
                                logger.info(
                                    "  structured direct apply OK (минует _locate_hunk): %s",
                                    error_rel,
                                )
                            else:
                                logger.warning(
                                    "  structured direct apply: anchor не найден — fallback к apply_patch"
                                )
                    except Exception as _se_err:
                        logger.warning(
                            "  structured direct apply упал: %s — fallback к apply_patch", _se_err
                        )

                if not applied:
                    applied = self.patch_engine.apply_patch(bfp, patch)

                if applied:
                    applied_any = True

            if not applied_any:
                _rollback_all()
                # O.15: LLM слил строки — даём одну попытку с явной подсказкой
                # Счётчик per-error-signature чтобы разные ошибки не блокировали друг друга (BUG-2).
                if self.patch_engine.last_error_code == "O.15":
                    self.patch_engine.last_error_code = ""
                    _o15_key = f"_o15_retries_{self._error_signature(error)}"
                    o15_retries = context.metadata.get(_o15_key, 0)
                    if o15_retries < 1:
                        logger.warning(
                            "  O.15 detected — retry с feedback (attempt %d)", o15_retries + 1
                        )
                        _m = dict(context.metadata)
                        _m[_o15_key] = o15_retries + 1
                        _m[MetadataKeys.LAST_PATCH_FAILURE] = (
                            "O.15: your patch merged two source lines into one string "
                            "(removed the newline between them). "
                            "Each source line MUST end with \\n in the unified diff."
                        )
                        context = context.update(metadata=_m)
                        return context.add_state_to_history(State.GENERATING_PATCH)
                logger.warning("  Не удалось применить патч ни к одному файлу")
                context = self._note_unanchorable_attempt(context, error)
                _apf_meta = dict(context.metadata)
                _apf_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                    "APPLY_FAILED: patch could not be applied to any file - "
                    "anchors or context lines did not match. Try a different, "
                    "minimal edit anchored to unambiguous surrounding lines."
                )
                context = context.update(metadata=_apf_meta)
                context = context.record_processed_error(_process_key(error))
                return context.add_state_to_history(State.NEXT_ERROR)

            # 2) Поштучные проверки целостности (баланс скобок + анкоры).
            #    Дополнительно: O.18 — отказ, если патч ДОБАВИЛ в файл
            #    LLM-noise (текст промпта / `# Line N: [E…]` / `// FIXME …`).
            #    Только если в исходнике этого шума НЕ было, а в новой версии
            #    появился — значит LLM протекла промптом в код.
            from fixers.rule_based_fixer import RuleBasedFixer  # локально, без cycle
            for key in order:
                bfp, bk = backups[key]
                new_content = bfp.read_text(encoding="utf-8")
                if new_content == bk:
                    continue  # этот файл не менялся
                # Прямая AST-проверка ИМЕННО этого файла. Project-wide
                # before/after-сравнение CRITICAL_SYNTAX (шаг 3 ниже) в
                # параллельном режиме ненадёжно: before считается из
                # context.current_errors, который ParallelExecutor наполняет
                # только ошибками ОДНОГО файла (см. core/engine/parallel.py),
                # а after — это полный скан всего проекта; совпадение по
                # числу может случайно пропустить регрессию именно этого
                # файла. ast.parse не зависит от других потоков/файлов и
                # ловит именно тот паттерн порчи (unterminated string и т.п.),
                # который ускользал раньше.
                if bfp.suffix == ".py":
                    try:
                        import ast as _ast_check
                        _ast_check.parse(new_content)
                    except SyntaxError as _se:
                        logger.warning(
                            "  Патч сделал %s синтаксически невалидным (%s) – откат",
                            bfp.name, _se,
                        )
                        _rollback_all()
                        context = self._note_unanchorable_attempt(context, error)
                        _asti_meta = dict(context.metadata)
                        _asti_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                            "AST_INVALID: your patch made the file unparseable "
                            "(invalid Python syntax) and was rolled back. "
                            "Re-check the diff context lines and try a smaller, "
                            "syntactically self-contained edit."
                        )
                        context = context.update(metadata=_asti_meta)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                if (RuleBasedFixer.contains_llm_noise(new_content)
                        and not RuleBasedFixer.contains_llm_noise(bk)):
                    logger.error(
                        "  O.18 — патч добавил в %s LLM-noise (промпт-маркеры). "
                        "Откатываем все файлы.",
                        bfp,
                    )
                    _rollback_all()
                    _noise_meta = dict(context.metadata)
                    _noise_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                        "LLM_NOISE: your patch injected prompt or report markers "
                        "into the file content and was rolled back. Output only "
                        "the source code diff, without any commentary or error "
                        "listings."
                    )
                    context = context.update(metadata=_noise_meta)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)
                if error.get("error_class") == "CRITICAL_SYNTAX":
                    o_o, o_c = count_braces_safe(bk)
                    orig_balance = abs(o_o - o_c)
                    n_o, n_c = count_braces_safe(new_content)
                    new_balance = abs(n_o - n_c)
                    if (new_balance > orig_balance and orig_balance > 0) or \
                       (orig_balance == 0 and new_balance > 0):
                        logger.warning("  Патч нарушил баланс скобок (%s) – откат", bfp.name)
                        _rollback_all()
                        context = self._note_unanchorable_attempt(context, error)
                        _brace_meta = dict(context.metadata)
                        _brace_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                            "BRACE_IMBALANCE: your patch changed the file's "
                            "bracket or brace balance and was rolled back. Make "
                            "sure every opening bracket you touch has a matching "
                            "closing one."
                        )
                        context = context.update(metadata=_brace_meta)
                        context = context.record_processed_error(_process_key(error))
                        return context.add_state_to_history(State.NEXT_ERROR)
                if not self._preserve_structural_anchors(bk, new_content):
                    logger.warning("  Патч нарушил структурные анкоры (%s) – откат", bfp.name)
                    _rollback_all()
                    context = self._note_unanchorable_attempt(context, error)
                    _anchor_meta = dict(context.metadata)
                    _anchor_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                        "ANCHOR_LOST: your patch removed or altered an existing "
                        "structural anchor (impl or fn or struct) and was rolled "
                        "back. Keep existing signatures intact and only edit "
                        "inside their bodies."
                    )
                    context = context.update(metadata=_anchor_meta)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)

                # --- Защита от попадания отчёта об ошибках в код ---
                if self._contains_error_report(new_content):
                    logger.warning("  Патч внёс отчёт об ошибках в %s — откат", bfp.name)
                    _rollback_all()
                    _rep_meta = dict(context.metadata)
                    _rep_meta[MetadataKeys.LAST_PATCH_FAILURE] = (
                        "ERROR_REPORT_LEAK: your patch embedded what looks like "
                        "an error report or listing into the file and was "
                        "rolled back. Output only the corrected source code."
                    )
                    context = context.update(metadata=_rep_meta)
                    context = context.record_processed_error(_process_key(error))
                    return context.add_state_to_history(State.NEXT_ERROR)

            # 3) Проектная проверка на новые CRITICAL_SYNTAX (один раз).
            after_errors = self._get_errors(work_dir)
            after_critical = sum(1 for e in after_errors if e.get('error_class') == 'CRITICAL_SYNTAX')
            before_critical = sum(1 for e in context.current_errors if e.get('error_class') == 'CRITICAL_SYNTAX')

            if after_critical > before_critical:
                logger.warning("  Появились новые CRITICAL_SYNTAX ошибки – откат")
                _rollback_all()
                context = context.record_processed_error(_process_key(error))
                # Изменение A (2026-07-07): тот же откат кормит и per-file
                # anchor-fail счётчик GeneratePatchStage — см.
                # _note_unanchorable_attempt (этот путь и был основным
                # источником 43-48 неучтённых откатов за прогон).
                context = self._note_unanchorable_attempt(context, error)
                # Кормим FileAntiLoop стабильной причиной неудачи. Без этого
                # (экзамен-проба 2026-07-04, pytest-homeassistant-custom-component)
                # 3 сложных .github/workflows/*.yml, которые structured-diff не мог
                # заанкорить, откатывались по CRITICAL_SYNTAX БЕЗ failure_reason →
                # early-abort «3× одна причина» не срабатывал, каждый файл жёг весь
                # лимit попыток (11/10) × ~35s LLM-генерации ≈ 19 мин из 31-мин
                # бюджета, cycles_run=0, продуктивные Python-фиксы не достигнуты.
                # Строка стабильна (без номеров строк) — streak накапливается;
                # успешный патч меняет file-hash → FileAntiLoop сбрасывает счётчики.
                _csm = dict(context.metadata)
                _csm[MetadataKeys.LAST_PATCH_FAILURE] = (
                    "CRITICAL_SYNTAX: your patch introduced a new syntax error in "
                    "the file and was rolled back. This file resists structured "
                    "patching (e.g. non-Python/YAML anchors) — try a different, "
                    "minimal edit or skip."
                )
                context = context.update(metadata=_csm)
                return context.add_state_to_history(State.NEXT_ERROR)

            logger.info("  Патч успешно применён (%d файл(ов)), переходим к валидации", len(order))
            # очищаем stale-ключи после успеха
            _clean = dict(context.metadata)
            _clean.pop(MetadataKeys.FILE_CONTENT_HASH, None)
            _clean.pop("_stale_retries", None)
            context = context.update(metadata=_clean)
            return context.add_state_to_history(State.VALIDATING)

        except Exception as e:
            logger.error("  Ошибка при применении патча: %s", e)
            _rollback_all()
            return context.add_state_to_history(State.FAILED)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------
    def _get_errors(self, project_dir: Path):
        if self.analyzer is not None:
            try:
                try:
                    errors = self.analyzer.analyze(project_dir, clean_before_each=False)
                except TypeError as te:
                    msg = str(te)
                    if "unexpected keyword argument" in msg and "clean_before_each" in msg:
                        errors = self.analyzer.analyze(project_dir)
                    else:
                        raise
                if self.classifier is not None:
                    for err in errors:
                        if not err.get("error_class"):
                            err["error_class"] = self.classifier.classify(err).get("class", "UNKNOWN")
                return errors
            except Exception as e:
                logger.debug(
                    "ApplyPatchStage: инжектированный анализатор упал (%s) — "
                    "fallback на AST-сканирование", e,
                )
        return self._ast_fallback_errors(project_dir)

    @staticmethod
    def _python_syntax_ok(file_path: Path, content: str) -> bool:
        """Прямая AST-проверка для путей полной замены файла
        (brace_heuristic / disaster_recovery / обычная полная замена) —
        они пишут full_content на диск без какой-либо проверки валидности
        синтаксиса (только баланс скобок и структурные анкоры, которые не
        ловят, например, unterminated string literal)."""
        if file_path.suffix != ".py":
            return True
        try:
            import ast as _ast_check
            _ast_check.parse(content)
            return True
        except SyntaxError:
            return False

    @staticmethod
    def _ast_fallback_errors(project_dir: Path) -> list:
        """Минимальная языко-агностическая страховка, когда analyzer не
        инжектирован (например, старые тесты). Ловит только файлы .py,
        которые вообще не парсятся — не замена полноценному анализатору,
        но не даёт явному SyntaxError пройти проверку незамеченным."""
        import ast as _ast
        exclude = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox"}
        errors = []
        try:
            for py_file in project_dir.rglob("*.py"):
                if any(part in exclude for part in py_file.parts):
                    continue
                try:
                    content = py_file.read_text(encoding="utf-8", errors="ignore")
                    _ast.parse(content)
                except SyntaxError:
                    errors.append({"error_class": "CRITICAL_SYNTAX", "file": str(py_file)})
                except Exception as _e_file:
                    logger.debug("_ast_fallback_errors: пропуск %s: %s", py_file, _e_file)
                    continue
        except Exception as _e_walk:
            logger.debug("_ast_fallback_errors: обход %s прерван: %s", project_dir, _e_walk)
        return errors

    @staticmethod
    def _is_brace_balance_ok(original_path: Path, new_content: str) -> bool:
        orig = original_path.read_text(encoding="utf-8")
        orig_counts = count_braces_safe(orig)
        new_counts = count_braces_safe(new_content)
        if orig_counts != new_counts:
            logger.warning("Баланс скобок нарушен: было %s, стало %s", orig_counts, new_counts)
            return False
        return True

    @staticmethod
    def _same_rel(a: str, b: str) -> bool:
        """Один ли это относительный путь (устойчиво к `\\`/`/` и `./`).

        M2 (аудит 2026-07-01): суффиксное сравнение — ТОЛЬКО по границе
        сегмента пути. Раньше голый endswith отождествлял `utils.py` с
        `tests/utils.py`/`src/utils.py`: патч на чужой одноимённый файл
        проходил проверку «затрагивает целевой файл», а блокировка
        .md/.json снималась не для того файла."""
        if not a or not b:
            return False

        def _norm(p: str) -> str:
            p = str(p).replace("\\", "/")
            while p.startswith("./"):
                p = p[2:]
            p = p.lstrip("/")
            # a/-b/-префиксы unified diff срезаем явно.
            if p.startswith(("a/", "b/")):
                p = p[2:]
            return p

        na = _norm(a)
        nb = _norm(b)
        if na == nb:
            return True
        # Лишние родительские каталоги (patch указывает путь от корня репо,
        # error — от project_path): суффикс засчитывается только целыми
        # сегментами И только если короткая сторона сама содержит ≥2
        # сегментов — голый basename ("utils.py") не должен матчить
        # одноимённые файлы из других директорий.
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        if "/" not in shorter:
            return False
        return longer.endswith("/" + shorter)

    # ------------------------------------------------------------------
    # NEW: блок на правку документации и data-json-файлов LLM-патчами.
    # ------------------------------------------------------------------
    # Базовые имена .json, которые система ДОЛЖНА уметь обновлять
    # (через dep-inference и LLM-патчи): манифесты пакетов и tsconfig.
    _MANIFEST_JSON = frozenset({
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "composer.json",
        "deno.json",
        "deno.jsonc",
        "jsr.json",
        "tsconfig.base.json",
        "tsconfig.app.json",
    })

    @classmethod
    def _is_blocked_for_llm_edit(cls, file_rel: str, error_file_rel: str) -> bool:
        """True, если этот файл из патча НЕЛЬЗЯ редактировать LLM-патчем.

        Блокируется:
          * `*.md` — документация (README, CHANGELOG, ...).
          * `*.json` НЕ из whitelist'а манифестов.
          * `*.jsonc` НЕ из whitelist'а (deno.jsonc).

        Исключение: если сам error относится к этому файлу (movok ловит
        реальную ошибку в README или package.json и сам же её чинит) —
        ПРОПУСКАЕМ блокировку, чтобы не отрубать целевой кейс.
        """
        if not file_rel:
            return False
        norm = str(file_rel).replace("\\", "/")
        base = norm.rsplit("/", 1)[-1].lower()
        # Если это и есть файл, на который указывает текущая ошибка — пропускаем.
        if error_file_rel and cls._same_rel(file_rel, error_file_rel):
            return False
        if base.endswith(".md"):
            return True
        if base.endswith(".json") or base.endswith(".jsonc"):
            return base not in {n.lower() for n in cls._MANIFEST_JSON}
        return False

    @staticmethod
    def _error_signature(error: Dict) -> str:
        from core.pipeline_stage import PipelineStage
        return PipelineStage._static_signature(error)

    def _note_unanchorable_attempt(self, context: PipelineContext, error: Dict) -> PipelineContext:
        """Кормит per-file anchor-fail счётчик GeneratePatchStage данными из
        ЭТОЙ (Apply) стадии.

        Инцидент 2026-07-04/07 (pytest-homeassistant-custom-component, три
        прогона): fast-fail 3bd4813 в GeneratePatchStage.execute() читает
        `_anchor_fail_counts`/`_structured_unanchorable_files`, но эти ключи
        пополнялись ТОЛЬКО в ветке ПУСТОГО ответа LLM (anchor_empty/
        anchor_mismatch, generate_patch_stage.py:1646). Реальный burn-путь —
        непустой патч → fallback apply мнёт YAML/py → откат ЗДЕСЬ, в Apply —
        счётчик не кормил ни разу: 43-48 CRITICAL_SYNTAX-откатов за прогон,
        fast-fail сработал 0 раз, ~35s LLM-генерации на каждый впустую.

        Apply лишь ведёт advisory-bookkeeping для ЧУЖОГО счётчика: решение
        NEEDS_REVIEW по-прежнему принимает только GeneratePatchStage через
        существующий гейт (execute():697) — контракт границ стадий (Generate
        только генерирует/решает, Apply только применяет) не нарушается,
        прецедент такого же rollback->Generate bookkeeping — установка
        LAST_PATCH_FAILURE из Apply (см. 76a16ca).
        """
        err_file = str(error.get("file") or "")
        if not err_file:
            return context

        # Локальный импорт — без cycle (Generate уже импортирует Apply
        # транзитивно через фабрики пайплайна).
        from core.stages.generate_patch_stage import GeneratePatchStage

        _af_meta = dict(context.metadata)
        _af_counts = dict(_af_meta.get("_anchor_fail_counts") or {})
        _af_counts[err_file] = _af_counts.get(err_file, 0) + 1
        _af_meta["_anchor_fail_counts"] = _af_counts
        if _af_counts[err_file] >= GeneratePatchStage.MAX_ANCHOR_FAILS_PER_FILE:
            _unf = list(_af_meta.get("_structured_unanchorable_files") or [])
            if err_file not in _unf:
                _unf.append(err_file)
                _af_meta["_structured_unanchorable_files"] = _unf
                logger.warning(
                    "  файл %s помечен structured-unanchorable после %d "
                    "отката(ов) в ApplyPatchStage — его новые ошибки идут в "
                    "NR без дорогой LLM-генерации",
                    err_file, _af_counts[err_file],
                )
        return context.update(metadata=_af_meta)

    @staticmethod
    def _reset_file_error_tracking(context: PipelineContext, file_name: str) -> PipelineContext:
        """Сбрасывает unfixable_errors и processed_errors для указанного файла."""
        unfixable = [e for e in context.unfixable_errors if e.get("file") != file_name]
        processed = {
            k: v for k, v in dict(context.processed_errors).items()
            if not k.startswith(f"{file_name}::")
        }
        logger.info("  Сброшены unfixable и processed для файла %s", file_name)
        return context.update(unfixable_errors=unfixable, processed_errors=processed)

    # H8 (аудит 2026-07-01): _deduplicate_file (huniq) удалён. Глобальная
    # дедупликация СТРОК всего файла ПОСЛЕ всех проверок применения удаляла
    # бы любые легитимные повторы (`return None`, `pass`, закрывающие скобки,
    # пустые строки) при установленном huniq — тот же класс бага, что
    # PreCleanup keep-first и _py_remove_duplicate_lines/@overload, но во
    # внешней непроверяемой утилите. Настоящие дубли символов ловят
    # O.17/symbol_duplication и PreCleanup со своими AST-проверками.

    @staticmethod
    def _preserve_structural_anchors(original: str, patched: str) -> bool:
        """
        Проверяет, что ключевые структурные элементы (impl, fn, struct)
        не были удалены или изменены в сигнатуре.
        Возвращает True, если структура сохранена.
        """
        import re
        
        def extract_anchors(content: str) -> set:
            anchors = set()
            for m in re.finditer(r'(?:pub\s+)?impl\s+(?:\w+::)?(\w+)(?:\s+for\s+\w+)?\s*\{', content):
                anchors.add(f"impl:{m.group(1)}")
            for m in re.finditer(r'^\s*(pub\s+)?fn\s+(\w+)\s*\(([^)]*)\)', content, re.MULTILINE):
                name = m.group(2)
                params = m.group(3).strip()
                anchors.add(f"fn:{name}({params})")
            for m in re.finditer(r'struct\s+(\w+)', content):
                anchors.add(f"struct:{m.group(1)}")
            return anchors
        
        original_anchors = extract_anchors(original)
        patched_anchors = extract_anchors(patched)
        missing = original_anchors - patched_anchors
        if missing:
            logger.warning("  Структурные анкоры удалены или изменены: %s", missing)
            return False
        return True

    @staticmethod
    def _contains_error_report(content: str) -> bool:
        """Проверяет, содержит ли файл строки, похожие на отчёт об ошибках.

        L1 (аудит 2026-07-01): "FIXME:" убран из маркеров — это обычный
        комментарий в реальном коде, полная замена файла с легитимным
        `# FIXME:` откатывалась ложно. Остаются только уникальные маркеры
        промпта."""
        markers = [
            "ALL ERRORS IN THIS FILE",
            "--- ALL ERRORS",
        ]
        return any(marker in content for marker in markers)