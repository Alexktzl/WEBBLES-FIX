"""
Python version-aware analysis (2026-06-22).

Контекст: CONVERSION_ANALYSIS_2026-06-22_FULL_ARCHIVE.md показал, что
invalid-syntax/E999 = 33% всего объёма решений с accept-долей 1.8%/17.3% —
крупнейшая категория потерь ACCEPT. Живая проверка на 5 реальных
GitHub-кейсах (project_e999_semantic_recovery_verdict) показала: 4 из 5
"неразрешимых E999" были НЕ багом, а несовместимостью версий Python — код
использует PEP 695 (`def foo[T](...)`, `type X = ...`), валидный в Python
3.12+, но не парсящийся в окружении пайплайна (3.11.x).

Эти тесты покрывают:
1. core/python_version_detector.py — детект версии из манифестов проекта +
   fallback по сигнатурам синтаксиса (юнит).
2. AnalyzeStage._filter_version_incompatible_errors — исключение таких
   "ошибок" из current_errors/initial_errors ДО классификации.
3. SyntaxRepairStage — пропуск файла без попытки репарации.
4. Живая проверка на РЕАЛЬНОМ содержимом pyproject.toml двух кейсов из
   отчёта (allenporter/python-google-nest-sdm, MartinHjelmare/leicacam).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.python_version_detector import (
    parse_min_version,
    detect_required_version_from_pyproject,
    detect_required_version_from_setup_cfg,
    detect_required_version_from_setup_py,
    detect_project_python_version,
    detect_newer_syntax_signature,
    classify_version_incompatibility,
)
from core.pipeline_context import PipelineContext
from core.stages.analyze_stage import AnalyzeStage
from core.stages.syntax_repair_stage import SyntaxRepairStage


# ---------------------------------------------------------------------------
# 1. parse_min_version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    (">=3.12", (3, 12)),
    ("^3.12", (3, 12)),
    ("~=3.12", (3, 12)),
    (">=3.10,<4", (3, 10)),
    ("3.12.*", (3, 12)),
    ("", None),
    (None, None),
])
def test_parse_min_version(spec, expected):
    assert parse_min_version(spec) == expected


# ---------------------------------------------------------------------------
# 2. Детект версии из манифестов
# ---------------------------------------------------------------------------

def test_detect_from_pyproject_requires_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.12"\n', encoding="utf-8",
    )
    assert detect_required_version_from_pyproject(tmp_path) == (3, 12)


def test_detect_from_pyproject_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.13"\n', encoding="utf-8",
    )
    assert detect_required_version_from_pyproject(tmp_path) == (3, 13)


def test_detect_from_pyproject_classifiers(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n'
        'classifiers = [\n'
        '  "Programming Language :: Python :: 3.12",\n'
        '  "Programming Language :: Python :: 3.13",\n'
        ']\n',
        encoding="utf-8",
    )
    assert detect_required_version_from_pyproject(tmp_path) == (3, 13)


def test_detect_from_pyproject_absent(tmp_path):
    assert detect_required_version_from_pyproject(tmp_path) is None


def test_detect_from_setup_cfg(tmp_path):
    (tmp_path / "setup.cfg").write_text(
        "[options]\npython_requires = >=3.12\n", encoding="utf-8",
    )
    assert detect_required_version_from_setup_cfg(tmp_path) == (3, 12)


def test_detect_from_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text(
        'from setuptools import setup\n'
        'setup(name="x", python_requires=">=3.12")\n',
        encoding="utf-8",
    )
    assert detect_required_version_from_setup_py(tmp_path) == (3, 12)


def test_detect_project_python_version_priority(tmp_path):
    """pyproject.toml имеет приоритет над setup.cfg, если оба есть."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n', encoding="utf-8",
    )
    (tmp_path / "setup.cfg").write_text(
        "[options]\npython_requires = >=3.9\n", encoding="utf-8",
    )
    assert detect_project_python_version(tmp_path) == (3, 12)


def test_real_pyproject_python_google_nest_sdm(tmp_path):
    """Реальный кейс из отчёта: allenporter/python-google-nest-sdm,
    requires-python = '>=3.13'."""
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\n'
        'build-backend = "setuptools.build_meta"\n'
        'requires = ["setuptools>=77.0"]\n\n'
        '[project]\n'
        'name = "google_nest_sdm"\n'
        'version = "9.1.2"\n'
        'requires-python = ">=3.13"\n'
        'classifiers = []\n',
        encoding="utf-8",
    )
    assert detect_project_python_version(tmp_path) == (3, 13)


def test_real_pyproject_leicacam(tmp_path):
    """Реальный кейс из отчёта: MartinHjelmare/leicacam, классификаторы
    только 3.12/3.13/3.14 (нет requires-python явно)."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'classifiers = [\n'
        '  "Development Status :: 4 - Beta",\n'
        '  "Programming Language :: Python :: 3.12",\n'
        '  "Programming Language :: Python :: 3.13",\n'
        '  "Programming Language :: Python :: 3.14",\n'
        ']\n',
        encoding="utf-8",
    )
    assert detect_project_python_version(tmp_path) == (3, 14)


# ---------------------------------------------------------------------------
# 3. Fallback по сигнатурам синтаксиса
# ---------------------------------------------------------------------------

def test_detect_pep695_generic_function():
    content = "def exception_handler[_T: Any](\n    func_name: str,\n) -> None:\n    pass\n"
    sig = detect_newer_syntax_signature(content)
    assert sig is not None
    assert sig[0] == (3, 12)


def test_detect_pep695_type_alias():
    content = "type StatementValue = str | int\n"
    sig = detect_newer_syntax_signature(content)
    assert sig == ((3, 12), "PEP 695 type alias statement (`type X = ...`)")


def test_detect_pep695_generic_class():
    content = "class Container[T]:\n    pass\n"
    sig = detect_newer_syntax_signature(content)
    assert sig is not None
    assert sig[0] == (3, 12)


def test_detect_except_star():
    content = "try:\n    pass\nexcept* ValueError:\n    pass\n"
    sig = detect_newer_syntax_signature(content)
    assert sig == ((3, 11), "PEP 654 exception groups (`except* ...`)")


def test_no_signature_for_normal_code():
    content = "def foo(x, y):\n    return x + y\n\nclass Bar:\n    pass\n"
    assert detect_newer_syntax_signature(content) is None


def test_real_pep695_cam_py_snippet():
    """Реальный кейс из отчёта: MartinHjelmare/leicacam, src/leicacam/cam.py:20."""
    content = (
        "def logger[**P, R](function: Callable[P, R]) -> Callable[P, R]:\n"
        '    """Decorate passed in function and log message to module logger."""\n'
    )
    sig = detect_newer_syntax_signature(content)
    assert sig is not None
    assert sig[0] == (3, 12)


# ---------------------------------------------------------------------------
# 4. classify_version_incompatibility — комбинированная проверка
# ---------------------------------------------------------------------------

def test_classify_via_project_metadata(tmp_path):
    result = classify_version_incompatibility(
        tmp_path, "x = 1\n", current_version=(3, 11), project_required_version=(3, 12),
    )
    assert result is not None
    assert result["required_version"] == "3.12"
    assert result["detected_via"] == "project_metadata"


def test_classify_via_syntax_fallback_when_no_metadata(tmp_path):
    result = classify_version_incompatibility(
        tmp_path, "type X = int\n", current_version=(3, 11), project_required_version=None,
    )
    assert result is not None
    assert result["detected_via"] == "syntax_signature"


def test_classify_none_for_genuine_syntax_error(tmp_path):
    """Настоящая синтаксическая ошибка (не version-specific конструкция) —
    None, должна остаться нормальным E999."""
    result = classify_version_incompatibility(
        tmp_path, "def foo(x, y)\n    return x + y\n",
        current_version=(3, 11), project_required_version=None,
    )
    assert result is None


def test_classify_none_when_environment_already_supports_required_version():
    """Если окружение УЖЕ современное (3.12+) — не считаем несовместимостью,
    даже если метаданные требуют 3.12 (значит, проблема в чём-то другом)."""
    result = classify_version_incompatibility(
        Path("."), "type X = int\n", current_version=(3, 12), project_required_version=(3, 12),
    )
    assert result is None


# ---------------------------------------------------------------------------
# 5. Интеграция: AnalyzeStage._filter_version_incompatible_errors
# ---------------------------------------------------------------------------

def test_analyze_stage_filters_version_incompatible_error(tmp_path):
    (tmp_path / "modern.py").write_text("type X = int\n", encoding="utf-8")
    errors = [
        {"file": "modern.py", "line": 1, "code": "invalid-syntax", "message": "invalid syntax"},
        {"file": "normal.py", "line": 5, "code": "E501", "message": "line too long"},
    ]
    kept, skipped = AnalyzeStage._filter_version_incompatible_errors(errors, tmp_path)
    assert len(kept) == 1
    assert kept[0]["code"] == "E501"
    assert len(skipped) == 1
    assert skipped[0]["file"] == "modern.py"
    assert skipped[0]["detected_via"] == "syntax_signature"


def test_analyze_stage_filters_mypy_syntax_code(tmp_path):
    """Реальная находка живой проверки (MartinHjelmare/leicacam): mypy
    репортит синтаксические ошибки с кодом буквально "syntax" (свой
    формат, см. analyzers/mypy_analyzer.py _CODE_TO_TYPE), НЕ "E999"/
    "invalid-syntax" — фильтр должен ловить и этот случай через
    error_type=="syntax", а не только через захардкоженный список кодов."""
    (tmp_path / "modern.py").write_text("def f[T](x: T) -> T:\n    return x\n", encoding="utf-8")
    errors = [
        {"file": "modern.py", "line": 1, "code": "syntax", "error_type": "syntax",
         "message": "invalid syntax"},
    ]
    kept, skipped = AnalyzeStage._filter_version_incompatible_errors(errors, tmp_path)
    assert kept == []
    assert len(skipped) == 1
    assert skipped[0]["file"] == "modern.py"


def test_analyze_stage_keeps_genuine_syntax_error(tmp_path):
    (tmp_path / "broken.py").write_text("def foo(x, y)\n    return x\n", encoding="utf-8")
    errors = [
        {"file": "broken.py", "line": 1, "code": "E999", "message": "invalid syntax"},
    ]
    kept, skipped = AnalyzeStage._filter_version_incompatible_errors(errors, tmp_path)
    assert len(kept) == 1
    assert len(skipped) == 0


def test_analyze_stage_no_syntax_errors_no_overhead(tmp_path):
    """Если в списке вообще нет E999/invalid-syntax — функция не должна
    трогать файлы (early-exit)."""
    errors = [{"file": "x.py", "line": 1, "code": "E501", "message": "m"}]
    kept, skipped = AnalyzeStage._filter_version_incompatible_errors(errors, tmp_path)
    assert kept == errors
    assert skipped == []


def test_execute_excludes_version_error_from_initial_errors(tmp_path):
    """End-to-end через AnalyzeStage.execute(): version-incompatible файл НЕ
    попадает в current_errors/initial_errors вообще — не ACCEPT/REJECT/NR,
    отдельная категория, не портящая метрики конверсии."""
    (tmp_path / "modern.py").write_text("type X = int\n", encoding="utf-8")
    (tmp_path / "normal.py").write_text("import os\nx=1\n", encoding="utf-8")

    analyzer = MagicMock()
    analyzer.analyze.return_value = [
        {"file": "modern.py", "line": 1, "code": "invalid-syntax", "message": "invalid syntax"},
        {"file": "normal.py", "line": 2, "code": "E225", "message": "missing whitespace"},
    ]
    stage = AnalyzeStage(analyzer=analyzer)
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path,
                          config={"tools": {"semgrep": {"enabled": False}}})

    result = stage.execute(ctx)

    codes = [e.get("code") for e in result.current_errors]
    assert "invalid-syntax" not in codes
    assert "E225" in codes
    assert len(result.initial_errors) == 1
    assert result.metadata.get("unsupported_python_version_count") == 1
    items = result.metadata.get("unsupported_python_version_items")
    assert items and items[0]["file"] == "modern.py"


def test_python_version_detection_can_be_disabled_via_config(tmp_path):
    """pipeline.python_version_detection=False — старое поведение (ошибка
    остаётся обычным E999)."""
    (tmp_path / "modern.py").write_text("type X = int\n", encoding="utf-8")

    analyzer = MagicMock()
    analyzer.analyze.return_value = [
        {"file": "modern.py", "line": 1, "code": "invalid-syntax", "message": "invalid syntax"},
    ]
    stage = AnalyzeStage(analyzer=analyzer)
    ctx = PipelineContext(
        project_path=tmp_path, language="python", working_path=tmp_path,
        config={"tools": {"semgrep": {"enabled": False}},
               "pipeline": {"python_version_detection": False}},
    )

    result = stage.execute(ctx)

    codes = [e.get("code") for e in result.current_errors]
    assert "invalid-syntax" in codes
    assert result.metadata.get("unsupported_python_version_count", 0) == 0


# ---------------------------------------------------------------------------
# 6. Интеграция: SyntaxRepairStage пропускает файл без попытки репарации
# ---------------------------------------------------------------------------

def test_syntax_repair_stage_skips_version_incompatible_file(tmp_path):
    """Файл с PEP 695 НЕ должен тратить попытки healer/tokenize/LLM — он
    пропускается целиком как unsupported_python_version."""
    (tmp_path / "modern.py").write_text(
        "def foo[T](x: T) -> T:\n    return x\n", encoding="utf-8",
    )
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)

    new_ctx, accepted, n_failed = SyntaxRepairStage().execute(ctx, tmp_path, llm_client=None)

    assert accepted == []
    assert n_failed == 0
    assert new_ctx.metadata.get("unsupported_python_version_count") == 1
    items = new_ctx.metadata.get("unsupported_python_version_items")
    assert items and items[0]["file"] == "modern.py"
    # decisions[] не должен содержать НИЧЕГО для этого файла — это не ACCEPT/REJECT.
    assert new_ctx.metadata.get("decisions", []) == []


def test_syntax_repair_stage_still_repairs_genuine_syntax_error(tmp_path):
    """Контрольный тест: настоящая (не version-specific) синтаксическая
    ошибка должна репариться как обычно, фильтр её не блокирует."""
    (tmp_path / "broken.py").write_text("x = [1, 2, 3\n", encoding="utf-8")
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)

    new_ctx, accepted, n_failed = SyntaxRepairStage().execute(ctx, tmp_path, llm_client=None)

    assert len(accepted) == 1
    assert new_ctx.metadata.get("unsupported_python_version_count", 0) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
