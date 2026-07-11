"""
Stage E — RuleBasedFixer.

Детерминированные правила для простых ошибок (CLEANUP/WARNING), которые
НЕ требуют LLM. Возвращает готовый `EditSet` (B.1), который дальше
проходит тот же путь, что и structured-LLM-патч: `to_unified_diff` →
apply → VALIDATE → REVIEW → DECIDE.

Принципы:
- **Только безопасные правила.** Если правило не уверено (строка не
  соответствует ожиданию, символ не извлекается) — возвращает None,
  и управление уходит к LLM-пути. Лучше не починить, чем испортить.
- **Точечность.** Каждое правило трогает ровно одну строку.
- **confidence фиксированный 0.95** — это детерминированная правка,
  не догадка (см. core.contract.default_confidence_for: rule-based → 0.95).

Покрытие (соответствует кодам из бенчмарка tests/benchmark):
  Rust:   unused_import, unused_variable, unused_mut,
          clippy::needless_return, clippy::redundant_clone,
          clippy::clone_on_copy
  Python: F401, W291, unsafe_deserialization (yaml.load→safe_load, K.7)
  JS:     eqeqeq, no-var, prefer-const, no-unused-vars, xss (innerHTML→textContent, K.7)

Security (Stage K.7): чиним ТОЛЬКО тривиальные однострочные дыры
(yaml.load→safe_load, innerHTML→textContent). Нетривиальное
(hardcoded_secret, eval, sql_injection, command_injection, pickle) НЕ имеет
правила → try_fix вернёт None → находка пойдёт в LLM/NEEDS_REVIEW.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fixers.structured_edit import Anchor, Edit, EditSet

logger = logging.getLogger(__name__)


# Confidence детерминированных правил (сверяется с contract.default_confidence_for).
RULE_CONFIDENCE = 0.95


class RuleBasedFixer:
    """Диспетчер детерминированных правил."""

    def try_fix(
        self,
        error: Dict[str, Any],
        file_content: str,
        language: str,
        max_line_length: int = 79,
    ) -> Optional[EditSet]:
        """Возвращает EditSet или None, если правила для этого кода нет
        или оно не уверено в правке."""
        code = (error.get("code") or "").strip()
        if not code or not file_content:
            return None
        lang = (language or "").lower()
        handler = self._dispatch(code, lang)
        if handler is None:
            return None
        try:
            self._max_line_length = max_line_length
            return handler(error, file_content)
        except Exception as e:
            logger.debug("RuleBasedFixer: правило %s упало: %s", code, e)
            return None

    # -----------------------------------------------------------------
    def can_fix_deterministically(self, code: str, lang: str) -> bool:
        """Есть ли для кода детерминированный фиксер. Единственный источник
        истины — _dispatch (2026-07-08, приоритизация: PrioritizeStage
        поднимает такие ошибки живых файлов раньше LLM-требующих — см.
        инцидент loguru: 3×E203 живого кода стояли в хвосте за 148
        нечинимыми корпусными ошибками и не дождались очереди)."""
        try:
            return self._dispatch(str(code or ""), lang) is not None
        except Exception:
            return False

    # -----------------------------------------------------------------
    def _dispatch(self, code: str, lang: str):
        rust = {
            "unused_import": self._rust_unused_import,
            "unused_variable": self._rust_unused_variable,
            "unused_mut": self._rust_unused_mut,
            "clippy::needless_return": self._rust_needless_return,
            "clippy::redundant_clone": self._rust_remove_clone,
            "clippy::clone_on_copy": self._rust_remove_clone,
        }
        python = {
            "E111": self._py_indent_multiple_of_4,
            "E114": self._py_indent_comment,
            "E115": self._py_under_indented,
            "E117": self._py_over_indented,
            "E201": self._py_bracket_space,
            "E202": self._py_bracket_space,
            "E203": self._py_slice_space,
            "E211": self._py_call_space,
            "E221": self._py_multi_space_before_op,
            "E225": self._py_operator_space,
            "E228": self._py_modulo_space,
            "E231": self._py_comma_space,
            "E251": self._py_keyword_space,
            "E261": self._py_comment_spaces,
            "E265": self._py_block_comment,
            "E271": self._py_keyword_multi_space,
            "E301": self._py_method_blank_line,
            "E302": self._py_blank_lines,
            "E303": self._py_extra_blank_lines,
            "E305": self._py_blank_lines_after,
            "E306": self._py_nested_def_blank,
            "E401": self._py_multiple_imports,
            "E701": self._py_multiple_statements,
            "E703": self._py_semicolon_end,
            "F401": self._py_unused_import,
            "F841": self._py_unused_variable,
            "W191": self._py_tab_indent,
            "W291": self._py_trailing_ws,
            "W292": self._py_missing_newline,
            "DUPLICATE_LINES": self._py_remove_duplicate_lines,
            "SLIPPED_LINE": self._py_separate_slipped_lines,
            "DEAD_CODE": self._py_remove_dead_code_after_return,
            "LLM_NOISE": self._py_remove_llm_noise,
            
            # Security (Stage K) — детерминированные однострочные/двухстрочные
            # фиксы. Для всего, что нельзя сделать безопасно автоматически —
            # возвращаем None, и SecurityScanner-находка уходит в LLM/NEEDS_REVIEW.
            "unsafe_deserialization": self._py_safe_yaml,
            "hardcoded_secret": self._py_hardcoded_secret,
            "dangerous_eval": self._py_dangerous_eval,
            # 2026-06-25: semgrep check_id передаётся как ПОЛНЫЙ dotted-путь
            # (см. core/stages/analyze_stage.py: finding.get("check_id")) —
            # ключ должен совпадать буквально. learning_cases.jsonl: 6
            # попыток через LLM, 100% успеха, простая детерминированная
            # замена hashlib.sha1(...)→hashlib.sha256(...) с сохранением
            # аргументов (включая usedforsecurity=False).
            "python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1":
                self._py_sha1_to_sha256,
            "W391": self._py_blank_line_at_eof,
            "W503": self._py_w503_move_operator,
            "E704": self._py_e704_inline_def,
            "E122": self._py_e127_e128_visual_indent,
            "E125": self._py_e125_closing_bracket_indent,
            "E126": self._py_e127_e128_visual_indent,
            "E127": self._py_e127_e128_visual_indent,
            "E128": self._py_e127_e128_visual_indent,
            "E402": self._py_e402_module_import_not_at_top,
            "F821": self._py_f821_typo_fix,
            # control series 12 analysis (2026-06-20): _py_e501_noqa был
            # написан (коммит 7377f42), но никогда не подключён к dispatch —
            # E501 всегда уходил в LLM. E501 — обычно топ-3 код по частоте в
            # реальных репо (1235/7134 = 17.3% в semiprime/pygenda).
            "E501": self._py_e501_noqa,
            # 2026-07-03: mypy list-item/arg-type на ГЕТЕРОГЕННЫХ
            # list-of-lists тест-векторах (pyca/bcrypt tests/test_bcrypt.py
            # parametrize-блоки). Честный фикс — внутренние списки → КОРТЕЖИ:
            # list инвариантен и гетерогенный ряд коллапсирует в list[object]
            # (mypy: "arg-type ... list[object]" / "list-item ... expected
            # tuple[...]"); tuple ковариантен и сохраняет типы по позициям, у
            # mypy претензий нет. Ни один литерал не меняется. LLM на этом
            # классе либо портила данные (O.19), либо плющила скобки. Триггер
            # строгий (AST): срабатывает ТОЛЬКО на list[list[простые-константы]]
            # с гетерогенным рядом (см. _py_list_item_tuple_repair).
            "list-item": self._py_list_item_tuple_repair,
            "arg-type": self._py_list_item_tuple_repair,
        }
        # P.4: ruff авто-фиксы. Для этих кодов `ruff check --fix` делает
        # точную правку без LLM. Делегируем тулу — он быстрее и точнее.
        ruff_fixable = {
            "F401",   # unused import (наш _py_unused_import тоже умеет — оставим как primary)
            "F811",   # redefinition
            "UP006",  # list→list[int]
            "UP007",  # Union → X|Y
            "UP008",  # super() with args
            "UP009",  # utf-8 encoding declaration
            "UP015",  # redundant open mode
            "UP018",  # native literal
            "UP032",  # use f-string
            "I001",   # unsorted imports
            "I002",   # missing required import
            "COM812", # missing trailing comma
            "B007",   # unused loop variable
            "B009",   # getattr with constant
            "B010",   # setattr with constant
            "Q000",   # quote style
            "Q001",   # quote style
            "Q002",   # quote style
            "PIE790", # unnecessary placeholder
            "PIE800", # unnecessary spread
            "C401",   # unnecessary generator
            "C408",   # dict() → {}
            "C416",   # unnecessary comprehension
            "SIM102", # nested if
            "SIM108", # ternary
            "SIM117", # combined with
            "RET504", # unnecessary assignment before return
            "RET505", # unnecessary else after return
        }
        for c in ruff_fixable:
            if c not in python:
                python[c] = self._py_ruff_autofix
        js = {
            "eqeqeq": self._js_eqeqeq,
            "no-var": self._js_no_var,
            "prefer-const": self._js_prefer_const,
            "no-unused-vars": self._js_no_unused_vars,
            # Security (Stage K)
            "xss": self._js_xss_textcontent,
        }
        if lang in ("rust", "rs"):
            return rust.get(code)
        if lang in ("python", "py"):
            return python.get(code)
        if lang in ("javascript", "typescript", "js", "ts"):
            return js.get(code)
        return None

    # -----------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------
    @staticmethod
    def _line_text(file_content: str, line_no: int) -> Optional[str]:
        lines = file_content.splitlines()
        if 1 <= line_no <= len(lines):
            return lines[line_no - 1]
        return None

    @staticmethod
    def _symbol_from_message(message: str) -> Optional[str]:
        """Первый идентификатор в backtick'ах или одинарных кавычках."""
        m = re.search(r"[`']([A-Za-z_][A-Za-z0-9_]*)[`']", message or "")
        return m.group(1) if m else None

    def _single_edit(
        self, error: Dict[str, Any], line_no: int, kind: str,
        new: str, intent: str, match: str,
    ) -> EditSet:
        return EditSet(
            intent=intent,
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=line_no, match=match.strip()),
                kind=kind,
                new=new,
                rationale="rule-based deterministic fix",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    # -----------------------------------------------------------------
    # Rust
    # -----------------------------------------------------------------
    def _rust_unused_import(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "use " not in line:
            return None
        return self._single_edit(error, line_no, "delete", "",
                                 "remove unused import", line)

    def _rust_unused_variable(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        var = self._symbol_from_message(error.get("message", ""))
        if line is None or not var or var in line.split("//")[0] and f"_{var}" in line:
            # уже с подчёркиванием — нечего делать
            pass
        if line is None or not var:
            return None
        # Префиксуем именно объявление: `let [mut ]var` → `let [mut ]_var`.
        new_line, n = re.subn(
            rf"\b(let\s+(?:mut\s+)?){re.escape(var)}\b",
            rf"\1_{var}", line, count=1,
        )
        if n == 0 or new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 f"prefix unused variable {var} with _", line)

    def _rust_unused_mut(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "mut " not in line:
            return None
        new_line, n = re.subn(r"\blet\s+mut\s+", "let ", line, count=1)
        if n == 0:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove needless mut", line)

    def _rust_needless_return(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "return " not in line:
            return None
        # `    return EXPR;`  →  `    EXPR`
        m = re.match(r"^(\s*)return\s+(.*?);\s*$", line)
        if not m:
            return None
        new_line = f"{m.group(1)}{m.group(2)}"
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "drop needless return", line)

    def _rust_remove_clone(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or ".clone()" not in line:
            return None
        new_line = line.replace(".clone()", "", 1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove redundant clone", line)

    # -----------------------------------------------------------------
    # Python
    # -----------------------------------------------------------------
    def _py_unused_import(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or not re.match(r"^\s*(import|from)\s", line):
            return None
        return self._single_edit(error, line_no, "delete", "",
                                 "remove unused import", line)

    def _py_w503_move_operator(self, error, file_content) -> Optional[EditSet]:
        """W503: перемещает бинарный оператор с начала строки N в конец строки N-1.

        Возвращает None если:
        - оператор не распознан или строка не совпадает с ожидаемым форматом
        - результирующая строка N-1 превысит project line-length (риск E501)
        В обоих случаях вызывающий код направит W503 в NEEDS_REVIEW без LLM.
        """
        _MAX_LEN = getattr(self, "_max_line_length", 79)
        line_no = int(error.get("line") or 0)
        if line_no < 2:
            return None
        lines = file_content.splitlines()
        if line_no > len(lines):
            return None

        current = lines[line_no - 1]   # строка N: оператор в начале
        prev = lines[line_no - 2]      # строка N-1: получит оператор в конце

        stripped = current.lstrip()
        indent = current[: len(current) - len(stripped)]

        # Распознаём бинарный оператор в начале строки
        _BINARY_OPS = ("and ", "or ", "+ ", "- ", "* ", "/ ", "| ", "& ",
                       "^ ", "% ", "// ", "** ", "<< ", ">> ")
        op = None
        rest_of_line = None
        for candidate in _BINARY_OPS:
            if stripped.startswith(candidate):
                op = candidate.rstrip()          # "and", "or", "+", …
                rest_of_line = stripped[len(candidate):]
                break
        if op is None or rest_of_line is None:
            return None

        prev_bare = prev.rstrip("\n\r")
        new_prev = prev_bare + " " + op
        if len(new_prev) > _MAX_LEN:
            return None  # слишком длинная строка → caller направит в NR

        new_current = indent + rest_of_line.rstrip("\n\r")
        if not new_current.strip():
            return None  # не оставляем пустую строку

        return EditSet(
            intent=f"move binary operator '{op}' to end of line {line_no - 1} (W503→W504 style)",
            edits=[
                Edit(
                    file=error.get("file", ""),
                    anchor=Anchor(line=line_no - 1, match=prev_bare.strip()),
                    kind="replace",
                    new=new_prev + "\n",
                    rationale="W503: append operator to previous line",
                ),
                Edit(
                    file=error.get("file", ""),
                    anchor=Anchor(line=line_no, match=current.strip()),
                    kind="replace",
                    new=new_current + "\n",
                    rationale="W503: remove operator from start of line",
                ),
            ],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    _E501_CONTINUATION_ENDINGS = frozenset({",", "\\", "+", "-", "*", "/", "|",
                                            "&", "or", "and", "not", "(", "[", "{"})

    def _py_e501_noqa(self, error, file_content) -> Optional[EditSet]:
        """E501: добавляет # noqa: E501 для строк-литералов и комментариев.

        Безопасно только когда строка НЕ является продолжением выражения.
        Для строк в середине многострочных вызовов возвращает None — LLM.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        stripped = line.lstrip()
        # Уже подавлено
        if "# noqa" in line:
            return None
        rstripped = line.rstrip()
        tail = rstripped.rstrip()
        # Пропускаем строки-продолжения (заканчиваются запятой, оператором, скобкой)
        if tail.endswith((",", "\\", "+", "-", "*", "/", "|", "&", "(", "[")):
            return None
        # Строки с ключевыми словами в конце
        for kw in ("or", "and", "not", "in", "is"):
            if tail.endswith(" " + kw):
                return None
        # Безопасные случаи: docstring, комментарий, строка-литерал, assert
        is_docstring = stripped.startswith('"""') or stripped.startswith("'''")
        is_comment = stripped.startswith("#")
        is_string_literal = stripped.startswith('"') or stripped.startswith("'")
        is_assert = stripped.startswith("assert ")
        if not (is_docstring or is_comment or is_string_literal or is_assert):
            return None
        new_line = rstripped + "  # noqa: E501"
        return self._single_edit(
            error, line_no, "replace",
            new_line + "\n",
            "suppress E501 with noqa: E501 for literal/comment",
            rstripped,
        )

    @staticmethod
    def _is_literal_only(node) -> bool:
        """True если AST-узел — Constant, либо Tuple/List/Set/Dict, состоящий
        ТОЛЬКО из таких же литералов (рекурсивно), либо UnaryOp(-, литерал).
        Используется для безопасного переноса E402-импорта: если ВСЁ, что
        предшествует ему — docstring/импорты/такие литеральные assignment'ы
        (например `__version__ = "1.0.0"`), перенос безопасен (не зависит
        от побочных эффектов вызовов)."""
        import ast as _ast
        if isinstance(node, _ast.Constant):
            return True
        if isinstance(node, _ast.UnaryOp) and isinstance(node.op, (_ast.USub, _ast.UAdd)):
            return RuleBasedFixer._is_literal_only(node.operand)
        if isinstance(node, (_ast.Tuple, _ast.List, _ast.Set)):
            return all(RuleBasedFixer._is_literal_only(e) for e in node.elts)
        if isinstance(node, _ast.Dict):
            return all(
                (k is None or RuleBasedFixer._is_literal_only(k))
                and RuleBasedFixer._is_literal_only(v)
                for k, v in zip(node.keys, node.values)
            )
        return False

    def _py_e402_module_import_not_at_top(self, error, file_content) -> Optional[EditSet]:
        """E402: импорт не в начале файла.

        2026-06-25 (learning_cases.jsonl: 34 попытки через LLM, 88% успеха,
        НО реальные кейсы в основном — намеренный паттерн
        `sys.path.insert(...); import x` (Sphinx conf.py и т.п.) или
        `pytest.importorskip(...); import x` — перенос импорта в начало
        файла в ЭТИХ случаях СЛОМАЛ БЫ логику (импорт зависит от
        предшествующего вызова). Дерево решений:

        1. Если ВСЁ, что предшествует импорту на модульном уровне —
           docstring/другие импорты/literal-only assignment (например
           `__version__ = "1.0.0"`) — перенос БЕЗОПАСЕН: переносим импорт
           сразу после последнего существующего импорта (или после
           docstring, если импортов ещё нет).
        2. Если встречается ЛЮБОЙ вызов функции/метода или что-то иное
           (sys.path.insert, pytest.importorskip, matplotlib.use, общий
           Expr/If/Try и т.п.) — перенос МОЖЕТ сломать порядок выполнения
           → добавляем `# noqa: E402` к строке импорта вместо переноса
           (детерминированно подавляет находку, не трогая семантику).

        Если файл не парсится или импорт не найден среди top-level
        statements — возвращает None (LLM/NEEDS_REVIEW)."""
        import ast as _ast

        line_no = int(error.get("line") or 0)
        try:
            tree = _ast.parse(file_content)
        except SyntaxError:
            return None

        target_node = None
        for node in tree.body:
            if isinstance(node, (_ast.Import, _ast.ImportFrom)) and node.lineno == line_no:
                target_node = node
                break
        if target_node is None:
            return None

        preceding = [n for n in tree.body if n.lineno < line_no]
        last_import_idx = None
        all_safe = True
        for idx, node in enumerate(preceding):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                last_import_idx = idx
                continue
            if isinstance(node, _ast.Expr) and isinstance(node.value, _ast.Constant) \
                    and isinstance(node.value.value, str):
                continue  # docstring / bare string literal
            if isinstance(node, _ast.Assign) and \
                    all(isinstance(t, _ast.Name) for t in node.targets) and \
                    self._is_literal_only(node.value):
                continue  # __version__ = "1.0.0" и подобное
            all_safe = False
            break

        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        stripped_line = line.rstrip("\n\r")

        if not all_safe:
            if "noqa" in stripped_line:
                return None
            new_line = stripped_line.rstrip() + "  # noqa: E402\n"
            return self._single_edit(
                error, line_no, "replace", new_line,
                "suppress E402 with noqa (import order is intentional — "
                "preceding code has side effects like sys.path manipulation)",
                stripped_line,
            )

        # Безопасный перенос: переставляем импорт сразу после последнего
        # существующего импорта среди preceding (или в начало, если
        # импортов ещё не было — после docstring, если он есть).
        lines = file_content.splitlines(keepends=True)
        import_lines = lines[target_node.lineno - 1:target_node.end_lineno]
        del lines[target_node.lineno - 1:target_node.end_lineno]

        if last_import_idx is not None:
            insert_after_line = preceding[last_import_idx].end_lineno
        elif preceding and isinstance(preceding[0], _ast.Expr) and \
                isinstance(preceding[0].value, _ast.Constant) and \
                isinstance(preceding[0].value.value, str):
            insert_after_line = preceding[0].end_lineno
        else:
            insert_after_line = 0

        # Корректируем индекс вставки, если импорт был ВЫШЕ точки вставки
        # (после удаления строк выше insert_after_line индексы сдвинулись).
        if target_node.lineno - 1 < insert_after_line:
            insert_after_line -= (target_node.end_lineno - target_node.lineno + 1)

        lines[insert_after_line:insert_after_line] = import_lines
        new_content = "".join(lines)
        if new_content == file_content:
            return None
        return EditSet(
            intent="move E402 import to top of file (after existing "
                   "imports/docstring — preceding code verified literal-only)",
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=1, match=""),
                kind="replace_file",
                new=new_content,
                rationale="rule-based deterministic fix",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    # -----------------------------------------------------------------
    # mypy list-item / arg-type — ремонт гетерогенных list-of-lists
    # тест-векторов (pyca/bcrypt tests/test_bcrypt.py). 2026-07-03.
    # -----------------------------------------------------------------
    @staticmethod
    def _vector_const_key(node):
        """Возвращает (type_name, value) для «простой константы» ряда вектора
        (ast.Constant любого литерала: bytes/str/int/float/bool/None, либо
        UnaryOp(+/-, числовая Constant) — типичное `-1`). Иначе None: имя,
        вызов, вложенный список/кортеж/dict, comprehension — всё, что делает
        триггер небезопасным. Implicit string/bytes concatenation
        (`b"a" b"b"`) в AST — уже ОДНА ast.Constant, отдельной обработки не
        требует. bool сводим к int (подтип, не считаем «другим» типом при
        проверке гетерогенности)."""
        import ast as _ast
        if isinstance(node, _ast.Constant):
            tn = type(node.value).__name__
            if tn == "bool":
                tn = "int"
            return (tn, node.value)
        if isinstance(node, _ast.UnaryOp) and isinstance(node.op, (_ast.USub, _ast.UAdd)):
            inner = node.operand
            if isinstance(inner, _ast.Constant) and isinstance(inner.value, (int, float, complex)):
                sign = -1 if isinstance(node.op, _ast.USub) else 1
                return (type(inner.value).__name__, sign * inner.value)
        return None

    @staticmethod
    def _constants_multiset(tree):
        """Мультимножество ВСЕХ ast.Constant дерева как Counter по
        (type_name, value). Внутренний O.19-супергард: список литералов
        обязан совпасть байт-в-байт до/после правки (мы двигаем ТОЛЬКО
        скобки, значит должно совпасть точно)."""
        import ast as _ast
        import collections
        c: "collections.Counter" = collections.Counter()
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Constant):
                c[(type(n.value).__name__, n.value)] += 1
        return c

    def _py_list_item_tuple_repair(self, error, file_content) -> Optional[EditSet]:
        """mypy list-item / arg-type: чинит гетерогенные list-of-lists
        тест-векторы, превращая ВНУТРЕННИЕ списки в кортежи.

        Мотивация (2026-07-03, pyca/bcrypt — «toxic»-класс из «исключаем» в
        «чиним»): тест-векторы вида list-of-lists с ГЕТЕРОГЕННЫМИ элементами
        (`[4, b"password", b"salt", b"\\x.."]`) mypy инферит как list[object]
        (list ИНВАРИАНТЕН), и в типовом контексте pytest.parametrize это даёт
        `arg-type ... incompatible type "list[object]"` / `list-item ...
        expected "tuple[...]"`. Честный фикс — внутренние списки → кортежи:
        tuple КОВАРИАНТЕН и хранит типы по позициям, mypy доволен. Ни один
        литерал не меняется — чистая структурная замена `[` `]` → `(` `)`.
        LLM на этом классе портила данные (O.19) или плющила скобки
        (bracket-flatten arg-type) — оба раза теряя байт-точные векторы.

        Строгие условия срабатывания (zero-collateral):
          (а) строка ошибки попадает в span ВНЕШНЕГО list-литерала L0, ВСЕ
              элементы которого — list-литералы (L1); L0 сам НЕ вложен в
              другой список (запрет вложенности глубже 2);
          (б) каждый L1 состоит ТОЛЬКО из простых констант (см.
              _vector_const_key) и содержит ≥2 элемента (1-элементный `[x]`
              → `(x)` — это НЕ кортеж, не рискуем);
          (в) хотя бы один L1 гетерогенен по типам констант (иначе mypy не
              флагал бы — гомогенные данные не трогаем).
        Замена — по позициям AST-узлов на уровне исходника (НЕ ast.unparse:
        он потерял бы форматирование, комментарии и hex-нотацию). Перед
        выдачей — самопроверка: (1) новый контент парсится; (2) мультимножество
        всех Constant совпадает до/после. Любое несоответствие → None (уходит
        по обычной цепочке к LLM). Fail-closed по всей длине."""
        import ast as _ast

        line_no = int(error.get("line") or 0)
        if line_no < 1 or not file_content:
            return None
        try:
            tree = _ast.parse(file_content)
        except SyntaxError:
            return None

        # Карта родителей — для запрета вложенности глубже 2 уровней.
        parent = {}
        for node in _ast.walk(tree):
            for child in _ast.iter_child_nodes(node):
                parent[child] = node

        # Кандидаты L0: ast.List, ВСЕ элементы которого — ast.List, чей span
        # покрывает строку ошибки, и который сам НЕ вложен в другой ast.List.
        best = None
        best_span = None
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.List) or not node.elts:
                continue
            if not all(isinstance(e, _ast.List) for e in node.elts):
                continue
            end = getattr(node, "end_lineno", None) or node.lineno
            if not (node.lineno <= line_no <= end):
                continue
            if isinstance(parent.get(node), _ast.List):
                # L0 сам — элемент внешнего списка → depth>2, не наш случай.
                continue
            span = end - node.lineno
            if best is None or span < best_span:
                best, best_span = node, span
        if best is None:
            return None
        L0 = best

        # Проверяем внутренние списки: только простые константы, ≥2 элементов,
        # хотя бы один список гетерогенен по типам констант.
        any_heterogeneous = False
        for inner in L0.elts:
            if not isinstance(inner, _ast.List) or len(inner.elts) < 2:
                return None
            types = set()
            for elt in inner.elts:
                key = self._vector_const_key(elt)
                if key is None:
                    return None  # имя/вызов/вложенность → отступаем
                types.add(key[0])
            if len(types) >= 2:
                any_heterogeneous = True
        if not any_heterogeneous:
            return None  # гомогенные списки mypy не флагает — не трогаем

        # Собираем позиции скобок для замены [ → ( и ] → ). Работаем по
        # позициям AST-узлов на уровне исходника; per-line, применяем
        # замены справа-налево, чтобы индексы в строке не «плыли».
        lines = file_content.splitlines(keepends=True)
        repl: dict = {}
        for inner in L0.elts:
            o_line = inner.lineno - 1
            o_col = inner.col_offset
            c_line = (getattr(inner, "end_lineno", None) or inner.lineno) - 1
            c_col = inner.end_col_offset - 1
            repl.setdefault(o_line, []).append((o_col, "[", "("))
            repl.setdefault(c_line, []).append((c_col, "]", ")"))

        new_lines = list(lines)
        for li, edits in repl.items():
            if li < 0 or li >= len(new_lines):
                return None
            s = new_lines[li]
            for col, expected, new_ch in sorted(edits, key=lambda t: t[0], reverse=True):
                # Fail-closed: если по позиции не ожидаемая скобка (сдвиг из-за
                # мультибайтных символов в байтовом col_offset и т.п.) —
                # отступаем целиком, не рискуя порчей.
                if col < 0 or col >= len(s) or s[col] != expected:
                    return None
                s = s[:col] + new_ch + s[col + 1:]
            new_lines[li] = s
        new_content = "".join(new_lines)
        if new_content == file_content:
            return None

        # --- Самопроверка (внутренний O.19-супергард) ---
        try:
            new_tree = _ast.parse(new_content)
        except SyntaxError:
            return None
        if self._constants_multiset(tree) != self._constants_multiset(new_tree):
            return None  # хоть один литерал изменился — не наш случай

        return EditSet(
            intent=(
                "convert heterogeneous inner test-vector lists to tuples "
                "(mypy list-item/arg-type: list invariance forces list[object]; "
                "tuple keeps per-position types)"
            ),
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=1, match=""),
                kind="replace_file",
                new=new_content,
                rationale="rule-based deterministic fix (vector→tuple repair)",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    def _py_trailing_ws(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or line == line.rstrip():
            return None
        stripped = line.rstrip()
        if not stripped:
            # Control series 2026-06-22, W291 REJECT-аномалия: строка из
            # ОДНИХ пробелов/табов (самый частый реальный случай W291—
            # "пустая" строка с висячими пробелами от редактора) не имеет
            # содержимого, по которому anchor.match мог бы однозначно
            # опознать строку — _find_anchor (structured_edit.py) требует
            # непустой needle и БЕЗ ЕГО НАХОЖДЕНИЯ молча пропускает весь
            # файл (diff пуст). Раньше match передавался как
            # `line.rstrip() or line` → "" после .strip() в Anchor — гарантированный
            # сбой anchor-проверки, патч "успешно применялся" структурно, но
            # реально НЕ менял файл → ложный REJECT "target_error_still_
            # present" (патч был, по факту, no-op). Честнее — НЕ генерировать
            # такую правку вовсе, отдать LLM (видит полный контекст файла).
            return None
        return self._single_edit(error, line_no, "replace", stripped + "\n",
                                 "strip trailing whitespace", stripped)

    def _py_blank_lines(self, error, file_content) -> Optional[EditSet]:
        """E302: добавляет недостающие пустые строки перед top-level определением."""
        line_no = int(error.get("line") or 0)
        if line_no < 2:
            return None
        lines = file_content.splitlines(keepends=True)
        if line_no > len(lines):
            return None
        # Считаем, сколько пустых строк уже есть перед line_no
        blank_count = 0
        idx = line_no - 2  # индекс строки перед line_no (0-based)
        while idx >= 0 and lines[idx].strip() == "":
            blank_count += 1
            idx -= 1
        if blank_count >= 2:
            return None  # всё уже на месте
        missing = 2 - blank_count
        # anchor — строка, перед которой нужно вставить пустые строки
        anchor_text = lines[line_no - 1].rstrip("\n")
        return self._single_edit(error, line_no, "insert_before",
                                 "\n" * missing, "add missing blank lines",
                                 anchor_text)

    def _py_blank_lines_after(self, error, file_content) -> Optional[EditSet]:
        """E305: добавляет недостающие пустые строки ПОСЛЕ top-level определения."""
        line_no = int(error.get("line") or 0)
        if line_no < 1:
            return None
        lines = file_content.splitlines(keepends=True)
        if line_no >= len(lines):
            return None
        # Считаем пустые строки после line_no
        blank_count = 0
        idx = line_no  # индекс строки ПОСЛЕ line_no (0-based)
        while idx < len(lines) and lines[idx].strip() == "":
            blank_count += 1
            idx += 1
        if blank_count >= 2:
            return None
        missing = 2 - blank_count
        anchor_line = line_no
        anchor_text = lines[line_no - 1].rstrip("\n")
        return self._single_edit(error, anchor_line, "insert_after",
                                 "\n" * missing, "add missing blank lines after definition",
                                 anchor_text)

    def _py_missing_newline(self, error, file_content) -> Optional[EditSet]:
        """W292: добавляет \\n в конец файла, если его нет."""
        line_no = int(error.get("line") or 0)
        if not file_content or file_content.endswith("\n"):
            return None
        line = self._line_text(file_content, line_no) or ""
        return self._single_edit(error, line_no, "replace", line.rstrip("\n") + "\n",
                                 "add newline at end of file", line)

    def _py_blank_line_at_eof(self, error, file_content) -> Optional[EditSet]:
        """W391: удаляет все пустые строки в конце файла через replace_file."""
        lines = file_content.splitlines(keepends=True)
        if not lines or lines[-1].strip() != "":
            return None
        # Отрезаем все trailing blank lines, добавляем одну финальную \n.
        i = len(lines)
        while i > 0 and lines[i - 1].strip() == "":
            i -= 1
        if i == 0:
            return None
        # Отрезаем trailing blank lines; последняя непустая строка уже
        # заканчивается на \n, дополнительный \n не добавляем.
        new_content = "".join(lines[:i]).rstrip("\n\r") + "\n"
        if new_content == file_content:
            return None
        return EditSet(
            intent="remove trailing blank lines at end of file",
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=1, match=""),
                kind="replace_file",
                new=new_content,
                rationale="rule-based deterministic fix (W391)",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    @staticmethod
    def _line_bracket_depth_delta(text: str) -> int:
        """Считает чистое изменение глубины скобок ( [ { в строке, игнорируя
        строковые литералы и комментарии — для переноса depth между строками
        многострочной сигнатуры def (см. _py_e704_inline_def)."""
        depth = 0
        in_str = False
        str_char = ""
        i = 0
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == "\\" and i + 1 < len(text):
                    i += 2
                    continue
                if text[i:i + len(str_char)] == str_char:
                    in_str = False
                    i += len(str_char)
                    continue
                i += 1
                continue
            if ch in ('"', "'"):
                if text[i:i + 3] in ('"""', "'''"):
                    str_char = text[i:i + 3]
                    in_str = True
                    i += 3
                    continue
                str_char = ch
                in_str = True
                i += 1
                continue
            if ch == "#":
                break
            if ch in ("(", "[", "{"):
                depth += 1
            elif ch in (")", "]", "}"):
                depth -= 1
            i += 1
        return depth

    def _py_e704_inline_def(self, error, file_content) -> Optional[EditSet]:
        """E704: разбивает `def f(args): body` на две строки.

        Ищет двоеточие окончания сигнатуры def по балансу скобок — корректно
        обрабатывает type hints, default-значения и аннотации возврата.

        2026-06-25 (learning_cases.jsonl: 8 попыток через LLM на этот код,
        0% успеха): для МНОГОСТРОЧНЫХ сигнатур (частый паттерн @overload/
        Protocol-стабов — `def f(\\n    args\\n) -> T: ...`) flake8 репортит
        E704 на ПОСЛЕДНЕЙ строке (`) -> T: ...`), которая не начинается с
        `def` — старая проверка `^\\s*def\\s+` отбрасывала такие случаи,
        уходили в LLM, который тоже не справлялся (та же путаница с
        перегрузками, что и в [[project_overload_regression_bug]], но это
        отдельный, более простой механизм — здесь просто не находили строку).
        Теперь при отсутствии `def` на самой строке сканируем НАЗАД (до 30
        строк) до начала сигнатуры, переносим накопленную глубину скобок —
        правка остаётся точечной (одна строка), назад мы только СЧИТАЕМ depth,
        не редактируем эти строки.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        stripped = line.rstrip("\n\r")
        carry_depth = 0
        if not re.match(r"^\s*def\s+", stripped):
            if not re.match(r"^\s*async\s+def\s+", stripped):
                # Возможно, это последняя строка многострочной сигнатуры —
                # ищем начало def, сканируя назад и накапливая depth.
                all_lines = file_content.splitlines()
                start_idx = None
                acc_depth = 0
                for back in range(1, 31):
                    idx = line_no - 1 - back
                    if idx < 0:
                        break
                    prev = all_lines[idx].rstrip("\n\r")
                    acc_depth += self._line_bracket_depth_delta(prev)
                    if re.match(r"^\s*(async\s+)?def\s+", prev):
                        start_idx = idx
                        break
                if start_idx is None:
                    return None
                # acc_depth накопилась от строки ПОСЛЕ def до строки ПЕРЕД
                # ошибочной — нужно её знак инвертировать на перенос
                # (мы шли назад, складывая дельты строк МЕЖДУ def и ошибкой).
                carry_depth = acc_depth
                # Отступ тела берём от строки `def`, не от закрывающей —
                # закрывающая скобка обычно совпадает по отступу с def
                # (PEP8), но на нестандартном форматировании может не
                # совпасть; отступ def — надёжный ориентир в любом случае.
                def_line = all_lines[start_idx].rstrip("\n\r")
                indent_source = def_line
            else:
                indent_source = stripped
        else:
            indent_source = stripped

        indent = len(indent_source) - len(indent_source.lstrip())
        body_indent = " " * (indent + 4)

        # Найти тело def: ищем первое ':' при depth==0 после которого есть непустой текст.
        depth = carry_depth
        in_str = False
        str_char = ""
        colon_idx = -1
        i = 0
        while i < len(stripped):
            ch = stripped[i]
            if in_str:
                if ch == "\\" and i + 1 < len(stripped):
                    i += 2
                    continue
                if ch == str_char:
                    in_str = False
            elif ch in ('"', "'"):
                # Обнаружение тройных кавычек
                if stripped[i:i+3] in ('"""', "'''"):
                    str_char = stripped[i:i+3]
                    in_str = True
                    i += 3
                    continue
                in_str = True
                str_char = ch
            elif ch in ("(", "[", "{"):
                depth += 1
            elif ch in (")", "]", "}"):
                depth -= 1
            elif ch == ":" and depth == 0:
                rest = stripped[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    colon_idx = i
                    break
            i += 1

        if colon_idx == -1:
            return None

        def_sig = stripped[:colon_idx]
        body = stripped[colon_idx + 1:].strip()
        if not body:
            return None

        new_text = f"{def_sig}:\n{body_indent}{body}\n"
        return self._single_edit(
            error, line_no, "replace", new_text,
            "split inline def onto separate line",
            stripped,
        )

    def _py_e127_e128_visual_indent(self, error, file_content) -> Optional[EditSet]:
        """E127/E128/E122/E126: выравнивание строки продолжения.

        Использует tokenize для корректной обработки строк и комментариев.
        Находит ближайшую незакрытую скобку перед проблемной строкой.
        Если после скобки есть токен на той же строке — visual indent style
        (E127/E128), выравниваем по нему. Если нет — hanging indent style
        (E122/E126), делегируем в `_py_hanging_indent_fix`.
        Возвращает None если скобка в конце строки (hanging indent — не E127/E128).
        """
        import io
        import tokenize as _tok

        line_no = int(error.get("line") or 0)
        if line_no < 2:
            return None

        try:
            tok_list = list(_tok.generate_tokens(io.StringIO(file_content).readline))
        except _tok.TokenError:
            return None

        # Стек незакрытых открывающих скобок до problem_line_no
        bracket_stack: list = []
        for tok_type, tok_str, tok_start, _tok_end, _tok_line in tok_list:
            row, col = tok_start
            if row >= line_no:
                break
            if tok_str in ("(", "[", "{"):
                bracket_stack.append((row, col))
            elif tok_str in (")", "]", "}"):
                if bracket_stack:
                    bracket_stack.pop()

        if not bracket_stack:
            return None

        anchor_row, anchor_col = bracket_stack[-1]

        # Первый реальный токен после anchor на anchor_row = visual indent
        _skip = {
            _tok.COMMENT, _tok.NL, _tok.NEWLINE,
            _tok.ENDMARKER, _tok.INDENT, _tok.DEDENT,
        }
        visual_col = None
        for tok_type, tok_str, tok_start, _tok_end, _tok_line in tok_list:
            row, col = tok_start
            if row != anchor_row or col <= anchor_col:
                continue
            if tok_type in _skip:
                continue
            visual_col = col
            break

        if visual_col is None:
            # После скобки нет токена на той же строке → hanging indent.
            # E127/E128 это не касается (они про visual indent) — пропускаем.
            # Для E122 (missing/outdented) и E126 (over-indented for hanging)
            # выравниваем по стандартному hanging indent = отступ открывающей
            # строки + 4. F5 (AUDIT_series11.md): LLM ненадёжно справляется
            # с этим без AST-понимания, делаем детерминированно.
            if error.get("code") not in ("E122", "E126"):
                return None
            return self._py_hanging_indent_fix(error, file_content, anchor_row, line_no)

        lines = file_content.splitlines(keepends=True)
        if line_no < 1 or line_no > len(lines):
            return None
        current_line = lines[line_no - 1]
        stripped_content = current_line.lstrip()
        if not stripped_content or stripped_content[0] == "#":
            return None

        current_col = len(current_line) - len(stripped_content)
        if current_col == visual_col:
            return None

        new_line = " " * visual_col + stripped_content
        # Skip if the fix would produce a line longer than max_line_length.
        _max_len = getattr(self, "_max_line_length", 79)
        new_visible_len = visual_col + len(stripped_content.rstrip("\n\r"))
        if new_visible_len > _max_len:
            # Fallback: restructure the call by moving everything after `(` on
            # the opening line to a new hanging-indent continuation line.
            # This converts visual-indent style to hanging-indent style and
            # keeps all lines within max_line_length.
            anchor_line = lines[anchor_row - 1]
            after_open = anchor_line[anchor_col + 1:].rstrip("\n\r")
            if not after_open.strip():
                return None
            base_ind = len(anchor_line) - len(anchor_line.lstrip())
            hang = " " * (base_ind + 4)
            new_open = anchor_line[:anchor_col + 1].rstrip() + "\n"
            moved = hang + after_open.lstrip() + "\n"
            if len(moved.rstrip("\n\r")) > _max_len:
                return None
            hang_cont = hang + stripped_content.rstrip("\n\r") + "\n"
            if len(hang_cont.rstrip("\n\r")) > _max_len:
                return None
            edits_list = [Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=anchor_row, match=anchor_line.strip()),
                kind="replace",
                new=new_open + moved,
                rationale="move args after ( to hanging-indent line to stay within line limit",
            )]
            if current_col != base_ind + 4:
                edits_list.append(Edit(
                    file=error.get("file", ""),
                    anchor=Anchor(line=line_no, match=current_line.rstrip("\n\r").strip()),
                    kind="replace",
                    new=hang_cont,
                    rationale="align continuation to new hanging indent",
                ))
            return EditSet(
                intent=(
                    f"restructure call to hanging indent "
                    f"(visual col {visual_col} > max {_max_len})"
                ),
                edits=edits_list,
                confidence=RULE_CONFIDENCE,
                risks=[],
            )
        return self._single_edit(
            error, line_no, "replace", new_line,
            f"align continuation line to visual indent (col {current_col}→{visual_col})",
            current_line.rstrip("\n\r"),
        )

    def _py_hanging_indent_fix(self, error, file_content, anchor_row: int,
                              line_no: int) -> Optional[EditSet]:
        """E122/E126: выравнивание hanging-indent строки продолжения.

        Вызывается из `_py_e127_e128_visual_indent`, когда после открывающей
        скобки на её строке нет токена (классический hanging indent, а не
        visual indent). Стандарт PEP8 — отступ открывающей строки + 4.
        E122 (missing/outdented) — строка недоотступлена или на уровне
        открывающей; E126 (over-indented) — отступ больше ожидаемого
        (например, совпадает с вложенным блоком). В обоих случаях фикс
        одинаков: выровнять на base_indent + 4.
        """
        lines = file_content.splitlines(keepends=True)
        if line_no < 1 or line_no > len(lines) or anchor_row < 1 or anchor_row > len(lines):
            return None
        anchor_line = lines[anchor_row - 1]
        current_line = lines[line_no - 1]
        stripped_content = current_line.lstrip()
        if not stripped_content or stripped_content[0] == "#":
            return None

        base_ind = len(anchor_line) - len(anchor_line.lstrip())
        expected_col = base_ind + 4
        current_col = len(current_line) - len(stripped_content)
        if current_col == expected_col:
            return None

        # E125-collision guard (living case 2026-07-02, bcrypt test_hashpw_new,
        # диагностика 88 детерминированных REJECT): для МНОГОСТРОЧНОЙ сигнатуры,
        # где строка-продолжение ЗАКРЫВАЕТ compound-заголовок (`def f(\n
        # args):` / `if (\n cond):`), выравнивание на base+4 даёт тот же отступ,
        # что и тело блока → pycodestyle меняет E122 на E125. Правка не убирает
        # цель, а подменяет код (target_removed=False, счётчик не падает →
        # REJECT). Не угадываем корректный двойной отступ детерминированно —
        # отдаём LLM/NEEDS_REVIEW (§4: лучше не починить, чем сломать на другое).
        cont_body = current_line.strip()
        if cont_body.endswith(":"):
            nxt_indent = None
            for k in range(line_no, len(lines)):
                if lines[k].strip():
                    nxt_indent = len(lines[k]) - len(lines[k].lstrip())
                    break
            if nxt_indent is not None and nxt_indent == expected_col:
                return None

        new_line = " " * expected_col + stripped_content
        _max_len = getattr(self, "_max_line_length", 79)
        if len(new_line.rstrip("\n\r")) > _max_len:
            # Выравнивание увело бы строку за лимит длины — не угадываем,
            # пусть решает LLM/NEEDS_REVIEW.
            return None

        return self._single_edit(
            error, line_no, "replace", new_line,
            f"align hanging-indent continuation line (col {current_col}→{expected_col})",
            current_line.rstrip("\n\r"),
        )

    def _py_e125_closing_bracket_indent(self, error, file_content) -> Optional[EditSet]:
        """E125: closing bracket line indented same as next logical line.

        Типичный случай:
            if (a or
                b):
                do_something()
        Строка `    b):` (закрывающая скобку + ':') имеет тот же отступ, что
        и тело блока `do_something()` — визуально неотличимы. Pycodestyle
        не предлагает конкретное число, поэтому правим однозначно: увеличиваем
        отступ ПРОБЛЕМНОЙ строки на 4, чтобы она перестала совпадать с телом.

        Безопасность: пересчитываем условие сами (не верим вслепую errors[]) —
        если отступ проблемной строки и следующей непустой строки не совпадают
        (например, номера строк уже сдвинулись от предыдущего патча), не угадываем.
        """
        line_no = int(error.get("line") or 0)
        if line_no < 1:
            return None
        lines = file_content.splitlines(keepends=True)
        if line_no > len(lines):
            return None
        current_line = lines[line_no - 1]
        stripped = current_line.lstrip()
        if not stripped or stripped[0] == "#":
            return None
        current_col = len(current_line) - len(stripped)

        body_col = None
        for nxt in lines[line_no:]:
            if not nxt.strip():
                continue
            body_col = len(nxt) - len(nxt.lstrip())
            break
        if body_col is None or current_col != body_col:
            return None

        new_indent = current_col + 4
        new_line = " " * new_indent + stripped
        _max_len = getattr(self, "_max_line_length", 79)
        if len(new_line.rstrip("\n\r")) > _max_len:
            return None

        return self._single_edit(
            error, line_no, "replace", new_line,
            f"increase closing-bracket line indent to disambiguate from body "
            f"(col {current_col}→{new_indent})",
            current_line.rstrip("\n\r"),
        )

    def _py_safe_yaml(self, error, file_content) -> Optional[EditSet]:
        """Security: `yaml.load(...)` → `yaml.safe_load(...)`.

        Чиним ТОЛЬКО этот безопасный однострочный случай. Любой другой код
        `unsafe_deserialization` (pickle.load(s)) восстановить тривиально и
        безопасно нельзя → возвращаем None, и находка уходит в LLM/NEEDS_REVIEW.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "yaml.load(" not in line or "safe_load" in line:
            return None
        new_line = line.replace("yaml.load(", "yaml.safe_load(", 1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "use yaml.safe_load for untrusted input", line)

    def _py_comment_spaces(self, error, file_content) -> Optional[EditSet]:
        """E261: добавляет недостающий пробел перед inline-комментарием."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "#" not in line:
            return None
        # Ищем комментарий и проверяем пробелы перед ним
        m = re.match(r"^(.*?\S)(\s+)#", line)
        if not m:
            return None
        code_part = m.group(1)
        spaces = m.group(2)
        if len(spaces) >= 2:
            return None  # уже ок
        # Увеличиваем пробелы до 2
        new_line = line.replace(code_part + spaces + "#", code_part + "  #", 1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "add space before inline comment", line)

    def _py_comma_space(self, error, file_content) -> Optional[EditSet]:
        """E231: добавляет пробел после запятой, если он пропущен."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "," not in line:
            return None
        # Заменяем только первый случай, где после запятой нет пробела
        new_line = re.sub(r",(\S)", r", \1", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "add space after comma", line)

    def _py_extra_blank_lines(self, error, file_content) -> Optional[EditSet]:
        """E303: удаляет лишние пустые строки (оставляет максимум 2)."""
        line_no = int(error.get("line") or 0)
        if line_no < 1:
            return None
        lines = file_content.splitlines(keepends=True)
        if line_no > len(lines):
            return None
        # Считаем пустые строки ПЕРЕД line_no
        blank_start = line_no - 1
        while blank_start > 0 and lines[blank_start - 1].strip() == "":
            blank_start -= 1
        blank_count = line_no - blank_start
        if blank_count <= 2:
            return None
        # Удаляем лишние — оставляем ровно 2 пустые строки
        to_remove = blank_count - 2
        # anchor — строка, с которой начинаем удаление (третья пустая подряд)
        anchor_line = line_no - to_remove
        anchor_text = lines[anchor_line - 1].rstrip("\n") if anchor_line <= len(lines) else ""
        return self._single_edit(error, anchor_line, "delete",
                                 "\n" * to_remove,  # это заглушка, real new=""
                                 "remove extra blank lines", anchor_text or " ")

    def _py_unused_variable(self, error, file_content) -> Optional[EditSet]:
        """F841: префиксует неиспользуемую переменную с `_`."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        # Извлекаем имя переменной из сообщения (обычно там кавычки)
        var = self._symbol_from_message(error.get("message", ""))
        if not var:
            return None
        # Ищем присваивание этой переменной в строке
        if not re.match(rf"\s*{re.escape(var)}\s*=", line):
            return None
        new_line = line.replace(f"{var} =", f"_{var} =", 1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 f"prefix unused variable '{var}' with '_'", line)

    def _py_keyword_space(self, error, file_content) -> Optional[EditSet]:
        """E251: убирает пробелы вокруг знака `=` в keyword аргументах."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        # Убираем пробел перед `=` и после `=` в keyword аргументах
        new_line = re.sub(r"(\w+)\s+=\s+", r"\1=", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove spaces around equals in keyword argument", line)

    def _py_bracket_space(self, error, file_content) -> Optional[EditSet]:
        """E201/E202: убирает пробел после открывающей или перед закрывающей скобкой."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        code = (error.get("code") or "").strip()
        unsafe = self._string_ranges(line)
        if code == "E201":
            m = re.search(r"\(\s+", line)
            if not m or m.start() in unsafe:
                return None
            new_line = line[:m.start()] + "(" + line[m.end():]
        elif code == "E202":
            m = re.search(r"\s+\)", line)
            if not m or m.start() in unsafe:
                return None
            new_line = line[:m.start()] + ")" + line[m.end():]
        else:
            return None
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove extra space inside brackets", line)

    def _py_operator_space(self, error, file_content) -> Optional[EditSet]:
        """E225: добавляет пробелы вокруг операторов (=, +, -, etc).

        Проверяет, что совпадение не попало в строковый литерал или комментарий.
        Если col доступен — используем точную позицию; иначе — первое совпадение
        вне строки.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        unsafe = self._string_ranges(line)
        col = int(error.get("col") or error.get("column") or 0)
        m = re.search(r"(\w)([+\-*/=])(\w)", line)
        if not m:
            return None
        # If ruff gave us a column, trust it; otherwise use first match outside string.
        if col > 0:
            # find match closest to col
            best = None
            for candidate in re.finditer(r"(\w)([+\-*/=])(\w)", line):
                if candidate.start(2) in unsafe:
                    continue
                if best is None or abs(candidate.start(2) - (col - 1)) < abs(best.start(2) - (col - 1)):
                    best = candidate
            m = best
        else:
            # pick first match outside string
            m = None
            for candidate in re.finditer(r"(\w)([+\-*/=])(\w)", line):
                if candidate.start(2) not in unsafe:
                    m = candidate
                    break
        if not m:
            return None
        new_line = (line[:m.start()]
                    + m.group(1) + " " + m.group(2) + " " + m.group(3)
                    + line[m.end():])
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "add spaces around operator", line)

    def _py_modulo_space(self, error, file_content) -> Optional[EditSet]:
        """E228: добавляет пробел вокруг оператора %."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        new_line = re.sub(r"(\w)%(\w)", r"\1 % \2", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "add space around modulo operator", line)

    def _py_block_comment(self, error, file_content) -> Optional[EditSet]:
        """E265: добавляет пробел после # в блочном комментарии."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or not line.lstrip().startswith("#"):
            return None
        new_line = re.sub(r"^(\s*#)(\S)", r"\1 \2", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "add space after # in block comment", line)

    def _py_keyword_multi_space(self, error, file_content) -> Optional[EditSet]:
        """E271: заменяет множественные пробелы после ключевых слов (if, for, while)."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        new_line = re.sub(r"(\b(?:if|for|while|def|class|with|import|from|return|yield|raise|assert)\b)  +",
                          r"\1 ", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove multiple spaces after keyword", line)

    def _py_multiple_imports(self, error, file_content) -> Optional[EditSet]:
        """E401: разбивает множественные импорты на отдельные строки."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "import " not in line or "," not in line:
            return None
        m = re.match(r"^(\s*)import\s+(.+)", line)
        if not m:
            return None
        indent = m.group(1)
        modules = [x.strip() for x in m.group(2).split(",")]
        new_lines = "\n".join(f"{indent}import {mod}" for mod in modules)
        return self._single_edit(error, line_no, "replace", new_lines + "\n",
                                 "split multiple imports into separate lines", line)

    def _py_multiple_statements(self, error, file_content) -> Optional[EditSet]:
        """E701: разбивает несколько операторов на одной строке через ;."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or ";" not in line:
            return None
        indent = " " * (len(line) - len(line.lstrip()))
        parts = [p.strip() for p in line.split(";") if p.strip()]
        if len(parts) <= 1:
            return None
        new_lines = "\n".join(f"{indent}{p}" for p in parts)
        return self._single_edit(error, line_no, "replace", new_lines + "\n",
                                 "split multiple statements into separate lines", line)

    def _py_slice_space(self, error, file_content) -> Optional[EditSet]:
        """E203: убирает пробел(ы) перед двоеточием (list[1 : 3] → list[1:3]).

        2026-06-25 (learning_cases.jsonl: 19 попыток через LLM, 0 через
        rule_based, хотя правило существует с 2026-06-16): старая версия
        требовала regex `\\[\\s*(\\w+)\\s*:\\s*(\\w+)\\s*\\]` — оба операнда
        ДОЛЖНЫ были быть простым identifier'ом. Реальные слайсы часто
        содержат ВЫРАЖЕНИЯ (`data[i : i + CHUNK_SIZE]`, `dum[k + 1 : 2 * k + 1]`)
        — regex не матчился, ВСЕГДА уходило в LLM. Теперь используем
        error["column"] (flake8 репортит точную позицию пробела ПЕРЕД ':')
        — убираем ровно этот пробел, независимо от сложности выражений по
        бокам.

        Конфликт с black: black для slice с непростыми операндами добавляет
        пробел С ОБЕИХ сторон ':' (`ham[a + 1 : b + 1]`). Мы убираем ТОЛЬКО
        пробел ПЕРЕД ':' (ровно то, на что жалуется E203) — пробел ПОСЛЕ
        не трогаем, он не флагуется flake8 и принадлежит соседнему
        выражению, не двоеточию. Минимальная, точечная правка — если в
        проекте используется black, он на следующем проходе сам
        восстановит свой стиль (косметика, не семантика), но прямого
        конфликта с этой правкой нет — она просто устраняет E203.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        col = int(error.get("column") or 0)
        if col < 1 or col > len(line):
            return None
        idx = col - 1  # flake8 column 1-based
        # Находим протяжённость пробелов, заканчивающихся перед ':'.
        start = idx
        while start > 0 and line[start - 1] == " ":
            start -= 1
        end = idx
        while end < len(line) and line[end] == " ":
            end += 1
        if end >= len(line) or line[end] != ":":
            return None  # не тот паттерн, что ожидали — отступаем
        if start == end:
            return None  # нет фактического пробела на этой позиции
        new_line = line[:start] + line[end:]
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove space before slice colon", line)

    def _py_call_space(self, error, file_content) -> Optional[EditSet]:
        """E211: убирает пробел перед открывающей скобкой вызова функции."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        new_line = re.sub(r"(\w+)\s+\(", r"\1(", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove space before function call parenthesis", line)

    def _py_multi_space_before_op(self, error, file_content) -> Optional[EditSet]:
        """E221: оставляет ровно один пробел перед оператором."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        new_line = re.sub(r"  +([+\-*/=<>!])", r" \1", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove extra spaces before operator", line)

    def _py_method_blank_line(self, error, file_content) -> Optional[EditSet]:
        """E301: добавляет одну пустую строку между методами класса."""
        line_no = int(error.get("line") or 0)
        if line_no < 2:
            return None
        lines = file_content.splitlines(keepends=True)
        if line_no > len(lines):
            return None
        # Считаем пустые строки перед line_no
        blank_count = 0
        idx = line_no - 2
        while idx >= 0 and lines[idx].strip() == "":
            blank_count += 1
            idx -= 1
        if blank_count >= 1:
            return None
        anchor_text = lines[line_no - 1].rstrip("\n")
        return self._single_edit(error, line_no, "insert_before", "\n",
                                 "add blank line between class methods", anchor_text)

    def _py_nested_def_blank(self, error, file_content) -> Optional[EditSet]:
        """E306: добавляет одну пустую строку перед вложенным определением."""
        line_no = int(error.get("line") or 0)
        if line_no < 2:
            return None
        lines = file_content.splitlines(keepends=True)
        if line_no > len(lines):
            return None
        blank_count = 0
        idx = line_no - 2
        while idx >= 0 and lines[idx].strip() == "":
            blank_count += 1
            idx -= 1
        if blank_count >= 1:
            return None
        anchor_text = lines[line_no - 1].rstrip("\n")
        return self._single_edit(error, line_no, "insert_before", "\n",
                                 "add blank line before nested definition", anchor_text)

    def _py_semicolon_end(self, error, file_content) -> Optional[EditSet]:
        """E703: убирает точку с запятой в конце оператора."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or not line.rstrip().endswith(";"):
            return None
        new_line = re.sub(r";\s*$", "", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove trailing semicolon", line)

    def _py_indent_multiple_of_4(self, error, file_content) -> Optional[EditSet]:
        """E111: приводит отступ к ближайшему кратному 4."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or line == line.lstrip():
            return None
        stripped = line.lstrip()
        current_indent = len(line) - len(stripped)
        new_indent = round(current_indent / 4) * 4
        if new_indent == current_indent:
            return None
        new_line = " " * new_indent + stripped
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 f"fix indentation {current_indent} → {new_indent} spaces", line)

    def _py_indent_comment(self, error, file_content) -> Optional[EditSet]:
        """E114: приводит отступ комментария к ближайшему кратному 4."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or not line.lstrip().startswith("#"):
            return None
        stripped = line.lstrip()
        current_indent = len(line) - len(stripped)
        new_indent = round(current_indent / 4) * 4
        if new_indent == current_indent:
            return None
        new_line = " " * new_indent + stripped
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 f"fix comment indentation {current_indent} → {new_indent}", line)

    def _py_tab_indent(self, error, file_content) -> Optional[EditSet]:
        """W191: заменяет табы на 4 пробела в начале строки."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or "\t" not in line:
            return None
        new_line = line.replace("\t", "    ")
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "replace tabs with 4 spaces", line)

    def _py_over_indented(self, error, file_content) -> Optional[EditSet]:
        """E117: убирает лишний отступ, выравнивая по предыдущей строке."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None or line == line.lstrip():
            return None
        if line_no < 2:
            return None
        prev_line = self._line_text(file_content, line_no - 1)
        if prev_line is None:
            return None
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        stripped = line.lstrip()
        new_line = " " * prev_indent + stripped
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "remove over-indentation", line)

    def _py_under_indented(self, error, file_content) -> Optional[EditSet]:
        """E115: добавляет недостающий отступ, выравнивая по предыдущей строке + 4."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        if line_no < 2:
            return None
        prev_line = self._line_text(file_content, line_no - 1)
        if prev_line is None:
            return None
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        expected_indent = prev_indent + 4
        stripped = line.lstrip()
        current_indent = len(line) - len(stripped)
        if current_indent >= expected_indent:
            return None
        new_line = " " * expected_indent + stripped
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 f"fix under-indentation: {current_indent} → {expected_indent}", line)

    def _py_remove_duplicate_lines(self, error, file_content) -> Optional[EditSet]:
        """Удаляет дубликаты строк/блоков. Два прохода:
        1) Смежные строки-дубли (быстрый regex-проход);
        2) Несмежные дубли `def`/`class` на топ-уровне через AST — оставляем
           ПЕРВОЕ определение, остальные удаляем целиком вместе с телом
           (по `lineno`..`end_lineno`).
        Если файл не парсится — выполняется только проход 1.
        """
        new_content = file_content

        # --- Проход 1: смежные дубли ---
        # 2026-07-08 (series5: starlette formparsers/requests, loguru
        # _colorizer, tests/test_exceptions_formatting): сравнение по
        # strip() считало «дублями» синтаксически РАЗНЫЕ строки —
        # вложенный `try:` под `try:` (разные отступы = разные блоки) и
        # смежные закрывающие `)` вложенных структур; удаление одной из
        # них делало файл невалидным (ast-предохранитель PreCleanup не
        # пускал на диск, но файл терял structured-фиксы до конца
        # прогона). Теперь: (а) сравнение СЫРЫХ строк — совпадать обязан
        # и отступ; (б) строки из одной пунктуации/скобок (закрывашки
        # `)`, `]`, `},` и т.п.) не дедупятся никогда — два одинаковых
        # закрытия подряд легитимны во вложенных литералах/вызовах.
        def _dedupable(raw: str) -> bool:
            s = raw.strip()
            if not s:
                return False
            if all(ch in ")]}(,:;" for ch in s):
                return False
            # 2026-07-09 (серия-8, tornado test/httpserver_test.py):
            # строки-ПРОДОЛЖЕНИЯ выражения легитимно повторяются подряд —
            # `+ newline` дважды (заголовок + пустая строка HTTP-протокола);
            # схлопывание тихо меняет семантику (содержимое строки), а в
            # классе рядом ломало и синтаксис. Строка, начинающаяся с
            # оператора/точки/запятой — продолжение, не самостоятельный
            # statement: не дедупим. Аналогично строка с хвостовым `\`.
            if s[0] in "+-*/%|&^.,@<>=~" or s.startswith(("or ", "and ", "not ", "in ", "is ")):
                return False
            if s.endswith("\\"):
                return False
            # 2026-07-09 (tornado): `yield self.read_response()` дважды
            # подряд — намеренное чтение ДВУХ ответов; yield/await-statement
            # повторяется легитимно, схлопывание тихо меняет семантику.
            if s.startswith(("yield", "await ")):
                return False
            return True

        lines = new_content.splitlines(keepends=True)
        cleaned: List[str] = []
        skipped = 0
        for i, line in enumerate(lines):
            if skipped > 0:
                skipped -= 1
                continue
            if (i + 1 < len(lines)
                    and lines[i].rstrip("\r\n") == lines[i + 1].rstrip("\r\n")
                    and _dedupable(lines[i])):
                cleaned.append(line)
                skipped = 1
            else:
                cleaned.append(line)
        new_content = "".join(cleaned)

        # --- Проход 2: несмежные дубли def/class через AST ---
        try:
            import ast
            tree = ast.parse(new_content)
        except SyntaxError:
            tree = None

        if tree is not None:
            # Соберём все def/class на топ-уровне И внутри классов
            def _is_overload(node) -> bool:
                for dec in getattr(node, "decorator_list", []):
                    dec_name = dec.id if isinstance(dec, ast.Name) else (
                        dec.attr if isinstance(dec, ast.Attribute) else None
                    )
                    if dec_name == "overload":
                        return True
                return False

            # 2026-07-07 (перемер httpx, encode/httpx): property-геттер и
            # его @<name>.setter/deleter ЛЕГИТИМНО делят одно имя метода —
            # это тот же класс проблемы, что и @overload выше (см. 2026-06-25
            # ниже), только через другой механизм языка (дескрипторный
            # протокол property вместо typing.overload). group by (kind, name)
            # считала `def request` (@property) и `def request` (@request.setter)
            # в httpx/_exceptions.py и httpx/_models.py дублями и удаляла
            # геттер (keep-last), что рвало property целиком: дальше атрибут
            # `request` переставал резолвиться на инстансах → каскад
            # фантомного F821 "undefined name 'request'", сгенерированного
            # самим движком, и мусорный retry-фикс поверх. PreCleanupStage
            # работает ДО ValidateStage — O.14 (missing_overload/symbol_regression)
            # этот путь не видит. Исключаем @property/@cached_property и
            # @<name>.setter/.deleter/.getter из дедупликации по имени так же,
            # как @overload — храним ВСЕ вхождения.
            def _is_property_accessor(node) -> bool:
                for dec in getattr(node, "decorator_list", []):
                    if isinstance(dec, ast.Name) and dec.id in ("property", "cached_property"):
                        return True
                    if isinstance(dec, ast.Attribute) and dec.attr in (
                        "setter", "deleter", "getter", "cached_property",
                    ):
                        return True
                return False

            def collect_defs(parent_body, parent_kind: str):
                # возвращает (name, (start_line, end_line), node)
                out = []
                for node in parent_body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        # 2026-07-08 (серия-7, attrs tests/test_dunders.py):
                        # node.lineno у декорированного def/class указывает на
                        # строку def/class, а декораторы стоят ВЫШЕ — диапазон
                        # удаления без них оставлял висячий `@attr.s(...)` над
                        # чужим кодом (invalid syntax). Начало диапазона —
                        # первая строка первого декоратора.
                        start = min(
                            [d.lineno for d in getattr(node, "decorator_list", [])]
                            + [node.lineno]
                        )
                        end = getattr(node, "end_lineno", start)
                        is_overload = (
                            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and (_is_overload(node) or _is_property_accessor(node))
                        )
                        out.append((node.name, start, end, parent_kind, is_overload))
                        if isinstance(node, ast.ClassDef):
                            # 2026-07-09 (серия-8, tornado test/*.py): ключ
                            # вложенности обязан включать ПОЛНУЮ родословную.
                            # Раньше parent_kind = f"class:{имя}" без пути —
                            # десятки РАЗНЫХ внешних классов с вложенным
                            # `class Handler` давали один ключ
                            # class:Handler::get, и keep-last стирал все
                            # ранние Handler.get через чужие классы.
                            out.extend(collect_defs(
                                node.body, f"{parent_kind}::class:{node.name}"
                            ))
                return out

            defs = collect_defs(tree.body, "module")
            # Группируем по (kind, name) — внутри одного класса не считаем дублем
            # с топ-левел функцией того же имени.
            #
            # 2026-06-23 (control series, RussellDash332/pytils): раньше тут
            # хранилось ПЕРВОЕ определение, удалялись остальные — backwards
            # относительно семантики Python. При повторном `def foo(...)` на
            # модульном уровне ИМЯ `foo` перепривязывается — реально
            # исполняется ПОСЛЕДНЕЕ определение, а все предыдущие уже мёртвый
            # код (shadowed). На pytils/fast_fourier_transform.py файл
            # содержал намеренно ДВА разных `def div` (вторая — расширенная
            # версия с остатком, комментарий "General division, possibly
            # with remainders") — старая логика удаляла ВТОРУЮ (реально
            # вызываемую) и хранила первую (мёртвую), стирая и вложенную
            # `sub`, меняя фактическое поведение кода для любого вызова
            # `div(...)` извне. Теперь храним диапазоны ВСЕХ вхождений и
            # удаляем все, КРОМЕ последнего.
            #
            # 2026-06-25 (контрольная серия на 10 проектах, agronholm/anyio):
            # @overload-декорированные функции ЛЕГИТИМНО делят одно имя
            # (typing.overload — единственный способ выразить несколько
            # сигнатур в Python) — эта проверка считала их "дублями" и
            # удаляла все, кроме последней (реальной реализации), теряя
            # 4 из 5 перегрузок connect_tcp/started/cache/... systemically
            # по всему проекту. PreCleanupStage работает ДО ValidateStage/
            # DecideStage — O.14 missing_overloads (analysis/symbol_regression.py)
            # никогда не видел этот путь. Теперь @overload-вхождения
            # полностью исключены из дедупликации по имени — храним ВСЕ.
            occurrences: Dict[str, List[tuple]] = {}
            for name, start, end, kind, is_overload in defs:
                if is_overload:
                    continue
                key = f"{kind}::{name}"
                occurrences.setdefault(key, []).append((start, end))
            to_remove: List[tuple] = []  # (start_line, end_line)
            for ranges in occurrences.values():
                if len(ranges) > 1:
                    to_remove.extend(ranges[:-1])

            if to_remove:
                # Сортируем по убыванию строки — удаляем с конца, чтобы индексы не съезжали
                to_remove.sort(key=lambda r: -r[0])
                ast_lines = new_content.splitlines(keepends=True)
                for start, end in to_remove:
                    # ast.lineno — 1-based, end_lineno включительно
                    del ast_lines[start - 1:end]
                new_content = "".join(ast_lines)

        if new_content == file_content:
            return None
        return EditSet(
            intent="remove duplicate lines and duplicate def/class blocks",
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=1, match=""),
                kind="replace_file",
                new=new_content,
                rationale="rule-based deterministic fix (adjacent + AST-based duplicates)",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    # Ключевые слова Python, которые НЕ могут быть посреди выражения —
    # их встреча после кода означает слипшуюся строку.
    _PY_STMT_KEYWORDS = (
        "def ", "class ", "async def ", "if ", "elif ", "else:", "for ", "while ",
        "try:", "except", "finally:", "with ", "return ", "return\n", "raise ",
        "import ", "from ", "yield ", "pass", "break", "continue", "global ",
        "nonlocal ", "assert ", "del ",
    )

    def _py_separate_slipped_lines(self, error, file_content) -> Optional[EditSet]:
        """Разъединяет слипшиеся строки. Работает в двух режимах:
        - если в `error` указан `line` — чинит одну строку (старый путь);
        - если `line` отсутствует или ==0 (PreCleanup path) — обходит файл целиком.

        Паттерны:
          1) код ⎵⎵+ код  (две инструкции через ≥2 пробела)
          2) код #коммент (без пробела перед #)
          3) код def/class/if/for/...  (статемент-keyword без переноса)
          4) код // FIXME ...          (JS-style комментарий — отдельная строка)
          5) statement1;statement2     (через `;`)
        """
        line_no = int(error.get("line") or 0)

        if line_no > 0:
            line = self._line_text(file_content, line_no)
            if line is None:
                return None
            new_line = self._split_one_slipped_line(line)
            if new_line is None or new_line == line:
                return None
            return self._single_edit(error, line_no, "replace", new_line,
                                     "separate slipped statements on one line", line)

        # Whole-file path (PreCleanup) — пробежимся и починим всё, что найдём.
        out_lines: List[str] = []
        changed = False
        for raw_line in file_content.splitlines(keepends=True):
            split = self._split_one_slipped_line(raw_line)
            if split is not None and split != raw_line:
                out_lines.append(split)
                changed = True
            else:
                out_lines.append(raw_line)
        if not changed:
            return None
        new_content = "".join(out_lines)
        return EditSet(
            intent="separate slipped statements/comments across the file",
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=1, match=""),
                kind="replace_file",
                new=new_content,
                rationale="rule-based deterministic fix (whole-file slipped-line pass)",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    def _split_one_slipped_line(self, line: str) -> Optional[str]:
        """Возвращает строку с разделёнными слипшимися частями (с `\\n` между ними)
        или None, если ничего не нашли. Сохраняет исходный отступ."""
        # Сохраняем хвостовой перевод строки, работаем с телом.
        tail = ""
        body = line
        if body.endswith("\r\n"):
            tail = "\r\n"
            body = body[:-2]
        elif body.endswith("\n"):
            tail = "\n"
            body = body[:-1]

        indent_m = re.match(r"^(\s*)", body)
        indent = indent_m.group(1) if indent_m else ""
        rest = body[len(indent):]
        if not rest:
            return None

        # 4) JS-style комментарий LLM-шума: `code // FIXME: line N` или `code // ERROR:`
        # ВАЖНО: `//` в Python — floor division, а не комментарий.
        # Ловим ТОЛЬКО специфичный LLM-шум: `// FIXME`, `// ERROR`, `// TODO`.
        m = re.search(r"\s*//\s+(FIXME|ERROR|TODO|NOTE)\b.*$", rest, re.IGNORECASE)
        if m:
            code_part = rest[:m.start()].rstrip()
            comment_part = rest[m.start():].lstrip()
            if code_part and comment_part:
                return f"{indent}{code_part}{tail}"

        # 5) statement; statement (стандартный E702/E703) — разделим
        # 2026-07-08: строки в line-continuation контексте не сплитим —
        # хвостовой `\` значит, что statement продолжается на следующей
        # строке (разрез посреди него ломает синтаксис), ведущая `;`
        # значит, что мы сами — продолжение (fixture loguru
        # tests/exceptions/source/diagnose/parenthesis.py).
        if rest.rstrip().endswith("\\") or rest.lstrip().startswith(";"):
            return None
        if ";" in rest and not rest.lstrip().startswith("#"):
            # Простая (без учёта строк/комментариев) эвристика; пропустим если ; в строке/коммента
            if not self._semicolon_in_string_or_comment(rest):
                parts = [p.strip() for p in rest.split(";") if p.strip()]
                if len(parts) >= 2:
                    joined = ("\n" + indent).join(parts)
                    return f"{indent}{joined}{tail}"

        # 3) keyword-statement без переноса: `…)def add_note():`
        # unsafe — позиции внутри строковых литералов/комментариев в `rest`.
        # Без этой проверки `kw` типа "from"/"import", встретившееся ВНУТРИ
        # строкового литерала (например 'from common Python development
        # tools.'), считалось слипшимся statement'ом и резало строку прямо
        # после открывающей кавычки — портя файл (unterminated string).
        unsafe = self._string_ranges(rest)
        for kw in self._PY_STMT_KEYWORDS:
            # ищем kw где-то после первого символа кода; начало строки — НЕ слип
            # (если это просто `def …` на пустой строке — не делим).
            idx = rest.find(kw, 1)
            while idx != -1:
                if idx in unsafe:
                    idx = rest.find(kw, idx + 1)
                    continue
                # Перед kw должен быть НЕ-пробельный символ (значит слиплось).
                prev = rest[idx - 1]
                if not prev.isspace():
                    code_part = rest[:idx].rstrip()
                    second_part = rest[idx:].lstrip()
                    if code_part and second_part:
                        # Не разделяем если перед kw идёт идентификатор-продолжение
                        # (например `endif`/`returncode`). Проверка: символ перед kw
                        # должен НЕ быть alnum/underscore.
                        #
                        # 2026-07-08 (series5: starlette ~10 файлов, loguru
                        # _colorizer/_simple_sinks, tests/*): kw матчился как
                        # ПОДСТРОКА без границы слова СПРАВА — в
                        # `middleware.exceptions` находился `except` (внутри
                        # слова "exceptions"), слева точка (не alnum) → строка
                        # резалась посреди dotted-пути импорта, файл становился
                        # синтаксически невалидным (ast-предохранитель
                        # PreCleanup не пускал на диск, но файл терял все
                        # structured-фиксы до конца прогона). Две новые
                        # проверки: (а) после kw — граница слова (не alnum/_);
                        # (б) перед kw не точка: после `.` всегда имя
                        # атрибута/модуля, statement-keyword там невозможен.
                        # kw из _PY_STMT_KEYWORDS в большинстве уже содержат
                        # правую границу ("def ", "try:", "return\n") — для
                        # них проверка не нужна; она нужна «голым» kw
                        # (except/pass/break/continue), которые матчатся
                        # внутри идентификаторов (exceptions, passthrough).
                        if kw[-1].isalnum() or kw[-1] == "_":
                            _kw_end = idx + len(kw)
                            _nxt = rest[_kw_end] if _kw_end < len(rest) else " "
                            _word_boundary_right = not (_nxt.isalnum() or _nxt == "_")
                        else:
                            _word_boundary_right = True
                        if (not (prev.isalnum() or prev == "_")
                                and prev != "."
                                and _word_boundary_right):
                            return f"{indent}{code_part}\n{indent}{second_part}{tail}"
                idx = rest.find(kw, idx + 1)

        # 1) код  ⎵⎵+ код (две инструкции через много пробелов)
        m = re.match(r'^(.*?[a-zA-Z0-9_)\]}"\'])\s{2,}([a-zA-Z0-9_@\["].*)$', rest)
        if m:
            space_pos = len(m.group(1))  # позиция начала пробелов в `rest`
            if space_pos not in self._string_ranges(rest):
                first_part = m.group(1).rstrip()
                second_part = m.group(2).lstrip()
                # 2026-07-08 (серия-6, dateutil tests/test_imports.py):
                # `assert weekday is not  None` — авторский ДВОЙНОЙ пробел
                # внутри выражения; «⎵⎵ = слиплось» резало легитимный
                # statement посередине. У настоящего слипа ОБЕ части —
                # самостоятельные валидные statement-ы; у разрезанного
                # выражения первая часть («assert weekday is not») не
                # парсится. compile() на строку — микросекунды, и только
                # при regex-матче.
                try:
                    compile(first_part.strip(), "<slip1>", "exec")
                    compile(second_part.strip(), "<slip2>", "exec")
                except SyntaxError:
                    pass
                else:
                    return f"{indent}{first_part}\n{indent}{second_part}{tail}"

        # 2) код#комментарий (без пробела перед #)
        m = re.match(r'^([^#]*?\S)(#.*)$', rest)
        if m:
            hash_pos = m.start(2)
            if not self._pos_inside_open_quote(rest, hash_pos):
                code_part = m.group(1).rstrip()
                comment_part = m.group(2)
                if code_part and not code_part.lstrip().startswith(("'", '"')):
                    return f"{indent}{code_part}\n{indent}{comment_part}{tail}"

        return None

    @staticmethod
    def _semicolon_in_string_or_comment(s: str) -> bool:
        """Очень простая проверка: есть ли `;` внутри строкового литерала
        или после `#`. Не AST, но дёшево и достаточно для типичных случаев.

        Если за весь проход НЕ встретилось ни одного `;` вне строки/комментария —
        значит все `;` в этой строке (а вызывающий код уже убедился, что хотя бы
        один есть) были "укрыты" строковым литералом, и делить строку нельзя.
        Раньше здесь был `return False` — то есть строки вида
        `' ...; ...; ...'` (целиком строковый литерал, например текст
        argparse help) считались "безопасными для split" и ломались."""
        in_str = False
        quote = ""
        for i, ch in enumerate(s):
            if in_str:
                if ch == quote and (i == 0 or s[i - 1] != "\\"):
                    in_str = False
                continue
            if ch in ('"', "'"):
                in_str = True
                quote = ch
                continue
            if ch == "#":
                # 2026-07-08 (starlette tests/test_formparsers.py, серия-6
                # контроль): дойдя до `#`, возвращали `';' in s[i:]` —
                # наличие `;` В КОММЕНТЕ. Для строки
                # `b'...; ...; ...'  # type: ignore` (все `;` в литерале,
                # в комменте нет) это давало False → вызывающий сплитил по
                # `;` ВНУТРИ bytes-литерала. Раз мы дошли до комментария,
                # не встретив голой `;`, — вся строка безопасна: и
                # строковые `;`, и комментные делить нельзя.
                return True
            if ch == ";":
                return False
        return True

    @staticmethod
    def _string_ranges(line: str) -> frozenset:
        """Возвращает frozenset позиций (0-based) внутри строковых литералов
        или комментария. Guard для regex-замен: если позиция совпадения входит
        в это множество — замена небезопасна и надо вернуть None."""
        positions: set = set()
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch in ('"', "'"):
                q = ch
                if line[i:i + 3] == q * 3:
                    start = i; i += 3
                    while i < n:
                        if line[i] == "\\" and i + 1 < n: i += 2; continue
                        if line[i:i + 3] == q * 3: i += 3; break
                        i += 1
                else:
                    start = i; i += 1
                    while i < n:
                        if line[i] == "\\" and i + 1 < n: i += 2; continue
                        if line[i] == q: i += 1; break
                        i += 1
                positions.update(range(start, i))
            elif ch == "#":
                positions.update(range(i, n)); break
            else:
                i += 1
        return frozenset(positions)

    @staticmethod
    def _pos_inside_open_quote(line: str, pos: int) -> bool:
        """True, если `pos` находится внутри ЕЩЁ НЕ закрытого строкового
        литерала при сканировании `line` слева до `pos`. В отличие от
        `_string_ranges`, различает «строка уже закрылась перед pos»
        и «строка не закрылась» — это нужно для `#`, который может стоять
        сразу после закрывающей кавычки (валидный комментарий) или внутри
        текста строки (ложный комментарий, нельзя резать строку)."""
        i = 0
        n = min(pos, len(line))
        in_str = False
        quote = ""
        triple = False
        while i < n:
            ch = line[i]
            if in_str:
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if triple:
                    if line[i:i + 3] == quote * 3:
                        in_str = False
                        i += 3
                        continue
                else:
                    if ch == quote:
                        in_str = False
                        i += 1
                        continue
                i += 1
                continue
            if ch in ('"', "'"):
                in_str = True
                quote = ch
                triple = line[i:i + 3] == ch * 3
                i += 3 if triple else 1
                continue
            i += 1
        return in_str

    def _py_remove_dead_code_after_return(self, error, file_content) -> Optional[EditSet]:
        """Удаляет мёртвый код после return/raise/continue/break ВНУТРИ ФУНКЦИИ.

        Через AST: для каждого FunctionDef/AsyncFunctionDef проходим тело и
        тела вложенных блоков (if/for/while/with/try); в каждом блоке после
        первого терминирующего statement (Return/Raise/Continue/Break) всё
        ОСТАЛЬНОЕ — мёртвый код, удаляем. Удаление аккуратное по line-диапазону
        (используем `lineno`/`end_lineno`).

        Если файл не парсится — возвращаем None (мёртвый код в битом файле
        безопасно НЕ трогать, иначе сломаем больше, чем починим).
        """
        try:
            import ast
            tree = ast.parse(file_content)
        except SyntaxError:
            return None

        ranges_to_remove: List[tuple] = []  # (start_lineno, end_lineno) — 1-based incl.

        # Терминирующие statement'ы
        TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)

        def process_block(stmts: List[ast.stmt]) -> None:
            """Идёт по списку statement'ов одного блока, рекурсивно заходит
            в составные блоки. Если встретил терминирующий — оставшиеся в
            ЭТОМ ЖЕ блоке помечает на удаление."""
            terminator_idx: Optional[int] = None
            for i, stmt in enumerate(stmts):
                if isinstance(stmt, TERMINATORS):
                    terminator_idx = i
                    break
                # Рекурсивный заход в тела вложенных блоков
                for attr in ("body", "orelse", "finalbody"):
                    inner = getattr(stmt, attr, None)
                    if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                        process_block(inner)
                # handlers у Try
                handlers = getattr(stmt, "handlers", None)
                if isinstance(handlers, list):
                    for h in handlers:
                        if hasattr(h, "body"):
                            process_block(h.body)
            if terminator_idx is not None and terminator_idx < len(stmts) - 1:
                # Мёртвый код = stmts[terminator_idx+1:]
                dead_start = stmts[terminator_idx + 1].lineno
                dead_end = stmts[-1].end_lineno or stmts[-1].lineno
                ranges_to_remove.append((dead_start, dead_end))

        # Запускаем только для тел функций (мёртвый код на топ-уровне модуля
        # может быть «вызов после возврата из main»-стилем — не трогаем).
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                process_block(node.body)

        if not ranges_to_remove:
            return None

        # Удаляем диапазоны с конца, чтобы не сдвигать индексы.
        ranges_to_remove.sort(key=lambda r: -r[0])
        lines = file_content.splitlines(keepends=True)
        for start, end in ranges_to_remove:
            del lines[start - 1:end]
        new_content = "".join(lines)
        if new_content == file_content:
            return None

        return EditSet(
            intent="remove dead code after return/raise/break/continue",
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=1, match=""),
                kind="replace_file",
                new=new_content,
                rationale="rule-based deterministic fix",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    # -----------------------------------------------------------------
    # LLM-noise: маркеры из промпта, попавшие в код
    # -----------------------------------------------------------------
    # Паттерны, которые НИКОГДА не появляются в нормальном Python-файле,
    # но регулярно вылезают от LLM, когда модель «забывает» что вставляет в
    # файл и переносит туда часть собственного prompt'а.
    LLM_NOISE_LINE_PATTERNS = (
        re.compile(r"^\s*#\s*(---\s*)?ALL\s+ERRORS\s+IN\s+THIS\s+FILE", re.IGNORECASE),
        re.compile(r"^\s*#\s*---\s*ALL\s+ERRORS\s*---", re.IGNORECASE),
        re.compile(r"^\s*#\s*ERRORS\s+RELEVANT\s+TO\s+THIS\s+SEGMENT", re.IGNORECASE),
        re.compile(r"^\s*#\s*GLOBAL\s+CONTEXT", re.IGNORECASE),
        re.compile(r"^\s*#\s*SEGMENT\s+TO\s+FIX", re.IGNORECASE),
        re.compile(r"^\s*#\s*FILE\s+CONTEXT", re.IGNORECASE),
        re.compile(r"^\s*#\s*EXAMPLE\s*\(\s*REFERENCE\s+ONLY\s*\)", re.IGNORECASE),
        re.compile(r"^\s*#\s*ABSOLUTE\s+RULES", re.IGNORECASE),
        re.compile(r"^\s*#\s*OUTPUT\s+FORMAT", re.IGNORECASE),
        re.compile(r"^\s*#\s*ERROR\s+TO\s+FIX", re.IGNORECASE),
        # JS-style маркеры от LLM
        re.compile(r"^\s*//\s*FIXME[:\s]\s*line\s+\d+", re.IGNORECASE),
        # Перечисления вида «# Line 16: [E501] line too long» подряд (≥2 строки).
        # Сами по себе одиночные такие строки трогаем ОТДЕЛЬНО через `_LINE_REF_RE`
        # — см. ниже.
    )
    _LLM_LINE_REF_RE = re.compile(
        r"^\s*#\s*Line\s+\d+\s*:\s*\[[A-Z]\d+\]\s+",
        re.IGNORECASE,
    )

    def _py_remove_llm_noise(self, error, file_content) -> Optional[EditSet]:
        """Удаляет строки-артефакты, оставленные LLM:
        - блоки промпта, попавшие в файл («ALL ERRORS IN THIS FILE»,
          «SEGMENT TO FIX», «ABSOLUTE RULES», «OUTPUT FORMAT» и т.п.);
        - JS-style маркеры (`// FIXME: line N, ...`);
        - подряд идущие строки `# Line N: [E…] message` (≥2 подряд).
        """
        lines = file_content.splitlines(keepends=True)
        if not lines:
            return None

        # 1) Линейный проход для одиночных маркеров.
        mark_remove = [False] * len(lines)
        for i, raw in enumerate(lines):
            for pat in self.LLM_NOISE_LINE_PATTERNS:
                if pat.search(raw):
                    mark_remove[i] = True
                    break

        # 2) Блоки `# Line N: [E…] ...` — удаляем когда подряд ≥2 такие строки.
        i = 0
        while i < len(lines):
            if self._LLM_LINE_REF_RE.match(lines[i]):
                j = i
                while j < len(lines) and self._LLM_LINE_REF_RE.match(lines[j]):
                    j += 1
                if j - i >= 2:
                    for k in range(i, j):
                        mark_remove[k] = True
                i = j
            else:
                i += 1

        if not any(mark_remove):
            return None

        new_lines = [l for i, l in enumerate(lines) if not mark_remove[i]]
        new_content = "".join(new_lines)
        if new_content == file_content:
            return None
        return EditSet(
            intent="remove LLM prompt-noise lines that leaked into source",
            edits=[Edit(
                file=error.get("file", ""),
                anchor=Anchor(line=1, match=""),
                kind="replace_file",
                new=new_content,
                rationale="rule-based deterministic fix (LLM noise scrub)",
            )],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    @staticmethod
    def contains_llm_noise(file_content: str) -> bool:
        """Быстрая проверка: есть ли в тексте маркеры LLM-промпта.
        Используется в ApplyPatchStage как guard — патч, который ДОБАВИЛ в
        файл такие строки, отклоняется без применения."""
        if not file_content:
            return False
        for pat in RuleBasedFixer.LLM_NOISE_LINE_PATTERNS:
            if pat.search(file_content):
                return True
        # ≥2 подряд `# Line N: [E…] …` — тоже шум.
        count = 0
        for line in file_content.splitlines():
            if RuleBasedFixer._LLM_LINE_REF_RE.match(line):
                count += 1
                if count >= 2:
                    return True
            else:
                count = 0
        return False

    # -----------------------------------------------------------------
    # Python security: hardcoded_secret
    # -----------------------------------------------------------------
    _SECRET_LITERAL_RE = re.compile(
        r"""^(?P<lead>\s*)(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*["'][^"']{4,}["']\s*(?:\#.*)?$"""
    )

    def _py_hardcoded_secret(self, error, file_content) -> Optional[EditSet]:
        """Security: `SECRET = "literal"` → `SECRET = os.environ["SECRET"]`.

        Чиним строго каноничный случай:
          * именованный константный литерал (UPPER_CASE = "..."),
          * значение — литерал длиной ≥4 (не f-string, не env-lookup).
        Если ничего не подходит — None.

        Дополнительно добавляем `import os` в самом верху файла, если его нет.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        m = self._SECRET_LITERAL_RE.match(line)
        if not m:
            return None
        name = m.group("name")
        lead = m.group("lead")
        new_line = f'{lead}{name} = os.environ["{name}"]'
        if new_line == line:
            return None

        edits = []
        # `import os` если его нет НИГДЕ в файле (не только в первых 30 строках —
        # иначе на больших файлах с импортом os после header вставим дубль).
        # Ловим оба варианта: `import os`, `import os, sys`, `import os as o`.
        has_import_os = re.search(
            r"^\s*import\s+os(\s*,\s*\w+)*\s*(?:as\s+\w+)?\s*(?:#.*)?$",
            file_content, re.MULTILINE,
        )
        if not has_import_os:
            # Вставляем `import os` перед первым существующим import/from чтобы
            # сохранить правильный порядок импортов (stdlib перед third-party).
            # Если импортов нет — вставляем перед самой константой.
            first_import_no = None
            first_import_match = None
            for ln_i, ln_text in enumerate(file_content.splitlines(), 1):
                stripped = ln_text.lstrip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    first_import_no = ln_i
                    first_import_match = stripped.rstrip()
                    break
            if first_import_no is not None:
                edits.append(Edit(
                    file=error.get("file", ""),
                    anchor=Anchor(line=first_import_no, match=first_import_match),
                    kind="insert_before",
                    new="import os\n",
                    rationale="add `import os` for env-var lookup",
                ))
            else:
                edits.append(Edit(
                    file=error.get("file", ""),
                    anchor=Anchor(line=max(1, line_no), match=name),
                    kind="insert_before",
                    new="import os\n",
                    rationale="add `import os` for env-var lookup",
                ))
        edits.append(Edit(
            file=error.get("file", ""),
            anchor=Anchor(line=line_no, match=line.strip()),
            kind="replace",
            new=new_line + "\n",
            rationale="replace hardcoded secret with environment variable lookup",
        ))
        return EditSet(
            intent="replace hardcoded secret with os.environ lookup",
            edits=edits,
            confidence=RULE_CONFIDENCE,
            risks=["startup fails if env var is missing — set it before running"],
        )

    # -----------------------------------------------------------------
    # Python security: dangerous_eval — превращаем `eval(input(...))` в
    # безопасную форму с комментарием, чтобы пайплайн не зацикливался и
    # пользователь увидел явное место для ручного решения.
    # -----------------------------------------------------------------
    _EVAL_INPUT_RE = re.compile(r"""eval\s*\(\s*input\s*\(([^)]*)\)\s*\)""")

    def _py_dangerous_eval(self, error, file_content) -> Optional[EditSet]:
        """Security: `eval(input(...))` → `# DISABLED unsafe eval: input(...)`.

        Чиним ТОЛЬКО самую опасную форму: `eval(input(...))` (выполнение
        произвольного пользовательского ввода). Произвольный `eval(x)` или
        `eval(expr_var)` оставляем LLM/NEEDS_REVIEW — там нужен контекст.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        match = self._EVAL_INPUT_RE.search(line)
        if not match:
            return None
        prompt_arg = match.group(1).strip()
        indent = " " * (len(line) - len(line.lstrip()))
        replacement = (
            f"{indent}# SECURITY: unsafe eval over user input removed — replace with explicit parsing\n"
            f"{indent}input({prompt_arg})"
        )
        # Заменяем ровно одну строку — на две (комментарий + чистый input).
        return self._single_edit(
            error, line_no, "replace", replacement + "\n",
            "neutralize eval(input(...)) — explicit safe parsing required",
            line.strip(),
        )

    # -----------------------------------------------------------------
    # Python security: hashlib.sha1 → hashlib.sha256 (insecure-hash-algorithm-sha1)
    # -----------------------------------------------------------------
    _SHA1_CALL_RE = re.compile(r"\bhashlib\.sha1\b")

    def _py_sha1_to_sha256(self, error, file_content) -> Optional[EditSet]:
        """Security: `hashlib.sha1(...)` → `hashlib.sha256(...)`.

        2026-06-25 (learning_cases.jsonl): простая детерминированная замена
        имени функции, аргументы вызова (включая usedforsecurity=False)
        остаются нетронутыми — semgrep сам рекомендует именно sha256 как
        замену. Чиним ТОЛЬКО прямой вызов `hashlib.sha1(` на строке с
        ошибкой — если паттерна нет (например алиасированный импорт
        `from hashlib import sha1` без префикса `hashlib.`), отступаем
        (None) — LLM/NEEDS_REVIEW справится с контекстом лучше.
        """
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        if not self._SHA1_CALL_RE.search(line):
            return None
        new_line = self._SHA1_CALL_RE.sub("hashlib.sha256", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(
            error, line_no, "replace", new_line + "\n",
            "replace insecure hashlib.sha1 with hashlib.sha256",
            line,
        )

    # -----------------------------------------------------------------
    # F821: undefined name — исправляем опечатку через ближайший defined name
    # -----------------------------------------------------------------
    def _py_f821_typo_fix(self, error, file_content) -> Optional[EditSet]:
        """F821: заменяет undefined name если он — опечатка известного имени.

        Алгоритм: парсим файл AST, собираем все определённые имена,
        ищем ближайшее к undefined через difflib (cutoff 0.82).
        При уверенном совпадении заменяем в строке ошибки.
        При отсутствии кандидата — None (не угадываем).
        """
        import ast as _ast
        import difflib

        line_no = int(error.get("line") or 0)
        if line_no < 1:
            return None

        msg = error.get("message", "")
        m = re.search(r"undefined name ['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", msg)
        if not m:
            return None
        undefined = m.group(1)

        try:
            tree = _ast.parse(file_content)
        except SyntaxError:
            return None

        defined: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Store):
                defined.add(node.id)
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    defined.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                for alias in node.names:
                    defined.add(alias.asname or alias.name)
        defined.discard(undefined)

        if not defined:
            return None

        matches = difflib.get_close_matches(undefined, defined, n=1, cutoff=0.82)
        if not matches:
            return None
        correct = matches[0]

        line = self._line_text(file_content, line_no)
        if line is None or undefined not in line:
            return None

        new_line = line.replace(undefined, correct, 1)
        if new_line == line:
            return None

        return self._single_edit(
            error, line_no, "replace", new_line,
            f"fix typo in name: {undefined!r} -> {correct!r}",
            line.rstrip("\n\r"),
        )

    # -----------------------------------------------------------------
    # P.4: ruff --fix как авто-фикс для широкого набора кодов
    # -----------------------------------------------------------------
    def _py_ruff_autofix(self, error, file_content) -> Optional[EditSet]:
        """Делегирует фикс инструменту `ruff check --fix --select=<code>`."""
        if not shutil.which("ruff"):
            return None
        code = (error.get("code") or "").strip()
        if not code:
            return None
        file_rel = error.get("file") or ""
        if not file_rel:
            return None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_file = Path(tmp) / Path(file_rel).name
                tmp_file.write_text(file_content, encoding="utf-8")
                subprocess.run(
                    ["ruff", "check", "--fix",
                     f"--select={code}", "--no-cache",
                     str(tmp_file)],
                    capture_output=True, text=True, timeout=30,
                )
                if not tmp_file.exists():
                    return None
                fixed = tmp_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.debug("ruff --fix failed for %s: %s", code, e)
            return None
        if fixed == file_content:
            return None
        edit = Edit(
            file=file_rel,
            anchor=Anchor(line=1, match=""),
            kind="replace_file",
            new=fixed,
            rationale=f"ruff --fix --select={code} produced canonical fix",
        )
        return EditSet(
            intent=f"ruff auto-fix: {code}",
            edits=[edit],
            confidence=RULE_CONFIDENCE,
            risks=[],
        )

    # -----------------------------------------------------------------
    # JavaScript
    # -----------------------------------------------------------------
    def _js_eqeqeq(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        new_line = re.sub(r"(?<![=!<>])(===?|!==?)(?!=)",
                          lambda m: {"==": "===", "!=": "!=="}.get(m.group(1), m.group(1)),
                          line)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "use strict equality", line)

    def _js_no_var(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        new_line, n = re.subn(r"\bvar\b", "let", line, count=1)
        if n == 0:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "replace var with let", line)

    def _js_prefer_const(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        new_line, n = re.subn(r"\blet\b", "const", line, count=1)
        if n == 0:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "use const for never-reassigned binding", line)

    def _js_no_unused_vars(self, error, file_content) -> Optional[EditSet]:
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        if not re.match(r"^\s*(const|let|var)\s+\w+\s*=", line):
            return None
        return self._single_edit(error, line_no, "delete", "",
                                 "remove unused variable declaration", line)

    def _js_xss_textcontent(self, error, file_content) -> Optional[EditSet]:
        """Security: X.innerHTML = Y -> X.textContent = Y."""
        line_no = int(error.get("line") or 0)
        line = self._line_text(file_content, line_no)
        if line is None:
            return None
        if not re.search(r"\.innerHTML\s*=(?!=)", line):
            return None
        new_line = re.sub(r"\.innerHTML(\s*=(?!=))", r".textContent\1", line, count=1)
        if new_line == line:
            return None
        return self._single_edit(error, line_no, "replace", new_line + "\n",
                                 "assign as text (textContent) to prevent XSS", line)
