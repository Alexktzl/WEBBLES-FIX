"""
2026-06-23 — Skyscanner/pycfmodel (control series): из 11 ACCEPT 4 ушли в
REAL_FIX, но 3 из них достигнуты типовой эрозией (`Any`), не содержательной
правкой; отдельно один STILL_FLAGGED-патч переименовал `.Effect` -> `.effect`
без связи с целевой ошибкой (semantic_suspicious_change, не покрывается
symbol_regression — имя не пропало, оно заменено похожим).

Тесты для runtime/autonomous_tester/verify_accepts.py (тестовая
инфраструктура, не часть Webbles, но логика чистая и тестируемая отдельно).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime" / "autonomous_tester"))

from verify_accepts import (  # noqa: E402
    changed_lines, classify_real_fix_strength, detect_semantic_suspicious_renames,
)


def test_any_annotation_is_weak():
    before = "def f(x):\n    result = {}\n    return result\n"
    after = "def f(x):\n    result: Dict[Any, Any] = {}\n    return result\n"
    r, a = changed_lines(before, after)
    out = classify_real_fix_strength(r, a)
    assert out["strength"] == "weak"
    assert "any_type" in out["weak_markers"]


def test_type_ignore_comment_is_weak():
    before = "import notionhub.client\n"
    after = "import notionhub.client  # type: ignore[import-not-found]\n"
    r, a = changed_lines(before, after)
    out = classify_real_fix_strength(r, a)
    assert out["strength"] == "weak"
    assert "type_ignore_comment" in out["weak_markers"]


def test_cast_any_is_weak():
    before = "value = compute()\n"
    after = "value = cast(Any, compute())\n"
    r, a = changed_lines(before, after)
    out = classify_real_fix_strength(r, a)
    assert out["strength"] == "weak"
    assert "cast_any" in out["weak_markers"]


def test_removed_check_without_replacement_is_weak():
    before = "if x is not None:\n    use(x)\n"
    after = "use(x)\n"
    r, a = changed_lines(before, after)
    out = classify_real_fix_strength(r, a)
    assert out["strength"] == "weak"
    assert "removed_check_without_replacement" in out["weak_markers"]


def test_genuine_logic_fix_is_strong():
    before = "def f(x):\n    return x.group(1)\n"
    after = "def f(x):\n    match = x\n    if match:\n        return match.group(1)\n    return None\n"
    r, a = changed_lines(before, after)
    out = classify_real_fix_strength(r, a)
    assert out["strength"] == "strong"
    assert out["weak_markers"] == []


def test_case_rename_detected_as_suspicious():
    before = ["    if self._is_statement_effect_allow(statement.Effect):"]
    after = [
        "    if not isinstance(statement, Statement):",
        "        continue",
        "    if self._is_statement_effect_allow(statement.effect):",
    ]
    sus = detect_semantic_suspicious_renames(before, after, 'has no attribute "get_expanded_action_list"')
    assert any(s["old"] == "Effect" and s["new"] == "effect" for s in sus)


def test_rename_matching_target_name_not_flagged():
    """Если переименованное имя — само есть в error message (целевая
    правка), это НЕ suspicious."""
    before = ["    return obj.old_name"]
    after = ["    return obj.new_name"]
    sus = detect_semantic_suspicious_renames(before, after, 'attribute "old_name" is deprecated, use "new_name"')
    assert sus == []


def test_unrelated_attrs_not_falsely_paired():
    before = ["    return obj.foo"]
    after = ["    return obj.bar"]
    sus = detect_semantic_suspicious_renames(before, after, "")
    assert sus == []


def test_target_line_scoping_prevents_cross_hunk_marker_leak():
    """2026-06-23 (openstack/automaton): два ACCEPT в ОДНОМ файле на
    близких строках (23 и 32) — маркер `# type: ignore` от строки 23
    ложно приписывался strong-фиксу на строке 32 (замена
    `obj.attr = x` на `obj.set_attr(x)`), когда changed_lines() сканировал
    ВЕСЬ файл вместо хунка рядом с целевой строкой."""
    before = (
        "from testtools import testcase\n"  # line 1
        "\n"  # 2
        "\n"  # 3
        "class X:\n"  # 4
        "    def f(self):\n"  # 5
        "        m = Machine()\n"  # 6
        "        m.default_start_state = start_state\n"  # 7
        "        return m\n"  # 8
    )
    after = (
        "from testtools import testcase  # type: ignore[import-not-found]\n"
        "\n"
        "\n"
        "class X:\n"
        "    def f(self):\n"
        "        m = Machine()\n"
        "        m.set_default_start_state(start_state)\n"
        "        return m\n"
    )
    # Без scoping (target_line=None) маркер от строки 1 "видит" и фикс на
    # строке 7 — старое (неправильное) поведение для сравнения:
    r_whole, a_whole = changed_lines(before, after)
    whole_out = classify_real_fix_strength(r_whole, a_whole, "misc")
    assert whole_out["strength"] == "weak"  # старая ошибка: ложно weak

    # Со scoping по целевой строке 7 — фикс правильно strong:
    r_scoped, a_scoped = changed_lines(before, after, target_line=7)
    scoped_out = classify_real_fix_strength(r_scoped, a_scoped, "misc")
    assert scoped_out["strength"] == "strong"
    assert scoped_out["weak_markers"] == []

    # И наоборот: со scoping по строке 1 (import) — by-design weak, не
    # видит фикс на строке 7:
    r_import, a_import = changed_lines(before, after, target_line=1)
    import_out = classify_real_fix_strength(r_import, a_import, "import-not-found")
    assert import_out["strength"] == "weak"
    assert "type_ignore_comment" in import_out["weak_markers"]
