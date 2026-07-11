"""
Единый словарь для unified-diff: классификация строк и общая регулярка хедера хунка.

До этого модуля каждый файл в fixers/ переписывал свою копию правил «строка тела
ханка»: одинаковые проверки `line.startswith((' ', '+', '-'))` дублировались
~9 раз и расходились в нюансах. В частности, никто из них не отличал файловый
хедер `+++ b/path` / `--- a/path` от настоящего `+` / `-` контента, поэтому
LLM-патчи с `+++` внутри хунка попадали в файл как `++ b/path` (буквально).

Здесь — один источник правды. Импортировать ОТСЮДА.
"""

import re
from typing import Optional

# Регулярка заголовка ханка. Используется patch_engine, patch_normalizer,
# patch_repair, validator — раньше у каждого была своя копия.
HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def is_file_header(line: str) -> bool:
    """True, если строка — файловый хедер unified-diff (`+++ /...` или `--- /...`)
    и НЕ контент тела ханка. Префикс из трёх знаков, а не одного.
    """
    if not line:
        return False
    return line[:3] in ('+++', '---')


def header_path(line: str) -> Optional[str]:
    """Для файлового хедера (`+++ b/path` / `--- a/path`) возвращает
    нормализованный относительный путь без префикса `a/` / `b/` и с прямыми
    слэшами. Для всего остального — None.

    Нужно, чтобы применение много-файлового diff'а понимало, к какому файлу
    относятся ханки очередной секции (раньше секции склеивались и ханки одного
    файла применялись к другому).
    """
    if not is_file_header(line):
        return None
    rest = line[3:].strip()
    # отрезаем timestamp после таба, если есть (формат GNU diff)
    rest = rest.split("\t", 1)[0].strip()
    if not rest or rest == "/dev/null":
        return rest or None
    rest = rest.replace("\\", "/")
    if rest.startswith(("a/", "b/")):
        rest = rest[2:]
    return rest or None


def body_kind(line: str) -> Optional[str]:
    """Возвращает '+', '-' или ' ' для строки тела ханка.
    Возвращает None для всего, что телом НЕ является:
      - пустых строк,
      - файловых хедеров (+++/---),
      - заголовков ханка (@@) и прочего служебного текста.
    """
    if not line or is_file_header(line):
        return None
    first = line[0]
    if first in ('+', '-', ' '):
        return first
    return None


def strip_body_prefix(line: str) -> str:
    """Снимает префикс +/-/пробел у строки тела ханка.
    Для всего остального возвращает строку как есть.
    """
    if line and line[0] in ('+', '-', ' '):
        return line[1:]
    return line


# Плейсхолдеры, которые модели любят подсовывать вместо реального кода
# в добавляемых строках diff'а. Если такая строка добавится в файл «как есть»,
# она остаётся в исходнике в виде комментария-обманки и часто ломает баланс.
_PLACEHOLDERS_NORM = tuple(p.lower() for p in (
    "// existing code",
    "// ... rest of code",
    "// ... existing code",
    "// ...",
    "// unchanged",
    "// rest unchanged",
    "/* unchanged */",
    "/* ... */",
    "/* existing code */",
    "<rest of code>",
    "<unchanged>",
    "# existing code",
    "# unchanged",
    "# ...",
    "... existing code",
    "... rest of code",
))


def is_llm_placeholder(content: str) -> bool:
    """True для строк-плейсхолдеров от LLM («// existing code», «// ...» и т.п.).
    Принимает уже снятый префикс ханка (т.е. чистый контент строки).
    """
    if not content:
        return False
    s = content.strip().lower()
    return any(s.startswith(p) for p in _PLACEHOLDERS_NORM)
