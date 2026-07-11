"""
O.18 — анти-эрозия типов после mypy-патча.

Control series 2026-06-23 (Skyscanner/pycfmodel): LLM «решала» mypy-ошибки
union-attr/operator/assignment/return-value добавлением `Any`/`cast(Any, ...)`
вместо содержательной правки — формально ошибка уходила (mypy замолкал), но
типобезопасность снижалась, а не повышалась (все 3 случая — в одном файле
одного прогона, систематический паттерн "сдаться в Any", не случайность).

Не относится к кодам из `GeneratePatchStage._UNFIXABLE_MYPY_CODES`
(import-not-found/import-untyped/untyped-decorator) — там `# type: ignore`
детерминированный и единственно доступный фикс без доступа к интернету для
stub-пакетов (см. `_make_type_ignore_patch`), это НЕ эрозия, а штатная
практика.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

_WEAK_MARKERS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bAny\b"), "any_type"),
    (re.compile(r"#\s*type:\s*ignore"), "type_ignore_comment"),
    (re.compile(r"\bcast\s*\(\s*Any\b"), "cast_any"),
    (re.compile(r"->\s*object\b"), "return_object"),
)

# Та же политика, что у GeneratePatchStage._UNFIXABLE_MYPY_CODES — держим
# отдельной константой здесь, чтобы analysis/ не зависела от core/stages/
# (избегаем циклического импорта).
ALLOWED_EROSION_CODES = frozenset({
    "import-untyped", "import-not-found", "untyped-decorator",
})


def check_type_erosion(before: str, after: str, error_code: str) -> Dict[str, Any]:
    """Возвращает {"ok": bool, "markers": [...], "reason": str}.

    `ok=False` означает: патч УВЕЛИЧИЛ число вхождений маркера типовой
    эрозии (Any/type:ignore/cast(Any)/->object) в файле целиком, а код
    ошибки НЕ входит в `ALLOWED_EROSION_CODES` (то есть содержательный
    фикс был в принципе достижим, и эрозия — это «сдача», а не
    необходимость).

    2026-07-01 (control series, tenacity — 0 ACCEPT, все патчи уходили в
    NEEDS_REVIEW): раньше маркер искался в diff-added строках (line-level
    difflib). Чисто структурная правка — например E704-фиксер разносит
    `def f(): ...  # type: ignore` на две строки — не добавляет НОВЫЙ
    маркер, а переносит существующий на другую строку; line-diff видит
    старую строку как removed и обе новые как added, включая ту же
    подстроку `# type: ignore` — и ложно засчитывал это как эрозию.
    Сравнение по СУММАРНОМУ числу вхождений в файле устойчиво к переносу
    текста между строками и по-прежнему ловит реальную эрозию (см. тесты
    test_phase_type_erosion_guard.py — LLM добавляет NEW Any/cast(Any,...),
    счётчик растёт)."""
    if error_code in ALLOWED_EROSION_CODES:
        return {"ok": True, "markers": [], "reason": "allowed_unfixable_code"}
    if not isinstance(before, str) or not isinstance(after, str):
        return {"ok": True, "markers": [], "reason": "non_string_input"}
    markers = [
        name for pat, name in _WEAK_MARKERS
        if len(pat.findall(after)) > len(pat.findall(before))
    ]
    ok = not markers
    return {"ok": ok, "markers": markers, "reason": "ok" if ok else "type_erosion"}
