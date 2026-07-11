"""
Конверсия-1 (2026-07-02): точечный mypy-re-check целевой ошибки.

Проблема (learning_cases 06-25..07-02: import-not-found accept=32%,
type_ignore accept=35%): after-скан ValidateStage — flake8-only, mypy-ошибок
в нём НЕТ никогда. Следствия:
  * «error_count_decreased» для mypy-кодов невыполним → детерминированный
    `# type: ignore[...]` уходил в REJECT error_count_not_decreased при
    реально исправленной ошибке;
  * target_still_present по flake8-списку всегда False — даже патч-пустышка
    на mypy-ошибке выглядел «исправившим цель».

Фикс: MypyAnalyzer.analyze(files=[...]) + ValidateStage._recheck_mypy_target
→ validation_results["target_recheck"]; DecideStage использует его как
истину в ОБЕ стороны; type_ignore — high-trust (review skip) + conf 0.9.

Запуск: python tests/test_phase_mypy_target_recheck.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest.mock as mock
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.contract import default_confidence_for  # noqa: E402
from core.pipeline_context import PipelineContext  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402
from core.stages.review_stage import ReviewStage  # noqa: E402
from core.stages.validate_stage import ValidateStage  # noqa: E402

_ERR = {"file": "noxfile.py", "line": 1, "code": "import-not-found",
        "message": 'Cannot find implementation or library stub for module named "nox"'}


def _vs() -> ValidateStage:
    return ValidateStage(None, None, None, None, None)


# --- 1. MypyAnalyzer.analyze(files=...) таргетирует конкретный файл ---
def test_mypy_analyzer_files_param():
    from analyzers.mypy_analyzer import MypyAnalyzer
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "noxfile.py").write_text("import nox\n", encoding="utf-8")
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            r = mock.MagicMock()
            r.stdout = ""
            r.stderr = ""
            r.returncode = 0
            return r

        analyzer = MypyAnalyzer()
        with mock.patch.object(type(analyzer), "available", return_value=True):
            with mock.patch("subprocess.run", side_effect=fake_run):
                analyzer.analyze(work, files=["noxfile.py"])
        assert any(c.endswith("noxfile.py") for c in captured["cmd"]), captured["cmd"]
        assert str(work) not in captured["cmd"][1:], (
            f"при files= не должен сканироваться весь проект: {captured['cmd']}"
        )


# --- 2. recheck: mypy-код + ошибка исправлена → present=False ---
def test_recheck_records_fixed():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = PipelineContext(project_path=work, language="python",
                              selected_error=dict(_ERR), working_path=work)
        results: dict = {}
        with mock.patch("analyzers.mypy_analyzer.MypyAnalyzer") as M:
            inst = M.return_value
            inst.available.return_value = True
            inst.analyze.return_value = []  # mypy больше не видит ошибку
            _vs()._recheck_mypy_target(ctx, results, work)
        tr = results.get("target_recheck")
        assert tr and tr["performed"] and tr["present"] is False, results


# --- 3. recheck: ошибка осталась → present=True ---
def test_recheck_records_still_present():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = PipelineContext(project_path=work, language="python",
                              selected_error=dict(_ERR), working_path=work)
        results: dict = {}
        with mock.patch("analyzers.mypy_analyzer.MypyAnalyzer") as M:
            inst = M.return_value
            inst.available.return_value = True
            inst.analyze.return_value = [dict(_ERR)]
            _vs()._recheck_mypy_target(ctx, results, work)
        assert results["target_recheck"]["present"] is True, results


# --- 4. flake8/semgrep-коды recheck не трогает ---
def test_recheck_skips_non_mypy_codes():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        for code in ("E501", "F821", "B603", "invalid-syntax",
                     "python.lang.security.audit.exec-detected.exec-detected"):
            err = dict(_ERR, code=code)
            ctx = PipelineContext(project_path=work, language="python",
                                  selected_error=err, working_path=work)
            results: dict = {}
            with mock.patch("analyzers.mypy_analyzer.MypyAnalyzer") as M:
                M.return_value.available.return_value = True
                M.return_value.analyze.return_value = []
                _vs()._recheck_mypy_target(ctx, results, work)
            assert "target_recheck" not in results, f"код {code} не должен re-check-аться"


# --- 5. Decide: recheck=исправлена + счётчик не упал → ACCEPT (раньше REJECT) ---
def test_decide_accepts_fixed_mypy_with_count_unchanged():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "noxfile.py").write_text("import nox  # type: ignore[import-not-found]\n",
                                         encoding="utf-8")
        ctx = PipelineContext(
            project_path=work, language="python", working_path=work,
            selected_error=dict(_ERR),
            current_errors=(),  # flake8-список: mypy-ошибок тут нет и не было
            validation_results=MappingProxyType({
                "error_count_before": 60, "error_count_after": 60,
                "current_errors_before": [],
                "target_recheck": {"performed": True, "tool": "mypy", "present": False},
            }),
            metadata={"patch_source": "type_ignore",
                      "review": {"verdict": "skipped"},
                      "patch_snapshots": [],
                      "_pre_patch_content": {"noxfile.py": "import nox\n"}},
        )
        out = DecideStage(quality_evaluator=None, analyzer=None).execute(ctx)
        assert out.metadata.get("last_decision") == "ACCEPT", (
            f"сработавший type:ignore при равном flake8-счётчике обязан приниматься: "
            f"{out.metadata.get('last_decision')}, decisions={out.metadata.get('decisions')}"
        )


# --- 6. Decide: recheck=осталась → НЕ ACCEPT (раньше цель «исчезала» из flake8-списка) ---
def test_decide_rejects_unfixed_mypy():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "noxfile.py").write_text("import nox\n", encoding="utf-8")
        ctx = PipelineContext(
            project_path=work, language="python", working_path=work,
            selected_error=dict(_ERR),
            current_errors=(),
            validation_results=MappingProxyType({
                "error_count_before": 60, "error_count_after": 60,
                "current_errors_before": [],
                "target_recheck": {"performed": True, "tool": "mypy", "present": True},
            }),
            metadata={"patch_source": "type_ignore",
                      "review": {"verdict": "skipped"},
                      "patch_snapshots": [],
                      "_pre_patch_content": {"noxfile.py": "import nox\n"},
                      # TESP-retry уже израсходован — сразу терминальное решение
                      "_tesp_retry_noxfile.py::import-not-found::Cannot find implementation or library stub for module named \"nox\"": 1},
        )
        out = DecideStage(quality_evaluator=None, analyzer=None).execute(ctx)
        assert out.metadata.get("last_decision") != "ACCEPT", (
            "патч, не убравший mypy-ошибку (recheck present=True), не может быть принят"
        )


# --- 7. type_ignore — high-trust: review пропускается, confidence 0.9 ---
def test_type_ignore_high_trust_and_confidence():
    assert "type_ignore" in ReviewStage.HIGH_TRUST_SOURCES
    assert default_confidence_for("type_ignore") == 0.9


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nMypy target recheck: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
