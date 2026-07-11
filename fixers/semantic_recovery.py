"""
E999 Semantic Recovery — экспериментальная стратегия 4-го уровня (2026-06-21).

Идея пользователя: когда стандартные репаранты (PythonSyntaxHealer, tokenize
unclosed-bracket fix, localized LLM-region patch — см.
core/stages/syntax_repair_stage.py) не справляются с каскадными синтаксическими
ошибками (AST не строится вовсе), вместо точечного патчинга — реставрировать
файл ЦЕЛИКОМ по его логике, гарантируя сохранение:
  - имён классов;
  - имён функций и методов;
  - сигнатур функций;
  - публичных интерфейсов;
  - существующих импортов (если они не источник ошибки).

Разрешается менять: внутреннюю реализацию, структуру блоков, отступы,
синтаксис, расположение кода внутри функций.

Опасность: LLM может тихо поменять ЛОГИКУ функции, оставив сигнатуру прежней
("нарисовать кота вместо Моны Лизы"). Эта проверка — НЕ здесь: сигнатурный
дифф (signatures_match) детерминирован и ловит только структурные расхождения;
проверку сохранения СМЫСЛА делает SocraticRefiner.compare_logic (LLM-судья) —
см. tools/socratic_refiner.py — на уровне ReviewStage.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class LogicSnapshot:
    """Слепок «контракта» файла — то, что обязательно сохранить.

    Извлекается ДО реконструкции (из СЛОМАННОГО файла — поэтому best-effort,
    regex/tokenize-based, а не AST-based: AST на сломанном файле не строится
    по определению этого сценария).
    """
    classes: Set[str] = field(default_factory=set)
    functions: Dict[str, Tuple[str, ...]] = field(default_factory=dict)  # name -> arg names
    imports: Set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not self.classes and not self.functions and not self.imports


_CLASS_RE = re.compile(r'^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:\(]', re.MULTILINE)
_DEF_RE = re.compile(r'^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)', re.MULTILINE)
_IMPORT_RE = re.compile(r'^[ \t]*(?:import[ \t]+([A-Za-z0-9_.,\t ]+)|from[ \t]+([A-Za-z0-9_.]+)[ \t]+import)', re.MULTILINE)


def _parse_arg_names(arg_str: str) -> Tuple[str, ...]:
    """Best-effort извлечение имён аргументов из сырой строки сигнатуры.
    Не пытается понять типы/дефолты — только имена, для сравнения «не
    потеряли ли параметр»."""
    names: List[str] = []
    for part in arg_str.split(","):
        part = part.strip()
        if not part or part in ("self", "cls"):
            continue
        # Снимаем default (=...), type hint (:...), *args/**kwargs префиксы.
        name = part.split("=")[0].split(":")[0].strip().lstrip("*").strip()
        if name:
            names.append(name)
    return tuple(names)


def extract_logic_snapshot(content: str) -> LogicSnapshot:
    """Извлекает слепок контракта из (возможно сломанного) исходника.

    Regex-based намеренно — рассчитан на работу даже когда ast.parse падает
    (это и есть условие применения всей стратегии)."""
    snapshot = LogicSnapshot()
    for m in _CLASS_RE.finditer(content):
        snapshot.classes.add(m.group(1))
    for m in _DEF_RE.finditer(content):
        name = m.group(1)
        snapshot.functions[name] = _parse_arg_names(m.group(2))
    for m in _IMPORT_RE.finditer(content):
        mod = (m.group(1) or m.group(2) or "").strip()
        if mod:
            for piece in mod.split(","):
                piece = piece.strip().split(" as ")[0].strip()
                if piece:
                    snapshot.imports.add(piece)
    return snapshot


def extract_signatures_ast(content: str) -> Optional[LogicSnapshot]:
    """AST-based извлечение — требует ВАЛИДНОГО синтаксиса (для проверки
    РЕЗУЛЬТАТА реконструкции, не исходного сломанного файла). Возвращает
    None, если файл всё ещё не парсится."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    snapshot = LogicSnapshot()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            snapshot.classes.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
            snapshot.functions[node.name] = tuple(args)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                snapshot.imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                snapshot.imports.add(node.module.split(".")[0])
    return snapshot


def signatures_match(old: LogicSnapshot, new: LogicSnapshot) -> Tuple[bool, List[str]]:
    """Детерминированное сравнение слепков. Возвращает (совпали?, причины
    расхождения). Импорты сравниваются МЯГКО (новые/удалённые лишние импорты
    — не блокер сами по себе, реконструкция может убрать source ошибки;
    главное — классы/функции/сигнатуры на месте)."""
    mismatches: List[str] = []

    missing_classes = old.classes - new.classes
    if missing_classes:
        mismatches.append(f"потеряны классы: {sorted(missing_classes)}")

    missing_funcs = set(old.functions) - set(new.functions)
    if missing_funcs:
        mismatches.append(f"потеряны функции/методы: {sorted(missing_funcs)}")

    for name in set(old.functions) & set(new.functions):
        old_args, new_args = old.functions[name], new.functions[name]
        if old_args != new_args:
            mismatches.append(
                f"сигнатура {name}() изменилась: {old_args} -> {new_args}"
            )

    return (not mismatches, mismatches)


_FENCE_RE = re.compile(r'^```[a-zA-Z]*\n|```\s*$', re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def build_reconstruction_prompt(broken_content: str, snapshot: LogicSnapshot,
                                error: Dict[str, Any], language: str) -> str:
    classes_str = ", ".join(sorted(snapshot.classes)) or "(none)"
    funcs_str = ", ".join(
        f"{name}({', '.join(args)})" for name, args in sorted(snapshot.functions.items())
    ) or "(none)"
    imports_str = ", ".join(sorted(snapshot.imports)) or "(none)"

    return (
        f"The following {language} file has cascading syntax errors and cannot be parsed "
        f"(AST construction fails). Standard repair heuristics (bracket matching, "
        f"localized line fixes) were unable to fix it.\n\n"
        f"Your task: reconstruct this file as VALID {language} code, preserving the "
        f"AUTHOR'S INTENT as closely as possible.\n\n"
        f"MUST preserve exactly (these will be verified automatically):\n"
        f"- Class names: {classes_str}\n"
        f"- Function/method names and signatures: {funcs_str}\n"
        f"- Existing imports (unless they are themselves the source of the syntax error): {imports_str}\n\n"
        f"You MAY freely change: internal implementation, block structure, indentation, "
        f"syntax details, code layout within function bodies.\n\n"
        f"Error context: {error.get('code', '')} at line {error.get('line', '?')}: "
        f"{error.get('message', '')}\n\n"
        f"Broken file content:\n```{language}\n{broken_content}\n```\n\n"
        f"Output ONLY the complete reconstructed file content, no explanation, no markdown fences."
    )


def reconstruct_file(
    llm_client: Any, broken_content: str, snapshot: LogicSnapshot,
    error: Dict[str, Any], language: str = "python",
) -> Optional[str]:
    """Запрашивает у LLM полную реконструкцию файла. Возвращает новый
    контент (если он валиден как Python) или None (LLM недоступен/ответ
    пуст/результат всё ещё не парсится)."""
    if llm_client is None or not getattr(llm_client, "providers", None):
        return None
    prompt = build_reconstruction_prompt(broken_content, snapshot, error, language)
    try:
        response = llm_client._call_llm(prompt)
    except Exception as e:
        logger.warning("semantic_recovery: LLM вызов упал: %s", e)
        return None
    if not response:
        return None
    new_content = _strip_code_fences(response)
    if not new_content.strip():
        return None
    try:
        ast.parse(new_content)
    except SyntaxError as e:
        logger.warning("semantic_recovery: реконструкция всё ещё не парсится: %s", e)
        return None
    return new_content
