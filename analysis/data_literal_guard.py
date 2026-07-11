"""
O.19 — анти-порча данных при mypy-фиксах value/data-кодов.

Живой прогон pyca/bcrypt (2026-07-02, детерминированно воспроизведён на двух
прогонах): LLM «чинила» mypy-ошибки list-item/arg-type/assignment в
tests/test_bcrypt.py, ПОРТЯ тестовые векторы вместо исправления типов:
    4              → "4"                 (int → str)
    b"salt"        → "salt"              (bytes → str, потерян b-префикс)
    b"\x60\x51..." → "6051be18..."       (bytes → str, содержимое переписано)
Каждый такой патч ЛЕГИТИМНО снимал mypy-ошибку (тип реально начинал
совпадать) и не порождал новой flake8-ошибки, поэтому все прежние гейты
молчали: target_recheck подтверждал «исправлено», net_delta_check не
запускался (число ошибок не росло), O.14/O.17 (символы) и O.18 (Any/cast)
не при чём. Финальный аудит откатывал файл постфактум, но на per-patch
уровне патч уходил в ACCEPT.

Ключевое наблюдение по learning_cases: ВСЕ легитимные mypy-фиксы в архиве
(dbus-fast arg-type ×16, pycfmodel arg-type, more-itertools/pytils
assignment, httpx/anyio attr-defined) СОХРАНЯЮТ существующие данные-литералы
дословно — они ДОБАВЛЯЮТ аннотацию/cast/isinstance, переписывают вызов
(`**{...}` → kwargs, оборачивают конструктор) или переименовывают
переменные, но НИКОГДА не понижают тип существующего bytes-/числового
литерала до str и не удаляют его. Единственный признак, отличающий порчу
данных от честного фикса с нулевым collateral по архиву: суммарное число
bytes-литералов ИЛИ числовых литералов в файле УМЕНЬШИЛОСЬ после патча.

Считаем ПО ТИПУ и суммарно (не по значению): это ловит все три формы порчи
(литерал bytes/число исчез или переехал в str) и при этом толерантно к
легитимной замене ЗНАЧЕНИЯ (индекс, размер) — там счётчик по типу не падает.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict

# mypy value/data-коды, которые LLM может «починить» подменой самого литерала
# данных (а не типа/аннотации). Сознательно НЕ включаем attr-defined/
# union-attr/return-value/operator — их эрозию через Any/cast ловит O.18, а
# литералы данных они не трогают; расширение сюда только увеличило бы surface.
DATA_MYPY_CODES = frozenset({
    "list-item", "arg-type", "assignment", "index", "dict-item", "call-arg",
})

# Fallback-детект bytes-литерала для случая, когда AST не парсится
# (b"...", b'...', rb"...", br'...', с любым регистром префикса).
_BYTES_LIT_RE = re.compile(r"""(?<![A-Za-z0-9_])(?:rb|br|b)(['"])""", re.IGNORECASE)


def _count_literals(source: str) -> Dict[str, int]:
    """Считает bytes- и числовые (int/float/complex, кроме bool) литералы в AST.

    Возвращает {"bytes": n, "numeric": m, "parsed": bool}. При провале парсинга
    parsed=False и bytes считается regex-фолбэком, numeric не считается (None-safe:
    вызывающий сравнивает numeric только когда обе стороны parsed)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {"bytes": len(_BYTES_LIT_RE.findall(source)), "numeric": 0, "parsed": False}
    n_bytes = 0
    n_numeric = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            v = node.value
            t = type(v)
            if t is bytes:
                n_bytes += 1
            elif t is int or t is float or t is complex:  # bool исключён: type(True) is bool
                n_numeric += 1
    return {"bytes": n_bytes, "numeric": n_numeric, "parsed": True}


def check_data_literal_mangling(before: str, after: str, error_code: str) -> Dict[str, Any]:
    """Возвращает {"ok": bool, "markers": [...], "reason": str, "detail": {...}}.

    `ok=False` (порча данных) когда для value/data-mypy-кода патч УМЕНЬШИЛ
    суммарное число bytes-литералов или числовых литералов в файле — признак,
    что существующий литерал данных понижен до str/удалён вместо исправления
    типа. Числовой счётчик сравнивается только если ОБЕ стороны распарсились
    (иначе fallback знает лишь про bytes)."""
    if error_code not in DATA_MYPY_CODES:
        return {"ok": True, "markers": [], "reason": "code_not_in_scope", "detail": {}}
    if not isinstance(before, str) or not isinstance(after, str):
        return {"ok": True, "markers": [], "reason": "non_string_input", "detail": {}}

    b = _count_literals(before)
    a = _count_literals(after)
    markers = []
    if a["bytes"] < b["bytes"]:
        markers.append("bytes_literal_demoted")
    if b["parsed"] and a["parsed"] and a["numeric"] < b["numeric"]:
        markers.append("numeric_literal_demoted")

    ok = not markers
    return {
        "ok": ok,
        "markers": markers,
        "reason": "ok" if ok else "data_literal_mangling",
        "detail": {"before": b, "after": a},
    }
