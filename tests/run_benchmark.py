"""
Stage F.2 — детерминистический бенчмарк-runner для Webbles Fix.

Запускает курированные кейсы из `tests/benchmark/cases/<lang>/<case>/`
(см. README.md) и измеряет, насколько хорошо отрабатывают **детерминистические**
компоненты системы: SymbolGraph (C.1), error_constraints (C.2),
ai_authored_detector (D.6), EditSet roundtrip (B.1–B.2), SecurityScanner (K).
LLM-вызовы здесь **не делаются** — бенчмарк должен быть воспроизводим до символа.

Что измеряется по каждому кейсу:

1. **structure** — `expected.json` корректно загружается, есть `before/`,
   `after/`, обязательные поля.
2. **constraints** — если в `expected.json.constraints_test` указано
   `expected_dont_contains` (или `expected_do_contains`) — проверяется,
   что `get_constraints(code)` возвращает соответствующие подсказки.
3. **symbol_graph** — если в `expected.json.symbol_graph_test` указано
   `must_find_definition_of: X` — проверяется, что `SymbolGraph(before).
   find_related_definitions({code, message, file, line})` находит
   определение X.
4. **security_detect / security_autofix** — для SECURITY-кейсов (Stage K):
   детектор находит код на нужной строке в before/ и не находит в after/;
   autofixable-находки чинятся rule-based точно в after/, не-autofixable —
   отклоняются (→ NEEDS_REVIEW).
5. **ai_authored_detector** — для каждого `before`-файла считается AI-score
   и проверяется, что он `< 0.5` (наши кейсы — рукописные минимальные,
   не должны триггерить AI-флаг).

Запуск:
    python3 tests/run_benchmark.py
    python3 tests/run_benchmark.py --language rust   # фильтр по языку

Выход: per-case строки + сводная таблица accuracy по классам.

Stage F.3 (регресс-тесты по фазам) — в отдельных файлах
`tests/test_phase_*.py`. Этот runner — для прогона на больших наборах
кейсов; pytest — для проверки конкретных инвариантов фаз.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# tests/ → корень проекта → sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Глушим лог-шум pipeline-компонентов; runner печатает своё.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

CASES_ROOT = ROOT / "tests" / "benchmark" / "cases"


# =================================================================
# Checks
# =================================================================
@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        mark = "OK " if self.passed else "FAIL"
        return f"  [{mark}] {self.name}: {self.detail}"


@dataclass
class CaseResult:
    language: str
    case: str
    category: str
    code: str
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)


def _check_structure(case_dir: Path, expected: Dict[str, Any]) -> CheckResult:
    """expected.json содержит обязательные поля, before/after — непустые."""
    must = ["description", "language", "category", "primary_error"]
    missing = [k for k in must if k not in expected]
    if missing:
        return CheckResult("structure", False, f"missing keys: {missing}")
    if not (case_dir / "before").exists():
        return CheckResult("structure", False, "no before/ dir")
    if not (case_dir / "after").exists():
        return CheckResult("structure", False, "no after/ dir")
    err = expected["primary_error"]
    if not all(k in err for k in ("code", "file", "line", "error_class", "message")):
        return CheckResult("structure", False, f"primary_error incomplete: {err}")
    # before/после должны различаться (иначе кейс ничего не тестирует).
    bf = (case_dir / "before" / err["file"]).read_text(encoding="utf-8", errors="ignore")
    af = (case_dir / "after" / err["file"]).read_text(encoding="utf-8", errors="ignore")
    if bf == af:
        return CheckResult("structure", False, "before == after (no fix to test)")
    return CheckResult("structure", True, f"category={expected['category']}")


def _check_constraints(expected: Dict[str, Any]) -> Optional[CheckResult]:
    """Если в кейсе указан constraints_test — проверяем."""
    ct = expected.get("constraints_test")
    if not ct:
        return None
    try:
        from analysis.constraints.error_constraints import get_constraints
    except Exception as e:
        return CheckResult("constraints", False, f"import error: {e}")
    code = expected["primary_error"]["code"]
    do_list, dont_list = get_constraints(code)
    want_dont = ct.get("expected_dont_contains")
    want_do = ct.get("expected_do_contains")
    failures: List[str] = []
    if want_dont:
        joined = " ".join(dont_list).lower()
        if want_dont.lower() not in joined:
            failures.append(f"missing DON'T fragment {want_dont!r}")
    if want_do:
        joined = " ".join(do_list).lower()
        if want_do.lower() not in joined:
            failures.append(f"missing DO fragment {want_do!r}")
    if failures:
        return CheckResult("constraints", False, "; ".join(failures))
    detail = f"do={len(do_list)} dont={len(dont_list)}"
    return CheckResult("constraints", True, detail)


def _check_symbol_graph(case_dir: Path, expected: Dict[str, Any]) -> Optional[CheckResult]:
    """Если в кейсе указан symbol_graph_test — проверяем."""
    sgt = expected.get("symbol_graph_test")
    if not sgt:
        return None
    target_sym = sgt.get("must_find_definition_of")
    if not target_sym:
        return None
    try:
        from analysis.symbol_graph import SymbolGraph
    except Exception as e:
        return CheckResult("symbol_graph", False, f"import error: {e}")
    lang_norm = {"rust": "rust", "python": "python", "javascript": "javascript"}[
        expected["language"]
    ]
    sg = SymbolGraph(case_dir / "before", lang_norm)
    err = expected["primary_error"]
    result = sg.find_related_definitions(err, working_path=case_dir / "before",
                                         max_symbols=5)
    if target_sym not in result:
        return CheckResult(
            "symbol_graph", False,
            f"definition of {target_sym!r} not found in result (len={len(result)})",
        )
    return CheckResult("symbol_graph", True, f"found definition of {target_sym!r}")


def _check_security_detect(case_dir: Path, expected: Dict[str, Any]) -> Optional[CheckResult]:
    """Для SECURITY-кейсов: детектор должен найти ожидаемый код на ожидаемой
    строке в before/, и НЕ находить его в after/ (фикс закрывает дыру).

    Это превращает security-кейсы в реальную проверку детекции (Stage K),
    а не только классификации/constraints.
    """
    if expected.get("category") != "SECURITY":
        return None
    try:
        from analysis.security_scanner import SecurityScanner
    except Exception as e:
        return CheckResult("security_detect", False, f"import error: {e}")
    err = expected["primary_error"]
    scanner = SecurityScanner()
    lang = expected["language"]
    before = scanner.scan_file(case_dir / "before" / err["file"], lang, rel=err["file"])
    hit = any(f["code"] == err["code"] and f["line"] == err["line"] for f in before)
    if not hit:
        got = [(f["code"], f["line"]) for f in before]
        return CheckResult("security_detect", False,
                           f"{err['code']}@{err['line']} not detected; got {got}")
    after = scanner.scan_file(case_dir / "after" / err["file"], lang, rel=err["file"])
    if any(f["code"] == err["code"] for f in after):
        return CheckResult("security_detect", False,
                           f"{err['code']} still flagged in after/ (fix doesn't clear it)")
    return CheckResult("security_detect", True,
                       f"detected {err['code']}@{err['line']}, after/ clean")


def _check_security_autofix(case_dir: Path, expected: Dict[str, Any]) -> Optional[CheckResult]:
    """Для SECURITY-кейсов: проверяем политику «авто-чинить тривиальное,
    остальное — в review» детерминированно.

    Берём autofixable-флаг находки детектора. Если код помечен autofixable —
    RuleBasedFixer обязан выдать EditSet, применение которого даёт ровно
    содержимое `after/`. Если НЕ autofixable — RuleBasedFixer обязан вернуть
    None (отказ → находка пойдёт в NEEDS_REVIEW, а не тихо применится).
    """
    if expected.get("category") != "SECURITY":
        return None
    try:
        from analysis.security_scanner import SecurityScanner
        from fixers.rule_based_fixer import RuleBasedFixer
    except Exception as e:
        return CheckResult("security_autofix", False, f"import error: {e}")
    err = expected["primary_error"]
    lang = expected["language"]
    before_path = case_dir / "before" / err["file"]
    before = before_path.read_text(encoding="utf-8", errors="ignore")

    findings = SecurityScanner().scan_source(before, lang, file_rel=err["file"])
    finding = next((f for f in findings
                    if f["code"] == err["code"] and f["line"] == err["line"]), None)
    if finding is None:
        return CheckResult("security_autofix", False, "no detector finding to drive autofix")

    fixer = RuleBasedFixer()
    edit_set = fixer.try_fix(finding, before, lang)

    if not finding.get("autofixable"):
        if edit_set is None:
            return CheckResult("security_autofix", True, "non-trivial → declined (review)")
        return CheckResult("security_autofix", False,
                           f"non-autofixable {err['code']} should be declined, got a fix")

    if edit_set is None:
        return CheckResult("security_autofix", False,
                           f"autofixable {err['code']} but RuleBasedFixer declined")
    applied = edit_set.apply({err["file"]: before})
    if not applied or err["file"] not in applied:
        return CheckResult("security_autofix", False, "EditSet.apply produced nothing")
    after = (case_dir / "after" / err["file"]).read_text(encoding="utf-8", errors="ignore")
    if applied[err["file"]] != after:
        return CheckResult("security_autofix", False,
                           "auto-fixed result != after/ golden")
    return CheckResult("security_autofix", True, f"auto-fixed {err['code']} == after/")


def _check_ai_authored(case_dir: Path, expected: Dict[str, Any]) -> CheckResult:
    """`before`-файл — рукописный минимальный, AI-score должен быть < 0.5."""
    try:
        from analysis.ai_authored_detector import score
    except Exception as e:
        return CheckResult("ai_authored", False, f"import error: {e}")
    file_rel = expected["primary_error"]["file"]
    text = (case_dir / "before" / file_rel).read_text(encoding="utf-8", errors="ignore")
    s = score(text)
    if s >= 0.5:
        return CheckResult(
            "ai_authored", False,
            f"score={s:.2f} ≥ 0.5 — кейс заподозрен в AI-style, пересмотри",
        )
    return CheckResult("ai_authored", True, f"score={s:.2f}")


# =================================================================
# Per-case runner
# =================================================================
def _run_case(case_dir: Path) -> CaseResult:
    expected_path = case_dir / "expected.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    language = expected.get("language", "?")
    category = expected.get("category", "?")
    code = expected.get("primary_error", {}).get("code", "?")
    result = CaseResult(language=language, case=case_dir.name,
                        category=category, code=code)

    # 1. structure
    result.checks.append(_check_structure(case_dir, expected))
    if not result.checks[-1].passed:
        return result  # дальше нет смысла

    # 2. constraints (опционально)
    cc = _check_constraints(expected)
    if cc is not None:
        result.checks.append(cc)

    # 3. symbol_graph (опционально)
    sgc = _check_symbol_graph(case_dir, expected)
    if sgc is not None:
        result.checks.append(sgc)

    # 3b. security_detect (опционально, только для SECURITY-кейсов)
    secc = _check_security_detect(case_dir, expected)
    if secc is not None:
        result.checks.append(secc)

    # 3c. security_autofix (опционально, только для SECURITY-кейсов)
    secf = _check_security_autofix(case_dir, expected)
    if secf is not None:
        result.checks.append(secf)

    # 4. ai_authored_detector
    result.checks.append(_check_ai_authored(case_dir, expected))
    return result


# =================================================================
# CLI
# =================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Stage F benchmark runner")
    parser.add_argument("--language", help="filter by language (rust/python/javascript)")
    parser.add_argument("--cases-root", default=str(CASES_ROOT))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    root = Path(args.cases_root)
    if not root.exists():
        print(f"FATAL: cases dir not found: {root}", file=sys.stderr)
        return 2

    case_dirs: List[Path] = sorted(
        p for p in root.glob("*/*") if p.is_dir() and (p / "expected.json").exists()
    )
    if args.language:
        case_dirs = [p for p in case_dirs if p.parent.name == args.language]

    if not case_dirs:
        print(f"FATAL: no cases found under {root}", file=sys.stderr)
        return 2

    results: List[CaseResult] = []
    for cd in case_dirs:
        try:
            r = _run_case(cd)
        except Exception as e:
            r = CaseResult(language="?", case=cd.name, category="?", code="?")
            r.checks.append(CheckResult(
                "<runner error>", False,
                f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}",
            ))
        results.append(r)
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.language:>10} / {r.case:<32} "
              f"({r.category:<14} {r.code:<10}) — {r.passed_count}/{r.total} checks")
        if args.verbose or not r.passed:
            for ch in r.checks:
                print(ch)

    # === Сводка ===
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    total_checks = sum(r.total for r in results)
    passed_checks = sum(r.passed_count for r in results)
    print(f"Cases:  {passed}/{total} pass  ({100*passed/total:.1f}%)")
    print(f"Checks: {passed_checks}/{total_checks} pass  ({100*passed_checks/total_checks:.1f}%)")

    print()
    print("By language:")
    by_lang: Dict[str, List[CaseResult]] = {}
    for r in results:
        by_lang.setdefault(r.language, []).append(r)
    for lang in sorted(by_lang):
        sub = by_lang[lang]
        sp = sum(1 for r in sub if r.passed)
        print(f"  {lang:<12} {sp}/{len(sub)}")

    print()
    print("By category:")
    by_cat: Dict[str, List[CaseResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)
    for cat in sorted(by_cat):
        sub = by_cat[cat]
        sp = sum(1 for r in sub if r.passed)
        print(f"  {cat:<16} {sp}/{len(sub)}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
