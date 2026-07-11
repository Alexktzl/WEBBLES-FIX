"""
Python AST-репэр — каскад безопасных правок «развалившихся» Python-файлов.

Цель: когда `PythonSyntaxHealer` (однострочные эвристики) не помогает, а LLM
ещё не дошёл, попробовать тривиальные структурные правки и валидировать через
`ast.parse`. Если правка превращает невалидный Python в валидный — принимаем.

Покрытие (от безопасного к менее):
  1) Подряд идущие одинаковые строки кода — удаляем дубль. Реальный кейс из
     лога: LLM-патч инжектировал второй раз `query = "..."; cursor.execute(...)`
     одной строкой выше — теперь видим этот дубль и снимаем.
  2) Отсутствующее `:` после `def/class/if/elif/else/for/while/try/except/finally`.
     (Дублирует часть `PythonSyntaxHealer`, но безопасно — `ast.parse`
     валидирует результат.)
  3) Несогласованный отступ строки внутри тела блока — выравниваем по соседу.
  4) Лишние закрывающие скобки в конце файла — отрезаем.

После каждой правки делаем `ast.parse(new)`: если стало OK — возвращаем
полностью переписанный текст. Если ни одна правка не помогла — `None`, и
дальше работает LLM.

Принципы:
  * никаких изменений, которые меняют семантику корректного кода — все правки
    мы применяем ТОЛЬКО к файлам, которые сейчас невалидны (`ast.parse` падает);
  * один проход — несколько правок подряд (cascade), на каждом шаге заново
    парсим;
  * лимит: до 8 правок за файл, чтобы не зациклиться.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


MAX_REPAIR_PASSES = 8
# Считаем «значимыми» строки, которые могут стать дублем. Пустые/комментарии и
# `import` не дублим (`import os` пишут специально несколько раз).
_DUP_SKIP_RE = re.compile(r"^\s*(#|$|import\s|from\s)")


def parses(source: str) -> Tuple[bool, Optional[str]]:
    """Возвращает `(ok, error_msg_lowered)`. Никогда не бросает."""
    try:
        ast.parse(source)
        return True, None
    except SyntaxError as e:
        return False, (str(e).lower() if e else "")
    except Exception as e:  # pragma: no cover
        return False, str(e).lower()


# ----------------------------------------------------------------------
# Кандидаты-правки
# ----------------------------------------------------------------------
def _try_drop_duplicate_lines(source: str) -> Optional[str]:
    """Удаляет ПОДРЯД идущие идентичные не-пустые/не-import строки.

    Срабатывает на реальный кейс пользователя:
        query = "SELECT ..."   cursor.execute(...)
        query = "SELECT ..."   cursor.execute(...)
    """
    lines = source.splitlines(keepends=True)
    out: List[str] = []
    prev_norm = None
    changed = False
    for ln in lines:
        norm = ln.rstrip("\r\n").rstrip()
        if (norm and not _DUP_SKIP_RE.match(norm) and norm == prev_norm):
            changed = True
            continue  # пропускаем дубль
        out.append(ln)
        prev_norm = norm
    return "".join(out) if changed else None


# Ключевые слова — без пробелов/двоеточий. Проверка считается «попаданием»,
# только если за keyword следует пробел / двоеточие / `#` / `(` / EOL — иначе
# `elsewhere = 1` или `tryout()` ошибочно подцеплялись (`stripped.startswith("else")`).
_BLOCK_OPENERS = (
    "if", "elif", "else", "for", "while", "with",
    "def", "async def", "class", "try", "except", "finally",
)


def _is_block_opener(stripped: str) -> bool:
    """True если строка начинается с keyword'а-открывателя блока (с учётом
    «жёстких» разделителей: пробел/двоеточие/комментарий/`(`/конец строки)."""
    for k in _BLOCK_OPENERS:
        if stripped == k:
            return True
        if not stripped.startswith(k):
            continue
        nxt = stripped[len(k): len(k) + 1]
        if nxt in (" ", "\t", ":", "(", "#"):
            return True
    return False


def _try_add_missing_colons(source: str) -> Optional[str]:
    """Добавляет `:` в конец строк-открывателей блока, где его нет."""
    lines = source.splitlines(keepends=True)
    changed = False
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        stripped = line.lstrip()
        if not stripped:
            continue
        if not _is_block_opener(stripped):
            continue
        code = line.split("#", 1)[0].rstrip()
        if not code:
            continue
        if code.endswith(":"):
            continue
        # `else` / `try` / `finally` без аргументов — тоже нужны двоеточия.
        # Не трогаем строки, которые продолжаются на следующей (висячий `\` или
        # открытая скобка) — корректно их обработать сложно.
        if code.endswith("\\"):
            continue
        # Грубая проверка скобочного баланса в строке:
        if code.count("(") != code.count(")"):
            continue
        # Меняем: добавляем `:` перед возможным комментарием.
        if "#" in raw:
            before, comment = raw.split("#", 1)
            lines[i] = before.rstrip() + ": #" + comment
        else:
            lines[i] = code + ":" + raw[len(line):]
        changed = True
    return "".join(lines) if changed else None


def _try_drop_trailing_close_brackets(source: str) -> Optional[str]:
    """Срезает «висячие» одиночные `)`/`]`/`}` в самом конце файла.

    Иногда LLM добавляет лишние закрывающие, и Python падает «unmatched ')'».
    Делаем только если ВНЕ строк/комментариев в конце файла одно из таких.
    """
    text = source.rstrip()
    if not text or text[-1] not in ")]}":
        return None
    # Считаем баланс по всему файлу — простой счёт (без учёта строк).
    pairs = {"(": ")", "[": "]", "{": "}"}
    opens = {v: k for k, v in pairs.items()}
    counts = {"(": 0, "[": 0, "{": 0}
    for ch in source:
        if ch in counts:
            counts[ch] += 1
        elif ch in opens:
            counts[opens[ch]] -= 1
    last = text[-1]
    if counts.get(opens[last], 0) >= 0:
        return None  # баланс не нарушен — не трогаем
    return source[: source.rfind(last)] + source[source.rfind(last) + 1:]


def _try_normalize_indent(source: str) -> Optional[str]:
    """Выравнивает отступ строки до кратного 4.

    Стратегия:
      1) tab/space смесь → tab развёрнут в 4 пробела;
      2) если отступ некратен 4 — пробуем `prev_block_indent + 4` (если
         предыдущая значимая строка — открыватель блока, заканчивается на `:`),
         иначе берём отступ предыдущей значимой строки. Никогда не округляем
         в 0, если контекст явно ожидает положительный отступ.
    """
    lines = source.splitlines(keepends=True)
    changed = False
    prev_indent = 0
    prev_is_opener = False
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        indent_chars = raw[: len(raw) - len(raw.lstrip(" \t"))]
        indent = len(indent_chars)
        # tab → 4 пробела (нормализация смеси)
        if "\t" in indent_chars:
            new_indent = " " * 4 * indent_chars.count("\t") + indent_chars.replace("\t", "")
            lines[i] = new_indent + raw.lstrip(" \t")
            indent = len(new_indent)
            changed = True
        # обновим контекст
        stripped = raw.lstrip(" \t").rstrip("\r\n")
        if indent % 4 == 0:
            prev_indent = indent
            prev_is_opener = stripped.split("#", 1)[0].rstrip().endswith(":")
            continue
        # некратно 4 → выровнять
        if prev_is_opener:
            target = prev_indent + 4
        elif prev_indent > 0:
            target = prev_indent
        else:
            # нет контекста — пытаемся ближайший положительный кратный 4
            target = max(4, round(indent / 4) * 4)
        if target == indent:
            continue
        lines[i] = " " * target + raw.lstrip(" \t")
        prev_indent = target
        prev_is_opener = stripped.split("#", 1)[0].rstrip().endswith(":")
        changed = True
    return "".join(lines) if changed else None


REPAIRS = (
    ("drop_duplicate_lines", _try_drop_duplicate_lines),
    ("add_missing_colons", _try_add_missing_colons),
    ("normalize_indent", _try_normalize_indent),
    ("drop_trailing_close_brackets", _try_drop_trailing_close_brackets),
)


# ----------------------------------------------------------------------
# Главная функция
# ----------------------------------------------------------------------
def try_repair(source: str) -> Optional[str]:
    """Каскад правок. Возвращает новый текст, если получилось сделать файл
    валидным; иначе `None`. Не вызывается на изначально-валидных файлах:
    мы хотим только чинить уже сломанное.
    """
    if not source or not source.strip():
        return None
    ok, _ = parses(source)
    if ok:
        return None  # ничего не делаем — файл уже валиден
    current = source
    applied: List[str] = []
    for _ in range(MAX_REPAIR_PASSES):
        progress = False
        for name, repair in REPAIRS:
            candidate = repair(current)
            if candidate is None or candidate == current:
                continue
            ok_now, _ = parses(candidate)
            if ok_now:
                applied.append(name)
                logger.info("python_ast_repair: применено %s", ", ".join(applied))
                return candidate
            # Не валидно ещё, но если правка УМЕНЬШИЛА длину/изменила текст —
            # принимаем как промежуточный шаг и продолжаем каскад.
            current = candidate
            applied.append(name)
            progress = True
            break  # начинаем заново сначала списка
        if not progress:
            break
    # После каскада — финальная проверка.
    ok_final, _ = parses(current)
    if ok_final and current != source:
        logger.info("python_ast_repair: применено %s (cascade)",
                    ", ".join(applied))
        return current
    return None
