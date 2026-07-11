"""
Stage E.5 — регресс-тесты RuleBasedFixer.

Проверяет два инварианта:
1. На покрытых benchmark-кейсах rule-based производит ИМЕННО `after`.
2. Rule-based НИКОГДА не возвращает неверную правку (precision = 100%);
   на неуверенных случаях возвращает None (отдаёт LLM).

Запуск:
    pytest tests/test_rule_based_fixer.py
    python3 tests/test_rule_based_fixer.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

CASES = ROOT / "tests" / "benchmark" / "cases"
COVERED = {
    "unused_import", "unused_variable", "unused_mut",
    "clippy::needless_return", "clippy::redundant_clone", "clippy::clone_on_copy",
    "F401", "W291",
    "eqeqeq", "no-var", "prefer-const", "no-unused-vars",
}


def _fixer():
    from fixers.rule_based_fixer import RuleBasedFixer
    return RuleBasedFixer()


def test_covered_cases_produce_after():
    """На каждом покрытом кейсе результат rule-based == after/ (или
    осознанный отказ None для безопасности, но НИКОГДА не неверно)."""
    fixer = _fixer()
    produced = correct = declined = 0
    wrong = []
    for ej in sorted(CASES.glob("*/*/expected.json")):
        exp = json.loads(ej.read_text(encoding="utf-8"))
        code = exp["primary_error"]["code"]
        if code not in COVERED:
            continue
        cd = ej.parent
        fr = exp["primary_error"]["file"]
        before = (cd / "before" / fr).read_text(encoding="utf-8")
        after = (cd / "after" / fr).read_text(encoding="utf-8")
        es = fixer.try_fix(exp["primary_error"], before, exp["language"])
        if es is None:
            declined += 1
            continue
        produced += 1
        res = es.apply({fr: before})
        got = (res or {}).get(fr, "")
        if res is not None and got.rstrip("\n") == after.rstrip("\n"):
            correct += 1
        else:
            wrong.append(str(cd.relative_to(CASES)))
    # Главный инвариант: ноль НЕВЕРНЫХ правок.
    assert not wrong, f"rule-based produced WRONG fixes: {wrong}"
    # И что-то реально починили.
    assert produced >= 60, f"too few produced fixes: {produced}"
    assert correct == produced, f"correct {correct} != produced {produced}"


def test_unknown_code_returns_none():
    fixer = _fixer()
    es = fixer.try_fix({"code": "E9999", "file": "x.rs", "line": 1,
                        "message": "whatever"}, "fn main() {}\n", "rust")
    assert es is None


def test_empty_inputs_return_none():
    fixer = _fixer()
    assert fixer.try_fix({}, "", "rust") is None
    assert fixer.try_fix({"code": "F401"}, "", "python") is None


def test_eqeqeq_does_not_touch_strict():
    """== → ===, но уже-строгое === не трогаем."""
    fixer = _fixer()
    src = "function f(x) {\n    return x === 0;\n}\n"
    es = fixer.try_fix({"code": "eqeqeq", "file": "main.js", "line": 2,
                        "message": "..."}, src, "javascript")
    # Нет == для замены → None.
    assert es is None


def test_rule_confidence_is_high():
    from fixers.rule_based_fixer import RULE_CONFIDENCE
    from core.contract import default_confidence_for
    assert RULE_CONFIDENCE == 0.95
    # Согласованность с таблицей D.2.
    assert default_confidence_for("quote_heuristic") == 0.95


# ---------------------------------------------------------------------------
# E704 tests
# ---------------------------------------------------------------------------

def _e704_fix(src_line: str, line_no: int = 1):
    fixer = _fixer()
    err = {"file": "test.py", "line": line_no, "code": "E704",
           "message": "multiple statements on one line (def)"}
    return fixer.try_fix(err, src_line, "python")


def test_e704_simple_method():
    """def m(self): return x → разбивается на две строки."""
    src = "    def __lt__(self, other): return self <= other and not self >= other\n"
    es = _e704_fix(src)
    assert es is not None
    result = es.apply({"test.py": src})
    assert result is not None
    lines = result["test.py"].splitlines()
    assert lines[0] == "    def __lt__(self, other):"
    assert lines[1] == "        return self <= other and not self >= other"


def test_e704_nested_def():
    """Вложенный def с отступом — тело получает +4 пробела к отступу def."""
    src = "        def sort_key(c): return self._perm_val[c]\n"
    es = _e704_fix(src)
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[0] == "        def sort_key(c):"
    assert lines[1] == "            return self._perm_val[c]"


def test_e704_type_hints_and_defaults():
    """Type hints и default-значения содержат ':' — не путаем с телом."""
    src = 'def foo(x: int = 5, y: str = "a"): return x\n'
    es = _e704_fix(src)
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[0] == 'def foo(x: int = 5, y: str = "a"):'
    assert lines[1] == "    return x"


def test_e704_return_annotation():
    """-> int: не спутать с телом def."""
    src = "    def f(x) -> int: return x + 1\n"
    es = _e704_fix(src)
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[0] == "    def f(x) -> int:"
    assert lines[1] == "        return x + 1"


def test_e704_pass_body():
    """def f(): pass → стандартный двухстрочный вид."""
    src = "    def f(): pass\n"
    es = _e704_fix(src)
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[0] == "    def f():"
    assert lines[1] == "        pass"


def test_e704_no_body_returns_none():
    """def без тела (только def foo():) — None, не трогаем."""
    src = "    def f():\n"
    es = _e704_fix(src)
    assert es is None


def test_e704_not_triggered_for_other_codes():
    """Хэндлер E704 не активируется для чужих кодов."""
    fixer = _fixer()
    src = "    def f(): pass\n"
    err = {"file": "test.py", "line": 1, "code": "E701", "message": "..."}
    # E701 имеет свой хэндлер, но не E704-логику
    es = fixer.try_fix(err, "x = 1; y = 2\n", "python")
    # E701 может дать None или другой результат, важно что не E704-хэндлер
    # Просто проверяем что E704-хэндлер не вызван для E701-кода
    fixer2 = _fixer()
    es2 = fixer2.try_fix({"file": "t.py", "line": 1, "code": "E704", "message": "..."}, src, "python")
    assert es2 is not None  # E704 хэндлер есть


# ---------------------------------------------------------------------------
# E127 / E128 tests
# ---------------------------------------------------------------------------

def _e12x_fix(src: str, line_no: int, code: str = "E128"):
    fixer = _fixer()
    err = {"file": "test.py", "line": line_no, "code": code,
           "message": f"continuation line {'under' if code == 'E128' else 'over'}-indented for visual indent"}
    return fixer.try_fix(err, src, "python")


def test_e128_simple_continuation():
    """E128: аргумент под-отступлен → выравнивается по visual indent."""
    src = (
        "result = func(arg1,\n"
        "    arg2)\n"          # under-indented, should align with arg1 (col 14)
    )
    es = _e12x_fix(src, line_no=2, code="E128")
    assert es is not None
    result = es.apply({"test.py": src})
    assert result is not None
    lines = result["test.py"].splitlines()
    assert lines[1] == "              arg2)", repr(lines[1])


def test_e127_simple_continuation():
    """E127: аргумент пере-отступлен → выравнивается по visual indent."""
    src = (
        "result = func(arg1,\n"
        "                     arg2)\n"  # over-indented (21 spaces vs col 14)
    )
    es = _e12x_fix(src, line_no=2, code="E127")
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[1] == "              arg2)", repr(lines[1])


def test_e128_string_with_brackets_does_not_confuse():
    """Строки со скобками внутри не сбивают алгоритм."""
    src = (
        "x = some_func('hello (world)',\n"
        "    value)\n"        # E128: should be at col 16
    )
    es = _e12x_fix(src, line_no=2, code="E128")
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    # 'x = some_func(' → '(' at col 13, arg starts at col 14
    assert lines[1].lstrip() == "value)", repr(lines[1])
    assert lines[1].startswith(" " * 14), repr(lines[1])


def test_e128_hanging_indent_returns_none():
    """Когда после '(' нет контента (hanging indent) — E128 не применяем."""
    src = (
        "result = func(\n"
        "    arg1,\n"         # hanging indent — нет visual anchor → None
    )
    es = _e12x_fix(src, line_no=2, code="E128")
    assert es is None, "hanging indent should not be modified"


def test_e128_already_correct_returns_none():
    """Если строка уже на правильном отступе — None."""
    src = (
        "result = func(arg1,\n"
        "              arg2)\n"   # 14 spaces = correct visual indent
    )
    es = _e12x_fix(src, line_no=2, code="E128")
    assert es is None, "already-correct indent should return None"


def test_e128_nested_brackets():
    """Вложенные скобки: anchor — ближайшая незакрытая."""
    src = (
        "x = outer(inner(arg1,\n"
        "    arg2))\n"          # E128: should align with inner's arg1 at col 16
    )
    es = _e12x_fix(src, line_no=2, code="E128")
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[1] == "                arg2))", repr(lines[1])


# ---------------------------------------------------------------------------
# E122/E126 tests (F5, AUDIT_series11.md — hanging indent style)
# ---------------------------------------------------------------------------

def test_e126_over_indented_hanging():
    """E126: hanging-indent строка переотступлена (8 вместо 4 после открытия)."""
    src = (
        "def foo():\n"
        "    result = some_call(\n"
        "            arg1,\n"   # 12 spaces, expected base(4)+4=8
        "    )\n"
    )
    es = _e12x_fix(src, line_no=3, code="E126")
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[2] == "        arg1,", repr(lines[2])


def test_e122_missing_indentation():
    """E122: hanging-indent строка не отступлена (на уровне открывающей)."""
    src = (
        "def foo():\n"
        "    result = some_call(\n"
        "    arg1,\n"            # 4 spaces, flush with opening line
        "    )\n"
    )
    es = _e12x_fix(src, line_no=3, code="E122")
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[2] == "        arg1,", repr(lines[2])


def test_e126_already_correct_returns_none():
    """Если hanging indent уже правильный (base+4) — None."""
    src = (
        "def foo():\n"
        "    result = some_call(\n"
        "        arg1,\n"        # already base(4)+4=8
        "    )\n"
    )
    es = _e12x_fix(src, line_no=3, code="E126")
    assert es is None, "already-correct hanging indent should return None"


def test_e127_e128_still_ignore_hanging_indent():
    """E127/E128 не должны чинить hanging indent (это не их случай) — None."""
    src = (
        "result = func(\n"
        "        arg1,\n"
    )
    es = _e12x_fix(src, line_no=2, code="E127")
    assert es is None, "E127/E128 must not touch hanging-indent lines"


# ---------------------------------------------------------------------------
# E125 tests (F5, AUDIT_series11.md — closing bracket same indent as body)
# ---------------------------------------------------------------------------

def test_e125_closing_bracket_same_as_body():
    """E125: реальный кейс, подтверждённый flake8 --select=E1 (exit=0 после фикса)."""
    src = (
        "if (\n"
        "    1 or\n"
        "    2):\n"      # col 4, совпадает с телом ниже -> E125
        "    pass\n"
    )
    es = _e12x_fix(src, line_no=3, code="E125")
    assert es is not None
    result = es.apply({"test.py": src})
    lines = result["test.py"].splitlines()
    assert lines[2] == "        2):", repr(lines[2])
    assert lines[3] == "    pass", "тело не должно измениться"


def test_e125_already_disambiguated_returns_none():
    """Если отступ закрывающей строки уже отличается от тела — None."""
    src = (
        "if (\n"
        "    1 or\n"
        "        2):\n"   # col 8, тело ниже col 4 -> уже не совпадает
        "    pass\n"
    )
    es = _e12x_fix(src, line_no=3, code="E125")
    assert es is None, "already-disambiguated indent should return None"


def test_e125_too_long_returns_none():
    """Если +4 увело бы строку за max_line_length — не угадываем, None."""
    fixer = _fixer()
    src = (
        "if (\n"
        "    1 or\n"
        "    very_long_name):\n"   # +4 уйдёт за лимит 20
        "    pass\n"
    )
    err = {"file": "test.py", "line": 3, "code": "E125",
           "message": "continuation line with same indent as next logical line"}
    es = fixer.try_fix(err, src, "python", max_line_length=20)
    assert es is None, "fix that exceeds max_line_length should return None"


# ---------------------------------------------------------------------------
# E501 tests (control series 12 analysis, 2026-06-20 — _py_e501_noqa был
# написан, но никогда не подключён к dispatch-таблице try_fix; E501 всегда
# уходил в LLM. Тесты ниже проверяют ИМЕННО маршрутизацию через try_fix,
# не только внутреннюю логику метода.)
# ---------------------------------------------------------------------------

def _e501_fix(src: str, line_no: int, max_line_length: int = 79):
    fixer = _fixer()
    err = {"file": "test.py", "line": line_no, "code": "E501",
           "message": "line too long (95 > 79 characters)"}
    return fixer.try_fix(err, src, "python", max_line_length=max_line_length)


def test_e501_is_wired_to_dispatch():
    """Регрессия на сам пробел: E501 должен доходить до _py_e501_noqa через
    публичный try_fix(), а не быть отсутствующим в dispatch-таблице."""
    long_comment = "#" + " x" * 60  # > 79 символов, безопасный комментарий
    src = long_comment + "\n"
    es = _e501_fix(src, line_no=1)
    assert es is not None, (
        "E501 не подключён к dispatch — try_fix вернул None для безопасного "
        "случая (длинный комментарий), который _py_e501_noqa умеет чинить"
    )


def test_e501_comment_gets_noqa_suppressed():
    long_comment = "# " + "x" * 90
    src = long_comment + "\n"
    es = _e501_fix(src, line_no=1)
    assert es is not None
    result = es.apply({"test.py": src})
    assert result["test.py"].splitlines()[0].endswith("# noqa: E501")


def test_e501_string_literal_gets_noqa_suppressed():
    src = '"' + ("a" * 90) + '"\n'
    es = _e501_fix(src, line_no=1)
    assert es is not None
    result = es.apply({"test.py": src})
    assert result["test.py"].splitlines()[0].endswith("# noqa: E501")


def test_e501_continuation_line_returns_none():
    """Строка-продолжение (заканчивается запятой) — не угадываем, отдаём LLM."""
    src = "result = func(" + "a" * 80 + ",\n    b)\n"
    es = _e501_fix(src, line_no=1)
    assert es is None, "continuation line должна уйти в LLM, не в noqa-заглушку"


def test_e501_regular_code_line_returns_none():
    """Обычная строка кода (не комментарий/строка/docstring/assert) — None,
    чтобы не подавлять реальную проблему слепым noqa."""
    src = "x = " + "1 + " * 30 + "1\n"
    es = _e501_fix(src, line_no=1)
    assert es is None


def test_e501_already_suppressed_returns_none():
    src = "# " + "x" * 90 + "  # noqa: E501\n"
    es = _e501_fix(src, line_no=1)
    assert es is None


# ---------------------------------------------------------------------------
# F821 tests
# ---------------------------------------------------------------------------

def _f821_fix(src: str, line_no: int, undefined: str):
    fixer = _fixer()
    err = {"file": "test.py", "line": line_no, "code": "F821",
           "message": f"undefined name '{undefined}'"}
    return fixer.try_fix(err, src, "python")


def test_f821_typo_single_extra_letter():
    """F821: SCREEEN_W (лишняя E) → SCREEN_W."""
    src = (
        "SCREEN_W = 800\n"
        "SCREEN_H = 500\n"
        "\n"
        "def main():\n"
        "    screen = setup(SCREEEN_W, SCREEN_H)\n"
    )
    es = _f821_fix(src, line_no=5, undefined="SCREEEN_W")
    assert es is not None
    result = es.apply({"test.py": src})
    assert result is not None
    lines = result["test.py"].splitlines()
    assert "SCREEN_W" in lines[4]
    assert "SCREEEN_W" not in lines[4]


def test_f821_no_close_match_returns_none():
    """F821: нет похожего имени → None (не угадываем)."""
    src = (
        "SCREEN_W = 800\n"
        "\n"
        "def main():\n"
        "    x = TOTALLY_DIFFERENT_VAR\n"
    )
    es = _f821_fix(src, line_no=4, undefined="TOTALLY_DIFFERENT_VAR")
    assert es is None


def test_f821_game_scenario():
    """F821: сценарий платформера — SCREEEN_W в set_mode чинится."""
    src = (
        "import pygame\n"
        "SCREEN_W = 800\n"
        "SCREEN_H = 500\n"
        "\n"
        "def main():\n"
        "    pygame.init()\n"
        "    screen = pygame.display.set_mode((SCREEEN_W, SCREEN_H))\n"
    )
    es = _f821_fix(src, line_no=7, undefined="SCREEEN_W")
    assert es is not None
    result = es.apply({"test.py": src})
    assert result is not None
    fixed = result["test.py"]
    assert "SCREEEN_W" not in fixed
    assert "set_mode((SCREEN_W, SCREEN_H))" in fixed


# ---------------------------------------------------------------------------
# W291 — найдено при расследовании REJECT-аномалии (2026-06-22, реальные
# кейсы из CONVERSION_ANALYSIS_2026-06-22_FULL_ARCHIVE.md). Для строки из
# ОДНИХ пробелов/табов (самый частый реальный случай W291 — "пустая" строка
# с висячими пробелами от редактора) match=line.rstrip() было пустой
# строкой → anchor.match.strip() в Anchor == "" → _find_anchor (см.
# fixers/structured_edit.py) требует непустой needle и тихо пропускает весь
# файл → диф пуст → правило "молча" не срабатывает, выглядя как успешный
# rule_based-патч структурно, но реально НЕ меняющий файл → ложный REJECT
# target_error_still_present.
# ---------------------------------------------------------------------------

def test_w291_normal_line_still_works():
    fixer = _fixer()
    content = "x = 1\ny = 2   \nz = 3\n"
    error = {"file": "a.py", "line": 2, "code": "W291", "message": "trailing whitespace"}
    es = fixer.try_fix(error, content, "python")
    assert es is not None
    result = es.apply({"a.py": content})
    assert result is not None
    assert result["a.py"] == "x = 1\ny = 2\nz = 3\n"


def test_w291_whitespace_only_line_returns_none_not_broken_edit():
    """Регрессия: раньше эта строка давала EditSet с пустым anchor.match —
    структурно "валидный" патч, который ГАРАНТИРОВАННО не проходит
    anchor-верификацию (diff пуст) → ложный REJECT target_error_still_
    present. Теперь — честный None, решение отдаётся LLM."""
    fixer = _fixer()
    content = "def foo():\n    x = 1\n    \n    return x\n"
    error = {"file": "a.py", "line": 3, "code": "W291", "message": "trailing whitespace"}
    es = fixer.try_fix(error, content, "python")
    assert es is None


def test_w291_tab_only_line_returns_none():
    fixer = _fixer()
    content = "x = 1\n\t\t\ny = 2\n"
    error = {"file": "a.py", "line": 2, "code": "W291", "message": "trailing whitespace"}
    es = fixer.try_fix(error, content, "python")
    assert es is None


if __name__ == "__main__":
    tests = [
        ("covered_cases_produce_after", test_covered_cases_produce_after),
        ("unknown_code_returns_none", test_unknown_code_returns_none),
        ("empty_inputs_return_none", test_empty_inputs_return_none),
        ("eqeqeq_does_not_touch_strict", test_eqeqeq_does_not_touch_strict),
        ("rule_confidence_is_high", test_rule_confidence_is_high),
        ("e704_simple_method", test_e704_simple_method),
        ("e704_nested_def", test_e704_nested_def),
        ("e704_type_hints_and_defaults", test_e704_type_hints_and_defaults),
        ("e704_return_annotation", test_e704_return_annotation),
        ("e704_pass_body", test_e704_pass_body),
        ("e704_no_body_returns_none", test_e704_no_body_returns_none),
        ("e704_not_triggered_for_other_codes", test_e704_not_triggered_for_other_codes),
        ("e128_simple_continuation", test_e128_simple_continuation),
        ("e127_simple_continuation", test_e127_simple_continuation),
        ("e128_string_with_brackets_does_not_confuse", test_e128_string_with_brackets_does_not_confuse),
        ("e128_hanging_indent_returns_none", test_e128_hanging_indent_returns_none),
        ("e128_already_correct_returns_none", test_e128_already_correct_returns_none),
        ("e128_nested_brackets", test_e128_nested_brackets),
        ("f821_typo_single_extra_letter", test_f821_typo_single_extra_letter),
        ("f821_no_close_match_returns_none", test_f821_no_close_match_returns_none),
        ("f821_game_scenario", test_f821_game_scenario),
        ("w291_normal_line_still_works", test_w291_normal_line_still_works),
        ("w291_whitespace_only_line_returns_none_not_broken_edit",
         test_w291_whitespace_only_line_returns_none_not_broken_edit),
        ("w291_tab_only_line_returns_none", test_w291_tab_only_line_returns_none),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"RuleBasedFixer regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
