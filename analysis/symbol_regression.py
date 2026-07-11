"""
O.14 (regression) + O.17 (duplication) — символьные проверки после патча.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from typing import Any, Dict, List, Set, Tuple


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


def _scan(pattern: str, source: str) -> Set[str]:
    return set(re.findall(pattern, source))


# --- Regex-фолбэк для синтаксически невалидного Python (аудит 2026-07-01, C2).
# `ast.parse` на файле с E999 падает, и раньше `_python_symbols` возвращал
# ПУСТЫЕ множества → `check_symbol_regression` уходил в ранний return
# `empty_before`/ok=True. Итог: ВЕСЬ путь ремонта E999 (SyntaxRepairStage,
# disaster_recovery, broken_file_mode) не имел символьной защиты вообще —
# ни per-patch, ни в финальном аудите (оригинал с E999 так же не парсится).
# Regex-набор ниже сознательно грубее AST (может зацепить `def` в строковом
# литерале) — направление ошибки безопасное: лишний flag уводит в
# NEEDS_REVIEW, а не пропускает потерю кода.
_PY_DEF_RE = re.compile(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+({_IDENT})\s*[(\[]", re.MULTILINE)
_PY_CLASS_RE = re.compile(rf"^[ \t]*class[ \t]+({_IDENT})\b", re.MULTILINE)
_PY_IMPORT_RE = re.compile(rf"^[ \t]*import[ \t]+(.+)$", re.MULTILINE)
_PY_FROM_IMPORT_RE = re.compile(
    rf"^[ \t]*from[ \t]+\S+[ \t]+import[ \t]+(\([^)]*\)|[^\n(]+)",
    re.MULTILINE,
)

# Codex-3 (2026-07-02): regex-детект `@overload`/`@typing.overload` для
# fallback-режима (before не парсится AST). Считаем СУММАРНОЕ число
# декораторов (не по именам — имя недоступно без AST): уменьшение => патч
# удалил перегрузку. Грубо, но fail-safe.
_OVERLOAD_DECORATOR_RE = re.compile(r"^\s*@\s*(?:typing\.)?overload\b", re.MULTILINE)


def _regex_python_symbols(source: str) -> Tuple[Set[str], Set[str]]:
    defs = set(_PY_DEF_RE.findall(source))
    classes = set(_PY_CLASS_RE.findall(source))
    return defs, classes


def _regex_python_imported_names(source: str) -> Set[str]:
    names: Set[str] = set()

    def _add_clause(clause: str) -> None:
        clause = clause.strip().strip("()").replace("\\", " ")
        for part in clause.split(","):
            part = part.strip()
            if not part or part == "*":
                continue
            m = re.match(rf"({_IDENT}(?:\.{_IDENT})*)(?:\s+as\s+({_IDENT}))?", part)
            if not m:
                continue
            if m.group(2):
                names.add(m.group(2))
            else:
                names.add(m.group(1).split(".")[0])

    for clause in _PY_IMPORT_RE.findall(source):
        _add_clause(clause)
    for clause in _PY_FROM_IMPORT_RE.findall(source):
        # from X import a, b as c / from X import (a,\n b)
        clause = clause.strip()
        if clause.startswith("("):
            clause = clause.strip("()")
        for part in clause.split(","):
            part = part.strip()
            if not part or part == "*":
                continue
            m = re.match(rf"({_IDENT})(?:\s+as\s+({_IDENT}))?", part)
            if m:
                names.add(m.group(2) or m.group(1))
    return names


def _python_symbols_ex(source: str) -> Tuple[Set[str], Set[str], bool]:
    """(defs, classes, parsed_ok). При SyntaxError — regex-фолбэк, parsed_ok=False."""
    defs: Set[str] = set()
    classes: Set[str] = set()
    try:
        tree = ast.parse(source)
    except Exception:
        d, c = _regex_python_symbols(source)
        return d, c, False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = getattr(node, "name", None)
            if isinstance(n, str) and n:
                defs.add(n)
        elif isinstance(node, ast.ClassDef):
            n = getattr(node, "name", None)
            if isinstance(n, str) and n:
                classes.add(n)
    return defs, classes, True


def _python_symbols(source: str) -> Tuple[Set[str], Set[str]]:
    defs, classes, _ = _python_symbols_ex(source)
    return defs, classes


def _python_imported_names(source: str) -> Set[str]:
    """Имена, доступные в модуле через import/from-import (alias, если есть).

    2026-06-23: ruff `--select=F401` (через RuffAutoFixStage, ДО основного
    пайплайна) и LLM-патчи для `import-not-found`/`import-untyped` могут
    зацепить и удалить из `from X import a, b, c` СОСЕДНИЕ имена, не
    относящиеся к конкретной целевой ошибке (control series 2026-06-23,
    malinkang/toggl2notion: фикс ОДНОЙ ошибки import-not-found попутно
    стёр 3 из 5 импортированных имён). `_python_symbols` выше не видит
    import-узлы вообще — эта функция отдельно собирает имена для
    `check_symbol_regression`, по той же безусловной политике, что уже
    действует для def/class (любое исчезновение — flag, без исключений
    на «это же была цель фикса»). Звёздный импорт (`from x import *`)
    пропускаем — у него нет перечисляемых имён для сравнения."""
    names: Set[str] = set()
    try:
        tree = ast.parse(source)
    except Exception:
        # C2 (аудит 2026-07-01): без фолбэка E999-файлы теряли и защиту
        # импортов — см. комментарий у _regex_python_symbols.
        return _regex_python_imported_names(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name)
    return names


def _python_overload_counts(source: str) -> "Counter[str]":
    """Считает def/async def с декоратором `@overload` (или `@typing.overload`)
    по имени функции.

    2026-06-24 (control series на 10 проектах, agronholm/anyio): "retry с
    full-file" (полная регенерация файла LLM вместо точечного патча) может
    дописать `@overload`-декораторы БЕЗ их сигнатур (оставив их голыми,
    подряд, с комментариями/пустыми строками между), теряя 4 из 5
    перегрузок `connect_tcp` — итоговый код синтаксически валиден (декораторы
    просто схлопываются на ОДНУ реальную реализацию), но создаёт НОВЫЕ
    ошибки: flake8 E304 ("blank lines found after function decorator") и
    mypy "Single overload definition, multiple required". `_python_symbols`/
    `check_symbol_regression` этого не видят — имя `connect_tcp` как def
    ВСЁ ЕЩЁ существует (реальная реализация осталась), просто пропали
    СИБЛИНГ-перегрузки с тем же именем. Множественные defs с одинаковым
    именем — нормальный, единственный способ выразить @overload в Python,
    поэтому считаем именно decorated-occurrences, а не общий def-count
    (иначе ложные срабатывания на легитимный рефакторинг обычных
    одноимённых вложенных функций)."""
    counts: Counter = Counter()
    try:
        tree = ast.parse(source)
    except Exception:
        return counts
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            dec_name = dec.id if isinstance(dec, ast.Name) else (
                dec.attr if isinstance(dec, ast.Attribute) else None
            )
            if dec_name == "overload":
                counts[node.name] += 1
                break
    return counts


def _rust_symbols(src: str) -> Tuple[Set[str], Set[str]]:
    defs = _scan(rf"\bfn\s+({_IDENT})\s*[<(]", src)
    classes = (
        _scan(rf"\bstruct\s+({_IDENT})\b", src)
        | _scan(rf"\benum\s+({_IDENT})\b", src)
        | _scan(rf"\btrait\s+({_IDENT})\b", src)
        | _scan(rf"\bimpl\s+(?:<[^>]+>\s*)?({_IDENT})\b", src)
    )
    return defs, classes


def _js_ts_symbols(src: str) -> Tuple[Set[str], Set[str]]:
    defs = (
        _scan(rf"\bfunction\s+({_IDENT})\s*\(", src)
        | _scan(rf"\bconst\s+({_IDENT})\s*=\s*(?:async\s*)?\(", src)
        | _scan(rf"\blet\s+({_IDENT})\s*=\s*(?:async\s*)?\(", src)
        | _scan(rf"\bvar\s+({_IDENT})\s*=\s*(?:async\s*)?function", src)
    )
    classes = (
        _scan(rf"\bclass\s+({_IDENT})\b", src)
        | _scan(rf"\binterface\s+({_IDENT})\b", src)
        | _scan(rf"\btype\s+({_IDENT})\s*=", src)
        | _scan(rf"\benum\s+({_IDENT})\b", src)
    )
    return defs, classes


def _go_symbols(src: str) -> Tuple[Set[str], Set[str]]:
    defs = _scan(rf"\bfunc\s+(?:\([^)]+\)\s+)?({_IDENT})\s*\(", src)
    classes = _scan(rf"\btype\s+({_IDENT})\s+(?:struct|interface)\b", src)
    return defs, classes


def _java_kotlin_symbols(src: str) -> Tuple[Set[str], Set[str]]:
    defs = (
        _scan(rf"\bfun\s+({_IDENT})\s*\(", src)
        | _scan(
            rf"(?:public|protected|private|static|final|abstract|override|"
            rf"synchronized|default)?\s*[\w<>\[\]\.]+\s+({_IDENT})\s*\([^;]*?\)\s*\{{",
            src,
        )
    )
    classes = (
        _scan(rf"\bclass\s+({_IDENT})\b", src)
        | _scan(rf"\binterface\s+({_IDENT})\b", src)
        | _scan(rf"\bobject\s+({_IDENT})\b", src)
        | _scan(rf"\benum\s+(?:class\s+)?({_IDENT})\b", src)
    )
    return defs, classes


def _c_family_symbols(src: str) -> Tuple[Set[str], Set[str]]:
    defs = _scan(
        rf"(?:^|\n)[\w\s\*&:<>,\[\]\.]+?\s+({_IDENT})\s*\([^;\n)]*\)\s*(?:\{{|;)",
        src,
    )
    classes = (
        _scan(rf"\bclass\s+({_IDENT})\b", src)
        | _scan(rf"\bstruct\s+({_IDENT})\b", src)
        | _scan(rf"\bunion\s+({_IDENT})\b", src)
        | _scan(rf"\benum\s+(?:class\s+)?({_IDENT})\b", src)
        | _scan(rf"\binterface\s+({_IDENT})\b", src)
    )
    return defs, classes


_LANG_GROUPS = {
    "python": "py", "py": "py",
    "rust": "rs", "rs": "rs",
    "javascript": "js", "js": "js", "jsx": "js", "mjs": "js", "cjs": "js",
    "typescript": "ts", "ts": "ts", "tsx": "ts",
    "go": "go",
    "java": "java",
    "kotlin": "kt", "kt": "kt",
    "c": "c", "cpp": "cpp", "cxx": "cpp", "cc": "cpp",
    "c++": "cpp", "cplusplus": "cpp", "csharp": "cs", "cs": "cs", "c#": "cs",
}


def _group_for_language(language: str) -> str:
    if not isinstance(language, str):
        return ""
    key = language.strip().lower()
    return _LANG_GROUPS.get(key, key)


def _collect(group: str, src: str) -> Tuple[Set[str], Set[str]]:
    if group == "py":
        return _python_symbols(src)
    if group == "rs":
        return _rust_symbols(src)
    if group in ("js", "ts"):
        return _js_ts_symbols(src)
    if group == "go":
        return _go_symbols(src)
    if group in ("java", "kt"):
        return _java_kotlin_symbols(src)
    if group in ("c", "cpp", "cs"):
        return _c_family_symbols(src)
    return set(), set()


# ---------------------------------------------------------------------
# O.17 — anti symbol-duplication (Counter-варианты).
# ---------------------------------------------------------------------

def _python_symbol_counts(source: str) -> Tuple[Counter, Counter]:
    """AST first, regex fallback on SyntaxError - чтобы ловить дубль `def`
    даже на синтаксически невалидных файлах (typical case: file-merge bug)."""
    defs: Counter = Counter()
    classes: Counter = Counter()
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n = getattr(node, "name", None)
                if isinstance(n, str) and n:
                    defs[n] += 1
            elif isinstance(node, ast.ClassDef):
                n = getattr(node, "name", None)
                if isinstance(n, str) and n:
                    classes[n] += 1
        return defs, classes
    except Exception:
        pass
    # Regex fallback for broken syntax.
    for n in re.findall(rf"^[ \t]*def\s+({_IDENT})\s*\(", source, flags=re.MULTILINE):
        defs[n] += 1
    for n in re.findall(rf"^[ \t]*async\s+def\s+({_IDENT})\s*\(", source, flags=re.MULTILINE):
        defs[n] += 1
    for n in re.findall(rf"^[ \t]*class\s+({_IDENT})\b", source, flags=re.MULTILINE):
        classes[n] += 1
    return defs, classes


def _regex_symbol_counts(group: str, src: str) -> Tuple[Counter, Counter]:
    defs_count: Counter = Counter()
    classes_count: Counter = Counter()
    if group == "rs":
        for n in re.findall(rf"\bfn\s+({_IDENT})\s*[<(]", src):
            defs_count[n] += 1
        for kw in (r"\bstruct", r"\benum", r"\btrait"):
            for n in re.findall(rf"{kw}\s+({_IDENT})\b", src):
                classes_count[n] += 1
        for n in re.findall(rf"\bimpl\s+(?:<[^>]+>\s*)?({_IDENT})\b", src):
            classes_count[n] += 1
    elif group in ("js", "ts"):
        for n in re.findall(rf"\bfunction\s+({_IDENT})\s*\(", src):
            defs_count[n] += 1
        for n in re.findall(rf"\bclass\s+({_IDENT})\b", src):
            classes_count[n] += 1
        for n in re.findall(rf"\binterface\s+({_IDENT})\b", src):
            classes_count[n] += 1
    elif group == "go":
        for n in re.findall(rf"\bfunc\s+(?:\([^)]+\)\s+)?({_IDENT})\s*\(", src):
            defs_count[n] += 1
        for n in re.findall(rf"\btype\s+({_IDENT})\s+(?:struct|interface)\b", src):
            classes_count[n] += 1
    elif group in ("java", "kt"):
        for n in re.findall(rf"\bfun\s+({_IDENT})\s*\(", src):
            defs_count[n] += 1
        for n in re.findall(rf"\bclass\s+({_IDENT})\b", src):
            classes_count[n] += 1
    elif group in ("c", "cpp", "cs"):
        for n in re.findall(rf"\bclass\s+({_IDENT})\b", src):
            classes_count[n] += 1
        for n in re.findall(rf"\bstruct\s+({_IDENT})\b", src):
            classes_count[n] += 1
    # Fallback to set-derived counts for unsupported groups.
    if not defs_count and not classes_count:
        defs_set, classes_set = _collect(group, src)
        defs_count = Counter({n: 1 for n in defs_set})
        classes_count = Counter({n: 1 for n in classes_set})
    return defs_count, classes_count


def _counts(group: str, src: str) -> Tuple[Counter, Counter]:
    if group == "py":
        return _python_symbol_counts(src)
    return _regex_symbol_counts(group, src)


def check_symbol_duplication(before: str, after: str, language: str) -> Dict[str, Any]:
    """O.17: returns names whose count in `after` is greater than in `before`."""
    if not isinstance(before, str) or not isinstance(after, str):
        return {
            "ok": True, "duplicated_defs": [], "duplicated_classes": [],
            "before_def_counts": {}, "after_def_counts": {},
            "before_class_counts": {}, "after_class_counts": {},
            "reason": "non_string_input",
        }
    group = _group_for_language(language)
    if not group:
        return {
            "ok": True, "duplicated_defs": [], "duplicated_classes": [],
            "before_def_counts": {}, "after_def_counts": {},
            "before_class_counts": {}, "after_class_counts": {},
            "reason": "unknown_language",
        }
    b_defs, b_classes = _counts(group, before)
    a_defs, a_classes = _counts(group, after)
    duplicated_defs: List[str] = sorted(
        name for name, cnt in a_defs.items()
        if cnt > 1 and cnt > b_defs.get(name, 0)
    )
    duplicated_classes: List[str] = sorted(
        name for name, cnt in a_classes.items()
        if cnt > 1 and cnt > b_classes.get(name, 0)
    )
    ok = not (duplicated_defs or duplicated_classes)
    return {
        "ok": ok,
        "duplicated_defs": duplicated_defs,
        "duplicated_classes": duplicated_classes,
        "before_def_counts": dict(b_defs),
        "after_def_counts": dict(a_defs),
        "before_class_counts": dict(b_classes),
        "after_class_counts": dict(a_classes),
        "reason": "ok" if ok else "duplication",
    }


# ---------------------------------------------------------------------
# O.14 — anti symbol-regression.
# ---------------------------------------------------------------------

def _regression_result(
    *,
    ok: bool,
    reason: str,
    missing_defs=None, missing_classes=None, missing_imports=None,
    missing_overloads=None,
    before_defs=None, after_defs=None, before_classes=None, after_classes=None,
    regex_fallback: bool = False,
) -> Dict[str, Any]:
    """Единая схема результата (аудит 2026-07-01, M8): один и тот же набор
    ключей на всех путях выхода, только JSON-типы (списки вместо set —
    результат попадает в context.metadata и может персиститься в state.json)."""
    return {
        "ok": ok,
        "missing_defs": list(missing_defs or []),
        "missing_classes": list(missing_classes or []),
        "missing_imports": list(missing_imports or []),
        "missing_overloads": list(missing_overloads or []),
        "before_defs": sorted(before_defs or []),
        "after_defs": sorted(after_defs or []),
        "before_classes": sorted(before_classes or []),
        "after_classes": sorted(after_classes or []),
        "regex_fallback": regex_fallback,
        "reason": reason,
    }


def check_symbol_regression(before: str, after: str, language: str) -> Dict[str, Any]:
    """Returns names that were in `before` but missing from `after`."""
    if not isinstance(before, str) or not isinstance(after, str):
        return _regression_result(ok=True, reason="non_string_input")
    group = _group_for_language(language)
    if not group:
        return _regression_result(ok=True, reason="unknown_language")
    regex_fallback = False
    if group == "py":
        before_defs, before_classes, before_parsed = _python_symbols_ex(before)
        if before_parsed:
            after_defs, after_classes, _ = _python_symbols_ex(after)
        else:
            # C2 (аудит 2026-07-01): before не парсится (E999-файл — типичный
            # вход SyntaxRepair/disaster_recovery). Сравниваем regex-vs-regex,
            # а не regex-vs-AST — оба множества собраны ОДНИМ способом, иначе
            # систематические расхождения способов сбора дали бы ложные
            # missing/лишние пропуски.
            regex_fallback = True
            after_defs, after_classes = _regex_python_symbols(after)
    else:
        before_defs, before_classes = _collect(group, before)
        after_defs, after_classes = _collect(group, after)
    # 2026-06-23: импортированные имена — отдельная, безусловная проверка
    # (та же политика, что у defs/classes: любое исчезновение — regression,
    # без попыток угадать «было ли это целью фикса»). Только Python — у
    # остальных языков нет единого AST-парсера в этом модуле для import.
    if group == "py":
        if regex_fallback:
            before_imports = _regex_python_imported_names(before)
            after_imports = _regex_python_imported_names(after)
        else:
            before_imports = _python_imported_names(before)
            after_imports = _python_imported_names(after)
    else:
        before_imports = set()
        after_imports = set()
    # 2026-06-24: см. docstring _python_overload_counts — отдельная,
    # count-based проверка (а не set-based, как defs/classes выше), потому
    # что @overload-сиблинги ДЕЛЯТ одно имя — set-based missing_defs этого
    # не видит вообще (имя как таковое не пропадает).
    # Codex-3 (2026-07-02): РАНЬШЕ при regex_fallback overload-проверка была
    # полностью отключена (before_overloads=after_overloads=Counter()), и патч,
    # чинящий синтаксис ценой удаления @overload-перегрузки, проходил ok=True.
    # Теперь в fallback-режиме сравниваем СУММАРНОЕ regex-число @overload
    # before/after (по именам нельзя — before не парсится); уменьшилось →
    # missing_overloads с псевдо-именем "<regex_total>", ok=False. AST-путь
    # (не fallback) остаётся точным, count-based по именам.
    before_overloads = (
        _python_overload_counts(before) if group == "py" and not regex_fallback else Counter()
    )
    if not before_defs and not before_classes and not before_imports:
        return _regression_result(
            ok=True, reason="empty_before",
            before_defs=before_defs, before_classes=before_classes,
            regex_fallback=regex_fallback,
        )
    after_overloads = (
        _python_overload_counts(after) if group == "py" and not regex_fallback else Counter()
    )
    missing_defs = sorted(before_defs - after_defs)
    missing_classes = sorted(before_classes - after_classes)
    missing_imports = sorted(before_imports - after_imports)
    if group == "py" and regex_fallback:
        _before_ovl_total = len(_OVERLOAD_DECORATOR_RE.findall(before))
        _after_ovl_total = len(_OVERLOAD_DECORATOR_RE.findall(after))
        missing_overloads = (
            [{
                "name": "<regex_total>",
                "before_count": _before_ovl_total,
                "after_count": _after_ovl_total,
            }]
            if _after_ovl_total < _before_ovl_total else []
        )
    else:
        missing_overloads = sorted(
            (
                {"name": name, "before_count": cnt, "after_count": after_overloads.get(name, 0)}
                for name, cnt in before_overloads.items()
                if after_overloads.get(name, 0) < cnt
            ),
            key=lambda d: d["name"],
        )
    ok = not (missing_defs or missing_classes or missing_imports or missing_overloads)
    return _regression_result(
        ok=ok,
        reason="ok" if ok else "regression",
        missing_defs=missing_defs,
        missing_classes=missing_classes,
        missing_imports=missing_imports,
        missing_overloads=missing_overloads,
        before_defs=before_defs, after_defs=after_defs,
        before_classes=before_classes, after_classes=after_classes,
        regex_fallback=regex_fallback,
    )
