"""
Stage G.3 / G.5 + H audit-mode — интеграционный smoke.

G.3 (AnalyzeStage): под флагом `pipeline.run_tests` падения тестов добавляются
    к ошибкам анализа; без флага — TestRunner не дёргается.
G.5 (ValidateStage): после патча оставшиеся падения тестов до-бавляются в набор
    ошибок (под флагом), так что счётчик after не падает → существующая логика
    отклонит патч. Если тест прошёл — ничего не добавляется.
H audit-mode (AnalyzeStage): под флагом `pipeline.semantic_audit` SemanticAuditor
    добавляет SEMANTIC_MISMATCH; без флага / без llm_client — пропуск.

Стабим analyzer/semgrep/test_runner/auditor — реальные тесты/семгреп/LLM НЕ запускаются.
Запуск: python3 tests/test_phase_g_integration.py
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# core.stages.__init__ тянет generate_patch_stage → fixers.toml_patcher → tomlkit.
# В средах без tomlkit (напр. изолированная песочница) подставляем заглушку:
# наш интеграционный тест toml_patcher не использует.
try:  # pragma: no cover
    import tomlkit  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext
from core.stages.analyze_stage import AnalyzeStage
from core.stages.validate_stage import ValidateStage

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _ctx(run_tests, language="python"):
    cfg = {"pipeline": {"run_tests": run_tests}}
    return PipelineContext(project_path=Path("/tmp/proj"), language=language, config=cfg)


class _FakeAnalyzer:
    def __init__(self, errors):
        self._errors = errors

    def analyze(self, path, clean_before_each=False):
        return list(self._errors)


def _test_failure_dict(name="test_add", file="t.py"):
    return {"file": file, "line": 0, "message": f"{name}: assert", "code": "TEST_FAILURE",
            "severity": "error", "error_type": "test", "error_class": "TEST_FAILURE",
            "test_name": name}


# --- G.3: AnalyzeStage ----------------------------------------------
def test_analyze_flag_on_adds_failures():
    stage = AnalyzeStage(_FakeAnalyzer([]))
    stage.semgrep.safe_run = lambda **kw: []
    stage.test_runner.run = lambda path, lang: (False, [_test_failure_dict()])
    ctx = _ctx(run_tests=True)
    out = stage.execute(ctx)
    codes = [e.get("code") for e in out.current_errors]
    check("analyze_on_adds_test_failure", "TEST_FAILURE" in codes)


def test_analyze_flag_off_skips():
    stage = AnalyzeStage(_FakeAnalyzer([]))
    stage.semgrep.safe_run = lambda **kw: []
    called = {"n": 0}

    def _spy(path, lang):
        called["n"] += 1
        return (False, [_test_failure_dict()])

    stage.test_runner.run = _spy
    ctx = _ctx(run_tests=False)
    out = stage.execute(ctx)
    codes = [e.get("code") for e in out.current_errors]
    check("analyze_off_no_test_failure", "TEST_FAILURE" not in codes)
    check("analyze_off_runner_not_called", called["n"] == 0)


def test_analyze_helper_flag():
    check("analyze_helper_on", AnalyzeStage._run_tests_enabled(_ctx(True)) is True)
    check("analyze_helper_off", AnalyzeStage._run_tests_enabled(_ctx(False)) is False)


# --- H audit-mode: AnalyzeStage семантический аудит -----------------
import tempfile


class _FakeAuditor:
    def __init__(self):
        self.calls = 0

    def audit_file(self, path, language, rel=""):
        self.calls += 1
        return [{"file": rel, "line": 1, "code": "SEMANTIC_MISMATCH",
                 "error_class": "SEMANTIC_MISMATCH", "error_type": "semantic",
                 "message": "doc/code mismatch — stub"}]


def _audit_ctx(d, semantic_audit):
    cfg = {"pipeline": {"semantic_audit": semantic_audit}}
    return PipelineContext(project_path=Path(d), language="python", config=cfg)


def test_audit_flag_on_adds_mismatch():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "m.py").write_text('def f():\n    """Doc."""\n    return 1\n', encoding="utf-8")
        stage = AnalyzeStage(_FakeAnalyzer([]), llm_client=object())
        stage.semgrep.safe_run = lambda **kw: []
        stage._semantic_auditor = _FakeAuditor()  # подменяем, чтобы не звать LLM
        out = stage.execute(_audit_ctx(d, semantic_audit=True))
        codes = [e.get("code") for e in out.current_errors]
        check("audit_on_adds_mismatch", "SEMANTIC_MISMATCH" in codes)


def test_audit_flag_off_skips():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "m.py").write_text('def f():\n    """Doc."""\n    return 1\n', encoding="utf-8")
        stage = AnalyzeStage(_FakeAnalyzer([]), llm_client=object())
        stage.semgrep.safe_run = lambda **kw: []
        auditor = _FakeAuditor()
        stage._semantic_auditor = auditor
        out = stage.execute(_audit_ctx(d, semantic_audit=False))
        codes = [e.get("code") for e in out.current_errors]
        check("audit_off_no_mismatch", "SEMANTIC_MISMATCH" not in codes)
        check("audit_off_auditor_not_called", auditor.calls == 0)


def test_audit_no_llm_client_skips():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "m.py").write_text('def f():\n    """Doc."""\n    return 1\n', encoding="utf-8")
        stage = AnalyzeStage(_FakeAnalyzer([]), llm_client=None)
        stage.semgrep.safe_run = lambda **kw: []
        # флаг включён, но нет llm_client → аудитор не строится → пропуск
        out = stage.execute(_audit_ctx(d, semantic_audit=True))
        codes = [e.get("code") for e in out.current_errors]
        check("audit_no_llm_skips", "SEMANTIC_MISMATCH" not in codes)


def test_audit_helper_flag():
    check("audit_helper_on", AnalyzeStage._semantic_audit_enabled(_audit_ctx("/tmp", True)) is True)
    check("audit_helper_off", AnalyzeStage._semantic_audit_enabled(_audit_ctx("/tmp", False)) is False)


# --- G.5: ValidateStage._merge_test_failures ------------------------
def _vstage():
    # compiler/linter/security/analyzer/degradation не нужны для _merge_*
    return ValidateStage(None, None, None, None, None)


class _StubRunner:
    def __init__(self, avail, result):
        self._avail = avail
        self._result = result

    def available(self, language):
        return self._avail

    def run(self, path, language):
        return self._result


def test_merge_flag_off():
    vs = _vstage()
    vs.test_runner = _StubRunner(True, (False, [_test_failure_dict()]))
    errs, ok = vs._merge_test_failures([{"code": "X"}], _ctx(False), Path("/tmp"))
    check("merge_off_unchanged", len(errs) == 1 and ok is True)


def test_merge_still_failing():
    vs = _vstage()
    vs.test_runner = _StubRunner(True, (False, [_test_failure_dict()]))
    errs, ok = vs._merge_test_failures([{"code": "X"}], _ctx(True), Path("/tmp"))
    check("merge_still_failing_appends", len(errs) == 2)
    check("merge_still_failing_flag", ok is False)
    check("merge_still_failing_has_test", any(e.get("code") == "TEST_FAILURE" for e in errs))


def test_merge_passed():
    vs = _vstage()
    vs.test_runner = _StubRunner(True, (True, []))
    errs, ok = vs._merge_test_failures([{"code": "X"}], _ctx(True), Path("/tmp"))
    check("merge_passed_unchanged", len(errs) == 1 and ok is True)


def test_merge_tool_unavailable():
    vs = _vstage()
    vs.test_runner = _StubRunner(False, (False, [_test_failure_dict()]))
    errs, ok = vs._merge_test_failures([{"code": "X"}], _ctx(True), Path("/tmp"))
    check("merge_unavail_unchanged", len(errs) == 1 and ok is True)


def test_merge_graceful_on_exception():
    vs = _vstage()

    class _Boom:
        def available(self, language):
            return True

        def run(self, path, language):
            raise RuntimeError("boom")

    vs.test_runner = _Boom()
    errs, ok = vs._merge_test_failures([{"code": "X"}], _ctx(True), Path("/tmp"))
    check("merge_exception_graceful", len(errs) == 1 and ok is True)


# --- G.5×Rust fix: project-scoped routing в ValidateStage.execute ---
# Баг: для выбранной TEST_FAILURE/SEMANTIC_MISMATCH на rust file-scoped fast-path
# считал error_count_after по целевому файлу, не видя падений теста (они на строке
# паники), → after≈before≈0 → DecideStage ложно REJECT'ил починенный патч.
# Фикс: _is_project_scoped_error отправляет такие ошибки в generic project-wide путь.

class _OkTool:
    def run(self, path, language):
        return (True, [])


class _Degradation:
    def update(self, errors):
        pass


def _rust_vstage(analyzer_errors, runner_result):
    vs = ValidateStage(_OkTool(), _OkTool(), _OkTool(),
                       _FakeAnalyzer(analyzer_errors), _Degradation())
    vs.hypothesis.enabled = False  # не звать property-based тул
    vs.test_runner = _StubRunner(True, runner_result)
    return vs


def _rust_test_failure_ctx():
    """rust-контекст, где выбранная ошибка — TEST_FAILURE (file = тестовый файл)."""
    tf = _test_failure_dict(file="src/lib.rs")
    ctx = PipelineContext(project_path=Path("/tmp/proj_rs"), language="rust",
                          config={"pipeline": {"run_tests": True}})
    ctx = ctx.set_errors([tf])
    ctx = ctx.set_selected_error(tf)
    return ctx


def test_helper_classifies_project_scoped():
    check("helper_test_failure_true",
          ValidateStage._is_project_scoped_error({"code": "TEST_FAILURE"}) is True)
    check("helper_semantic_true",
          ValidateStage._is_project_scoped_error({"error_class": "SEMANTIC_MISMATCH"}) is True)
    check("helper_compile_false",
          ValidateStage._is_project_scoped_error({"code": "E0308"}) is False)
    check("helper_none_false",
          ValidateStage._is_project_scoped_error(None) is False)


def test_rust_test_failure_fixed_accepts():
    # тест прошёл после патча → новых падений нет; analyzer чист.
    vs = _rust_vstage(analyzer_errors=[], runner_result=(True, []))
    out = vs.execute(_rust_test_failure_ctx())
    res = out.validation_results
    # пошли в generic-путь (нет rust-only ключа project_error_count_after):
    check("rust_tf_fixed_generic_path", "project_error_count_after" not in res)
    # проектный счётчик: было 1, стало 0 → after < before (ACCEPT-able)
    check("rust_tf_fixed_before", res.get("error_count_before") == 1)
    check("rust_tf_fixed_after", res.get("error_count_after") == 0)
    check("rust_tf_fixed_decreased",
          res.get("error_count_after") < res.get("error_count_before"))


def test_rust_test_failure_still_failing_rejects():
    # тест всё ещё падает → падение до-бавлено в errors; after не упал → REJECT.
    vs = _rust_vstage(analyzer_errors=[],
                      runner_result=(False, [_test_failure_dict(file="src/lib.rs")]))
    out = vs.execute(_rust_test_failure_ctx())
    res = out.validation_results
    check("rust_tf_fail_generic_path", "project_error_count_after" not in res)
    check("rust_tf_fail_before", res.get("error_count_before") == 1)
    check("rust_tf_fail_after", res.get("error_count_after") == 1)
    check("rust_tf_fail_not_decreased",
          not (res.get("error_count_after") < res.get("error_count_before")))


if __name__ == "__main__":
    print("Stage G.3/G.5 + H audit-mode integration smoke:")
    test_analyze_flag_on_adds_failures()
    test_analyze_flag_off_skips()
    test_analyze_helper_flag()
    test_audit_flag_on_adds_mismatch()
    test_audit_flag_off_skips()
    test_audit_no_llm_client_skips()
    test_audit_helper_flag()
    test_merge_flag_off()
    test_merge_still_failing()
    test_merge_passed()
    test_merge_tool_unavailable()
    test_merge_graceful_on_exception()
    test_helper_classifies_project_scoped()
    test_rust_test_failure_fixed_accepts()
    test_rust_test_failure_still_failing_rejects()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage G integration: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
