"""
FIX_RECIPES — карточки-рецепты по коду ошибки (2026-07-10, идея Алекса).

«Разжевать мастеру»: для кодов с КАНОНИЧЕСКИМ фиксом суём в промт готовый
образец было→стало + правило. Слабая/локальная модель копирует паттерн → выше
yield БЕЗ смены модели. Обобщение SECURITY_EXAMPLES на обычные баг-коды.

Доказано вживую: Qwen2.5-Coder-7B на C#-демо БЕЗ рецептов — 2 фикса + 1 reject;
С рецептами — все 3 бага (CS8602/CA1806/CA2000) исправлены, независимый скан = 0
осталось, 0 reject. CA2000 `using var` пришёл прямо из рецепта.

Запуск: python tests/test_phase_fix_recipes.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analysis.constraints.error_constraints import get_fix_recipe, FIX_RECIPES  # noqa: E402
from fixers.prompt_builder import PromptBuilder  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def test_recipes_exist_for_common_codes():
    for code in ("CS8602", "CA2000", "CA1806", "CA2201",
                 "cppcheck::nullPointer", "cppcheck::noExplicitConstructor",
                 "bugprone-switch-missing-default-case"):
        check(f"recipe_{code}", bool(get_fix_recipe(code, "").strip()))


def test_unknown_code_returns_empty():
    check("unknown_empty", get_fix_recipe("CA9999", "csharp") == "")
    check("blank_empty", get_fix_recipe("", "csharp") == "")


def test_recipes_are_before_after_or_rule():
    """Каждый рецепт — либо образец BAD/GOOD, либо чёткое правило (не пусто)."""
    for code, text in FIX_RECIPES.items():
        t = text.strip()
        check(f"nonempty_{code}", len(t) > 20, f"len={len(t)}")


def test_recipe_rendered_into_case_file():
    pb = PromptBuilder()
    cf = pb.build_case_file(
        error={"file": "a.cs", "line": 6, "code": "CA2000", "message": "dispose"},
        file_content="class C {}",
        fix_recipe=get_fix_recipe("CA2000", "csharp"),
    )
    check("section_present", "HOW TO FIX" in cf)
    check("using_pattern_in_prompt", "using var" in cf)


def test_recipe_absent_when_no_recipe():
    pb = PromptBuilder()
    cf = pb.build_case_file(
        error={"file": "a.cs", "line": 1, "code": "CA9999", "message": "x"},
        file_content="class C {}",
        fix_recipe=get_fix_recipe("CA9999", "csharp"),
    )
    check("no_section_when_empty", "HOW TO FIX" not in cf)


if __name__ == "__main__":
    test_recipes_exist_for_common_codes()
    test_unknown_code_returns_empty()
    test_recipes_are_before_after_or_rule()
    test_recipe_rendered_into_case_file()
    test_recipe_absent_when_no_recipe()
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"fix_recipes: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
