"""
Авто-сбор рецептов из побед — ФИЛЬТР (2026-07-10, идея Алекса).

Безопасность фичи = строгость фильтра: плохой рецепт тиражирует ошибку. Тесты
кодируют, что КАЖДЫЙ гейт режет опасное и пропускает только безупречные победы.

Запуск: python tests/test_phase_recipe_harvester.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analysis.constraints.recipe_harvester import (  # noqa: E402
    is_harvestable, harvest, MIN_WINS,
)

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def _win(**over):
    """Базовая ЧИСТАЯ победа (проходит все гейты), с переопределениями."""
    rec = {
        "verdict": "REAL_FIX",
        "unsafe_accept": False,
        "strength": "strong",
        "weak_markers": [],
        "semantic_suspicious_change": [],
        "code": "CA2000",
        "before_snippet": ["var r = new StreamReader(path);"],
        "after_snippet": ["using var r = new StreamReader(path);"],
    }
    rec.update(over)
    return rec


def test_clean_win_passes():
    ok, why = is_harvestable(_win())
    check("clean_win_ok", ok, why)


def test_rejects_unsafe_accept():
    ok, why = is_harvestable(_win(unsafe_accept=True))
    check("unsafe_rejected", not ok and why == "unsafe_accept")


def test_rejects_weak_strength():
    ok, why = is_harvestable(_win(strength="weak"))
    check("weak_rejected", not ok and why == "not_strong")


def test_rejects_weak_markers():
    ok, why = is_harvestable(_win(weak_markers=["comment_only"]))
    check("weak_markers_rejected", not ok and why == "weak_markers")


def test_rejects_not_real_fix():
    for v in ("STILL_FLAGGED", "UNCHANGED_FILE", "UNVERIFIABLE_IO"):
        ok, why = is_harvestable(_win(verdict=v))
        check(f"reject_{v}", not ok and why == "not_real_fix")


def test_rejects_semantic_suspicious():
    ok, why = is_harvestable(_win(semantic_suspicious_change=["renamed_symbol"]))
    check("semantic_rejected", not ok and why == "semantic_suspicious")


def test_rejects_added_suppression():
    ok, why = is_harvestable(_win(
        before_snippet=["result = eval(x)"],
        after_snippet=["result = eval(x)  # noqa"], code="E999b"))
    check("suppression_rejected", not ok and why == "adds_suppression")


def test_rejects_non_canonical_code():
    ok, why = is_harvestable(_win(code="CA1508",
                                  before_snippet=["if (n < 0) return;"],
                                  after_snippet=["return;"]))
    check("non_canonical_rejected", not ok and why == "non_canonical_code")


def test_rejects_no_change_and_empty():
    ok, _ = is_harvestable(_win(after_snippet=["var r = new StreamReader(path);"]))
    check("no_change_rejected", not ok)
    ok2, _ = is_harvestable(_win(after_snippet=["", "  "]))
    check("empty_after_rejected", not ok2)


def test_rejects_too_large():
    big = [f"line{i}" for i in range(20)]
    ok, why = is_harvestable(_win(after_snippet=big))
    check("too_large_rejected", not ok and why == "too_large")


# ---- промоушен (harvest): консистентность + MIN_WINS ----

def test_single_win_not_promoted():
    """Один фарт не становится рецептом (нужно >= MIN_WINS)."""
    recipes = harvest([_win()])
    check("single_win_no_recipe", "CA2000" not in recipes, f"MIN_WINS={MIN_WINS}")


def test_consistent_wins_promoted():
    """Несколько побед с ОБЩИМ добавленным токеном (`using`) → рецепт."""
    recipes = harvest([
        _win(before_snippet=["var a = new StreamReader(p);"],
             after_snippet=["using var a = new StreamReader(p);"]),
        _win(before_snippet=["var b = new FileStream(q);"],
             after_snippet=["using var b = new FileStream(q);"]),
    ])
    check("consistent_promoted", "CA2000" in recipes)
    check("recipe_has_body", "using var" in recipes.get("CA2000", {}).get("recipe", ""))


def test_inconsistent_wins_not_promoted():
    """Победы без общего паттерна (разные трансформации) → НЕ рецепт
    (контекст-зависимый код, обобщать нельзя)."""
    recipes = harvest([
        _win(code="CAX", before_snippet=["foo();"], after_snippet=["bar();"]),
        _win(code="CAX", before_snippet=["alpha();"], after_snippet=["beta();"]),
    ])
    check("inconsistent_not_promoted", "CAX" not in recipes)


def test_dataset_labels_positive_and_negative():
    from analysis.constraints.recipe_harvester import build_dataset
    verify = [
        _win(),  # чистая победа → positive
        _win(unsafe_accept=True, verdict="STILL_FLAGGED"),  # сломал символы → negative
        {"verdict": "STILL_FLAGGED", "unsafe_accept": False, "strength": "strong",
         "code": "CA9", "before_snippet": ["a();"], "after_snippet": ["b();"],
         "weak_markers": []},  # применён, не помог → negative
    ]
    learning = [
        {"decision": "REJECT", "error_code": "E1", "patch_candidate": "--- diff ---",
         "original_context": "x=1", "decision_reason": "error_count_not_decreased"},
    ]
    rows = build_dataset(verify, learning)
    labels = {}
    for r in rows:
        labels.setdefault(r["label"], []).append(r)
    check("has_positive", len(labels.get("positive", [])) >= 1)
    check("has_negative", len(labels.get("negative", [])) >= 2)
    # unsafe попал в negative с правильной причиной
    reasons = {r["reason"] for r in labels.get("negative", [])}
    check("unsafe_labeled", "unsafe_symbol_loss" in reasons, f"reasons={reasons}")
    check("reject_labeled", any("error_count" in r for r in reasons), f"reasons={reasons}")
    # каждая строка размечена явно
    check("every_row_labeled", all(r["label"] in ("positive", "negative") for r in rows))


def test_excludes_unverifiable_correctness():
    """linter-прошёл != корректно (SHA/версия): mutable-action-tag НЕ идёт ни в
    рецепты, ни в датасет-позитив (модель могла бы выучить неверный SHA)."""
    from analysis.constraints.recipe_harvester import is_dataset_worthy
    rec = _win(
        code="yaml.github-actions.security.github-actions-mutable-action-tag."
             "github-actions-mutable-action-tag",
        before_snippet=["uses: x@v4"],
        after_snippet=["uses: x@8ade135a41bc03ea155e62e844d188df1ea18608"])
    ok_r, why_r = is_harvestable(rec)
    check("recipe_excludes_unverifiable", not ok_r and why_r == "non_canonical_code")
    ok_d, why_d = is_dataset_worthy(rec)
    check("dataset_excludes_unverifiable", not ok_d and why_d == "unverifiable_correctness")


def test_dataset_excludes_unlabeled_junk():
    """Не-REAL_FIX без unsafe/still-flagged и не-REJECT — не попадает никуда."""
    from analysis.constraints.recipe_harvester import build_dataset
    rows = build_dataset([_win(verdict="UNCHANGED_FILE")], [{"decision": "ACCEPT"}])
    check("junk_excluded", rows == [])


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"recipe_harvester: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
