"""
2026-06-23: контрольная серия (ellwise/kedro-light, ClimateImpactLab/dodola) —
проекты с реальными ошибками mypy давали "Все ошибки невалидны — завершаем
цикл" (0 циклов, 0 ACCEPT), хотя `context.initial_errors` показывал 16-21
ошибку.

Корень: `MypyAnalyzer._parse_output` получает от mypy ОТНОСИТЕЛЬНЫЙ путь
(mypy запускается с `cwd=project_path`), но резолвил его через
`Path(file_path).resolve()` без явной привязки к `project_path` — это
резолвит путь от cwd ТЕКУЩЕГО python-процесса (например, `C:\\dev\\webbles_fix`,
откуда запущен `run_agent.py`), а не от `project_path`. `relative_to()` тогда
падает с ValueError, и старый fallback `Path(file_path).name` отбрасывал
директорию целиком: "kedro_light/kedro.py" → просто "kedro.py".
`ErrorContextValidator.is_valid()` искал несуществующий `project_path/kedro.py`
и помечал ВСЕ такие ошибки невалидными — PrioritizeStage отбрасывал их все
сразу, без единого error_dequeued.

Эти тесты воспроизводят баг с реальной (не текущей) project_path и
проверяют, что путь с поддиректорией сохраняется.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analyzers.mypy_analyzer import MypyAnalyzer


def test_relative_path_with_subdir_preserved_when_cwd_differs():
    """project_path != Path.cwd() (как в реальном запуске run_agent.py из
    C:\\dev\\webbles_fix на проект в runtime/.../clones/...) — путь с
    поддиректорией должен сохраниться, а не превратиться в basename."""
    with tempfile.TemporaryDirectory() as d:
        project_path = Path(d) / "some_project"
        (project_path / "pkg").mkdir(parents=True)
        out = 'pkg\\module.py:3:1: error: msg  [import-not-found]\n'
        errors = MypyAnalyzer._parse_output(out, project_path)
        assert len(errors) == 1
        # Раньше тут было errors[0]["file"] == "module.py" (баг).
        assert errors[0]["file"].replace("\\", "/") == "pkg/module.py"


def test_relative_path_nested_deeper_preserved():
    with tempfile.TemporaryDirectory() as d:
        project_path = Path(d) / "proj"
        (project_path / "a" / "b").mkdir(parents=True)
        out = 'a\\b\\c.py:1: error: msg  [arg-type]\n'
        errors = MypyAnalyzer._parse_output(out, project_path)
        assert len(errors) == 1
        assert errors[0]["file"].replace("\\", "/") == "a/b/c.py"


def test_absolute_path_still_resolved_correctly():
    """Если mypy всё же отдал абсолютный путь — поведение не меняется."""
    with tempfile.TemporaryDirectory() as d:
        project_path = Path(d) / "proj"
        (project_path / "sub").mkdir(parents=True)
        abs_file = project_path / "sub" / "x.py"
        out = f'{abs_file}:1: error: msg  [misc]\n'
        errors = MypyAnalyzer._parse_output(out, project_path)
        assert len(errors) == 1
        assert errors[0]["file"].replace("\\", "/") == "sub/x.py"


def test_truly_unrelated_absolute_path_falls_back_to_basename():
    """Путь СОВСЕМ вне project_path (например stdlib stub) — fallback на
    basename — это осознанный fallback для недостижимого случая, не баг."""
    with tempfile.TemporaryDirectory() as d:
        project_path = Path(d) / "proj"
        project_path.mkdir(parents=True)
        unrelated = Path(d) / "elsewhere" / "stub.py"
        out = f'{unrelated}:1: error: msg  [misc]\n'
        errors = MypyAnalyzer._parse_output(out, project_path)
        assert len(errors) == 1
        assert errors[0]["file"] == "stub.py"
