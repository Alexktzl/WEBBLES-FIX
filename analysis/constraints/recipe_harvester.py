"""
Авто-сбор карточек-рецептов из ПОДТВЕРЖДЁННЫХ побед (2026-07-10, идея Алекса).

Система сама учится: починила ошибку код X, независимая проверка подтвердила →
вынимаем «было→стало» и делаем авто-рецепт для X. В следующий раз X чинится по
готовому образцу.

БЕЗОПАСНОСТЬ = ФИЛЬТР. Плохой рецепт тиражирует ошибку на все проекты, поэтому
берём ТОЛЬКО безупречные победы. Многоступенчатый фильтр (все гейты обязательны):

  Per-record (is_harvestable):
    1. verdict == REAL_FIX      — независимо подтверждён (ошибка реально исчезла)
    2. unsafe_accept == False   — НИКОГДА не учимся на тронувшем инвариант
    3. strength == "strong"     — не weak (слабый/сомнительный фикс)
    4. НЕТ weak_markers         — verify не пометил подозрительным
    5. before/after непустые и РАЗНЫЕ — реальное изменение
    6. компактно                — локальный паттерн, не переписывание файла
    7. не suppression           — after не добавляет noqa/nosec/allow/type:ignore
    8. код НЕ контекст-зависимый — dead-code/логика не имеют канона (денилист)

  Per-code (distill, промоушен):
    9. >= MIN_WINS различных побед — один фарт не становится каноном
   10. победы КОНСИСТЕНТНЫ        — трансформации похожи (иначе контекст-зависимо)

КУДА: авто-рецепты в отдельный store `runtime/auto_recipes.json` (НЕ в ручные
FIX_RECIPES). get_fix_recipe читает ручные ПЕРВЫМИ (высший траст), авто —
фолбэком. Авто-рецепты помечены (человек может ревьюить/чистить).
"""

from __future__ import annotations

import glob
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MIN_WINS = 2            # минимум различных подтверждённых побед на код
MAX_SNIPPET_LINES = 8   # компактность: макс. строк в before/after
MAX_RECIPE_EXAMPLES = 2  # сколько образцов держим на код

# Маркеры подавления — если after ИХ добавляет, это фейк-фикс, не рецепт.
_SUPPRESSION_RE = re.compile(
    r"#\s*(noqa|nosec|type:\s*ignore)|nosemgrep|#\[allow\(|//\s*NOLINT|@SuppressWarnings",
    re.IGNORECASE,
)

# Коды, где ПРОХОЖДЕНИЕ ЛИНТЕРА != КОРРЕКТНОСТЬ фикса: правильный фикс требует
# ВНЕШНЕГО ФАКТА, который ни модель, ни verify (тот же линтер) проверить не могут
# — валидный SHA экшена под КОНКРЕТНЫЙ репозиторий, точная версия, рабочий URL.
# Инцидент 2026-07-10 (simdjson): модель пиннила ВСЕ разные github-actions на
# ОДИН запомненный SHA (actions/checkout) → у setup-cmake и пр. такого SHA нет,
# workflow сломается, но semgrep видит «пиннут» и засчитывает REAL_FIX. Такие
# «победы» НЕЛЬЗЯ учить (научат неверным SHA) — исключаем из рецептов И датасета.
_UNVERIFIABLE_CORRECTNESS_CODES = frozenset({
    "yaml.github-actions.security.github-actions-mutable-action-tag."
    "github-actions-mutable-action-tag",
})

# Коды БЕЗ канонического фикса — трансформация зависит от конкретной логики,
# обобщённый рецепт ввёл бы модель в заблуждение. Не авто-харвестим (рецепты).
_NON_CANONICAL_CODES = frozenset({
    "CA1508",                              # мёртвое условие — чинить логику
    "cppcheck::knownConditionTrueFalse",
    "cppcheck::duplicateBranch", "bugprone-branch-clone",  # зависит от веток
    "invalid-syntax", "E999", "syntax",    # синтаксис — контекст файла
    "list-item", "arg-type", "assignment", "return-value",  # mypy-типы контекстны
}) | _UNVERIFIABLE_CORRECTNESS_CODES


import re as _re


def _infer_language(code: str, given: Any = None) -> Optional[str]:
    """Язык из явного поля, иначе из ПРЕФИКСА кода (verify-записи языка не
    несут → 198 «None», 2026-07-11)."""
    g = (given or "").lower()
    if g:
        return g
    c = (code or "").strip()
    if _re.match(r"^(CS|CA|IDE|SA|SX|IDISP)\d", c):
        return "csharp"
    if c.startswith(("cppcheck::", "bugprone-", "clang-", "GCC_", "modernize-",
                     "readability-", "performance-", "misc-", "cppcoreguidelines-")):
        return "cpp"
    if c.startswith(("clippy::", "RUSTSEC")):
        return "rust"
    if _re.match(r"^(E|W|F|C|B|D|N|S|T|A|I)\d", c):  # flake8/pyflakes/plugins
        return "python"
    if c in ("attr-defined", "arg-type", "name-defined", "call-arg", "return-value",
             "assignment", "import-not-found", "list-item", "index", "union-attr",
             "var-annotated", "no-redef", "misc", "valid-type", "type-var"):
        return "python"  # mypy
    if c.startswith("python."):
        return "python"
    if c.startswith(("yaml.", "html.", "package_managers.")):
        return "config"  # semgrep на конфигах/разметке
    return None


def _norm_lines(snip: Any) -> List[str]:
    if isinstance(snip, list):
        return [str(x) for x in snip]
    if isinstance(snip, str):
        return snip.splitlines()
    return []


def is_harvestable(rec: Dict[str, Any]) -> Tuple[bool, str]:
    """Per-record гейт. Возвращает (ок, причина_отказа)."""
    if not isinstance(rec, dict):
        return False, "not_dict"
    if rec.get("verdict") != "REAL_FIX":
        return False, "not_real_fix"
    if rec.get("unsafe_accept") is True:
        return False, "unsafe_accept"
    if rec.get("strength") != "strong":
        return False, "not_strong"
    if rec.get("weak_markers"):
        return False, "weak_markers"
    if rec.get("semantic_suspicious_change"):
        return False, "semantic_suspicious"
    code = (rec.get("code") or "").strip()
    if not code:
        return False, "no_code"
    if code in _NON_CANONICAL_CODES:
        return False, "non_canonical_code"
    before = _norm_lines(rec.get("before_snippet"))
    after = _norm_lines(rec.get("after_snippet"))
    b = [l for l in before if l.strip()]
    a = [l for l in after if l.strip()]
    if not b or not a:
        return False, "empty_snippet"
    if b == a:
        return False, "no_change"
    if len(before) > MAX_SNIPPET_LINES or len(after) > MAX_SNIPPET_LINES:
        return False, "too_large"
    # suppression: after добавил маркер, которого не было в before
    added = "\n".join(after)
    if _SUPPRESSION_RE.search(added) and not _SUPPRESSION_RE.search("\n".join(before)):
        return False, "adds_suppression"
    return True, "ok"


def _change_signature(rec: Dict[str, Any]) -> str:
    """Грубая подпись трансформации для проверки консистентности: какие
    непустые строки ушли/пришли (нормализованные по пробелам)."""
    before = {re.sub(r"\s+", " ", l).strip() for l in _norm_lines(rec.get("before_snippet")) if l.strip()}
    after = {re.sub(r"\s+", " ", l).strip() for l in _norm_lines(rec.get("after_snippet")) if l.strip()}
    removed = tuple(sorted(before - after))
    addeds = tuple(sorted(after - before))
    return repr((removed, addeds))


def _tokens_added(rec: Dict[str, Any]) -> set:
    before = " ".join(_norm_lines(rec.get("before_snippet")))
    after = " ".join(_norm_lines(rec.get("after_snippet")))
    bt = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", before))
    at = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", after))
    return at - bt


def _consistent(recs: List[Dict[str, Any]]) -> bool:
    """Победы консистентны, если разные примеры вводят ПЕРЕСЕКАЮЩИЙСЯ набор
    новых токенов (напр. везде появляется `using`, `explicit`, `?.`), т.е.
    трансформация повторяющаяся, а не случайная под конкретный контекст."""
    if len(recs) < MIN_WINS:
        return False
    token_sets = [_tokens_added(r) for r in recs]
    token_sets = [t for t in token_sets if t]
    if len(token_sets) < MIN_WINS:
        return False
    common = set.intersection(*token_sets)
    # хотя бы один содержательный общий добавленный токен (>2 символов)
    return any(len(t) > 2 for t in common)


def _format_recipe(code: str, recs: List[Dict[str, Any]]) -> str:
    examples = []
    for r in recs[:MAX_RECIPE_EXAMPLES]:
        b = "\n".join(l for l in _norm_lines(r.get("before_snippet")) if l.strip())
        a = "\n".join(l for l in _norm_lines(r.get("after_snippet")) if l.strip())
        examples.append(f"BAD:\n{b}\nGOOD:\n{a}")
    body = "\n---\n".join(examples)
    return (f"{body}\n(авто-рецепт из {len(recs)} подтверждённых фиксов — "
            f"следуй паттерну было→стало)")


def harvest(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Из плоского списка verify_accepts-записей → {code: {"recipe":..,"wins":N}}.
    Каждый code проходит per-record фильтр + консистентность + MIN_WINS."""
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        ok, _reason = is_harvestable(rec)
        if not ok:
            continue
        by_code.setdefault((rec.get("code") or "").strip(), []).append(rec)

    out: Dict[str, Dict[str, str]] = {}
    for code, recs in by_code.items():
        # дедуп по подписи трансформации (различные победы)
        seen, uniq = set(), []
        for r in recs:
            sig = _change_signature(r)
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append(r)
        if len(uniq) < MIN_WINS:
            continue
        if not _consistent(uniq):
            continue
        out[code] = {"recipe": _format_recipe(code, uniq), "wins": len(uniq)}
    return out


def is_dataset_worthy(rec: Dict[str, Any]) -> Tuple[bool, str]:
    """Гейт для ДАТАСЕТА (актив: обучающие примеры проверенных фиксов). Те же
    SAFETY-гейты, что is_harvestable, НО без ограничений на канон/компактность —
    датасету нужны ВСЕ безупречные победы (в т.ч. контекст-зависимые и крупные),
    важна лишь безопасность и что это реальный подтверждённый фикс."""
    if not isinstance(rec, dict):
        return False, "not_dict"
    if rec.get("verdict") != "REAL_FIX":
        return False, "not_real_fix"
    if rec.get("unsafe_accept") is True:
        return False, "unsafe_accept"
    if rec.get("strength") != "strong":
        return False, "not_strong"
    if rec.get("weak_markers"):
        return False, "weak_markers"
    if rec.get("semantic_suspicious_change"):
        return False, "semantic_suspicious"
    code = (rec.get("code") or "").strip()
    if not code:
        return False, "no_code"
    # линтер-прошёл != корректно (SHA/версия) — не эталон, может быть неверным
    if code in _UNVERIFIABLE_CORRECTNESS_CODES:
        return False, "unverifiable_correctness"
    before = _norm_lines(rec.get("before_snippet"))
    after = _norm_lines(rec.get("after_snippet"))
    b = [l for l in before if l.strip()]
    a = [l for l in after if l.strip()]
    if not b or not a or b == a:
        return False, "no_change"
    added = "\n".join(after)
    if _SUPPRESSION_RE.search(added) and not _SUPPRESSION_RE.search("\n".join(before)):
        return False, "adds_suppression"
    return True, "ok"


def _ds_row(label: str, reason: str, *, language, code, message,
            before, after, patch=None, source, verdict=None, strength=None):
    return {
        "label": label,            # "positive" | "negative"
        "reason": reason,          # почему (для negative: тип провала)
        "language": _infer_language(code, language),
        "code": (code or "").strip(),
        "message": (message or "")[:300],
        "before": list(before or []),
        "after": list(after or []),
        "patch": patch,            # для learning-негативов (кандидат-диф)
        "verdict": verdict,
        "strength": strength,
        "source": source,          # "verify" | "learning"
    }


def build_dataset(verify_records: List[Dict[str, Any]],
                  learning_records: Optional[List[Dict[str, Any]]] = None
                  ) -> List[Dict[str, Any]]:
    """Строки датасета с ЧЁТКОЙ разметкой positive/negative.

    POSITIVE — подтверждённые безопасные фиксы (обучающий эталон «как надо»).
    NEGATIVE — доказанно ПЛОХИЕ попытки (модель учится, чего НЕ делать):
      * unsafe_accept==True — патч сломал символы, поймал guard (ценнейшее);
      * verdict==STILL_FLAGGED — патч применён, но ошибку не устранил;
      * learning REJECT — патч отклонён валидацией (net-delta/target/rollback).
    """
    rows: List[Dict[str, Any]] = []
    # --- POSITIVE + verify-NEGATIVE ---
    for rec in verify_records:
        ok, _ = is_dataset_worthy(rec)
        base = dict(
            language=rec.get("language"), code=rec.get("code"),
            message=rec.get("message"),
            before=_norm_lines(rec.get("before_snippet")),
            after=_norm_lines(rec.get("after_snippet")),
            source="verify", verdict=rec.get("verdict"),
            strength=rec.get("strength"),
        )
        if ok:
            rows.append(_ds_row("positive", "verified_real_fix", **base))
        elif rec.get("unsafe_accept") is True:
            rows.append(_ds_row("negative", "unsafe_symbol_loss", **base))
        elif rec.get("verdict") == "STILL_FLAGGED":
            # только с реальным изменением (иначе нечему учить)
            b = [l for l in base["before"] if l.strip()]
            a = [l for l in base["after"] if l.strip()]
            if b and a and b != a:
                rows.append(_ds_row("negative", "still_flagged_no_effect", **base))
    # --- learning-SINGLE (объём): REJECT → negative, чистый ACCEPT → positive ---
    for rec in (learning_records or []):
        dec = (rec.get("decision") or "").upper()
        patch = rec.get("patch_candidate")
        if not patch:
            continue
        if dec == "REJECT":
            rows.append(_ds_row(
                "negative", str(rec.get("decision_reason") or "reject")[:80],
                language=rec.get("language"), code=rec.get("error_code"),
                message=rec.get("error_message"),
                before=_norm_lines(rec.get("original_context")), after=[],
                patch=_patch_str(patch), source="learning", verdict="REJECT",
            ))
        elif dec == "ACCEPT" and _clean_accept(rec):
            # in-pipeline «чистая победа» (прокси; слабее verify REAL_FIX —
            # помечено source=learning, обучение может взвесить ниже)
            rows.append(_ds_row(
                "positive", "clean_accept_inpipeline",
                language=rec.get("language"), code=rec.get("error_code"),
                message=rec.get("error_message"),
                before=_norm_lines(rec.get("original_context")), after=[],
                patch=_patch_str(patch), source="learning", verdict="ACCEPT",
            ))
    return rows


def _row_key(row: Dict[str, Any]) -> str:
    return repr((row.get("label"), row.get("code"),
                 tuple(row.get("before") or []), tuple(row.get("after") or []),
                 row.get("patch"),
                 row.get("chosen"), row.get("rejected")))  # DPO-строки


def append_dataset(rows: List[Dict[str, Any]], path: Path) -> int:
    """Дописывает НОВЫЕ строки в datasets/verified_fixes.jsonl (дедуп по
    code+before+after). Возвращает число добавленных."""
    import time
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing.add(_row_key(json.loads(line)))
            except Exception:
                continue
    added = 0
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            k = _row_key(row)
            if k in existing:
                continue
            existing.add(k)
            row = dict(row, harvested_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            added += 1
    return added


def _clean_accept(rec: Dict[str, Any]) -> bool:
    """«Чистая» принятая попытка из learning_cases (in-pipeline прокси качества,
    т.к. независимого verify-вердикта в learning_cases нет): цель устранена,
    ошибок не прибавилось, без отката, есть патч, без suppression."""
    if (rec.get("decision") or "").upper() != "ACCEPT":
        return False
    if not rec.get("patch_candidate"):
        return False
    if rec.get("target_removed") is False:
        return False
    nd = rec.get("net_delta")
    if isinstance(nd, (int, float)) and nd > 0:
        return False
    if rec.get("rollback_used") is True:
        return False
    p = rec.get("patch_candidate")
    if _SUPPRESSION_RE.search(p if isinstance(p, str) else json.dumps(p)):
        return False
    code = (rec.get("error_code") or "").strip()
    if code in _UNVERIFIABLE_CORRECTNESS_CODES:
        return False
    return True


def _sig(rec: Dict[str, Any]) -> str:
    return str(rec.get("error_signature")
               or (rec.get("error_code"), rec.get("file_path"), rec.get("error_line")))


def _patch_str(p) -> str:
    return (p if isinstance(p, str) else json.dumps(p, ensure_ascii=False))[:3000]


def build_dpo_pairs(learning_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DPO-пары (chosen/rejected на ОДНУ находку) из learning_cases — самый
    ценный формат для дообучения. Для находки, где есть И чистый ACCEPT, И
    REJECT-с-патчем: chosen=принятый фикс, rejected=отклонённая попытка."""
    by_sig: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for rec in learning_records:
        dec = (rec.get("decision") or "").upper()
        if dec not in ("ACCEPT", "REJECT"):
            continue
        by_sig.setdefault(_sig(rec), {"a": [], "r": []})
        (by_sig[_sig(rec)]["a"] if dec == "ACCEPT" else by_sig[_sig(rec)]["r"]).append(rec)

    pairs = []
    for sig, grp in by_sig.items():
        chosen = next((a for a in grp["a"] if _clean_accept(a)), None)
        rejected = next((r for r in grp["r"] if r.get("patch_candidate")), None)
        if not chosen or not rejected:
            continue
        pairs.append({
            "code": (chosen.get("error_code") or "").strip(),
            "language": (chosen.get("language") or "").lower() or None,
            "message": (chosen.get("error_message") or "")[:300],
            "context": _norm_lines(chosen.get("original_context"))[:12],
            "chosen": _patch_str(chosen.get("patch_candidate")),
            "rejected": _patch_str(rejected.get("patch_candidate")),
            "rejected_reason": str(rejected.get("decision_reason") or "reject")[:80],
            "source": "learning",
        })
    return pairs


def _load_verify_records(source_dirs: List[Path]) -> List[Dict[str, Any]]:
    """verify_accepts-записи из результатов прогонов. Источник —
    runtime/_verify_fix/results/*.json (там top-level `verify_accepts` со
    снимками before/after); statistic/ имеет другую схему (decisions), но
    сканируем и его на случай, если ключ появится."""
    recs: List[Dict[str, Any]] = []
    for d0 in source_dirs:
        # rglob надёжнее glob-строки с Windows-разделителями
        for f in Path(d0).rglob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            va = d.get("verify_accepts") if isinstance(d, dict) else None
            if isinstance(va, list):
                recs.extend(x for x in va if isinstance(x, dict))
    return recs


def _load_learning_records(root: Path) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    p = Path(root) / "runtime" / "learning_cases.jsonl"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    return recs


def run_harvest(root: Path, out_path: Path) -> Dict[str, Any]:
    """Читает результаты прогонов → (1) авто-рецепты (auto_recipes.json),
    (2) размеченный датасет positive/negative (datasets/fix_dataset.jsonl)."""
    root = Path(root)
    verify = _load_verify_records([
        root / "runtime" / "_verify_fix" / "results",
        root / "statistic",
    ])
    learning = _load_learning_records(root)

    # (1) авто-рецепты — строгий фильтр + консистентность
    recipes = harvest(verify)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8")

    # (2) датасет — positive (проверенные+чистые) + negative (плохие попытки)
    ds_rows = build_dataset(verify, learning)
    added = append_dataset(ds_rows, root / "datasets" / "fix_dataset.jsonl")
    from collections import Counter
    labels = Counter(r["label"] for r in ds_rows)

    # (3) DPO-пары (chosen/rejected на одну находку) — для дообучения
    pairs = build_dpo_pairs(learning)
    dpo_added = append_dataset(pairs, root / "datasets" / "dpo_pairs.jsonl")

    logger.info("recipe_harvester: %d verify + %d learning → %d рецептов; "
                "датасет +%d (pos=%d neg=%d); DPO-пар +%d",
                len(verify), len(learning), len(recipes), added,
                labels.get("positive", 0), labels.get("negative", 0), dpo_added)
    return {"verify_records": len(verify), "learning_records": len(learning),
            "recipes": len(recipes), "codes": sorted(recipes.keys()),
            "dataset_added": added,
            "dataset_positive": labels.get("positive", 0),
            "dataset_negative": labels.get("negative", 0),
            "dpo_pairs_added": dpo_added}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    summary = run_harvest(root, root / "runtime" / "auto_recipes.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
