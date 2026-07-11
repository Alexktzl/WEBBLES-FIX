"""
Валидатор патчей для Webbles Fix.
Проверяет, что строка похожа на unified diff, без дублирования логики PatchEngine.
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class PatchValidator:
    """
    Проверяет, что патч имеет корректный формат unified diff.
    Не применяет патч и не парсит ханки.
    """

    @staticmethod
    def validate_patch(patch_content: str) -> bool:
        """
        Проверяет, что строка похожа на unified diff.
        Возвращает True, если есть заголовки ---/+++ и хотя бы один ханк.
        """
        if not patch_content or not isinstance(patch_content, str):
            return False

        if not patch_content.strip():
            return False

        has_headers = bool(re.search(r'^--- .+', patch_content, re.MULTILINE)) and \
                      bool(re.search(r'^\+\+\+ .+', patch_content, re.MULTILINE))
        has_hunk = bool(re.search(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@', patch_content, re.MULTILINE))

        return has_headers and has_hunk

    @staticmethod
    def _internal_apply(original_content: str, patch_content: str) -> Optional[str]:
        """
        Применяет unified diff к строке содержимого и возвращает результат.
        Возвращает None, если патч не содержит корректных ханков.
        Ханки применяются по убыванию позиции, чтобы не сбивать индексы строк
        (та же логика, что и в PatchEngine).
        """
        if not isinstance(original_content, str) or not isinstance(patch_content, str):
            return None

        from fixers.patch_engine import PatchEngine
        hunks = PatchEngine._parse_patch_static(patch_content)
        if not hunks:
            return None

        from fixers.patch_lines import body_kind, is_llm_placeholder, strip_body_prefix
        result_lines = original_content.splitlines(keepends=True)
        for old_start, old_count, new_lines in sorted(hunks, key=lambda h: h[0], reverse=True):
            if old_start < 1 or old_start - 1 > len(result_lines):
                return None
            cleaned_new_lines = []
            for line in new_lines:
                kind = body_kind(line)
                # None покрывает и файловые хедеры (+++/---), и пустые строки —
                # они НЕ часть тела ханка и не должны попадать в файл.
                if kind is None:
                    continue
                content = strip_body_prefix(line)
                if kind == '+':
                    if is_llm_placeholder(content):
                        continue
                    cleaned_new_lines.append(content)
                elif kind == ' ':
                    cleaned_new_lines.append(content)
                # '-' — пропускаем (удаляем строку)
            result_lines[old_start - 1: old_start - 1 + old_count] = cleaned_new_lines

        return ''.join(result_lines)