"""
Cascade collapse — универсальный пре-фильтр для списка ошибок.

ЗАЧЕМ. Один корневой баг (пропущенный `#include`, `using`, `import`, `use`,
синтаксическая ошибка) часто рождает 5-30 «следствий» — undefined-символы,
type errors и т.п. — которые исчезнут сами после фикса корня. Если пайплайн
гоняет LLM по каждой производной ошибке, это:
  * шум в case-file (LLM пытается чинить следствие, а не причину);
  * лишние LLM-вызовы (дорогие, иногда вредные);
  * memory копит «фиксы», которые на самом деле не нужны.

ЧТО ДЕЛАЕТ. Принимает список error-dict'ов (после дедупа в analyze_stage),
выявляет:
  * **CRITICAL_SYNTAX cascade** — синтаксическая ошибка в файле на строке N
    «ломает парсер», все ошибки того же файла >= N — следствия;
  * **Missing dependency cascade** — missing import/include/use/using (`E0432`,
    `CS0246`/`CS0103`, `JAVAC_PACKAGE`/`SYMBOL`, `TS2304`/`TS2307`,
    `GCC_INCLUDE`/`UNDECLARED`) → undefined-символы того же имени в том же
    файле ниже — следствия;
  * **Symbol-use cluster** — несколько ошибок упоминают один и тот же символ
    (по backtick'ам); считаем самую раннюю «корнем», остальные — следствиями.

Не УДАЛЯЕТ ошибки — только переупорядочивает (root перед derived) и помечает:
  * `_cascade_root: True` у корня (или у одиночек);
  * `_cascade_derived_from: <root_sig>` у следствий;
  * `_cascade_reason: <строка>` (для debug-логов).

prioritize_stage увидит изменённый порядок и веса. Стадии вверх по течению
(LLM, reviewer, memory) могут читать `_cascade_*` чтобы понимать контекст.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# Коды «missing import/include/use» по языкам. Расширяется по мере добавления
# языков и анализаторов. Используется для распознавания root'а.
_MISSING_DEP_CODES = {
    # Rust
    "E0432", "E0433",
    # C# (cannot find type/name)
    "CS0246", "CS0234", "CS0103", "CS1061",
    # Java
    "JAVAC_PACKAGE", "JAVAC_SYMBOL",
    # C/C++
    "GCC_INCLUDE", "GCC_UNDECLARED",
    # TypeScript
    "TS2304", "TS2307", "TS2552",
    # Python (Pyflakes/PyLint style)
    "F401", "F821", "E0401", "ModuleNotFoundError", "NameError",
    # JS (ESLint)
    "no-undef",
}


def _error_sig(err: Dict[str, Any]) -> str:
    """Канонический ключ ошибки (стабильнее, чем `error_signature` из utils:
    для cascade нам важна тройка file/line/code-or-msg)."""
    return f"{err.get('file', '')}::{err.get('line', 0)}::{err.get('code', '') or err.get('message', '')[:60]}"


_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_:.<>\-]*)`")
_SINGLE_QUOTE_RE = re.compile(r"'([A-Za-z_][A-Za-z0-9_:.<>\-]*)'")
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]+)\b")


def _extract_symbols(message: str) -> List[str]:
    """Достаёт имена символов из сообщения. Поддерживает обе нотации:
      * backticks (rustc/javac, частично csc/Roslyn): `foo`
      * одинарные кавычки (g++, csc, dotnet): 'foo'
    Возвращает до 5 уникальных имён в порядке появления."""
    if not message:
        return []
    found: List[str] = []
    for rx in (_BACKTICK_RE, _SINGLE_QUOTE_RE):
        for sym in rx.findall(message):
            if sym not in found:
                found.append(sym)
    return found[:5]


def _mark(err: Dict[str, Any], **kwargs) -> None:
    for k, v in kwargs.items():
        err[k] = v


def collapse_cascades(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Главный entry point.

    Возвращает НОВЫЙ список (исходный не мутируется): сначала корни и одиночки,
    затем производные ошибки в порядке убывания «производности». Каждая запись
    в результате — копия исходной с `_cascade_root` / `_cascade_derived_from`
    / `_cascade_reason`-аннотациями.

    Поведение НИКОГДА не падает: при любой ошибке возвращает входной список
    как есть (без аннотаций).
    """
    try:
        if not errors:
            return list(errors)

        # Работаем с копиями, чтобы не мутировать вход.
        work = [dict(e) for e in errors]

        # Группируем по файлу для пер-файловых эвристик.
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for e in work:
            by_file.setdefault(e.get("file", "") or "", []).append(e)

        # ───── 1) CRITICAL_SYNTAX cascade (per-file) ─────
        for fname, group in by_file.items():
            if not fname:
                continue
            group.sort(key=lambda e: int(e.get("line", 0) or 0))
            crit = next(
                (e for e in group if str(e.get("error_class", "")).upper() == "CRITICAL_SYNTAX"),
                None,
            )
            if crit is None:
                continue
            crit_line = int(crit.get("line", 0) or 0)
            _mark(crit, _cascade_root=True,
                  _cascade_reason="CRITICAL_SYNTAX root in file")
            for e in group:
                if e is crit:
                    continue
                if e.get("_cascade_derived_from"):
                    continue
                if int(e.get("line", 0) or 0) >= crit_line:
                    _mark(e,
                          _cascade_derived_from=_error_sig(crit),
                          _cascade_reason="downstream of CRITICAL_SYNTAX in same file")

        # ───── 2) Missing-dependency cascade (per-file, по символам) ─────
        for fname, group in by_file.items():
            if not fname:
                continue
            group.sort(key=lambda e: int(e.get("line", 0) or 0))
            for root in group:
                if root.get("_cascade_derived_from"):
                    continue
                code = root.get("code", "") or ""
                if code not in _MISSING_DEP_CODES:
                    continue
                syms = _extract_symbols(root.get("message", "") or "")
                # Если backtick'ов нет — берём слово после кодов вроде "import X"
                if not syms:
                    m = re.search(r"\b(?:import|use|using|include)\s+['\"<]?([A-Za-z_][A-Za-z0-9_.:/<>]+)",
                                  root.get("message", "") or "")
                    if m:
                        syms = [m.group(1).strip("'\"<>")]
                if not syms:
                    continue
                # Делаем root'ом
                _mark(root, _cascade_root=True,
                      _cascade_reason=f"missing dep '{syms[0]}'")
                # Любая ошибка в том же файле, упоминающая тот же символ —
                # следствие. И «cannot find»-следствия даже без явного упоминания
                # символа в сообщении группируем по эвристике near-by line.
                for e in group:
                    if e is root or e.get("_cascade_derived_from"):
                        continue
                    if int(e.get("line", 0) or 0) < int(root.get("line", 0) or 0):
                        continue
                    e_msg = e.get("message", "") or ""
                    if any(s and s in e_msg for s in syms):
                        _mark(e,
                              _cascade_derived_from=_error_sig(root),
                              _cascade_reason=f"derived from missing '{syms[0]}'")

        # ───── 3) Symbol-use cluster (cross-file допускаем, по backtick'ам) ─────
        # Группируем оставшиеся неаннотированные ошибки по их первому символу
        # в backtick'ах: если ≥3 ошибок про один символ — самая ранняя (по
        # file+line) становится root, остальные — derivative.
        sym_groups: Dict[str, List[Dict[str, Any]]] = {}
        for e in work:
            if e.get("_cascade_root") or e.get("_cascade_derived_from"):
                continue
            syms = _extract_symbols(e.get("message", "") or "")
            if not syms:
                continue
            sym_groups.setdefault(syms[0], []).append(e)
        for sym, lst in sym_groups.items():
            if len(lst) < 3:
                continue
            lst.sort(key=lambda e: (e.get("file", ""), int(e.get("line", 0) or 0)))
            root = lst[0]
            _mark(root, _cascade_root=True,
                  _cascade_reason=f"symbol cluster '{sym}'")
            for e in lst[1:]:
                _mark(e,
                      _cascade_derived_from=_error_sig(root),
                      _cascade_reason=f"derived from cluster '{sym}'")

        # ───── Финальный порядок: roots+standalone сначала ─────
        # Стабильная сортировка: (is_derived, original_index). Roots и одиночки
        # сохраняют относительный порядок, derivatives тоже.
        with_index = list(enumerate(work))

        def _key(item):
            idx, e = item
            is_derived = 1 if e.get("_cascade_derived_from") else 0
            return (is_derived, idx)

        with_index.sort(key=_key)
        result = [e for _, e in with_index]

        roots = sum(1 for e in result if e.get("_cascade_root"))
        derived = sum(1 for e in result if e.get("_cascade_derived_from"))
        if derived:
            logger.info("cascade collapse: %d roots, %d derivatives из %d ошибок",
                        roots, derived, len(result))
        return result
    except Exception as e:
        logger.warning("cascade collapse упал, возвращаем вход как есть: %s", e)
        return list(errors)


def group_cascades(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Для debug/отчётов: возвращает структуру вида
        [{root: <err>, derivatives: [<err>, ...], reason: <str>}, ...]
    плюс «одиночки» (root=None). Не вызывает `collapse_cascades` — работает на
    УЖЕ аннотированном списке (после collapse).
    """
    sig_to_err = {_error_sig(e): e for e in errors}
    groups: List[Dict[str, Any]] = []
    seen_roots: Set[str] = set()
    for e in errors:
        if e.get("_cascade_derived_from"):
            continue
        sig = _error_sig(e)
        if sig in seen_roots:
            continue
        seen_roots.add(sig)
        derivatives = [d for d in errors if d.get("_cascade_derived_from") == sig]
        groups.append({
            "root": e,
            "derivatives": derivatives,
            "reason": e.get("_cascade_reason", ""),
        })
    return groups
