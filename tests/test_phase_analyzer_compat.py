"""
Compat-проверка: AnalyzeStage не должен падать TypeError, если анализатор
не принимает kwarg `clean_before_each` (поддерживается только RustAnalyzer,
все остальные — python/js/ts/cpp/csharp/go/java/kotlin — не принимают).

Стейдж пробует с kwarg, при TypeError откатывается к минимальной сигнатуре.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# core.stages.__init__ тянет toml_patcher через generate_patch_stage. Без
# tomlkit подставляем заглушку, чтобы тест поднимался в чистой среде.
try:  # pragma: no cover
    import tomlkit  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext
from core.stages.analyze_stage import AnalyzeStage

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class _AnalyzerWithKwarg:
    """Старый контракт — принимает clean_before_each (как RustAnalyzer)."""
    def __init__(self):
        self.calls = []

    def analyze(self, path, clean_before_each=False):
        self.calls.append({"path": str(path), "kwarg": clean_before_each})
        return [{"file": "a.rs", "line": 1, "code": "E0382",
                 "message": "moved", "severity": "error"}]


class _AnalyzerNoKwarg:
    """Новый контракт — принимает только project_path (как CppAnalyzer и пр.)."""
    def __init__(self):
        self.calls = []

    def analyze(self, path):
        self.calls.append({"path": str(path)})
        return [{"file": "main.cpp", "line": 10, "code": "GCC_SEMI",
                 "message": "expected ';'", "severity": "error"}]


class _AnalyzerKwargAware:
    """Принимает **kwargs (новейший стиль) — kwarg тихо проглатывается."""
    def __init__(self):
        self.calls = []

    def analyze(self, path, **kwargs):
        self.calls.append({"path": str(path), "kwargs": dict(kwargs)})
        return [{"file": "x.py", "line": 1, "code": "F401",
                 "message": "unused import", "severity": "warning"}]


class _AnalyzerWithDifferentTypeError:
    """Не относящийся к kwargу TypeError должен НЕ глушиться."""
    def analyze(self, path, clean_before_each=False):
        raise TypeError("совсем не про clean_before_each")


def _ctx(lang="python"):
    return PipelineContext(project_path=Path("/tmp/proj"), language=lang,
                           config={"pipeline": {}})


def _disable_extras(stage):
    """Глушим все опциональные шаги — нам нужен только основной analyze."""
    stage.semgrep.safe_run = lambda **kw: []
    stage.test_runner.run = lambda path, lang: (True, [])
    # SecurityScanner может выкинуть на путь /tmp/proj — глушим явно.
    stage.security_scanner.scan_project = lambda path, lang: []


# --- A. legacy (с kwargом) ------------------------------------------------
def test_legacy_kwarg_path_still_passes_kwarg():
    a = _AnalyzerWithKwarg()
    stage = AnalyzeStage(a)
    _disable_extras(stage)
    out = stage.execute(_ctx("rust"))
    check("legacy_called_once", len(a.calls) == 1)
    check("legacy_kwarg_propagated", a.calls[0]["kwarg"] is False)
    codes = [e.get("code") for e in out.current_errors]
    check("legacy_errors_propagated", "E0382" in codes)


# --- B. новый контракт (без kwargа) — БЫЛ БАГОМ ---------------------------
def test_no_kwarg_analyzer_does_not_crash():
    a = _AnalyzerNoKwarg()
    stage = AnalyzeStage(a)
    _disable_extras(stage)
    out = stage.execute(_ctx("cpp"))
    check("nokwarg_called_once", len(a.calls) == 1)
    codes = [e.get("code") for e in out.current_errors]
    check("nokwarg_errors_propagated", "GCC_SEMI" in codes)
    # Стейдж должен НЕ скатиться в FAILED.
    last_state = out.state_history[-1] if out.state_history else None
    check("nokwarg_not_failed", str(last_state.name if hasattr(last_state, "name") else last_state) != "FAILED")


# --- C. **kwargs-aware ----------------------------------------------------
def test_kwargs_aware_analyzer():
    a = _AnalyzerKwargAware()
    stage = AnalyzeStage(a)
    _disable_extras(stage)
    out = stage.execute(_ctx("python"))
    check("kwa_called_once", len(a.calls) == 1)
    check("kwa_kwarg_propagated",
          a.calls[0]["kwargs"].get("clean_before_each") is False)
    codes = [e.get("code") for e in out.current_errors]
    check("kwa_errors_propagated", "F401" in codes)


# --- D. сторонний TypeError не должен глушиться ---------------------------
def test_unrelated_typeerror_not_swallowed():
    stage = AnalyzeStage(_AnalyzerWithDifferentTypeError())
    _disable_extras(stage)
    out = stage.execute(_ctx("rust"))
    # Стейдж ловит общее исключение и уводит в FAILED — это правильно: значит
    # сторонний TypeError пробросился, а не был перехвачен compat-обёрткой.
    last_state = out.state_history[-1]
    check("unrelated_te_falls_to_failed",
          (last_state.name if hasattr(last_state, "name") else str(last_state)) == "FAILED")


if __name__ == "__main__":
    print("Analyzer compat smoke (clean_before_each):")
    test_legacy_kwarg_path_still_passes_kwarg()
    test_no_kwarg_analyzer_does_not_crash()
    test_kwargs_aware_analyzer()
    test_unrelated_typeerror_not_swallowed()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nAnalyzer compat: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
