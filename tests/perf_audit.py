"""
Performance audit — Python pipeline, no LLM calls.
Measures wall-clock time for every deterministic stage component.
Run: python tests/perf_audit.py
"""
import sys
import time
import shutil
import tempfile
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

PROJECTS = {
    "test_python_mixed":        Path(r"C:\Users\zov31\test_python_mixed"),
    "test_python_security_heavy": Path(r"C:\Users\zov31\test_python_security_heavy"),
}

results = {}  # project -> {component -> seconds}


def timer(label: str, fn, *args, **kwargs):
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        result = f"ERROR: {e}"
    elapsed = time.perf_counter() - t0
    return elapsed, result


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def row(label, secs, note=""):
    bar = "#" * min(int(secs / 2), 30)
    print(f"  {label:<42} {secs:6.2f}s  {bar}  {note}")


# ── sandbox copy ────────────────────────────────────────────────
def measure_sandbox(project_path: Path):
    def _copy():
        tmp = Path(tempfile.mkdtemp(prefix="perf_audit_"))
        shutil.copytree(project_path, tmp, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("target", ".git", "__pycache__", ".venv"))
        return tmp
    elapsed, tmp = timer("sandbox_copy", _copy)
    if isinstance(tmp, Path) and tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    return elapsed, tmp


# ── flake8 analyzer ─────────────────────────────────────────────
def measure_flake8(project_path: Path):
    from analyzers.python_analyzer import PythonAnalyzer
    a = PythonAnalyzer()
    elapsed, errs = timer("flake8", a.analyze, project_path)
    n = len(errs) if isinstance(errs, list) else 0
    return elapsed, n


# ── ruff analyzer ───────────────────────────────────────────────
def measure_ruff(project_path: Path):
    try:
        from analyzers.ruff_analyzer import RuffAnalyzer
        a = RuffAnalyzer()
        elapsed, errs = timer("ruff", a.analyze, project_path)
        n = len(errs) if isinstance(errs, list) else 0
        return elapsed, n
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── mypy analyzer ────────────────────────────────────────────────
def measure_mypy(project_path: Path):
    try:
        from analyzers.mypy_analyzer import MypyAnalyzer
        a = MypyAnalyzer()
        if not a.available():
            return 0.0, "not_in_path"
        elapsed, errs = timer("mypy", a.analyze, project_path)
        n = len(errs) if isinstance(errs, list) else 0
        return elapsed, n
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── bandit analyzer ──────────────────────────────────────────────
def measure_bandit(project_path: Path):
    try:
        from analyzers.bandit_analyzer import BanditAnalyzer
        a = BanditAnalyzer()
        if not a.available():
            return 0.0, "not_in_path"
        elapsed, errs = timer("bandit", a.analyze, project_path)
        n = len(errs) if isinstance(errs, list) else 0
        return elapsed, n
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── security scanner ─────────────────────────────────────────────
def measure_security(project_path: Path):
    from analysis.security_scanner import SecurityScanner
    s = SecurityScanner()
    elapsed, errs = timer("security_scanner", s.scan_project, project_path, "python")
    n = len(errs) if isinstance(errs, list) else 0
    return elapsed, n


# ── semgrep ──────────────────────────────────────────────────────
def measure_semgrep(project_path: Path):
    try:
        from tools.semgrep_analyzer import SemgrepAnalyzer
        s = SemgrepAnalyzer()
        elapsed, errs = timer("semgrep", s.safe_run, project_path=project_path)
        n = len(errs) if isinstance(errs, list) else 0
        return elapsed, n
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── pip-audit ────────────────────────────────────────────────────
def measure_pip_audit(project_path: Path):
    try:
        from analysis.pip_audit_scan import PipAuditScanner
        s = PipAuditScanner()
        if not s.is_available():
            return 0.0, "not_in_path"
        elapsed, errs = timer("pip_audit", s.scan, project_path)
        n = len(errs) if isinstance(errs, list) else 0
        return elapsed, n
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── python_runtime_analyzer ──────────────────────────────────────
def measure_python_runtime(project_path: Path):
    try:
        from analyzers.python_runtime_analyzer import PythonRuntimeAnalyzer
        a = PythonRuntimeAnalyzer(timeout=20)
        elapsed, errs = timer("python_runtime", a.analyze, project_path)
        n = len(errs) if isinstance(errs, list) else 0
        return elapsed, n
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── cascade_collapse ─────────────────────────────────────────────
def measure_cascade(errors):
    try:
        from analysis.error_cascade import collapse_cascades
        elapsed, out = timer("cascade_collapse", collapse_cascades, errors)
        return elapsed, len(out) if isinstance(out, list) else 0
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── dependency_recovery ──────────────────────────────────────────
def measure_dep_recovery(project_path: Path):
    try:
        from analysis.python_dependency_inference import PythonDependencyInference
        inf = PythonDependencyInference(project_path)
        elapsed, _ = timer("dep_inference_scan", inf.infer_missing)
        return elapsed
    except Exception as e:
        return 0.0


# ── ruff rule subprocess (external examples) ─────────────────────
def measure_ruff_rule():
    try:
        if not shutil.which("ruff"):
            return 0.0, "no_ruff"
        t0 = time.perf_counter()
        proc = subprocess.run(["ruff", "rule", "F401"],
                              capture_output=True, text=True, timeout=10)
        elapsed = time.perf_counter() - t0
        return elapsed, "ok" if proc.returncode == 0 else "nonzero"
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── symbol_regression check ──────────────────────────────────────
def measure_symbol_regression(project_path: Path):
    try:
        from analysis.symbol_regression import check_symbol_regression
        py_files = list(project_path.glob("*.py"))
        if not py_files:
            return 0.0, 0
        sample = py_files[0].read_text(encoding="utf-8", errors="ignore")
        elapsed, _ = timer("symbol_regression", check_symbol_regression,
                           sample, sample, "python")
        return elapsed * len(py_files), len(py_files)
    except Exception as e:
        return 0.0, f"skip:{e}"


# ── validate_stage full re-analyze ───────────────────────────────
def measure_validate_reanalyze(project_path: Path):
    """ValidateStage calls analyzer.analyze() again after each patch."""
    from analyzers.python_analyzer import PythonAnalyzer
    a = PythonAnalyzer()
    # In validate_stage, uses run_with_timeout(analyzer.analyze, 1200, work_path)
    elapsed, errs = timer("validate_reanalyze_flake8", a.analyze, project_path)
    n = len(errs) if isinstance(errs, list) else 0
    return elapsed, n


# ── planning overhead ─────────────────────────────────────────────
def measure_planning_init(project_path: Path):
    """Just the PlanningFactory.create() without actual LLM calls."""
    try:
        from core.planning_factory import PlanningFactory
        from analyzers.python_analyzer import PythonAnalyzer
        from validation.compiler_checks import CompilerChecks
        from validation.lint_checks import LintChecks
        from validation.security_checks import SecurityChecks
        from memory.learning import MemoryLearning
        from core.system_health import SystemHealthEvaluator
        from analysis.error_classifier import ErrorClassifier
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="perf_plan_"))
        mem_file = tmp / "memory.json"
        mem = MemoryLearning(mem_file)
        classifier = ErrorClassifier()
        he = SystemHealthEvaluator(10, classifier)
        factory = PlanningFactory(
            project_path=project_path, language="python",
            analyzer=PythonAnalyzer(),
            compiler=CompilerChecks(), linter=LintChecks(),
            security=SecurityChecks(), memory=mem,
            health_evaluator=he,
        )
        elapsed, components = timer("planning_factory_create", factory.create,
                                    {"use_planning": True, "planning_depth": 2,
                                     "beam_width": 3})
        shutil.rmtree(tmp, ignore_errors=True)
        return elapsed, "ok" if components else "None"
    except Exception as e:
        return 0.0, f"skip:{e}"


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

# ── validate_stage full (Python generic path) ────────────────────
def _timed(fn):
    t0 = time.perf_counter()
    try:
        fn()
    except Exception:
        pass
    return time.perf_counter() - t0


def measure_validate_full(project_path: Path):
    """ValidateStage Python path: compiler + linter + security + flake8 + ruff."""
    import subprocess as _sp
    from analyzers.python_analyzer import PythonAnalyzer

    timings = {}

    # compiler: pyright -> mypy fallback
    def _compiler():
        try:
            _sp.run(["pyright", str(project_path)], capture_output=True,
                    text=True, timeout=120, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            _sp.run(["mypy", str(project_path)], capture_output=True,
                    text=True, timeout=120, encoding="utf-8", errors="replace")
        except Exception:
            pass
    timings["compiler(pyright/mypy)"] = _timed(_compiler)

    # linter: flake8
    def _linter():
        try:
            _sp.run(["flake8", str(project_path)], capture_output=True,
                    text=True, timeout=120, encoding="utf-8", errors="replace")
        except Exception:
            pass
    timings["linter(flake8)"] = _timed(_linter)

    # security: bandit
    def _security():
        try:
            _sp.run(["bandit", "-r", str(project_path), "-ll"],
                    capture_output=True, text=True, timeout=120,
                    encoding="utf-8", errors="replace")
        except Exception:
            pass
    timings["security(bandit)"] = _timed(_security)

    # analyzer.analyze (flake8 via PythonAnalyzer)
    timings["analyzer.analyze(flake8)"] = _timed(lambda: PythonAnalyzer().analyze(project_path))

    # ruff
    def _ruff():
        try:
            _sp.run(["ruff", "check", "--output-format=json", str(project_path)],
                    capture_output=True, text=True, timeout=60,
                    encoding="utf-8", errors="replace")
        except Exception:
            pass
    timings["ruff_recheck"] = _timed(_ruff)

    return timings


# ── semgrep is_available cost ─────────────────────────────────────
def measure_semgrep_available():
    """semgrep.is_available() is called every safe_run()."""
    import subprocess
    try:
        t0 = time.perf_counter()
        subprocess.run(["semgrep", "--version"], capture_output=True, timeout=5)
        return time.perf_counter() - t0
    except Exception:
        return 0.0


if __name__ == "__main__":
    print("\n" + "="*60)
    print("  WEBBLES FIX — PERFORMANCE AUDIT (no LLM)")
    print("="*60)

    for proj_name, proj_path in PROJECTS.items():
        if not proj_path.exists():
            print(f"\n[SKIP] {proj_name} — path not found: {proj_path}")
            continue

        section(f"Project: {proj_name}")
        r = {}

        # 1. Sandbox copy
        t, _ = measure_sandbox(proj_path)
        r["sandbox_copy"] = t
        row("sandbox_copy (shutil.copytree)", t)

        # 2. flake8
        t, n = measure_flake8(proj_path)
        r["flake8"] = t
        row("flake8 (main analyzer)", t, f"-> {n} errors")

        # 3. ruff
        t, n = measure_ruff(proj_path)
        r["ruff"] = t
        row("ruff check", t, f"-> {n}")

        # 4. mypy
        t, n = measure_mypy(proj_path)
        r["mypy"] = t
        row("mypy", t, f"-> {n}")

        # 5. bandit
        t, n = measure_bandit(proj_path)
        r["bandit"] = t
        row("bandit", t, f"-> {n}")

        # 6. security_scanner (internal)
        t, n = measure_security(proj_path)
        r["security_scanner"] = t
        row("SecurityScanner (internal rules)", t, f"-> {n}")

        # 7. semgrep
        t, n = measure_semgrep(proj_path)
        r["semgrep"] = t
        row("semgrep", t, f"-> {n}")

        # 8. pip-audit
        t, n = measure_pip_audit(proj_path)
        r["pip_audit"] = t
        row("pip-audit", t, f"-> {n}")

        # 9. python_runtime
        t, n = measure_python_runtime(proj_path)
        r["python_runtime"] = t
        row("PythonRuntimeAnalyzer", t, f"-> {n}")

        # 10. cascade_collapse (run on flake8 errors as proxy)
        from analyzers.python_analyzer import PythonAnalyzer
        flake8_errs = PythonAnalyzer().analyze(proj_path) or []
        t, n = measure_cascade(flake8_errs)
        r["cascade_collapse"] = t
        row("cascade_collapse", t, f"→ {n} after collapse")

        # 11. validate re-analyze (flake8 only, as in validate_stage)
        t, n = measure_validate_reanalyze(proj_path)
        r["validate_reanalyze"] = t
        row("validate_stage re-analyze (flake8)", t, f"-> {n}")

        # 12. ruff rule subprocess (external examples, per error)
        t, note = measure_ruff_rule()
        r["ruff_rule_subprocess"] = t
        row("ruff rule <code> subprocess (1 call)", t, note)

        # 13. symbol regression per file
        t, n = measure_symbol_regression(proj_path)
        r["symbol_regression"] = t
        row("symbol_regression (all .py files)", t, f"→ {n} files")

        # 14. planning factory init
        t, note = measure_planning_init(proj_path)
        r["planning_init"] = t
        row("PlanningFactory.create() [no LLM]", t, note)

        # 15. validate_stage full (Python generic path)
        print("\n  -- ValidateStage Python generic path (called per patch) --")
        vt = measure_validate_full(proj_path)
        r_validate = {}
        for k, v in vt.items():
            r_validate[f"validate.{k}"] = v
            row(f"  validate > {k}", v)
        validate_total = sum(vt.values())
        row("  validate TOTAL (per patch)", validate_total, "<< called N times")
        r.update(r_validate)

        # 16. semgrep is_available latency
        t_avail = measure_semgrep_available()
        r["semgrep_is_available"] = t_avail
        row("semgrep is_available() cost", t_avail, "called per safe_run")

        # — totals —
        det_total = sum(v for k, v in r.items() if isinstance(v, float))
        print(f"\n  {'DETERMINISTIC TOTAL (analyze+validate)':<42} {det_total:6.2f}s")

        # — LLM estimate —
        print("\n  --- LLM estimates (from session logs) ---")
        print("  (not measured — requires live API key)")
        print("  InvariantGuard.enrich_prompt [per patch]  ~2-5s  (1× LLM call)")
        print("  GeneratePatchStage._call_llm [per patch]  ~5-30s (1-3× calls)")
        print("  ReviewStage.review_patch [per patch]      ~3-10s (1× LLM call)")
        print("  Planning beam_search [depth=2,width=3]    ~30-90s (6+ LLM calls)")
        print("  InvariantGuard.enrich per planning node   ~2-5s × nodes")

        results[proj_name] = r

    # ── cross-project summary ─────────────────────────────────────
    section("CROSS-PROJECT SUMMARY (deterministic components)")
    comps = set()
    for r in results.values():
        comps.update(r.keys())
    print(f"\n  {'Component':<42} {'mixed':>8}  {'sec_heavy':>10}")
    print(f"  {'-'*42} {'-'*8}  {'-'*10}")
    for c in sorted(comps):
        vals = []
        for proj in ["test_python_mixed", "test_python_security_heavy"]:
            v = results.get(proj, {}).get(c, "-")
            vals.append(f"{v:6.2f}s" if isinstance(v, float) else f"{'n/a':>8}")
        print(f"  {c:<42} {vals[0]:>8}  {vals[1]:>10}")

    # ── bottleneck ranking ────────────────────────────────────────
    section("TOP-N DETERMINISTIC BOTTLENECKS (test_python_mixed)")
    mixed_r = results.get("test_python_mixed", {})
    ranked = sorted(
        [(k, v) for k, v in mixed_r.items() if isinstance(v, float)],
        key=lambda x: x[1], reverse=True
    )
    for i, (k, v) in enumerate(ranked[:7], 1):
        print(f"  #{i}  {k:<42} {v:.2f}s")

    print("\n[audit done]")
