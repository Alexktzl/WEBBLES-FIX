"""
Stage D.6 — AI-fingerprint detector.

Эвристически определяет, насколько вероятно код «написан AI».
Применение: в `DecideStage._classify_decision` (D.4) если score > 0.5 —
поднимаем порог ACCEPT на +0.1 (т.е. для AI-кода требуем confidence ≥ 0.95
вместо 0.85). Идея: на AI-сгенерированном коде патчи чаще оказываются
«плохими, но правдоподобными» — нам нужна более строгая планка.

Эвристики (каждая даёт собственный sub-score ∈ [0,1], итог — взвешенная
сумма с clamp в [0,1]):

1. **Placeholder-комментарии:** `TODO`, `FIXME`, `XXX`, «implementation here»,
   «your code here», «add logic», «later». LLM любит такие, как маркер
   «я ещё не дописал». Признак сильный.
2. **Дубли блоков:** одна и та же непустая строка ≥5 раз. LLM иногда
   копирует целые блоки вместо рефакторинга.
3. **Гипер-длинные имена:** идентификаторы длиннее 35 символов. Особенно
   `verbose_function_name_that_describes_everything`. Признак средний —
   у людей тоже бывают длинные имена, но не так массово.
4. **Лишние импорты рядом с активным кодом:** импорты, которые НЕ
   встречаются в файле как имена (off-the-shelf «на всякий случай»).
5. **Смешанные стили:** одновременно tabs и spaces в индентации; CRLF
   и LF в одном файле; mixed snake_case + camelCase для своих функций.

Все эвристики — лёгкие regex-based + AST-light. Никакого LLM, никакого
выхода в сеть. Файл анализируется автономно.
"""

import logging
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# Веса эвристик (сумма = 1.0). Подбираются грубо — точная калибровка
# придёт после Stage F бенчмарка.
_WEIGHTS = {
    "placeholder_comments": 0.30,
    "duplicate_blocks":     0.20,
    "hyper_long_names":     0.15,
    "useless_imports":      0.15,
    "mixed_styles":         0.20,
}


# === Placeholder-комментарии ====================================
_PLACEHOLDER_RE = re.compile(
    r"\b("
    r"TODO|FIXME|XXX|HACK|"
    r"implementation\s+here|"
    r"your\s+code\s+here|"
    r"add\s+(?:logic|implementation|code)\s+here|"
    r"placeholder|"
    r"\.\.\.|"
    r"unimplemented!"
    r")\b",
    re.IGNORECASE,
)


def _score_placeholder_comments(text: str) -> float:
    """Доля «placeholder»-маркеров от общего числа строк, capped."""
    lines = text.splitlines() or [""]
    matches = sum(1 for ln in lines if _PLACEHOLDER_RE.search(ln))
    # 1 placeholder на 10 строк уже подозрительно; 1 на 3 строк — уверенный AI.
    ratio = matches / max(1, len(lines))
    return min(1.0, ratio * 10)


# === Дубли блоков ===============================================
def _score_duplicate_blocks(text: str) -> float:
    """Самая частая непустая строка длиной ≥10 — сколько раз встречается?
    Один и тот же блок повторённый 5+ раз — характерный AI-паттерн."""
    meaningful = [
        ln.strip() for ln in text.splitlines()
        if len(ln.strip()) >= 10 and not ln.strip().startswith(("#", "//", "/*", "*"))
    ]
    if not meaningful:
        return 0.0
    counter = Counter(meaningful)
    top_count = counter.most_common(1)[0][1]
    if top_count < 3:
        return 0.0
    # 3 повтора = 0.3, 5 = 0.6, ≥8 = 1.0.
    return min(1.0, (top_count - 2) / 6)


# === Гипер-длинные имена =========================================
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{34,}\b")  # ≥35 символов


def _score_hyper_long_names(text: str) -> float:
    """Сколько уникальных идентификаторов длиной ≥35? Норма ≤1; 3+ — заметно."""
    long_names = set(_IDENT_RE.findall(text))
    if not long_names:
        return 0.0
    return min(1.0, len(long_names) / 5)


# === «Лишние» импорты ============================================
_RUST_USE_RE = re.compile(r"^\s*use\s+([A-Za-z_][\w:]*)", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w\.]*)", re.MULTILINE)


def _score_useless_imports(text: str) -> float:
    """Доля импортов, которые НЕ встречаются в теле файла. Грубо: для
    каждого import-токена смотрим, попадается ли его последний segment
    (`Player` из `crate::Player`, `os` из `import os`) где-то ниже."""
    imports: List[str] = []
    for m in _RUST_USE_RE.finditer(text):
        imports.append(m.group(1).split("::")[-1])
    for m in _PY_IMPORT_RE.finditer(text):
        imports.append(m.group(1).split(".")[-1])
    if not imports:
        return 0.0
    body = text  # ищем по всему файлу — это даёт false negatives, но дёшево.
    unused = 0
    for name in imports:
        if not name:
            continue
        # одно вхождение — это сам импорт; нужно ≥2 чтобы считать «использован».
        if len(re.findall(rf"\b{re.escape(name)}\b", body)) <= 1:
            unused += 1
    ratio = unused / max(1, len(imports))
    return min(1.0, ratio)


# === Смешанные стили =============================================
_SNAKE_FN = re.compile(r"\b(?:def|fn)\s+([a-z][a-z0-9_]*)", re.IGNORECASE)
_CAMEL_FN = re.compile(r"\b(?:def|fn)\s+([a-z][A-Za-z0-9]*[A-Z]\w*)")


def _score_mixed_styles(text: str) -> float:
    """Mixed indentation (tabs+spaces), mixed line endings (\\r\\n + \\n),
    mixed naming (snake + camel для пользовательских функций)."""
    score = 0.0
    # 1) Indentation
    has_tabs = "\n\t" in text
    has_4space_indent = "\n    " in text
    if has_tabs and has_4space_indent:
        score += 0.4
    # 2) Line endings
    has_crlf = "\r\n" in text
    # Если файл не пустой и содержит \n, без \r\n у нас чистый LF.
    has_lone_lf = "\n" in text.replace("\r\n", "")
    if has_crlf and has_lone_lf:
        score += 0.3
    # 3) Smешанный naming у функций. Не для встроенных языковых функций,
    # а для тех что определяются здесь же.
    snake_names = set(_SNAKE_FN.findall(text))
    camel_names = set(_CAMEL_FN.findall(text))
    # _SNAKE_FN жадно ловит и camel — отфильтруем «настоящий» snake (с _ или
    # сплошным lowercase).
    real_snake = {n for n in snake_names if "_" in n or n.islower()}
    if real_snake and camel_names:
        score += 0.3
    return min(1.0, score)


# === Сводный score ===============================================
def detect(text: str) -> Dict[str, Any]:
    """Возвращает `{'score': float ∈ [0,1], 'features': {name: sub_score}}`.

    Не бросает исключений; на пустом входе вернёт `{score: 0.0, features: {}}`.
    """
    if not text or not text.strip():
        return {"score": 0.0, "features": {}}
    features: Dict[str, float] = {
        "placeholder_comments": _score_placeholder_comments(text),
        "duplicate_blocks":     _score_duplicate_blocks(text),
        "hyper_long_names":     _score_hyper_long_names(text),
        "useless_imports":      _score_useless_imports(text),
        "mixed_styles":         _score_mixed_styles(text),
    }
    score = sum(features[k] * _WEIGHTS[k] for k in features)
    score = max(0.0, min(1.0, score))
    return {"score": score, "features": features}


def score(text: str) -> float:
    """Удобный сокращённый API: только финальный score."""
    return detect(text)["score"]
