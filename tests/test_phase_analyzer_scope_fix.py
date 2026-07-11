"""
2026-06-23: финальный пересчёт ошибок (current_total_errors) раньше дёргал
ТОЛЬКО self.analyzer.analyze() (flake8-only), а baseline/initial_errors
считается AnalyzeStage через flake8+semgrep(+bandit/mypy/ruff если включены).
Разный скоуп инструментов делал current_total_errors структурно ниже
baseline везде, где semgrep что-то находил — выглядело как "ошибки исчезли
без единого ACCEPT" (контрольная серия, errors_removed-расследование:
archinfo/dodola/roomba_rest980).

Фикс: AnalyzeStage._collect_multi_tool_errors / run_full_scan — единая
multi-tool сборка, используемая И для baseline (execute()), И для финального
пересчёта (PipelineEngine._finalize_and_audit). Эти тесты проверяют, что
breakdown по инструментам считается верно и что total соответствует
post-dedup/cascade/version-filter числу (сравнимому с initial_error_count).
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:  # pragma: no cover
    import tomlkit  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext
from core.stages.analyze_stage import AnalyzeStage


class _FakeFlake8:
    def analyze(self, path, clean_before_each=False):
        return [
            {"file": "a.py", "line": 1, "code": "E501", "message": "line too long"},
            {"file": "a.py", "line": 2, "code": "F401", "message": "unused import"},
        ]


def _ctx(**pipeline_overrides):
    cfg = {"pipeline": dict(pipeline_overrides), "tools": {"semgrep": {"enabled": True}}}
    return PipelineContext(project_path=Path("/tmp/proj"), language="python", config=cfg)


def _stage_with_fakes(semgrep_findings=None, bandit_findings=None):
    stage = AnalyzeStage(_FakeFlake8())
    stage.semgrep.safe_run = lambda **kw: list(semgrep_findings or [])
    stage.semgrep.enabled = True
    stage.semgrep.is_available = lambda: True
    stage.test_runner.run = lambda path, lang: (True, [])
    stage.security_scanner.scan_project = lambda path, lang: []
    return stage


def test_breakdown_counts_flake8_and_semgrep_separately():
    semgrep_findings = [{
        "path": "b.py", "start": {"line": 5, "col": 1},
        "extra": {"message": "security issue", "severity": "ERROR"},
        "check_id": "python.lang.security.foo",
    }]
    stage = _stage_with_fakes(semgrep_findings=semgrep_findings)
    unique_errors, breakdown, version_skipped, meta = stage._collect_multi_tool_errors(
        _ctx(python_version_detection=False), Path("/tmp/proj"),
    )
    assert breakdown["flake8"] == 2
    assert breakdown["semgrep"] == 1
    assert breakdown["bandit"] == 0
    assert len(unique_errors) == 3
    assert version_skipped == []


def test_run_full_scan_total_matches_combined_unique_count():
    semgrep_findings = [{
        "path": "b.py", "start": {"line": 5, "col": 1},
        "extra": {"message": "security issue", "severity": "ERROR"},
        "check_id": "python.lang.security.foo",
    }]
    stage = _stage_with_fakes(semgrep_findings=semgrep_findings)
    result = stage.run_full_scan(_ctx(python_version_detection=False), Path("/tmp/proj"))
    assert result["total"] == 3
    assert result["breakdown"]["flake8"] == 2
    assert result["breakdown"]["semgrep"] == 1


def test_semgrep_disabled_gives_zero_breakdown_not_missing_key():
    stage = _stage_with_fakes()
    ctx = PipelineContext(
        project_path=Path("/tmp/proj"), language="python",
        config={"pipeline": {"python_version_detection": False},
                "tools": {"semgrep": {"enabled": False}}},
    )
    _, breakdown, _, _ = stage._collect_multi_tool_errors(ctx, Path("/tmp/proj"))
    assert breakdown["semgrep"] == 0
    assert breakdown["flake8"] == 2


def test_force_fresh_semgrep_bypasses_throttle_cache():
    """Без force_fresh_semgrep — повторный вызов в том же global_cycle может
    отдать кэш (см. _get_semgrep_errors); с force_fresh_semgrep=True — ВСЕГДА
    дёргает safe_run заново. Критично для финального пересчёта: нельзя
    унести в total_current_errors устаревший снимок с середины прогона."""
    calls = {"n": 0}

    def _safe_run(**kw):
        calls["n"] += 1
        return [{
            "path": "b.py", "start": {"line": calls["n"], "col": 1},
            "extra": {"message": "x", "severity": "ERROR"},
            "check_id": "rule.x",
        }]

    stage = _stage_with_fakes()
    stage.semgrep.safe_run = _safe_run
    # Прогрев throttle-кэша обычным путём (cycle=1 -> due_for_rescan True).
    ctx1 = _ctx(python_version_detection=False)
    ctx1 = ctx1.update(metadata={**ctx1.metadata, "_current_global_cycle": 1})
    stage._collect_multi_tool_errors(ctx1, Path("/tmp/proj"))
    assert calls["n"] == 1

    # Без force_fresh: cycle=2, interval default 3 -> due_for_rescan False -> кэш, НЕ новый вызов.
    ctx2 = _ctx(python_version_detection=False)
    ctx2 = ctx2.update(metadata={**ctx2.metadata, "_current_global_cycle": 2})
    stage._collect_multi_tool_errors(ctx2, Path("/tmp/proj"), force_fresh_semgrep=False)
    assert calls["n"] == 1, "ожидали reuse throttle-кэша без force_fresh_semgrep"

    # С force_fresh: тот же cycle=2 -> ИГНОРИРУЕТ кэш, новый safe_run.
    stage._collect_multi_tool_errors(ctx2, Path("/tmp/proj"), force_fresh_semgrep=True)
    assert calls["n"] == 2, "force_fresh_semgrep должен форсировать свежий safe_run"


def test_execute_still_works_via_shared_helper():
    """Регрессия: execute() должен продолжать строить context.current_errors
    так же, как раньше (через тот же _collect_multi_tool_errors)."""
    stage = _stage_with_fakes()
    out = stage.execute(_ctx(python_version_detection=False))
    codes = sorted(e.get("code") for e in out.current_errors)
    assert codes == ["E501", "F401"]
