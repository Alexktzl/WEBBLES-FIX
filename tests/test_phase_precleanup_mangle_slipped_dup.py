"""
PreCleanup-мангл: SLIPPED_LINE подстрочный keyword-матч и DUPLICATE_LINES
strip-сравнение (2026-07-08, серия-5: starlette ~10 файлов, loguru
_colorizer/_simple_sinks, tests/*).

Два класса одного бага «правило видит паттерн там, где его нет»:

1. SLIPPED_LINE (_split_one_slipped_line, ветка 3): statement-keyword
   искался подстрокой с границей слова только СЛЕВА — в
   `from starlette.middleware.exceptions import X` находился `except`
   (внутри слова "exceptions"), слева точка (не alnum) → строка резалась
   посреди dotted-пути. Инвариант: граница слова с ОБЕИХ сторон, и после
   `.` keyword невозможен (там всегда имя атрибута/модуля).

2. DUPLICATE_LINES (проход 1, смежные дубли): сравнение по strip()
   считало дублями синтаксически разные строки — вложенный `try:` под
   `try:` (разные отступы = разные блоки, starlette/formparsers.py) и
   смежные закрывающие `)` вложенных структур (loguru/_colorizer.py).
   Инвариант: сырое сравнение (отступ обязан совпадать) + строки из
   одной пунктуации не дедупятся никогда.

Последствие до фикса: ast-предохранитель PreCleanup не пускал порчу на
диск, но файл попадал в known-bad и терял ВСЕ structured-фиксы до конца
прогона (E203 _colorizer так и не был починен за 7 прогонов loguru).

Запуск: python tests/test_phase_precleanup_mangle_slipped_dup.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from fixers.rule_based_fixer import RuleBasedFixer  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


FX = RuleBasedFixer()


def _run_pipeline(src: str) -> str:
    cur = src
    for code, fn in [
        ("LLM_NOISE", FX._py_remove_llm_noise),
        ("SLIPPED_LINE", FX._py_separate_slipped_lines),
        ("DUPLICATE_LINES", FX._py_remove_duplicate_lines),
        ("DEAD_CODE", FX._py_remove_dead_code_after_return),
    ]:
        es = fn({"file": "x.py", "line": 0, "code": code, "message": ""}, cur)
        if es is not None and es.edits:
            cur = es.edits[0].new
    return cur


# --- 1. SLIPPED_LINE: keyword внутри идентификатора ---

def test_dotted_import_with_embedded_keyword_untouched():
    src = (
        "from starlette.middleware.exceptions import ExceptionMiddleware\n"
        "from starlette.middleware.errors import ServerErrorMiddleware\n"
    )
    out = _run_pipeline(src)
    check("dotted_exceptions_untouched", out == src, f"out: {out!r}")


def test_attribute_access_keyword_names_untouched():
    """Идентификаторы, содержащие/начинающиеся с keyword: exceptions,
    importlib, deferred, classify — не режутся."""
    src = (
        "value = obj.exceptions\n"
        "import importlib.util\n"
        "x = self.deferred.classify(a)\n"
    )
    out = _run_pipeline(src)
    check("keyword_like_names_untouched", out == src, f"out: {out!r}")


def test_real_slipped_keyword_still_split():
    """Настоящий слип `…)def f():` по-прежнему разделяется."""
    line = "    x = call(1)def helper():\n"
    out = FX._split_one_slipped_line(line)
    check("real_slip_still_split", out is not None and "\n    def helper():" in out,
          f"out: {out!r}")


# --- 2. DUPLICATE_LINES: strip-дубли ---

def test_nested_try_different_indent_untouched():
    src = (
        "if x:\n"
        "    try:\n"
        "        try:\n"
        "            import a\n"
        "        except ModuleNotFoundError:\n"
        "            import b\n"
        "    except ModuleNotFoundError:\n"
        "        a = None\n"
    )
    out = _run_pipeline(src)
    check("nested_try_untouched", out == src, f"out: {out!r}")
    ast.parse(out)


def test_adjacent_closing_brackets_untouched():
    src = (
        "messages = [\n"
        "    (\n"
        "        wrap(\n"
        "            a, b\n"
        "        )\n"
        "    )\n"
        "    for a in items\n"
        "]\n"
    )
    out = _run_pipeline(src)
    check("closing_brackets_untouched", out == src, f"out: {out!r}")
    ast.parse(out)


def test_true_adjacent_duplicate_still_removed():
    """Настоящий смежный дубль (байт-в-байт, содержательная строка) —
    по-прежнему схлопывается keep-one."""
    src = (
        "x = compute()\n"
        "x = compute()\n"
        "y = 2\n"
    )
    out = _run_pipeline(src)
    check("true_duplicate_removed", out == "x = compute()\ny = 2\n", f"out: {out!r}")


def test_same_text_different_indent_untouched():
    """Одинаковый текст на РАЗНЫХ отступах — разные синтаксические роли,
    не дубль."""
    src = (
        "if a:\n"
        "    return None\n"
        "return None\n"
    )
    out = _run_pipeline(src)
    check("different_indent_not_duplicate", out == src, f"out: {out!r}")


def test_semicolons_inside_bytes_literal_with_comment_untouched():
    """2026-07-08 (starlette tests/test_formparsers.py): все `;` внутри
    bytes-литерала + хвостовой `# type: ignore` без `;` — строка не
    сплитится (ветка `#` в _semicolon_in_string_or_comment возвращала
    наличие `;` в комменте вместо «до комментария голых `;` не было»)."""
    line = (
        "                b'Content-Disposition: form-data; name=\"file\";"
        " filename=\"x.txt\"'  # type: ignore\n"
    )
    out = FX._split_one_slipped_line(line)
    check("bytes_literal_semicolons_untouched",
          out is None or out == line, f"out: {out!r}")


def test_real_semicolon_statements_still_split():
    """Настоящие `a = 1; b = 2` по-прежнему разделяются."""
    out = FX._split_one_slipped_line("    a = 1; b = 2\n")
    check("real_semicolons_still_split",
          out is not None and out == "    a = 1\n    b = 2\n", f"out: {out!r}")


def test_double_space_inside_expression_untouched():
    """2026-07-08 (серия-6, dateutil tests/test_imports.py): авторский
    двойной пробел ВНУТРИ выражения (`assert weekday is not  None`) — не
    слип; ветка 1 обязана требовать, чтобы ОБЕ части были
    самостоятельными валидными statement-ами."""
    out = FX._split_one_slipped_line("    assert weekday is not  None\n")
    check("double_space_expression_untouched", out is None, f"out: {out!r}")


def test_real_two_statements_with_spaces_still_split():
    """Настоящий слип `x = call(1)  y = 2` — обе части валидны — делится."""
    out = FX._split_one_slipped_line("    x = call(1)  y = 2\n")
    check("real_two_statements_split",
          out == "    x = call(1)\n    y = 2\n", f"out: {out!r}")


def test_duplicate_class_removal_includes_decorators():
    """2026-07-08 (серия-7, attrs tests/test_dunders.py): при keep-last
    дедупе переопределённого class диапазон удаления обязан включать
    ДЕКОРАТОРЫ первого определения — node.lineno указывает на строку
    class, а @attr.s(...) стоит выше; без этого над чужим кодом висел
    сиротский декоратор (invalid syntax)."""
    src = (
        "import attr\n"
        "\n"
        "@attr.s(unsafe_hash=True)\n"
        "class C:\n"
        "    x = 1\n"
        "\n"
        "@attr.s(order=True)\n"
        "class C:\n"
        "    y = 2\n"
    )
    out = _run_pipeline(src)
    ast.parse(out)  # главный инвариант: валидность
    check("decorated_dup_class_valid", True)
    check("kept_last_definition", "order=True" in out and "y = 2" in out, f"out: {out!r}")
    check("no_orphan_decorator", "unsafe_hash=True" not in out, f"out: {out!r}")


def test_nested_same_named_classes_not_cross_deduped():
    """2026-07-09 (серия-8, tornado test/*.py): вложенные `class Handler`
    в РАЗНЫХ внешних классах — разные классы; их методы не дубли. Ключ
    группировки обязан включать полную родословную."""
    src = (
        "class TestA:\n"
        "    class Handler:\n"
        "        def get(self):\n"
        "            return 1\n"
        "\n"
        "class TestB:\n"
        "    class Handler:\n"
        "        def get(self):\n"
        "            return 2\n"
    )
    out = _run_pipeline(src)
    ast.parse(out)
    check("cross_class_methods_survive", out.count("def get(self):") == 2, f"out: {out!r}")


def test_duplicate_method_within_same_class_still_deduped():
    """Настоящий дубль def в ОДНОМ классе — по-прежнему keep-last."""
    src = (
        "class A:\n"
        "    def f(self):\n"
        "        return 1\n"
        "\n"
        "    def f(self):\n"
        "        return 2\n"
    )
    out = _run_pipeline(src)
    ast.parse(out)
    check("same_class_dup_removed", out.count("def f(self):") == 1, f"out: {out!r}")
    check("kept_last_impl", "return 2" in out and "return 1" not in out, f"out: {out!r}")


def test_adjacent_expression_continuations_and_yields_survive():
    """2026-07-09 (tornado): `+ newline` дважды (продолжения выражения) и
    `yield f()` дважды (намеренные повторы) — не дубли."""
    src = (
        "def w():\n"
        "    stream.write(\n"
        "        head\n"
        "        + newline\n"
        "        + newline\n"
        "        + body\n"
        "    )\n"
        "    yield read()\n"
        "    yield read()\n"
    )
    out = _run_pipeline(src)
    ast.parse(out)
    check("plus_newline_pair_survives", out.count("+ newline") == 2, f"out: {out!r}")
    check("yield_pair_survives", out.count("yield read()") == 2, f"out: {out!r}")


if __name__ == "__main__":
    test_dotted_import_with_embedded_keyword_untouched()
    test_attribute_access_keyword_names_untouched()
    test_real_slipped_keyword_still_split()
    test_nested_try_different_indent_untouched()
    test_adjacent_closing_brackets_untouched()
    test_true_adjacent_duplicate_still_removed()
    test_same_text_different_indent_untouched()
    test_semicolons_inside_bytes_literal_with_comment_untouched()
    test_real_semicolon_statements_still_split()
    test_double_space_inside_expression_untouched()
    test_real_two_statements_with_spaces_still_split()
    test_duplicate_class_removal_includes_decorators()
    test_nested_same_named_classes_not_cross_deduped()
    test_duplicate_method_within_same_class_still_deduped()
    test_adjacent_expression_continuations_and_yields_survive()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"precleanup_mangle_slipped_dup: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
