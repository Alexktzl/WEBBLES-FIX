"""
Ранняя полоса для детерминированно-починяемых ошибок живого кода
(2026-07-08, Delgan/loguru, 6 прогонов серии series5).

Инцидент: 3×E203 в loguru/*.py — единственные гарантированно-починяемые
(rule-based `_py_slice_space`) ошибки живого кода — стояли в хвосте очереди
(CLEANUP=30) за 148 нечинимыми корпусными B018/F821 (те же 30-90) и
CI-yaml (SECURITY=90). Прогон ни разу не дошёл до них за 30-минутный
бюджет: 0 ACCEPT на живом коде при 206 LLM-вызовах.

Инвариант: детерминированно-починяемая ошибка файла ВНЕ тестов/CI получает
вес >= 95 — выше LLM-требующих SECURITY (90), но НИЖЕ BLOCKING (100) и
CRITICAL_SYNTAX (200): контракт «сначала чинить то, что не собирается»
не нарушается. Той же ошибке в tests/ или .github/ буст не даётся.

Запуск: python tests/test_phase_prioritize_live_rule_based_first.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def test_can_fix_deterministically_registry():
    from fixers.rule_based_fixer import RuleBasedFixer
    rb = RuleBasedFixer()
    check("e203_is_deterministic", rb.can_fix_deterministically("E203", "python"))
    check("f401_is_deterministic", rb.can_fix_deterministically("F401", "python"))
    check("b018_is_not", not rb.can_fix_deterministically("B018", "python"))
    check("unknown_code_is_not", not rb.can_fix_deterministically("no-such-code", "python"))
    check("empty_code_is_not", not rb.can_fix_deterministically("", "python"))


def test_live_rule_based_outranks_corpus_and_yaml_security():
    """Сквозной инвариант через PrioritizeStage.execute: живой E203 выходит
    в очереди раньше корпусного B018 и CI-yaml SECURITY, но позже BLOCKING."""
    from core.stages.prioritize_stage import PrioritizeStage
    from core.state_machine import State
    from unittest.mock import MagicMock, patch

    live_e203 = {"file": "loguru/_better_exceptions.py", "line": 10, "code": "E203",
                 "message": "whitespace before ':'", "error_class": "CLEANUP"}
    corpus_b018 = {"file": "tests/exceptions/source/modern/x.py", "line": 5, "code": "B018",
                   "message": "useless expression", "error_class": "CLEANUP"}
    yaml_sec = {"file": ".github/workflows/tests.yml", "line": 3,
                "code": "yaml.github-actions.security.github-actions-mutable-action-tag.github-actions-mutable-action-tag",
                "message": "mutable tag", "error_class": "SECURITY"}
    blocking = {"file": "loguru/_logger.py", "line": 1, "code": "import-error",
                "message": "cannot import", "error_class": "BLOCKING"}
    errors = [corpus_b018, yaml_sec, live_e203, blocking]

    stage = PrioritizeStage.__new__(PrioritizeStage)
    stage.priority_engine = MagicMock()
    stage.priority_engine.rank.side_effect = lambda es: es  # сохранить порядок сортировки

    ctx = MagicMock()
    ctx.memory = None  # авто-мок дал бы truthy get_known_fix -> всем 98
    ctx.current_errors = errors
    ctx.language = "python"
    ctx.metadata = {}
    ctx.project_path = Path(".")
    ctx.update.return_value = ctx
    ctx.add_state_to_history.return_value = ctx
    ctx.add_unfixable_error.return_value = ctx

    with patch("core.error_context_validator.ErrorContextValidator.is_valid", return_value=True):
        stage.execute(ctx)

    ranked = list(stage.priority_engine.rank.call_args[0][0])
    order = [e.get("file") for e in ranked]
    w = {e.get("file"): e.get("_weight") for e in ranked}
    check("live_e203_weight_95", w.get("loguru/_better_exceptions.py") == 95.0, f"weights: {w}")
    check("corpus_b018_not_boosted", w.get("tests/exceptions/source/modern/x.py", 0) < 95.0)
    check("blocking_still_first", order[0] == "loguru/_logger.py", f"order: {order}")
    check("live_e203_before_yaml_sec",
          order.index("loguru/_better_exceptions.py") < order.index(".github/workflows/tests.yml"),
          f"order: {order}")
    check("live_e203_before_corpus",
          order.index("loguru/_better_exceptions.py") < order.index("tests/exceptions/source/modern/x.py"),
          f"order: {order}")


def test_same_code_in_tests_dir_not_boosted():
    """E203 в tests/ буст не получает — полоса только для живого кода."""
    from core.stages.prioritize_stage import PrioritizeStage
    from unittest.mock import MagicMock, patch

    test_e203 = {"file": "tests/test_x.py", "line": 2, "code": "E203",
                 "message": "whitespace before ':'", "error_class": "CLEANUP"}
    stage = PrioritizeStage.__new__(PrioritizeStage)
    stage.priority_engine = MagicMock()
    stage.priority_engine.rank.side_effect = lambda es: es
    ctx = MagicMock()
    ctx.memory = None
    ctx.current_errors = [test_e203]
    ctx.language = "python"
    ctx.metadata = {}
    ctx.project_path = Path(".")
    ctx.update.return_value = ctx
    ctx.add_state_to_history.return_value = ctx
    ctx.add_unfixable_error.return_value = ctx
    with patch("core.error_context_validator.ErrorContextValidator.is_valid", return_value=True):
        stage.execute(ctx)
    ranked = list(stage.priority_engine.rank.call_args[0][0])
    check("tests_dir_e203_not_boosted", ranked[0].get("_weight") < 95.0,
          f"weight: {ranked[0].get('_weight')}")


def test_known_memory_fix_outranks_everything_but_blocking():
    """Up-front memory-pass (2026-07-08): ошибка с известным фиксом в
    памяти → вес 98 (выше полосы 95 и SECURITY 90, ниже BLOCKING 100) —
    повторный прогон начинает с реплея доказанных фиксов."""
    from core.stages.prioritize_stage import PrioritizeStage
    from unittest.mock import MagicMock, patch

    known = {"file": "src/x.py", "line": 7, "code": "E501",
             "message": "line too long", "error_class": "CLEANUP"}
    unknown = {"file": "src/y.py", "line": 3, "code": "E501",
               "message": "line too long", "error_class": "CLEANUP"}
    blocking = {"file": "src/z.py", "line": 1, "code": "import-error",
                "message": "no module", "error_class": "BLOCKING"}

    stage = PrioritizeStage.__new__(PrioritizeStage)
    stage.priority_engine = MagicMock()
    stage.priority_engine.rank.side_effect = lambda es: es

    memory = MagicMock()
    from core.pipeline_stage import PipelineStage as _PS
    known_sig = _PS._static_signature(known)
    memory.get_known_fix.side_effect = (
        lambda sig, language=None, file_ext=None: "patch" if sig == known_sig else None
    )

    ctx = MagicMock()
    ctx.current_errors = [unknown, known, blocking]
    ctx.language = "python"
    ctx.metadata = {}
    ctx.memory = memory
    ctx.project_path = Path(".")
    ctx.update.return_value = ctx
    ctx.add_state_to_history.return_value = ctx
    ctx.add_unfixable_error.return_value = ctx

    with patch("core.error_context_validator.ErrorContextValidator.is_valid", return_value=True):
        stage.execute(ctx)

    ranked = list(stage.priority_engine.rank.call_args[0][0])
    order = [e.get("file") for e in ranked]
    w = {e.get("file"): e.get("_weight") for e in ranked}
    check("known_fix_weight_98", w.get("src/x.py") == 98.0, f"weights: {w}")
    check("blocking_still_above_memory", order[0] == "src/z.py", f"order: {order}")
    check("known_before_unknown",
          order.index("src/x.py") < order.index("src/y.py"), f"order: {order}")


if __name__ == "__main__":
    test_can_fix_deterministically_registry()
    test_live_rule_based_outranks_corpus_and_yaml_security()
    test_same_code_in_tests_dir_not_boosted()
    test_known_memory_fix_outranks_everything_but_blocking()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"prioritize_live_rule_based_first: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
