"""
Универсальный нормализатор и санитайзер патчей для Webbles Fix.
Очищает патчи от дубликатов строк, «шумовых» ханков, пустых замен,
корректирует заголовки ханков и форматирует изменённые строки через rustfmt.
Добавлена защита от дублирования существующих строк.
Все ключевые операции логируются.
"""

import difflib
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Единый словарь для unified-diff: используем общую регулярку, а не свою копию.
from fixers.patch_lines import HUNK_HEADER_RE, is_file_header

logger = logging.getLogger(__name__)
_COMMENT_PATTERN = re.compile(r'^\s*//|^\s*\*|^\s*/\*')

def _line_changes_logic(line: str) -> bool:
    """True, если строка добавляет/удаляет не только пробелы/комментарии."""
    if not line or line[0] not in ('+', '-'):
        return False
    content = line[1:].strip()
    return bool(content) and not _COMMENT_PATTERN.match(content)


def _has_logical_changes(hunk_lines: List[str]) -> bool:
    return any(_line_changes_logic(l) for l in hunk_lines)


class PatchSanitizer:
    """Нормализует и очищает unified diff с учётом целевой ошибки."""

    def __init__(self, target_error: Optional[Dict[str, Any]] = None):
        self.target_error = target_error or {}
        self.target_file = self.target_error.get("file", "unknown")
        self.target_lines: Set[int] = self._target_lines()

    def _target_lines(self) -> Set[int]:
        lines = set()
        err = self.target_error
        if not err:
            return lines
        line = err.get("line")
        if isinstance(line, int):
            lines.add(line)
            for offset in (-2, -1, 1, 2):
                lines.add(line + offset)
        span = err.get("span")
        if isinstance(span, (list, tuple)) and len(span) >= 2:
            for l in range(span[0], span[1] + 1):
                lines.add(l)
        return {l for l in lines if l > 0}

    def is_relevant_hunk(self, hunk_lines: List[str],
                         header: Optional[str] = None) -> bool:
        """Релевантен ли ханк целевой ошибке.

        Базовое условие — наличие логических изменений (не только пробелы/комментарии).
        Если известен набор целевых строк и передан заголовок ханка, дополнительно
        требуем, чтобы старый диапазон ханка пересекался с целевыми строками.
        """
        if not _has_logical_changes(hunk_lines):
            return False
        if not self.target_lines or header is None:
            return True
        m = HUNK_HEADER_RE.match(header)
        if not m:
            # Заголовок невалидный — не штрафуем, оставляем как при отсутствии заголовка
            return True
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) else 1
        old_end = old_start + max(0, old_count - 1)
        return any(old_start <= ln <= old_end for ln in self.target_lines)

    def sanitize(self, patch_text: str, file_name: str = "unknown",
                 original_content: Optional[str] = None) -> Optional[str]:
        """
        Очищает патч. Если задан original_content, удаляет из патча строки,
        которые уже присутствуют в исходном файле (дубликаты).
        """
        if not patch_text or not isinstance(patch_text, str):
            return None

        lines = patch_text.splitlines(keepends=True)
        result: List[str] = []
        current_header: Optional[str] = None
        hunk_lines: List[str] = []

        # Предварительно разобьём оригинал на множество "чистых" строк (без отступов)
        orig_stripped_lines = set()
        if original_content:
            orig_stripped_lines = {line.strip() for line in original_content.splitlines()}

        def flush():
            nonlocal current_header, hunk_lines
            if not hunk_lines:
                return
            # Удаление подряд идущих дубликатов
            unique = []
            prev = None
            for line in hunk_lines:
                if line == prev:
                    continue
                unique.append(line)
                prev = line

            # Удаление "пустых" замен (когда удалённая и добавленная строки совпадают)
            filtered = []
            skip_next = False
            for i, line in enumerate(unique):
                if skip_next:
                    skip_next = False
                    continue
                if line.startswith('-') and i + 1 < len(unique) and unique[i+1].startswith('+'):
                    if line[1:] == unique[i+1][1:]:
                        # Пустая замена – пропускаем обе строки
                        skip_next = True
                        continue
                filtered.append(line)
            unique = filtered

            # Удаление добавлений, которые уже есть в оригинале (борьба с дубликатами)
            if orig_stripped_lines:
                deduped = []
                for line in unique:
                    if line.startswith('+') and not line.startswith('+++'):
                        stripped = line[1:].strip()
                        if stripped in orig_stripped_lines:
                            logger.debug("Удалён дубликат строки из патча: %s", stripped)
                            continue
                    deduped.append(line)
                unique = deduped

            removed = [l[1:] for l in unique if l.startswith('-')]
            added   = [l[1:] for l in unique if l.startswith('+')]

            if not removed and not added:
                logger.debug("Ханк %s: после фильтрации пуст, отброшен", file_name)
                current_header = None
                hunk_lines = []
                return

            if not self.is_relevant_hunk(unique, current_header):
                logger.debug("Ханк %s: нерелевантен (нет логических изменений или вне целевых строк), отброшен", file_name)
                current_header = None
                hunk_lines = []
                return

            if current_header:
                result.append(current_header)
            result.extend(unique)
            logger.debug("Ханк %s сохранён: -%d +%d", file_name, len(removed), len(added))
            current_header = None
            hunk_lines = []

        for line in lines:
            if line.startswith('@@'):
                flush()
                if HUNK_HEADER_RE.match(line):
                    current_header = line
                else:
                    current_header = None
                continue

            if current_header and line and line[0] in (' ', '+', '-'):
                # +++ b/path / --- a/path внутри тела ханка — это не контент,
                # а файловые хедеры (LLM иногда суёт их перед своими '+'/'-').
                # Раньше они утекали в выход санитайзера как тело ханка.
                if is_file_header(line):
                    continue
                hunk_lines.append(line)
            else:
                flush()
                if line.startswith(('---', '+++')):
                    result.append(line)
                else:
                    logger.debug("sanitize: строка вне ханка проигнорирована: %s", line.strip())
        flush()

        if not any(l.startswith(('+', '-')) and not l.startswith(('+++', '---')) for l in result):
            logger.info("Патч %s не содержит изменений после санитайзера", file_name)
            return None

        normalized = ''.join(result)
        if not re.search(r'^@@ ', normalized, re.MULTILINE):
            logger.warning("Патч %s не содержит валидного ханка", file_name)
            return None
        return normalized

    @staticmethod
    def format_patch_with_rustfmt(patch_text: str, file_path: Path, language: str) -> Optional[str]:
        if language not in ("rust", "rs"):
            return patch_text
        try:
            original = file_path.read_text(encoding="utf-8")
            with tempfile.NamedTemporaryFile(mode='w', suffix='.rs', delete=False, encoding='utf-8') as tmp:
                tmp.write(original)
                tmp_path = Path(tmp.name)
            from fixers.patch_engine import PatchEngine
            pe = PatchEngine()
            if not pe.apply_patch(tmp_path, patch_text):
                tmp_path.unlink(missing_ok=True)
                return patch_text
            subprocess.run(["rustfmt", "--edition", "2021", str(tmp_path)], check=False, capture_output=True)
            formatted = tmp_path.read_text(encoding="utf-8")
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                formatted.splitlines(keepends=True),
                fromfile=str(file_path),
                tofile=str(file_path),
            )
            new_patch = ''.join(diff)
            tmp_path.unlink(missing_ok=True)
            return new_patch if new_patch else patch_text
        except Exception as e:
            logger.warning("format_patch_with_rustfmt: ошибка %s", e)
            return patch_text


def normalize_patch(patch_text: str, file_name: str = "unknown",
                   target_error: Optional[Dict[str, Any]] = None,
                   original_content: Optional[str] = None) -> Optional[str]:
    sanitizer = PatchSanitizer(target_error)
    return sanitizer.sanitize(patch_text, file_name, original_content)