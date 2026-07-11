"""
Python version-aware analysis (2026-06-22).

Контекст: анализ конверсии (CONVERSION_ANALYSIS_2026-06-22_FULL_ARCHIVE.md)
показал, что invalid-syntax/E999 = 33% всего объёма решений с accept-долей
1.8%/17.3% — крупнейшая категория потерь ACCEPT во всём пайплайне. Живая
проверка на 5 реальных GitHub-кейсах (project_e999_semantic_recovery_verdict)
показала: 4 из 5 "неразрешимых E999" были НЕ багом, а несовместимостью версий
Python — код использует PEP 695 (`def foo[T](...)`, `type X = ...`, generic
классы `class Foo[T]:`), валидный в Python 3.12+, но не парсящийся в
окружении пайплайна (3.11.x).

Цель этого модуля: определить, что конкретная "синтаксическая ошибка" на
самом деле — артефакт несовместимости версий Python, а не баг в коде, ДО
того как она попадёт в classify/repair-конвейер. Обнаруженные случаи
помечаются `unsupported_python_version`/`needs_environment_upgrade` и
ИСКЛЮЧАЮТСЯ из current_errors/initial_errors целиком (см.
core/stages/analyze_stage.py) — не REJECT, не NEEDS_REVIEW, отдельная
категория, не портящая метрики конверсии.

Два источника детекта (комбинируются, не взаимоисключающие):
1. **Метаданные проекта** — `pyproject.toml` (`[project] requires-python`,
   `[tool.poetry.dependencies] python`, classifiers), `setup.cfg`
   (`[options] python_requires`), `setup.py` (`python_requires=` — текстовый
   поиск, БЕЗ выполнения файла).
2. **Fallback по сигнатурам синтаксиса** — если метаданных нет/они не
   говорят явно о версии новее текущей, ищем характерные паттерны PEP 695
   (`type X = ...`, `def f[T](...)`, `class C[T]:`) непосредственно в
   содержимом файла. Если файл их использует — это сильный сигнал, что
   ВЕСЬ файл рассчитан на Python 3.12+, независимо от того, что написано
   (или не написано) в манифестах проекта.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CURRENT_PYTHON_VERSION: Tuple[int, int] = sys.version_info[:2]


# ---------------------------------------------------------------------------
# Версия из метаданных проекта
# ---------------------------------------------------------------------------

# Берём МИНИМАЛЬНУЮ версию из спецификатора вида ">=3.12", "^3.12", "~=3.12",
# "==3.12.*", ">=3.10,<4". Не претендует на полный PEP 440 — нам нужна только
# нижняя граница major.minor.
_VERSION_NUM_RE = re.compile(r'(\d+)\.(\d+)')


def parse_min_version(spec: str) -> Optional[Tuple[int, int]]:
    """Извлекает минимальную major.minor версию из строки-спецификатора
    зависимости (requires-python/python_requires/poetry python)."""
    if not spec:
        return None
    nums = _VERSION_NUM_RE.findall(spec)
    if not nums:
        return None
    # Для составных спецификаторов (">=3.10,<4") берём ПЕРВОЕ совпадение —
    # обычно это нижняя граница; нам важен MAX среди всех найденных версий >=,
    # но в подавляющем большинстве реальных спецификаторов одна версия.
    versions = [(int(maj), int(min_)) for maj, min_ in nums]
    return max(versions)


_CLASSIFIER_RE = re.compile(
    r'Programming\s+Language\s*::\s*Python\s*::\s*(\d+)\.(\d+)', re.IGNORECASE
)


def _max_classifier_version(text: str) -> Optional[Tuple[int, int]]:
    versions = [(int(maj), int(min_)) for maj, min_ in _CLASSIFIER_RE.findall(text)]
    return max(versions) if versions else None


def detect_required_version_from_pyproject(project_path: Path) -> Optional[Tuple[int, int]]:
    pp = Path(project_path) / "pyproject.toml"
    if not pp.exists():
        return None
    try:
        text = pp.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.debug("python_version_detector: cannot read pyproject.toml: %s", e)
        return None

    try:
        try:
            import tomllib  # Python 3.11+
            data = tomllib.loads(text)
        except ImportError:
            import toml  # type: ignore
            data = toml.loads(text)
    except Exception:
        data = None

    if isinstance(data, dict):
        req = (data.get("project") or {}).get("requires-python")
        v = parse_min_version(req) if isinstance(req, str) else None
        if v:
            return v
        poetry_py = (
            (data.get("tool") or {}).get("poetry", {}).get("dependencies", {}).get("python")
        )
        v = parse_min_version(poetry_py) if isinstance(poetry_py, str) else None
        if v:
            return v
        classifiers = (data.get("project") or {}).get("classifiers") or []
        if isinstance(classifiers, list):
            v = _max_classifier_version("\n".join(str(c) for c in classifiers))
            if v:
                return v

    # Fallback: TOML-парсер недоступен/файл не строго валиден — текстовый поиск.
    m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', text)
    if m:
        v = parse_min_version(m.group(1))
        if v:
            return v
    v = _max_classifier_version(text)
    if v:
        return v
    return None


def detect_required_version_from_setup_cfg(project_path: Path) -> Optional[Tuple[int, int]]:
    sc = Path(project_path) / "setup.cfg"
    if not sc.exists():
        return None
    try:
        text = sc.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'python_requires\s*=\s*(.+)', text)
    if m:
        v = parse_min_version(m.group(1).strip())
        if v:
            return v
    v = _max_classifier_version(text)
    if v:
        return v
    return None


def detect_required_version_from_setup_py(project_path: Path) -> Optional[Tuple[int, int]]:
    """Текстовый поиск (НЕ выполняет setup.py — это исполняемый код,
    запускать его ради метаданных небезопасно)."""
    sp = Path(project_path) / "setup.py"
    if not sp.exists():
        return None
    try:
        text = sp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', text)
    if m:
        v = parse_min_version(m.group(1))
        if v:
            return v
    v = _max_classifier_version(text)
    if v:
        return v
    return None


def detect_project_python_version(project_path: Path) -> Optional[Tuple[int, int]]:
    """Пробует все источники метаданных проекта по порядку, возвращает
    первую найденную минимальную требуемую версию."""
    for fn in (
        detect_required_version_from_pyproject,
        detect_required_version_from_setup_cfg,
        detect_required_version_from_setup_py,
    ):
        try:
            v = fn(project_path)
        except Exception as e:
            logger.debug("python_version_detector: %s failed: %s", fn.__name__, e)
            v = None
        if v:
            return v
    return None


# ---------------------------------------------------------------------------
# Fallback: сигнатуры синтаксиса PEP 695 (и др. version-specific конструкции)
# ---------------------------------------------------------------------------

# Каждый паттерн привязан к минимальной версии Python, в которой он появился.
_SYNTAX_SIGNATURES: Tuple[Tuple[re.Pattern, Tuple[int, int], str], ...] = (
    (
        re.compile(r'^\s*type\s+[A-Za-z_]\w*\s*(\[[^\]\n]*\])?\s*=', re.MULTILINE),
        (3, 12),
        "PEP 695 type alias statement (`type X = ...`)",
    ),
    (
        re.compile(r'^\s*(?:async\s+)?def\s+[A-Za-z_]\w*\s*\[', re.MULTILINE),
        (3, 12),
        "PEP 695 generic function syntax (`def f[T](...)`)",
    ),
    (
        re.compile(r'^\s*class\s+[A-Za-z_]\w*\s*\[', re.MULTILINE),
        (3, 12),
        "PEP 695 generic class syntax (`class C[T]:`)",
    ),
    (
        re.compile(r'^\s*except\*\s+', re.MULTILINE),
        (3, 11),
        "PEP 654 exception groups (`except* ...`)",
    ),
)


def detect_newer_syntax_signature(file_content: str) -> Optional[Tuple[Tuple[int, int], str]]:
    """Ищет характерные паттерны version-specific синтаксиса. Возвращает
    (минимальная_версия, описание) для САМОЙ новой найденной конструкции,
    или None, если ничего не найдено."""
    best: Optional[Tuple[Tuple[int, int], str]] = None
    for pattern, min_version, description in _SYNTAX_SIGNATURES:
        if pattern.search(file_content):
            if best is None or min_version > best[0]:
                best = (min_version, description)
    return best


# ---------------------------------------------------------------------------
# Комбинированная классификация
# ---------------------------------------------------------------------------

def classify_version_incompatibility(
    project_path: Path, file_content: str,
    current_version: Optional[Tuple[int, int]] = None,
    project_required_version: Optional[Tuple[int, int]] = None,
) -> Optional[Dict[str, Any]]:
    """Возвращает диагностику, если данный файл, скорее всего, не парсится
    из-за того, что требует более новую версию Python, чем доступна для
    анализа — а НЕ из-за реальной синтаксической ошибки. None — если нет
    оснований так считать (значит, это вероятно настоящий E999).

    `current_version`/`project_required_version` — для тестируемости
    (позволяют не зависеть от sys.version_info и реального файла проекта в
    юнит-тестах); по умолчанию вычисляются нормально.
    """
    sys_version = current_version or CURRENT_PYTHON_VERSION
    required = (
        project_required_version
        if project_required_version is not None
        else detect_project_python_version(project_path)
    )

    if required and required > sys_version:
        return {
            "required_version": "%d.%d" % required,
            "current_version": "%d.%d" % sys_version,
            "detected_via": "project_metadata",
            "signature": None,
        }

    sig = detect_newer_syntax_signature(file_content)
    if sig:
        min_version, description = sig
        if min_version > sys_version:
            return {
                "required_version": "%d.%d" % min_version,
                "current_version": "%d.%d" % sys_version,
                "detected_via": "syntax_signature",
                "signature": description,
            }
    return None
