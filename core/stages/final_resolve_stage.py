"""
Стадия финального разрешения неисправимых ошибок.
Заменяет оставшиеся BLOCKING ошибки на todo!() только в изолированной копии.
Вынесена из pipeline_stage.py для модульности.
Содержит DEBUG-логи.
"""

import logging
import re
from typing import Any, Dict

from core.pipeline_context import PipelineContext
from core.pipeline_stage import PipelineStage
from fixers.patch_engine import PatchEngine

logger = logging.getLogger(__name__)


class FinalResolveStage(PipelineStage):
    """
    Финальное разрешение неисправимых ошибок.
    Заменяет на todo!() все оставшиеся ошибки компиляции,
    которые конвейер не смог исправить.
    Работает ТОЛЬКО с изолированной копией (working_path).
    """

    BLOCKING_TYPES = {
        "unresolved_function",
        "unresolved_method",
        "type_mismatch_argument",
        "ownership_error",
        "mutability_error",
    }

    def __init__(self):
        logger.debug("FinalResolveStage инициализирован")

    def execute(self, context: PipelineContext) -> PipelineContext:
        logger.info("Стадия FINAL_RESOLVE: финальное разрешение неисправимых ошибок...")
        # H7 (аудит 2026-07-01): подстановка `let _ = todo!(...)` — Rust-
        # синтаксис. Раньше единственной защитой было совпадение строк
        # error_type с Rust-набором BLOCKING_TYPES; любой не-Rust анализатор,
        # выставивший, например, "unresolved_function", получил бы Rust-код
        # внутри своего файла ПОСЛЕ всех guard-ов и прямо перед аудитом.
        if (context.language or "").lower() not in ("rust", "rs"):
            logger.info("  FINAL_RESOLVE: язык %s не поддерживается (только Rust) — пропуск",
                        context.language)
            return context
        resolved = 0
        remaining_unfixable = []

        # +++ ИСПОЛЬЗУЕМ ИЗОЛИРОВАННУЮ КОПИЮ +++
        work_dir = context.working_path if hasattr(context, 'working_path') else context.project_path
        logger.debug("  Рабочая директория: %s", work_dir)

        all_errors = list(context.unfixable_errors) + list(context.current_errors)
        logger.info("  Всего ошибок для финального разрешения: %d", len(all_errors))

        for error in all_errors:
            error_type = error.get("error_type", "")
            if error_type not in self.BLOCKING_TYPES:
                remaining_unfixable.append(error)
                continue

            file_path_str = error.get("file", "")
            if not file_path_str:
                remaining_unfixable.append(error)
                continue
            # +++ ПУТЬ СТРОИТСЯ ОТНОСИТЕЛЬНО РАБОЧЕЙ КОПИИ +++
            file_path = work_dir / file_path_str
            if not file_path.exists():
                remaining_unfixable.append(error)
                continue
            line_num = error.get("line", 0)
            if not isinstance(line_num, int) or line_num <= 0:
                remaining_unfixable.append(error)
                continue

            try:
                lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if line_num - 1 >= len(lines):
                    remaining_unfixable.append(error)
                    continue
                original_line = lines[line_num - 1]
                indent = original_line[: len(original_line) - len(original_line.lstrip())]
                code = error.get("code", "UNKNOWN")
                message = error.get("message", "unknown error")
                replacement = f'{indent}let _ = todo!("[WEBBLES FIX] Manual intervention required: {code} — {message}");\n'

                patch = (
                    f"--- a/{file_path_str}\n"
                    f"+++ b/{file_path_str}\n"
                    f"@@ -{line_num},1 +{line_num},1 @@\n"
                    f"-{original_line}"
                    f"+{replacement}"
                )
                pe = PatchEngine()
                if pe.apply_patch(file_path, patch):
                    logger.info("  Нерешаемая ошибка %s в %s:%d заменена на todo!()", code, file_path_str, line_num)
                    resolved += 1
                else:
                    logger.debug("  Не удалось применить патч todo для %s:%d", file_path_str, line_num)
                    remaining_unfixable.append(error)
            except Exception as e:
                logger.warning("  Ошибка при финальном разрешении %s:%d: %s", file_path_str, line_num, e)
                remaining_unfixable.append(error)

        if remaining_unfixable:
            new_unfixable = []
            for e in remaining_unfixable:
                sig = self._error_signature(e)
                if not any(self._error_signature(x) == sig for x in new_unfixable):
                    new_unfixable.append(e)
            context = context.update(unfixable_errors=new_unfixable)
        else:
            context = context.update(unfixable_errors=[])

        if resolved > 0:
            new_metadata = dict(context.metadata)
            new_metadata["final_resolved"] = resolved
            context = context.update(metadata=new_metadata)
            logger.info("  Финальное разрешение: %d ошибок заменены на todo!()", resolved)
        else:
            logger.info("  Финальное разрешение: нечего заменять")
        return context

    @staticmethod
    def _error_signature(error: Dict[str, Any]) -> str:
        file = error.get("file", "")
        code = error.get("code") or error.get("error_code")
        if code:
            return f"{file}::{code}"
        msg = error.get("message", "")
        normalized = re.sub(r'\d+', '#', msg)
        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = ' '.join(normalized.split())
        return f"{file}::{normalized[:100]}"