"""
Модуль синтаксического ремонта для JavaScript и TypeScript.
Лечит самые частые синтаксические ошибки: пропущенные ';', скобки, кавычки.
Добавлены эвристики для дублирующихся определений, отсутствующих '{', несовпадения отступов.
"""

import logging
import re
from pathlib import Path
from typing import Dict, Optional

from fixers.syntax_repair import SyntaxRepair

logger = logging.getLogger(__name__)


class JavaScriptSyntaxHealer:
    """Лечит синтаксические ошибки JS/TS без вызова LLM."""

    def heal(self, error: Dict, file_path: Path,
             llm_client=None, invariant_guard=None, segmenter=None) -> Optional[str]:
        code = error.get("code", "")
        msg = error.get("message", "").lower()
        line_num = error.get("line", 0)

        # 1. Пропущенная точка с запятой
        if "missing semicolon" in msg or "expected ';'" in msg or "semi" in msg or code == "TS1005":
            return self._heal_missing_semicolon(file_path, line_num)

        # 2. Незакрытые скобки/кавычки
        if "unclosed" in msg or "unterminated" in msg or "unexpected end" in msg:
            return self._heal_unclosed_delimiter(file_path, line_num)

        # 3. Лишняя или отсутствующая скобка
        if "unexpected token" in msg and (")" in msg or "}" in msg or "]" in msg):
            return self._heal_extra_delimiter(file_path, line_num)

        # 4. Пропущенная закрывающая скобка в вызове функции
        if "expected ')'" in msg or "expected '('" in msg:
            return self._heal_missing_parenthesis(file_path, line_num)

        # 5. Отсутствующая '{'
        if "expected '{'" in msg:
            return self._heal_missing_open_brace(file_path, line_num)

        # 6. Дублирующиеся определения
        if "duplicate" in msg or code == "TS2300":
            return self._heal_duplicate_definition(error, file_path)

        # 7. Несовпадение отступов
        if "different indentation" in msg:
            return self._heal_indentation_mismatch(error, file_path)

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
            if any(stripped.startswith(kw) for kw in ('if ', 'else ', 'while ', 'for ', 'switch ', 'catch ', 'do ', 'try ')):
                if not stripped.endswith('{'):
                    lines[line_num - 1] = line + ' {\n'
                    file_path.write_text("".join(lines), encoding="utf-8")
                    logger.info("JS/TS: добавлена недостающая '{' в строку %d", line_num)
                    return "HEALED"
            return None
        except Exception:
            logger.exception("Ошибка в _heal_missing_open_brace")
            return None

    def _heal_duplicate_definition(self, error: Dict, file_path: Path) -> Optional[str]:
        """Удаляет дублирующиеся определения."""
        try:
            content = file_path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            line_num = error.get("line", 0)
            msg = error.get("message", "")
            name_match = re.search(r"'(.*?)'", msg)
            if not name_match:
                return None
            name = name_match.group(1)

            first_def_line = None
            for i in range(line_num - 2, -1, -1):
                if name in lines[i]:
                    first_def_line = i + 1
                    break
            if first_def_line is None:
                return None

            second_def_line = None
            for i in range(line_num - 1, len(lines)):
                if name in lines[i]:
                    second_def_line = i + 1
                    break
            if second_def_line is None or second_def_line == first_def_line:
                return None

            del lines[second_def_line - 1]
            file_path.write_text("".join(lines), encoding="utf-8")
            logger.info("JS/TS: удалено дублирующееся определение '%s'", name)
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
                logger.info("JS/TS: исправлен отступ закрывающей скобки в строке %d", close_line)
                return "HEALED"
            return None
        except Exception:
            logger.exception("Ошибка в _heal_indentation_mismatch")
            return None

    # -----------------------------------------------------------------
    # Старые методы (без изменений)
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

    def _heal_extra_delimiter(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                return None
            line = lines[line_num - 1]
            for delim in (')', '}', ']'):
                if delim in line:
                    new_line = line.replace(delim, '', 1)
                    if new_line != line:
                        lines[line_num - 1] = new_line
                        file_path.write_text("".join(lines), encoding="utf-8")
                        logger.info("Удалена лишняя скобка '%s' на строке %d", delim, line_num)
                        return "HEALED"
            return None
        except Exception as e:
            logger.debug("Ошибка в _heal_extra_delimiter: %s", e)
            return None

    def _heal_missing_parenthesis(self, file_path: Path, line_num: int) -> Optional[str]:
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
            logger.debug("Ошибка в _heal_missing_parenthesis: %s", e)
            return None