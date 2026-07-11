"""
Stage K — security detection smoke.

Покрывает встроенный детектор уязвимостей (`analysis/security_scanner.py`),
его проводку в AnalyzeStage и rule-based security auto-fix (K.7):

* каждый код детектится на синтетическом сниппете (py / js / rust);
* безопасные эквиваленты НЕ флагуются (нет ложных срабатываний);
* строки-комментарии пропускаются; чтение секрета из окружения не считается
  hardcoded_secret;
* error-dict соответствует контракту (error_class=SECURITY, error_type,
  confidence, autofixable, line);
* флаг `pipeline.security_scan` (default True) включает/выключает скан в
  AnalyzeStage; при off сканер не дёргается;
* K.7: тривиальное чинится (yaml.load→safe_load, innerHTML→textContent),
  нетривиальное отклоняется (→ NEEDS_REVIEW).

LLM/сеть/Semgrep не используются. Запуск: python3 tests/test_phase_k_security.py
"""

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

# core.stages.__init__ тянет generate_patch_stage → fixers.toml_patcher → tomlkit.
try:  # pragma: no cover
    import tomlkit  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from analysis.security_scanner import SecurityScanner
from core.pipeline_context import PipelineContext
from core.stages.analyze_stage import AnalyzeStage

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _codes(findings):
    return [f["code"] for f in findings]


# --- detection: positive cases --------------------------------------
def test_detect_python():
    sc = SecurityScanner()
    check("py_hardcoded", "hardcoded_secret" in _codes(sc.scan_source('API_KEY = "sk-live-abc123"\n', "python")))
    check("py_eval", "dangerous_eval" in _codes(sc.scan_source('x = eval(data)\n', "python")))
    check("py_yaml", "unsafe_deserialization" in _codes(sc.scan_source('cfg = yaml.load(t)\n', "python")))
    check("py_pickle", "unsafe_deserialization" in _codes(sc.scan_source('o = pickle.loads(b)\n', "python")))
    check("py_sql", "sql_injection" in _codes(sc.scan_source('cur.execute("SELECT * FROM t WHERE id = " + uid)\n', "python")))
    check("py_cmd", "command_injection" in _codes(sc.scan_source('os.system("rm " + name)\n', "python")))


def test_detect_js():
    sc = SecurityScanner()
    check("js_hardcoded", "hardcoded_secret" in _codes(sc.scan_source('const token = "ghp_abc123def456";\n', "javascript")))
    check("js_eval", "dangerous_eval" in _codes(sc.scan_source('return eval(input);\n', "javascript")))
    check("js_xss", "xss" in _codes(sc.scan_source('el.innerHTML = userInput;\n', "javascript")))
    check("js_ts_reuses_rules", "xss" in _codes(sc.scan_source('el.innerHTML = x;\n', "typescript")))


def test_detect_rust():
    sc = SecurityScanner()
    check("rust_hardcoded", "hardcoded_secret" in _codes(sc.scan_source('let api_key = "secret123";\n', "rust")))
    check("rust_cmd", "command_injection" in _codes(sc.scan_source('Command::new("sh").arg("-c").arg(cmd);\n', "rust")))


# --- detection: no false positives ----------------------------------
def test_clean_no_findings():
    sc = SecurityScanner()
    check("py_env_clean", sc.scan_source('API_KEY = os.environ["API_KEY"]\n', "python") == [])
    check("py_safe_yaml_clean", sc.scan_source('cfg = yaml.safe_load(t)\n', "python") == [])
    check("py_param_sql_clean", sc.scan_source('cur.execute("SELECT * FROM t WHERE id = ?", (uid,))\n', "python") == [])
    check("js_env_clean", sc.scan_source('const token = process.env.TOKEN;\n', "javascript") == [])
    check("js_textcontent_clean", sc.scan_source('el.textContent = userInput;\n', "javascript") == [])
    check("js_eq_not_assign", sc.scan_source('if (el.innerHTML == x) {}\n', "javascript") == [])
    check("rust_env_clean", sc.scan_source('let api_key = std::env::var("API_KEY").unwrap();\n', "rust") == [])
    # переменная без секретного имени — не находка
    check("py_plain_var_clean", sc.scan_source('greeting = "hello world"\n', "python") == [])


def test_comment_lines_skipped():
    sc = SecurityScanner()
    # Live-code rules (dangerous_eval, xss, etc.) must NOT fire on comment lines.
    # Note: since O.15 Level 1, a Python comment with dangerous patterns WILL
    # produce a `code_in_comment` finding — that is intentional and correct.
    codes_py = _codes(sc.scan_source('# x = eval(data)\n', "python"))
    check("py_comment_skipped", "dangerous_eval" not in codes_py)
    check("js_comment_skipped", sc.scan_source('// el.innerHTML = x;\n', "javascript") == [])
    check("rust_comment_skipped", sc.scan_source('// let token = "abc12345";\n', "rust") == [])


def test_unsupported_language():
    sc = SecurityScanner()
    check("unknown_lang_empty", sc.scan_source('eval(x)\n', "cobol") == [])
    check("empty_source_empty", sc.scan_source('', "python") == [])


# --- error-dict contract --------------------------------------------
def test_error_dict_contract():
    sc = SecurityScanner()
    f = sc.scan_source('API_KEY = "sk-live-abc123"\n', "python", file_rel="cfg.py")[0]
    for key in ("file", "line", "column", "message", "code", "severity",
                "error_type", "error_class", "confidence", "autofixable"):
        check(f"contract_has_{key}", key in f)
    check("contract_class", f["error_class"] == "SECURITY")
    check("contract_type", f["error_type"] == "security")
    check("contract_file", f["file"] == "cfg.py")
    check("contract_line", f["line"] == 1)
    # hardcoded_secret: автофиксим каноничную форму `UPPER = "literal"` через
    # `os.environ`; SecurityScanner объявляет код autofixable=True. Кейсы,
    # которые правило не возьмёт (lowercase-имя, короткий литерал), вернут
    # None из RuleBasedFixer.try_fix — это нормально (фолбэк на LLM/review).
    check("contract_hardcoded_autofixable", f["autofixable"] is True)
    # xss и yaml.load — тривиальный однострочный фикс → autofixable
    xf = SecurityScanner().scan_source('el.innerHTML = x;\n', "javascript")[0]
    check("contract_xss_autofixable", xf["autofixable"] is True)


# --- K.7: rule-based security auto-fix ------------------------------
def test_autofix_xss():
    from fixers.rule_based_fixer import RuleBasedFixer
    err = {"code": "xss", "file": "a.js", "line": 1}
    es = RuleBasedFixer().try_fix(err, "el.innerHTML = userInput;\n", "javascript")
    check("autofix_xss_editset", es is not None)
    if es:
        out = es.apply({"a.js": "el.innerHTML = userInput;\n"})
        check("autofix_xss_result", out and out["a.js"] == "el.textContent = userInput;\n")


def test_autofix_yaml():
    from fixers.rule_based_fixer import RuleBasedFixer
    err = {"code": "unsafe_deserialization", "file": "a.py", "line": 1}
    es = RuleBasedFixer().try_fix(err, "cfg = yaml.load(t)\n", "python")
    check("autofix_yaml_editset", es is not None)
    if es:
        out = es.apply({"a.py": "cfg = yaml.load(t)\n"})
        check("autofix_yaml_result", out and out["a.py"] == "cfg = yaml.safe_load(t)\n")


def test_autofix_declines_nontrivial():
    from fixers.rule_based_fixer import RuleBasedFixer
    fx = RuleBasedFixer()
    # pickle под тем же кодом unsafe_deserialization — НЕ чиним
    check("decline_pickle", fx.try_fix({"code": "unsafe_deserialization", "file": "a.py", "line": 1},
                                       "o = pickle.loads(b)\n", "python") is None)
    # hardcoded_secret/dangerous_eval теперь авто-фиксятся для каноничных
    # случаев. Здесь проверяем: на НЕ-каноничном входе (lowercase-имя,
    # `eval(var)` без `input(...)`) правило корректно отказывается → None,
    # дальше идёт LLM/review.
    check("decline_hardcoded_lowercase_name",
          fx.try_fix({"code": "hardcoded_secret", "file": "a.py", "line": 1},
                     'api_key = "sk-live-abc123"\n', "python") is None)
    check("decline_eval_non_input",
          fx.try_fix({"code": "dangerous_eval", "file": "a.py", "line": 1},
                     "x = eval(data)\n", "python") is None)
    check("decline_sql", fx.try_fix({"code": "sql_injection", "file": "a.py", "line": 1},
                                    'cur.execute("SELECT " + x)\n', "python") is None)
    # document.write под кодом xss — не эквивалентно textContent → None
    check("decline_doc_write", fx.try_fix({"code": "xss", "file": "a.js", "line": 1},
                                          "document.write(x);\n", "javascript") is None)


# --- O.15 Level 1: code_in_comment via tokenize ---------------------
def test_code_in_comment():
    sc = SecurityScanner()

    # Positive cases: dangerous patterns in real comment tokens
    check("cic_eval_pure_comment",
          "code_in_comment" in _codes(sc.scan_source('# eval(user_data)\n', "python")))
    check("cic_exec_pure_comment",
          "code_in_comment" in _codes(sc.scan_source('# exec(payload)\n', "python")))
    check("cic_os_system_comment",
          "code_in_comment" in _codes(sc.scan_source('# os.system(cmd)\n', "python")))
    check("cic_subprocess_comment",
          "code_in_comment" in _codes(sc.scan_source('# subprocess.run(cmd)\n', "python")))
    check("cic_yaml_load_comment",
          "code_in_comment" in _codes(sc.scan_source('# cfg = yaml.load(data)\n', "python")))
    check("cic_pickle_loads_comment",
          "code_in_comment" in _codes(sc.scan_source('# obj = pickle.loads(b)\n', "python")))

    # Positive: inline (trailing) comment on a live-code line
    check("cic_eval_inline_comment",
          "code_in_comment" in _codes(sc.scan_source('x = 1  # eval(x)\n', "python")))
    check("cic_subprocess_inline_comment",
          "code_in_comment" in _codes(sc.scan_source('result = foo()  # subprocess.check_output(cmd)\n', "python")))

    # Critical false-positive prevention: '#' inside a string is NOT a comment token
    check("cic_fp_hash_in_string",
          "code_in_comment" not in _codes(sc.scan_source('x = "abc#eval(d)"\n', "python")))
    check("cic_fp_hash_in_single_string",
          "code_in_comment" not in _codes(sc.scan_source("x = 'hello#os.system(cmd)'\n", "python")))

    # No detection on benign comment text
    check("cic_safe_comment",
          "code_in_comment" not in _codes(sc.scan_source('# this is a normal comment\n', "python")))
    check("cic_todo_comment",
          "code_in_comment" not in _codes(sc.scan_source('# TODO: refactor this method\n', "python")))

    # No detection for JS/Rust (tokenize path is Python-only)
    check("cic_no_js",
          "code_in_comment" not in _codes(sc.scan_source('// eval(x);\n', "javascript")))
    check("cic_no_rust",
          "code_in_comment" not in _codes(sc.scan_source('// eval(x);\n', "rust")))

    # Contract: code_in_comment finding has correct fields
    findings = sc.scan_source('# eval(user_data)\n', "python", file_rel="x.py")
    cic = next((f for f in findings if f["code"] == "code_in_comment"), None)
    check("cic_contract_exists", cic is not None)
    if cic:
        check("cic_contract_class", cic["error_class"] == "SECURITY")
        check("cic_contract_severity", cic["severity"] == "warning")
        check("cic_contract_confidence", abs(cic["confidence"] - 0.40) < 1e-9)
        check("cic_contract_autofixable", cic["autofixable"] is False)
        check("cic_contract_line", cic["line"] == 1)


# --- AnalyzeStage wiring --------------------------------------------
class _FakeAnalyzer:
    def analyze(self, path, clean_before_each=False):
        return []


def _ctx(d, security_scan, language="python"):
    cfg = {"pipeline": {"security_scan": security_scan}}
    return PipelineContext(project_path=Path(d), language=language, config=cfg)


def test_helper_flag_default_and_explicit():
    # default True (ключа нет)
    ctx_default = PipelineContext(project_path=Path("/tmp"), language="python", config={"pipeline": {}})
    check("helper_default_true", AnalyzeStage._security_scan_enabled(ctx_default) is True)
    check("helper_explicit_true", AnalyzeStage._security_scan_enabled(_ctx("/tmp", True)) is True)
    check("helper_explicit_false", AnalyzeStage._security_scan_enabled(_ctx("/tmp", False)) is False)


def test_analyze_flag_on_adds_security():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "v.py").write_text('API_KEY = "sk-live-abc123"\n', encoding="utf-8")
        stage = AnalyzeStage(_FakeAnalyzer())
        stage.semgrep.safe_run = lambda **kw: []
        out = stage.execute(_ctx(d, security_scan=True))
        codes = [e.get("code") for e in out.current_errors]
        check("analyze_on_adds_security", "hardcoded_secret" in codes)
        classes = [e.get("error_class") for e in out.current_errors]
        check("analyze_on_class_security", "SECURITY" in classes)


def test_analyze_flag_off_skips():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "v.py").write_text('API_KEY = "sk-live-abc123"\n', encoding="utf-8")
        stage = AnalyzeStage(_FakeAnalyzer())
        stage.semgrep.safe_run = lambda **kw: []
        called = {"n": 0}

        def _spy(work_dir, language):
            called["n"] += 1
            return []

        stage.security_scanner.scan_project = _spy
        out = stage.execute(_ctx(d, security_scan=False))
        codes = [e.get("code") for e in out.current_errors]
        check("analyze_off_no_security", "hardcoded_secret" not in codes)
        check("analyze_off_scanner_not_called", called["n"] == 0)


def test_analyze_graceful_on_exception():
    with tempfile.TemporaryDirectory() as d:
        stage = AnalyzeStage(_FakeAnalyzer())
        stage.semgrep.safe_run = lambda **kw: []

        def _boom(work_dir, language):
            raise RuntimeError("boom")

        stage.security_scanner.scan_project = _boom
        # не должно ронять анализ — execute отрабатывает, ошибок security нет
        out = stage.execute(_ctx(d, security_scan=True))
        check("analyze_exception_graceful", out is not None)


if __name__ == "__main__":
    print("Stage K security detection smoke:")
    test_detect_python()
    test_detect_js()
    test_detect_rust()
    test_clean_no_findings()
    test_comment_lines_skipped()
    test_unsupported_language()
    test_error_dict_contract()
    test_autofix_xss()
    test_autofix_yaml()
    test_autofix_declines_nontrivial()
    test_code_in_comment()
    test_helper_flag_default_and_explicit()
    test_analyze_flag_on_adds_security()
    test_analyze_flag_off_skips()
    test_analyze_graceful_on_exception()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage K security: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
