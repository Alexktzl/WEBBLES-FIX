"""
Модуль синтаксического ремонта для C++.
Лечит частые синтаксические ошибки: пропущенные ';', скобки, директивы #include.
Добавлены эвристики для дублирующихся определений, отсутствующих '{', несовпадения отступов.
"""

import logging
import re
from pathlib import Path
from typing import Dict, Optional

from fixers.syntax_repair import SyntaxRepair

logger = logging.getLogger(__name__)


class CppSyntaxHealer:
    """Лечит синтаксические ошибки C++ без вызова LLM."""

    def heal(self, error: Dict, file_path: Path,
             llm_client=None, invariant_guard=None, segmenter=None) -> Optional[str]:
        code = error.get("code", "")
        msg = error.get("message", "").lower()
        line_num = error.get("line", 0)

        # C2143 / expected ';'
        if "expected ';'" in msg or "c2143" in msg:
            return self._heal_missing_semicolon(file_path, line_num)

        # expected '}'
        if "expected '}'" in msg or "c2059" in msg and "}" in msg:
            return self._heal_missing_close_brace(file_path, line_num)

        # expected '{'
        if "expected '{'" in msg:
            return self._heal_missing_open_brace(file_path, line_num)

        # expected '(' or ')'
        if "expected '('" in msg:
            return self._heal_missing_open_paren(file_path, line_num)
        if "expected ')'" in msg:
            return self._heal_missing_close_paren(file_path, line_num)

        # Незакрытые скобки
        if "unclosed" in msg or "unterminated" in msg:
            return self._heal_unclosed_delimiter(file_path, line_num)

        # Дублирующиеся определения (C2084)
        if "duplicate" in msg or code == "C2084":
            return self._heal_duplicate_definition(error, file_path)

        # Несовпадение отступов
        if "different indentation" in msg:
            return self._heal_indentation_mismatch(error, file_path)

        # C1083: не найден заголовочный файл
        if "c1083" in msg or "cannot open include file" in msg:
            return self._heal_missing_include(file_path, line_num, error)

        return None

    # -----------------------------------------------------------------
    # Новые эвристики
    # -----------------------------------------------------------------
    def _heal_missing_open_brace(self, file_path: Path, line_num: int) -> Optional[str]:
        """Добавляет '{' после сигнатуры функции/блока."""
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                return None
            line = lines[line_num - 1].rstrip('\n')
            if line.rstrip().endswith('{'):
                return None
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in ('if ', 'while ', 'for ', 'switch ', 'catch ', 'else')):
                if not stripped.endswith('{'):
                    lines[line_num - 1] = line + ' {\n'
                    file_path.write_text("".join(lines), encoding="utf-8")
                    logger.info("C++: добавлена недостающая '{' в строку %d", line_num)
                    return "HEALED"
            return None
        except Exception:
            logger.exception("Ошибка в _heal_missing_open_brace")
            return None

    def _heal_duplicate_definition(self, error: Dict, file_path: Path) -> Optional[str]:
        """Удаляет дублирующиеся определения функций/классов."""
        try:
            content = file_path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            line_num = error.get("line", 0)
            msg = error.get("message", "")
            # Пытаемся извлечь имя из сообщения
            name_match = re.search(r"'(.*?)'", msg)
            if not name_match:
                return None
            name = name_match.group(1)
            # Ищем первое определение до строки ошибки
            first_def_line = None
            for i in range(line_num - 2, -1, -1):
                if name in lines[i]:
                    first_def_line = i + 1
                    break
            if first_def_line is None:
                return None
            # Ищем второе определение начиная с указанной строки
            second_def_line = None
            for i in range(line_num - 1, len(lines)):
                if name in lines[i]:
                    second_def_line = i + 1
                    break
            if second_def_line is None or second_def_line == first_def_line:
                return None
            # Удаляем второе определение (обычно короче или более позднее)
            del lines[second_def_line - 1]
            file_path.write_text("".join(lines), encoding="utf-8")
            logger.info("C++: удалено дублирующееся определение '%s'", name)
            return "HEALED"
        except Exception:
            logger.exception("Ошибка в _heal_duplicate_definition")
            return None

    def _heal_indentation_mismatch(self, error: Dict, file_path: Path) -> Optional[str]:
        """Исправляет несовпадение отступов."""
        try:
            content = file_path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            msg = error.get("message", "")
            # Ищем номера строк в сообщении (аналогично Rust)
            line_matches = re.findall(r'(\d+)\s*\|', msg)
            if len(line_matches) < 2:
                return None
            try:
                open_line = int(line_matches[0])
                close_line = int(line_matches[1])
            except (IndexError, ValueError):
                return None
            if not (1 <= open_line <= len(lines) and 1 <= close_line <= len(lines)):
                return None
            open_indent = len(lines[open_line - 1]) - len(lines[open_line - 1].lstrip())
            close_stripped = lines[close_line - 1].lstrip()
            if close_stripped.startswith('}'):
                lines[close_line - 1] = ' ' * open_indent + close_stripped
                file_path.write_text("".join(lines), encoding="utf-8")
                logger.info("C++: исправлен отступ закрывающей скобки в строке %d", close_line)
                return "HEALED"
            return None
        except Exception:
            logger.exception("Ошибка в _heal_indentation_mismatch")
            return None

    # -----------------------------------------------------------------
    # Старые методы (без изменений в сигнатуре, оставлены как были)
    # -----------------------------------------------------------------
    def _heal_missing_semicolon(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                return None
            line = lines[line_num - 1].rstrip('\n').rstrip()
            if line and not line.endswith((';', '{', '}', '(', ')', ',', ':', '//', '/*')):
                fixed = line + ';\n'
                file_name = file_path.name
                return (
                    f"--- a/{file_name}\n"
                    f"+++ b/{file_name}\n"
                    f"@@ -{line_num},1 +{line_num},1 @@\n"
                    f"-{lines[line_num - 1]}"
                    f"+{fixed}"
                )
            return None
        except Exception as e:
            logger.debug("Ошибка в _heal_missing_semicolon: %s", e)
            return None

    def _heal_missing_close_brace(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if 1 <= line_num <= len(lines):
                lines.insert(line_num, "}\n")
            else:
                lines.append("}\n")
            file_path.write_text("".join(lines), encoding="utf-8")
            logger.info("Добавлена недостающая '}' после строки %d", line_num)
            return "HEALED"
        except Exception as e:
            logger.debug("Ошибка в _heal_missing_close_brace: %s", e)
            return None

    def _heal_missing_open_paren(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                return None
            line = lines[line_num - 1].rstrip('\n').rstrip()
            fixed = re.sub(r'\b(if|while|for|switch|catch)\b\s*', r'\1 (', line)
            if fixed != line:
                fixed += '\n'
                file_name = file_path.name
                return (
                    f"--- a/{file_name}\n"
                    f"+++ b/{file_name}\n"
                    f"@@ -{line_num},1 +{line_num},1 @@\n"
                    f"-{lines[line_num - 1]}"
                    f"+{fixed}"
                )
            return None
        except Exception as e:
            logger.debug("Ошибка в _heal_missing_open_paren: %s", e)
            return None

    def _heal_missing_close_paren(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                return None
            line = lines[line_num - 1].rstrip('\n').rstrip()
            if '(' in line and ')' not in line:
                fixed = line + ')' + '\n'
                file_name = file_path.name
                return (
                    f"--- a/{file_name}\n"
                    f"+++ b/{file_name}\n"
                    f"@@ -{line_num},1 +{line_num},1 @@\n"
                    f"-{lines[line_num - 1]}"
                    f"+{fixed}"
                )
            return None
        except Exception as e:
            logger.debug("Ошибка в _heal_missing_close_paren: %s", e)
            return None

    def _heal_unclosed_delimiter(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            content = file_path.read_text(encoding="utf-8")
            paren_diff = content.count('(') - content.count(')')
            brace_diff = content.count('{') - content.count('}')
            bracket_diff = content.count('[') - content.count(']')
            if paren_diff == 0 and brace_diff == 0 and bracket_diff == 0:
                return None
            lines = content.splitlines(keepends=True)
            fixes = ')' * paren_diff + '}' * brace_diff + ']' * bracket_diff
            if fixes:
                if 1 <= line_num <= len(lines):
                    lines.insert(line_num, fixes + '\n')
                else:
                    lines.append(fixes + '\n')
                file_path.write_text("".join(lines), encoding="utf-8")
                logger.info("Добавлены недостающие скобки: %s", fixes)
                return "HEALED"
            return None
        except Exception as e:
            logger.debug("Ошибка в _heal_unclosed_delimiter: %s", e)
            return None

    def _heal_missing_include(self, file_path: Path, line_num: int, error: Dict) -> Optional[str]:
        try:
            msg = error.get("message", "")
            match = re.search(r"['\"<]([^'\">]+)['\">]", msg)
            if not match:
                return None
            header = match.group(1)
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            include_line = f'#include "{header}"\n' if not header.startswith('<') else f'#include {header}\n'
            last_include_idx = -1
            for i, line in enumerate(lines):
                if line.strip().startswith('#include'):
                    last_include_idx = i
            if last_include_idx >= 0:
                lines.insert(last_include_idx + 1, include_line)
            else:
                lines.insert(0, include_line)
            file_path.write_text("".join(lines), encoding="utf-8")
            logger.info("Добавлен #include %s", header)
            return "HEALED"
        except Exception as e:
            logger.debug("Ошибка в _heal_missing_include: %s", e)
            return None