"""
Семантическое восстановление патчей для webles_conveyor.
Ремонтирует плохо отформатированные LLM патчи через нечёткое сопоставление контекста.
Теперь также очищает патч от синтаксических огрехов (слипшиеся строки и т.п.) перед применением.
Все этапы восстановления покрыты DEBUG-логами.
"""

import difflib
import logging
import re
from typing import List, Optional, Tuple

from fixers.patch_lines import HUNK_HEADER_RE

logger = logging.getLogger(__name__)


class SemanticPatchRepair:
    """
    Пытается восстановить и применить патчи, которые не проходят из-за мелких несовпадений контекста.
    Использует нечёткое сопоставление строк для поиска правильной позиции каждого hunk'а.
    """

    def __init__(self, context_window: int = 10, similarity_threshold: float = 0.8):
        self.context_window = context_window
        self.similarity_threshold = similarity_threshold
        logger.debug("SemanticPatchRepair инициализирован: context_window=%d, threshold=%.2f",
                     context_window, similarity_threshold)

    def repair_patch(self, original_content: str, patch_content: str) -> Optional[str]:
        """Пытается восстановить патч и вернуть исправленную версию."""
        logger.debug("repair_patch: начало восстановления, размер оригинала=%d, размер патча=%d",
                     len(original_content), len(patch_content) if isinstance(patch_content, str) else 0)

        if not isinstance(patch_content, str):
            logger.error("repair_patch: patch_content должен быть str, получен %s", type(patch_content).__name__)
            return None

        if not patch_content.strip():
            logger.debug("repair_patch: патч пустой")
            return None

        original_lines = original_content.splitlines(keepends=True)
        patch_lines = patch_content.splitlines(keepends=True)
        patch_lines = [str(line) for line in patch_lines if line is not None]
        logger.debug("repair_patch: оригинал %d строк, патч %d строк", len(original_lines), len(patch_lines))

        hunks = self._extract_hunks(patch_lines)
        if not hunks:
            logger.debug("repair_patch: ханки не найдены")
            return None
        logger.debug("repair_patch: найдено ханков: %d", len(hunks))

        repaired_hunks = []
        for start, hunk_lines in hunks:
            if not hunk_lines or not isinstance(hunk_lines[0], str) or not hunk_lines[0].startswith("@@"):
                logger.warning("Пропущен некорректный hunk (заголовок отсутствует или невалидный)")
                continue
            logger.debug("repair_patch: восстановление ханка на позиции %d", start)
            repaired = self._repair_hunk(original_lines, hunk_lines)
            if repaired is None:
                logger.debug("repair_patch: не удалось восстановить ханк")
                return None
            repaired_hunks.append(repaired)

        result = self._reconstruct_patch(patch_lines, repaired_hunks)
        logger.debug("repair_patch: патч восстановлен, размер=%d", len(result) if result else 0)
        return result

    def _extract_hunks(self, patch_lines: List[str]) -> List[Tuple[int, List[str]]]:
        hunks = []
        i = 0
        logger.debug("_extract_hunks: разбор %d строк", len(patch_lines))
        while i < len(patch_lines):
            line = patch_lines[i]
            if not isinstance(line, str):
                i += 1
                continue
            if line.startswith("@@"):
                start = i
                i += 1
                while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                    i += 1
                hunk_slice = patch_lines[start:i]
                if hunk_slice and isinstance(hunk_slice[0], str):
                    hunks.append((start, hunk_slice))
                    logger.debug("_extract_hunks: добавлен ханк из %d строк", len(hunk_slice))
            else:
                i += 1
        logger.debug("_extract_hunks: всего ханков=%d", len(hunks))
        return hunks

    def _repair_hunk(self, original_lines: List[str], hunk_lines: List[str]) -> Optional[List[str]]:
        if not hunk_lines:
            return None
        header = hunk_lines[0]
        if not isinstance(header, str):
            logger.warning("Заголовок hunk'а имеет тип %s", type(header).__name__)
            header = str(header)

        logger.debug("_repair_hunk: заголовок=%s", header.strip())
        body = hunk_lines[1:]

        m = HUNK_HEADER_RE.match(header)
        if not m:
            logger.debug("_repair_hunk: заголовок не соответствует формату unified diff")
            return None
        orig_start = int(m.group(1))
        orig_len = int(m.group(2)) if m.group(2) else 1
        logger.debug("_repair_hunk: исходный старт=%d, длина=%d", orig_start, orig_len)

        context_lines = []
        for line in body:
            if not isinstance(line, str):
                line = str(line) if line is not None else ''
            if line.startswith(' '):
                context_lines.append(line[1:].rstrip('\n'))
            elif line.startswith('\n'):
                context_lines.append('')

        if not context_lines:
            logger.debug("_repair_hunk: контекстных строк нет, переход к восстановлению добавления")
            return self._repair_addition_hunk(original_lines, orig_start, header, body)

        logger.debug("_repair_hunk: контекстных строк=%d, запуск нечёткого поиска", len(context_lines))
        best_match_start = self._fuzzy_find_context(original_lines, context_lines, orig_start - 1)
        if best_match_start is None:
            logger.debug("_repair_hunk: нечёткий поиск не дал результата")
            return None

        new_orig_start = best_match_start + 1
        logger.debug("_repair_hunk: новый старт=%d", new_orig_start)
        new_hunk_lines = [f"@@ -{new_orig_start},{len(context_lines)} +{new_orig_start},{len(body)} @@\n"]
        new_hunk_lines.extend(body)
        return new_hunk_lines

    def _repair_addition_hunk(
        self,
        original_lines: List[str],
        orig_start: int,
        header: str,
        body: List[str]
    ) -> Optional[List[str]]:
        max_line = len(original_lines)
        if orig_start > max_line:
            orig_start = max_line
            logger.debug("_repair_addition_hunk: orig_start скорректирован до %d", orig_start)
        new_header = f"@@ -{orig_start},0 +{orig_start},{len(body)} @@\n"
        logger.debug("_repair_addition_hunk: новый заголовок=%s", new_header.strip())
        return [new_header] + body

    def _fuzzy_find_context(
        self,
        original_lines: List[str],
        context_lines: List[str],
        hint_start: int
    ) -> Optional[int]:
        logger.debug("_fuzzy_find_context: hint_start=%d", hint_start)
        if not context_lines:
            return hint_start

        orig_normalized = [line.rstrip('\n') for line in original_lines]
        ctx_normalized = [line.rstrip('\n') for line in context_lines]

        search_start = max(0, hint_start - self.context_window)
        search_end = min(len(orig_normalized) - len(ctx_normalized) + 1,
                        hint_start + self.context_window)
        logger.debug("_fuzzy_find_context: поиск от %d до %d", search_start, search_end)

        best_ratio = 0.0
        best_pos = None

        for pos in range(search_start, search_end):
            candidate = orig_normalized[pos:pos + len(ctx_normalized)]
            ratio = difflib.SequenceMatcher(None, candidate, ctx_normalized).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_pos = pos

        if best_ratio >= self.similarity_threshold:
            logger.debug("_fuzzy_find_context: найдено совпадение на позиции %d, рейтинг=%.2f", best_pos, best_ratio)
            return best_pos
        logger.debug("_fuzzy_find_context: совпадение не найдено, лучший рейтинг=%.2f", best_ratio)
        return None

    def _reconstruct_patch(self, original_patch_lines: List[str], repaired_hunks: List[List[str]]) -> str:
        result = []
        hunk_idx = 0
        i = 0
        while i < len(original_patch_lines):
            line = original_patch_lines[i]
            if isinstance(line, str) and line.startswith("@@"):
                if hunk_idx < len(repaired_hunks):
                    result.extend(repaired_hunks[hunk_idx])
                    logger.debug("_reconstruct_patch: вставлен восстановленный ханк #%d", hunk_idx)
                    hunk_idx += 1
                i += 1
                while i < len(original_patch_lines) and not original_patch_lines[i].startswith("@@"):
                    i += 1
            else:
                if isinstance(line, str):
                    result.append(line)
                i += 1
        logger.debug("_reconstruct_patch: итоговый патч %d строк", len(result))
        return ''.join(result)


def apply_patch_with_repair(original_content: str, patch_content: str, language: str = "unknown") -> Optional[str]:
    """Пытается применить патч, при необходимости восстанавливая его."""
    logger.debug("apply_patch_with_repair: язык=%s, размер оригинала=%d, размер патча=%d",
                 language, len(original_content), len(patch_content) if isinstance(patch_content, str) else 0)

    from fixers.patch_validator import PatchValidator
    from fixers.syntax_repair import SyntaxRepair

    if not isinstance(patch_content, str):
        logger.error("apply_patch_with_repair: patch_content должен быть str, получен %s", type(patch_content).__name__)
        return None

    # Очистка патча от синтаксических огрехов
    try:
        patch_content = SyntaxRepair.repair_diff(patch_content, language)
        logger.debug("apply_patch_with_repair: патч очищен через SyntaxRepair.repair_diff")
    except Exception as e:
        logger.warning("Не удалось очистить патч: %s", e)

    # Прямое применение
    logger.debug("apply_patch_with_repair: попытка прямого применения")
    patched = PatchValidator._internal_apply(str(original_content), patch_content)
    if patched is not None:
        logger.debug("apply_patch_with_repair: прямое применение успешно")
        return patched

    # Семантическое восстановление
    logger.debug("apply_patch_with_repair: прямое применение провалилось, запуск семантического восстановления")
    repairer = SemanticPatchRepair()
    repaired_patch = repairer.repair_patch(str(original_content), patch_content)
    if repaired_patch is None:
        logger.debug("apply_patch_with_repair: семантическое восстановление не удалось")
        return None

    if not isinstance(repaired_patch, str):
        logger.error("apply_patch_with_repair: восстановленный патч имеет тип %s", type(repaired_patch).__name__)
        return None

    logger.debug("apply_patch_with_repair: попытка применения восстановленного патча")
    return PatchValidator._internal_apply(str(original_content), repaired_patch)