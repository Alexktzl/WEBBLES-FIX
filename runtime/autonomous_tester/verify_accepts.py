"""
Независимая верификация ACCEPT-патчей контрольной серии (не часть Webbles,
тестовая инфраструктура autonomous_tester).

Для каждого "[applied] {...}" события в захваченном stdout прогона:
  1. before = `git show HEAD:<file>` в клоне (исходное содержимое до фикса).
  2. after  = текущее содержимое файла в клоне (после copy-back ACCEPT'а).
  3. Свежий multi-tool рескан (AnalyzeStage.run_full_scan) ПОСЛЕ прогона —
     проверяет, что конкретная (file, code) пара реально пропала из
     анализа, а не просто система сказала ACCEPT.

Вердикты (ось 1 — была ли цель фикса реально достигнута):
  - REAL_FIX        — файл изменился И (file,code) больше не находится
                       свежим сканом. Дальше дробится на:
       * strong  — содержательная правка (логика/код изменена по существу).
       * weak    — ошибка ушла за счёт `Any`/`# type: ignore`/`cast(Any, ...)`/
                    удаления проверки/ослабления типа, без содержательной
                    правки (2026-06-23, Skyscanner/pycfmodel: 3 из 4 REAL_FIX
                    оказались типовой эрозией до `Any`).
  - UNCHANGED_FILE   — файл вообще не изменился (before == after) —
                       ACCEPT заявлен, но патч не применился физически.
  - STILL_FLAGGED    — файл изменился, но та же (file,code) пара ВСЁ ЕЩЁ
                       есть в свежем рескане — лечение не подействовало.

unsafe_accept (ось 2, независимая от вердикта выше) — `check_symbol_regression`
(analysis/symbol_regression.py) на ВСЁМ файле: True, если патч попутно потерял
def/class/import, не относящиеся к целевой ошибке (2026-06-23,
malinkang/toggl2notion: фикс import-not-found стёр 3 из 5 импортированных
имён). REAL_FIX и unsafe_accept=True НЕ взаимоисключающи.

semantic_suspicious_change (ось 3, независимая) — переименование
атрибута/поля/ключа РЯДОМ с целевой правкой, не относящееся к ней (2026-06-23,
Skyscanner/pycfmodel: `.Effect` → `.effect` без связи с исходной ошибкой
attr-defined). В отличие от unsafe_accept (имя ПРОПАЛО), здесь имя ЗАМЕНЕНО
похожим — symbol_regression этого не видит вообще. Эвристика: среди
атрибутов, исчезнувших/появившихся ТОЛЬКО в изменённых строках diff'а, ищем
пары с совпадением без учёта регистра или с маленьким расстоянием
Левенштейна, не совпадающие с именем, упомянутым в error message.

Использование:
    python verify_accepts.py <clone_path> <stdout_log_path> <config_json_path>
Печатает JSON-список записей в stdout.
"""
from __future__ import annotations

import ast
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path


APPLIED_RE = re.compile(r"^\s*\[applied\]\s+(\{.*\})\s*$")

_WEAK_MARKERS = (
    (re.compile(r"\bAny\b"), "any_type"),
    (re.compile(r"#\s*type:\s*ignore"), "type_ignore_comment"),
    (re.compile(r"\bcast\s*\(\s*Any\b"), "cast_any"),
    (re.compile(r"->\s*object\b"), "return_object"),
    (re.compile(r"#\s*noqa\b"), "noqa_suppress"),
)
_CHECK_LINE_RE = re.compile(r"^\s*(assert\b|if\s)")
_QUOTED_NAME_RE = re.compile(r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
_ATTR_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)\b")


def parse_applied_events(log_text: str) -> list:
    out = []
    for line in log_text.splitlines():
        m = APPLIED_RE.match(line)
        if not m:
            continue
        try:
            data = ast.literal_eval(m.group(1))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("file"):
            out.append(data)
    return out


def git_show_head(clone_path: Path, rel_file: str) -> str | None:
    posix_rel = rel_file.replace("\\", "/")
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{posix_rel}"],
            cwd=str(clone_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


def read_current(clone_path: Path, rel_file: str) -> str | None:
    fp = clone_path / rel_file
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def diff_snippet(before: str, after: str, line: int, window: int = 3) -> dict:
    b_lines = before.splitlines()
    a_lines = after.splitlines()
    lo = max(0, line - 1 - window)
    hi_b = min(len(b_lines), line - 1 + window + 1)
    hi_a = min(len(a_lines), line - 1 + window + 1)
    return {
        "before_snippet": b_lines[lo:hi_b],
        "after_snippet": a_lines[lo:hi_a],
    }


def changed_lines(before: str, after: str, target_line: int | None = None, hunk_tol: int = 3) -> tuple:
    """Возвращает (removed_lines, added_lines) — строки, присутствующие
    только в before / только в after, по `difflib.SequenceMatcher`.

    2026-06-23 (control series, openstack/automaton): когда НЕСКОЛЬКО
    ACCEPT трогают ОДИН файл, маркер эрозии от ОДНОГО патча (например,
    `# type: ignore` на строке 23) ложно приписывался ДРУГОМУ патчу на
    соседней строке (32) того же файла — потому что считались ВСЕ хунки
    whole-file диффа, не только тот, что относится к проверяемой ошибке.

    Если `target_line` задан — учитываются только хунки (delete/insert/
    replace), чей диапазон в `before` (i1..i2) находится в пределах
    `hunk_tol` строк от `target_line` (1-based). Без `target_line` —
    старое поведение (весь файл; обратная совместимость для тестов)."""
    sm = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    removed, added = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag not in ("delete", "replace", "insert"):
            continue
        if target_line is not None:
            # i1/i2 — 0-based, target_line — 1-based.
            hunk_lo = i1 + 1 - hunk_tol
            hunk_hi = i2 + hunk_tol
            if not (hunk_lo <= target_line <= hunk_hi):
                continue
        if tag in ("delete", "replace"):
            removed.extend(before.splitlines()[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(after.splitlines()[j1:j2])
    return removed, added


_FALLBACK_ALLOWED_EROSION_CODES = frozenset({
    "import-untyped", "import-not-found", "untyped-decorator",
})


def classify_real_fix_strength(
    removed: list, added: list, error_code: str = "",
    allowed_codes: frozenset = _FALLBACK_ALLOWED_EROSION_CODES,
) -> dict:
    """strong/weak — есть ли в добавленных строках маркеры типовой эрозии
    (Any/type:ignore/cast(Any)/noqa) ИЛИ удалена проверка (assert/if) без
    содержательной замены.

    weak дробится на:
      - by_design   — код входит в ALLOWED_EROSION_CODES (та же константа,
                       что в продакшен-защите analysis/type_erosion_guard.py)
                       — для import-not-found/import-untyped/untyped-decorator
                       `# type: ignore` детерминированный и единственно
                       доступный фикс, это штатная практика, не деградация.
      - concerning  — эрозия на коде, для которого содержательный фикс был
                       в принципе достижим (LLM «сдалась» в Any/cast).
    """
    added_text = "\n".join(added)
    markers = [name for pat, name in _WEAK_MARKERS if pat.search(added_text)]

    removed_checks = [l for l in removed if _CHECK_LINE_RE.match(l)]
    added_checks = [l for l in added if _CHECK_LINE_RE.match(l)]
    if removed_checks and not added_checks:
        markers.append("removed_check_without_replacement")

    if not markers:
        return {"strength": "strong", "weak_markers": [], "weak_category": None}

    weak_category = "by_design" if error_code in allowed_codes else "concerning"
    return {"strength": "weak", "weak_markers": markers, "weak_category": weak_category}


def detect_semantic_suspicious_renames(
    removed: list, added: list, message: str,
) -> list:
    """Ищет переименования атрибутов в изменённых строках, не совпадающие
    с именем из error message (целевым)."""
    target_names = {n.lower() for n in _QUOTED_NAME_RE.findall(message or "")}
    removed_attrs = sorted(set(_ATTR_RE.findall("\n".join(removed))))
    added_attrs = sorted(set(_ATTR_RE.findall("\n".join(added))))
    removed_only = [a for a in removed_attrs if a not in added_attrs]
    added_only = [a for a in added_attrs if a not in removed_attrs]

    suspicious = []
    for old in removed_only:
        if old.lower() in target_names:
            continue
        for new in added_only:
            if new.lower() in target_names:
                continue
            if old == new:
                continue
            if old.lower() == new.lower():
                suspicious.append({"old": old, "new": new, "kind": "case_rename"})
                continue
            if abs(len(old) - len(new)) <= 2:
                dist = _edit_distance(old.lower(), new.lower())
                if dist <= 2 and min(len(old), len(new)) >= 4:
                    suspicious.append({"old": old, "new": new, "kind": "near_rename", "distance": dist})
    return suspicious


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(
                prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb),
            )
        prev = cur
    return prev[-1]


def main() -> int:
    clone_path = Path(sys.argv[1]).resolve()
    log_path = Path(sys.argv[2])
    config_path = Path(sys.argv[3])

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    applied = parse_applied_events(log_text)

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core.pipeline_context import PipelineContext
    from core.stages.analyze_stage import AnalyzeStage
    from analysis.symbol_regression import check_symbol_regression
    from analysis.type_erosion_guard import ALLOWED_EROSION_CODES

    # Язык — 4-й arg (default python). БЕЗ него verify_accepts гонял
    # PythonAnalyzer на любом проекте → для не-python fresh-скан пуст → каждый
    # фикс ложно = REAL_FIX (STILL_FLAGGED не детектился). Теперь диспатчим
    # анализатор по языку — независимая проверка честна для всех языков
    # (2026-07-10, C#-этап).
    language = (sys.argv[4] if len(sys.argv) > 4 else "python").lower()

    def _make_analyzer(lang: str):
        if lang in ("csharp", "cs"):
            from analyzers.csharp_analyzer import CsharpAnalyzer
            return CsharpAnalyzer(), "csharp"
        if lang == "rust":
            from analyzers.rust_analyzer import RustAnalyzer
            return RustAnalyzer(), "rust"
        if lang in ("cpp", "c++", "cxx", "cc", "c"):
            from analyzers.cpp_analyzer import CppAnalyzer
            return CppAnalyzer(), "cpp"
        from analyzers.python_analyzer import PythonAnalyzer
        return PythonAnalyzer(), "python"

    _analyzer, _sym_lang = _make_analyzer(language)

    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    skill_pipeline = cfg.get("pipeline", {}).get("config_overrides", {}).get("pipeline", {})
    run_cfg = {"pipeline": dict(skill_pipeline), "tools": {"semgrep": {"enabled": True}}}

    stage = AnalyzeStage(_analyzer)
    ctx = PipelineContext(project_path=clone_path, language=_sym_lang, config=run_cfg)
    fresh = stage.run_full_scan(ctx, clone_path, force_fresh_semgrep=True)
    fresh_errors = fresh["errors"]

    def fresh_has(rel_file: str, code: str, line: int, tol: int = 5) -> bool:
        norm = rel_file.replace("\\", "/")
        for e in fresh_errors:
            ef = str(e.get("file", "")).replace("\\", "/")
            if ef != norm:
                continue
            if e.get("code") != code:
                continue
            if abs(int(e.get("line", 0) or 0) - line) <= tol:
                return True
        return False

    records = []
    for ev in applied:
        rel_file = ev.get("file", "")
        code = ev.get("code", "")
        line = int(ev.get("line", 0) or 0)
        message = ev.get("message", "")
        before = git_show_head(clone_path, rel_file)
        after = read_current(clone_path, rel_file)
        if before is None or after is None:
            verdict = "UNVERIFIABLE_IO"
        elif before == after:
            verdict = "UNCHANGED_FILE"
        elif fresh_has(rel_file, code, line):
            verdict = "STILL_FLAGGED"
        else:
            verdict = "REAL_FIX"
        rec = {
            "file": rel_file, "line": line, "code": code,
            "message": message[:200],
            "verdict": verdict,
            "unsafe_accept": False,
            "semantic_suspicious_change": [],
        }
        if before is not None and after is not None:
            rec.update(diff_snippet(before, after, line))
            # Сканируем маркеры/переименования ТОЛЬКО в хунке(ах) рядом с
            # целевой строкой — иначе при нескольких ACCEPT в одном файле
            # маркер от ОДНОГО патча ложно приписывается ДРУГОМУ (см.
            # docstring changed_lines).
            removed, added = changed_lines(before, after, target_line=line)

            if verdict == "REAL_FIX":
                rec.update(classify_real_fix_strength(removed, added, code, ALLOWED_EROSION_CODES))

            reg = check_symbol_regression(before, after, _sym_lang)
            if not reg.get("ok", True):
                rec["unsafe_accept"] = True
                rec["unsafe_accept_reason"] = {
                    "missing_defs": reg.get("missing_defs", []),
                    "missing_classes": reg.get("missing_classes", []),
                    "missing_imports": reg.get("missing_imports", []),
                }

            suspicious = detect_semantic_suspicious_renames(removed, added, message)
            if suspicious:
                rec["semantic_suspicious_change"] = suspicious
        records.append(rec)

    # 2026-07-02 (контрольная серия): на Windows-консоли cp1251 print с
    # ensure_ascii=False падал UnicodeEncodeError на не-кириллических
    # символах из диффов (tenacity) — весь verify-результат терялся.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
