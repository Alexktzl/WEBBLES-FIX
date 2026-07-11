"""
Модуль синтаксического ремонта для Python.
Лечит типичные синтаксические ошибки: пропущенные ':', скобки, отступы.
Добавлены эвристики для дублирующихся определений, отсутствующих ':', несовпадения отступов.
"""

import difflib
import logging
import re
from pathlib import Path
from typing import Dict, Optional

from fixers.syntax_repair import SyntaxRepair

logger = logging.getLogger(__name__)


class PythonSyntaxHealer:
    """Лечит синтаксические ошибки Python без вызова LLM."""

    def heal(self, error: Dict, file_path: Path,
             llm_client=None, invariant_guard=None, segmenter=None) -> Optional[str]:
        code = error.get("code", "")
        msg = error.get("message", "").lower()
        line_num = error.get("line", 0)

        # 1. Пропущенное двоеточие
        #    flake8/Python пишут это сообщение в нескольких вариантах:
        #    «expected ':'», «':' expected», «invalid syntax» с двоеточием в тексте.
        if ("expected ':'" in msg or "':' expected" in msg or "missing colon" in msg
                or (code == "E999" and "':'" in msg)):
            r = self._heal_missing_colon(file_path, line_num)
            if r is not None:
                return r

        # 2. Незакрытые скобки/кавычки
        if "unclosed" in msg or "unterminated" in msg or "eof" in msg or "missing closing quote" in msg:
            if "unterminated string" in msg:
                r = self._heal_unterminated_string(error, file_path)
                if r is not None:
                    return r
            r = self._heal_unclosed_bracket(file_path, line_num)
            if r is not None:
                return r

        # 3. Проблемы с отступами
        #    Покрываем «unexpected indent», «unindent does not match …»,
        #    «expected an indented block», «inconsistent use of tabs and spaces»,
        #    а также flake8-коды E111/E113/E117/W191.
        if ("indentation" in msg or "unexpected indent" in msg
                or "unindent" in msg or "indented block" in msg
                or "tabs and spaces" in msg
                or code in ("E111", "E113", "E114", "E115", "E117", "W191")):
            r = self._heal_indentation(file_path, line_num)
            if r is not None:
                return r

        # 4. Пропущенная скобка в вызове функции
        if "missing parenthesis" in msg or "unexpected eof" in msg:
            return self._heal_missing_parenthesis(file_path, line_num)

        # 5. Отсутствующая '{' – для Python более актуально ':'
        if "expected ':'" in msg:
            return self._heal_missing_colon(file_path, line_num)

        # 6. Дублирующиеся определения
        if "duplicate" in msg:
            return self._heal_duplicate_definition(error, file_path)

        # 7. Несовпадение отступов
        if "different indentation" in msg:
            return self._heal_indentation_mismatch(error, file_path)

        return None

    # -----------------------------------------------------------------
    # Новые эвристики
    # -----------------------------------------------------------------
    def _heal_duplicate_definition(self, error: Dict, file_path: Path) -> Optional[str]:
        """Удаляет дублирующиеся определения функций/классов."""
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
                if f"def {name}" in lines[i] or f"class {name}" in lines[i]:
                    first_def_line = i + 1
                    break
            if first_def_line is None:
                return None

            second_def_line = None
            for i in range(line_num - 1, len(lines)):
                if f"def {name}" in lines[i] or f"class {name}" in lines[i]:
                    second_def_line = i + 1
                    break
            if second_def_line is None or second_def_line == first_def_line:
                return None

            del lines[second_def_line - 1]
            file_path.write_text("".join(lines), encoding="utf-8")
            logger.info("Python: удалено дублирующееся определение '%s'", name)
            return "HEALED"
        except Exception:
            logger.exception("Ошибка в _heal_duplicate_definition")
            return None

    def _heal_indentation_mismatch(self, error: Dict, file_path: Path) -> Optional[str]:
        """Исправляет несовпадение отступов."""
        try:
            content = file_path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            line_num = error.get("line", 0)
            msg = error.get("message", "")
            # Ищем номера строк в сообщении
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
            # В Python отступы для блоков обычно 4 пробела
            correct_indent = ' ' * open_indent
            if close_stripped:
                lines[close_line - 1] = correct_indent + close_stripped
                file_path.write_text("".join(lines), encoding="utf-8")
                logger.info("Python: исправлен отступ в строке %d", close_line)
                return "HEALED"
            return None
        except Exception:
            logger.exception("Ошибка в _heal_indentation_mismatch")
            return None

    # -----------------------------------------------------------------
    # Старые методы (без изменений)
    # -----------------------------------------------------------------
    def _heal_missing_colon(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                return None
            raw = lines[line_num - 1]
            line = raw.rstrip("\n").rstrip()
            stripped = line.lstrip()
            # Открыватели блока. Проверка с «жёстким» разделителем — пробел/`(`
            # /конец строки — иначе `elsewhere = 1` и `tryout()` ошибочно
            # подцепятся через простой `startswith("else")` / `startswith("try")`.
            openers = ("if", "elif", "else", "for", "while", "with",
                       "def", "async def", "class", "try", "except", "finally")
            is_opener = False
            for kw in openers:
                if stripped == kw:
                    is_opener = True
                    break
                if stripped.startswith(kw):
                    nxt = stripped[len(kw): len(kw) + 1]
                    if nxt in (" ", "\t", "(", ":", "#", ""):
                        is_opener = True
                        break
            if not is_opener:
                return None
            if line.rstrip().endswith(":"):
                return None
            # Не трогаем строки с line-continuation (`\`) — они не самостоятельные.
            if line.endswith("\\"):
                return None
            fixed = line + ":" + "\n"
            name = file_path.name
            return (
                f"--- a/{name}\n"
                f"+++ b/{name}\n"
                f"@@ -{line_num},1 +{line_num},1 @@\n"
                f"-{raw}"
                f"+{fixed}"
            )
        except Exception as e:
            logger.debug("Ошибка в _heal_missing_colon: %s", e)
            return None

    def _find_unclosed_quote_on_line(self, line: str) -> Optional[str]:
        """Возвращает символ незакрытой кавычки на строке или None если баланс в порядке."""
        in_sq = False
        in_dq = False
        i = 0
        while i < len(line):
            c = line[i]
            if c == '\\' and i + 1 < len(line):
                i += 2
                continue
            # Тройные кавычки — пропускаем весь блок если он закрыт на этой же строке
            if line[i:i+3] in ("'''", '"""'):
                tq = line[i:i+3]
                j = line.find(tq, i + 3)
                if j != -1:
                    i = j + 3
                    continue
                else:
                    return None  # Незакрытый тройной блок — не наш случай
            if not in_dq and c == "'":
                in_sq = not in_sq
            elif not in_sq and c == '"':
                in_dq = not in_dq
            i += 1
        if in_sq:
            return "'"
        if in_dq:
            return '"'
        return None

    def _heal_unterminated_string(self, error: Dict, file_path: Path) -> Optional[str]:
        """Закрывает незакрытую строковую литеру: добавляет кавычку в конец строки.

        Ищет на строке ошибки и до 2 строк выше: первую строку где кол-во кавычек
        нечётное — туда добавляет закрывающую кавычку.
        """
        try:
            content = file_path.read_text(encoding='utf-8')
            lines = content.splitlines(keepends=True)
            line_num = error.get('line', 0)
            if not line_num or line_num > len(lines):
                return None

            file_str = error.get('file', file_path.name)

            for offset in range(3):
                idx = line_num - 1 - offset
                if idx < 0:
                    break
                stripped = lines[idx].rstrip('\r\n')
                quote_char = self._find_unclosed_quote_on_line(stripped)
                if not quote_char:
                    continue
                eol = '\r\n' if lines[idx].endswith('\r\n') else '\n'
                # Если строка — только голая кавычка, склеиваем со следующей строкой:
                # '   \n  from common...',  →  'from common...',
                if stripped.strip() == quote_char and idx + 1 < len(lines):
                    next_stripped = lines[idx + 1].rstrip('\r\n')
                    leading = len(stripped) - len(stripped.lstrip())
                    merged = ' ' * leading + quote_char + next_stripped.lstrip() + eol
                    new_lines = list(lines)
                    new_lines[idx] = merged
                    del new_lines[idx + 1]
                    logger.info(
                        '  _heal_unterminated_string: склеиваем голую кавычку %r '
                        'на строке %d со следующей строкой', quote_char, idx + 1)
                else:
                    fixed = stripped + quote_char + eol
                    new_lines = list(lines)
                    new_lines[idx] = fixed
                    logger.info('  _heal_unterminated_string: закрываем %r на строке %d', quote_char, idx + 1)
                new_content = ''.join(new_lines)
                patch = list(difflib.unified_diff(
                    lines, new_content.splitlines(keepends=True),
                    fromfile=f'a/{file_str}', tofile=f'b/{file_str}',
                ))
                if patch:
                    return ''.join(patch)
            return None
        except Exception as e:
            logger.debug('_heal_unterminated_string ошибка: %s', e)
            return None

    def heal_multi_uts(self, uts_errors: list, file_path: Path) -> Optional[str]:
        """Fixes multiple unterminated string literals at once using bare-quote patterns.

        Identifies (unclosed_line, bare_quote_line) pairs from the ORIGINAL file
        and applies fixes in reverse-index order to keep positions stable.

        Returns None if any real UTS cannot be fixed with the bare-quote pattern
        (letting the LLM handle complex cases instead).
        """
        try:
            content = file_path.read_text(encoding='utf-8')
            lines = content.splitlines(keepends=True)
            file_str = str(file_path.name)

            uts_line_nums = sorted(
                [e.get('line', 0) for e in uts_errors if e.get('line')]
            )  # Forward order for pair building
            if not uts_line_nums:
                return None

            # Build action list from ORIGINAL file indices.
            # consumed tracks bare-quote indices already claimed by a close+delete pair.
            consumed: set = set()
            # Each action: tuple whose first element is the type string.
            # Types: 'merge' (idx, next_idx, merged_str) or 'close+delete' (idx, next_idx, closed_str)
            #         or 'delete' (idx,)
            actions = []

            for line_num in uts_line_nums:
                idx = line_num - 1
                if idx < 0 or idx >= len(lines) or idx in consumed:
                    continue
                stripped = lines[idx].rstrip('\r\n')
                eol = '\r\n' if lines[idx].endswith('\r\n') else '\n'
                quote_char = self._find_unclosed_quote_on_line(stripped)
                if not quote_char:
                    continue  # False-positive UTS from cascade errors — skip

                if stripped.strip() == quote_char:
                    # This line IS a bare quote
                    if idx + 1 < len(lines):
                        nxt = lines[idx + 1].rstrip('\r\n')
                        nxt_lstripped = nxt.lstrip()
                        if nxt_lstripped.startswith(("'", '"')):
                            # Next line is its own string → bare quote is stray → DELETE
                            actions.append(('delete', idx))
                        else:
                            # Next line is continuation text → MERGE bare quote with it
                            leading = len(stripped) - len(stripped.lstrip())
                            merged = ' ' * leading + quote_char + nxt_lstripped + eol
                            actions.append(('merge', idx, idx + 1, merged))
                            consumed.add(idx + 1)
                else:
                    # Unclosed string: must be followed by a bare-quote line
                    if idx + 1 >= len(lines):
                        logger.debug('heal_multi_uts: строка %d — нет следующей', line_num)
                        return None
                    nxt = lines[idx + 1].rstrip('\r\n')
                    nxt_quote = self._find_unclosed_quote_on_line(nxt)
                    if nxt_quote and nxt.strip() == nxt_quote:
                        # Pattern: close this + delete bare quote next
                        closed = stripped + quote_char + eol
                        actions.append(('close+delete', idx, idx + 1, closed))
                        consumed.add(idx + 1)
                    else:
                        logger.debug(
                            'heal_multi_uts: строка %d без bare-quote → отдаём LLM', line_num)
                        return None

            if not actions:
                return None

            # Apply in reverse order of the "primary" index (highest index first)
            # so earlier modifications don't shift later positions.
            def _sort_key(a):
                # For 'delete': the deleted idx. For others: max(idx, next_idx).
                if a[0] == 'delete':
                    return a[1]
                return max(a[1], a[2])

            new_lines = list(lines)
            for action in sorted(actions, key=_sort_key, reverse=True):
                kind = action[0]
                if kind == 'delete':
                    del new_lines[action[1]]
                    logger.info('  heal_multi_uts: удаляем голую кавычку (строка %d)', action[1] + 1)
                elif kind == 'merge':
                    new_lines[action[1]] = action[3]
                    del new_lines[action[2]]
                    logger.info('  heal_multi_uts: merge строка %d', action[1] + 1)
                elif kind == 'close+delete':
                    new_lines[action[1]] = action[3]
                    del new_lines[action[2]]
                    logger.info(
                        '  heal_multi_uts: закрываем строку %d, удаляем %d',
                        action[1] + 1, action[2] + 1)

            new_content = ''.join(new_lines)
            patch = list(difflib.unified_diff(
                lines, new_content.splitlines(keepends=True),
                fromfile=f'a/{file_str}', tofile=f'b/{file_str}',
            ))
            if patch:
                return ''.join(patch)
            return None
        except Exception as e:
            logger.debug('heal_multi_uts ошибка: %s', e)
            return None

    def _heal_unclosed_bracket(self, file_path: Path, line_num: int) -> Optional[str]:
        try:
            content = file_path.read_text(encoding="utf-8")
            open_paren = content.count('(') - content.count(')')
            open_bracket = content.count('[') - content.count(']')
            open_brace = content.count('{') - content.count('}')
            if open_paren == 0 and open_bracket == 0 and open_brace == 0:
                return None
            lines = content.splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                line_num = len(lines)
            fixes = ')' * open_paren + ']' * open_bracket + '}' * open_brace
            if fixes:
                if line_num <= len(lines):
                    lines.insert(line_num, fixes + '\n')
                else:
                    lines.append(fixes + '\n')
                file_path.write_text("".join(lines), encoding="utf-8")
                logger.info("Добавлены недостающие скобки: %s", fixes)
                return "HEALED"
            return None
        except Exception as e:
            logger.debug("Ошибка в _heal_unclosed_bracket: %s", e)
            return None

    def _heal_indentation(self, file_path: Path, line_num: int) -> Optional[str]:
        """Чинит типовые IndentationError.

        Покрывает три случая (детерминированные, без LLM):
        1) Строка после `:` пришла без отступа → добавляем 4 пробела.
        2) Отступ строки не кратен 4 / меньше, чем у соседей того же блока →
           выравниваем по соседней непустой строке выше или по `prev_indent+4`
           если та строка — открыватель блока (заканчивается на `:`).
        3) Tab/space смесь — нормализуем в пробелы.
        """
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not (1 <= line_num <= len(lines)):
                return None
            cur_line = lines[line_num - 1]
            cur_stripped = cur_line.lstrip(" \t")
            if not cur_stripped.strip():
                return None  # пустая строка — нечего чинить
            cur_indent = len(cur_line) - len(cur_stripped)

            # Ищем выше первую непустую строку — она диктует ожидаемый отступ.
            expected = None
            for i in range(line_num - 2, -1, -1):
                prev = lines[i]
                if not prev.strip():
                    continue
                prev_indent = len(prev) - len(prev.lstrip(" \t"))
                if prev.rstrip().endswith(":"):
                    # Тело блока — на один уровень глубже.
                    expected = prev_indent + 4
                else:
                    # Тот же блок — повторяем отступ соседа.
                    expected = prev_indent
                break
            if expected is None:
                return None
            # Если отступ уже правильный — но содержит tab/смесь — всё равно
            # стоит перенормализовать.
            has_tab = "\t" in cur_line[: max(cur_indent, 1)]
            if expected == cur_indent and not has_tab:
                return None
            fixed_line = " " * expected + cur_stripped
            if fixed_line == cur_line:
                return None
            return self._build_one_line_diff(file_path, line_num, cur_line, fixed_line)
        except Exception as e:
            logger.debug("Ошибка в _heal_indentation: %s", e)
            return None

    @staticmethod
    def _build_one_line_diff(file_path: Path, line_num: int,
                             old_line: str, new_line: str) -> str:
        """Единый unified diff на одну строку. Преффиксы a/ b/ + единый @@ хедер."""
        name = file_path.name
        # Гарантируем '\n' в конце для apply.
        if not old_line.endswith("\n"):
            old_line = old_line + "\n"
        if not new_line.endswith("\n"):
            new_line = new_line + "\n"
        return (
            f"--- a/{name}\n"
            f"+++ b/{name}\n"
            f"@@ -{line_num},1 +{line_num},1 @@\n"
            f"-{old_line}"
            f"+{new_line}"
        )

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