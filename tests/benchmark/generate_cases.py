"""
Stage F.1 (extension) — генератор параметризованных benchmark-кейсов.

Идея: вместо ручного написания сотен почти одинаковых кейсов берём
небольшое число РЕАЛЬНЫХ шаблонов ошибок и подставляем разные имена/
типы/модули из пулов. Это honest-проверка надёжности: SymbolGraph (C.1),
error_constraints (C.2) и ai_authored_detector (D.6) должны работать на
`Account` / `Widget` / `Session`, а не только на захардкоженном `Player`.

Сгенерированные кейсы лежат в `cases/<lang>/gen_<code>_<variant>/` и
помечены `"generated": true` в expected.json — их легко отличить от
ручных. Запуск идемпотентен: повторный прогон перезаписывает gen_-кейсы,
ручные не трогает.

    python3 tests/benchmark/generate_cases.py            # сгенерировать
    python3 tests/benchmark/generate_cases.py --clean    # удалить gen_*

Шаблон описывается как dict с полями:
    code, category, error_class, file, line,
    message  (f-string-подобный с {ph}),
    before / after (с {ph}),
    optional: constraints_test, symbol_graph_var (имя плейсхолдера,
              определение которого должно найтись — попадёт в
              symbol_graph_test.must_find_definition_of)
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases"

# Пулы подстановок ------------------------------------------------------
TYPES = ["Account", "Widget", "Session", "Order", "Profile", "Invoice",
         "Customer", "Payment", "Booking", "Ticket", "Device", "Channel"]
FIELDS = [("name", "label"), ("count", "total"), ("value", "amount"),
          ("status", "state"), ("id", "key"), ("size", "length")]
MODULES_RUST = ["HashMap", "BTreeMap", "HashSet", "VecDeque", "BTreeSet"]
MODULES_PY = ["os", "sys", "json", "math", "random", "collections",
              "itertools", "functools", "datetime", "hashlib"]
FUNCS = ["compute", "process", "validate", "transform", "render",
         "serialize", "dispatch", "resolve", "aggregate", "normalize"]
VARS = ["temp", "buffer", "counter", "result", "cache", "handle",
        "offset", "cursor", "payload", "token"]
JS_VARS = ["data", "config", "result", "buffer", "context", "handler",
           "payload", "session", "cache", "registry"]


def _rust_cargo() -> str:
    return ('[package]\nname = "demo"\nversion = "0.0.1"\nedition = "2021"\n')


# Шаблоны ---------------------------------------------------------------
def _rust_templates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # E0609 — обращение к несуществующему полю.
    for i, (Type, (good, bad)) in enumerate(
        [(t, FIELDS[i % len(FIELDS)]) for i, t in enumerate(TYPES)]
    ):
        out.append({
            "code": "E0609", "category": "BLOCKING", "error_class": "STRUCTURAL",
            "file": "src/main.rs", "line": 4,
            "message": f"no field `{bad}` on type `{Type}`",
            "before": (
                f"pub struct {Type} {{ pub {good}: u32 }}\n"
                f"fn main() {{\n"
                f"    let v = {Type} {{ {good}: 1 }};\n"
                f"    println!(\"{{}}\", v.{bad});\n"
                f"}}\n"
            ),
            "after": (
                f"pub struct {Type} {{ pub {good}: u32 }}\n"
                f"fn main() {{\n"
                f"    let v = {Type} {{ {good}: 1 }};\n"
                f"    println!(\"{{}}\", v.{good});\n"
                f"}}\n"
            ),
            "constraints_test": {"expected_dont_contains": "add a new field"},
            "symbol_graph_value": Type,
            "rust": True,
        })
    # unused_import.
    for coll in MODULES_RUST:
        out.append({
            "code": "unused_import", "category": "CLEANUP", "error_class": "CLEANUP",
            "file": "src/main.rs", "line": 1,
            "message": f"unused import: `std::collections::{coll}`",
            "before": f"use std::collections::{coll};\nfn main() {{ println!(\"hi\"); }}\n",
            "after": "fn main() { println!(\"hi\"); }\n",
            "constraints_test": {"expected_do_contains": "Remove the unused"},
            "rust": True,
        })
    # unused_variable.
    for var in VARS:
        out.append({
            "code": "unused_variable", "category": "CLEANUP", "error_class": "CLEANUP",
            "file": "src/main.rs", "line": 2,
            "message": f"unused variable: `{var}`",
            "before": f"fn main() {{\n    let {var} = 5;\n    println!(\"hi\");\n}}\n",
            "after": f"fn main() {{\n    let _{var} = 5;\n    println!(\"hi\");\n}}\n",
            "constraints_test": {"expected_do_contains": "Prefix the binding with"},
            "rust": True,
        })
    # E0382 — borrow of moved value.
    for var in VARS:
        out.append({
            "code": "E0382", "category": "BLOCKING", "error_class": "OWNERSHIP",
            "file": "src/main.rs", "line": 5,
            "message": f"borrow of moved value: `{var}`",
            "before": (
                f"fn consume(s: String) {{ println!(\"{{}}\", s); }}\n"
                f"fn main() {{\n    let {var} = String::from(\"x\");\n"
                f"    consume({var});\n    consume({var});\n}}\n"
            ),
            "after": (
                f"fn consume(s: String) {{ println!(\"{{}}\", s); }}\n"
                f"fn main() {{\n    let {var} = String::from(\"x\");\n"
                f"    consume({var}.clone());\n    consume({var});\n}}\n"
            ),
            "constraints_test": {"expected_dont_contains": "unsafe"},
            "rust": True,
        })
    # dead_code — функция, которая никогда не вызывается.
    for fn in FUNCS:
        out.append({
            "code": "dead_code", "category": "CLEANUP", "error_class": "CLEANUP",
            "file": "src/main.rs", "line": 1,
            "message": f"function `{fn}` is never used",
            "before": f"fn {fn}() -> i32 {{ 42 }}\nfn main() {{ println!(\"hi\"); }}\n",
            "after": "fn main() { println!(\"hi\"); }\n",
            "constraints_test": {"expected_dont_contains": "blanket-silence"},
            "rust": True,
        })
    return out


def _python_templates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # F401 unused import. before/after консистентны с «удалить строку import»
    # (без лишней пустой строки — иначе rule-based и LLM разойдутся в одну строку).
    for mod in MODULES_PY:
        out.append({
            "code": "F401", "category": "CLEANUP", "error_class": "CLEANUP",
            "file": "main.py", "line": 1,
            "message": f"'{mod}' imported but unused",
            "before": f"import {mod}\nprint('hello')\n",
            "after": "print('hello')\n",
            "py": True,
        })
    # NameError typo (с подсказкой Did you mean — symbol resolver найдёт def).
    for fn in FUNCS:
        out.append({
            "code": "NameError", "category": "BLOCKING", "error_class": "STRUCTURAL",
            "file": "main.py", "line": 4,
            "message": f"name '{fn}x' is not defined. Did you mean: '{fn}'?",
            "before": (
                f"def {fn}(n):\n    return n * 2\n\n"
                f"print({fn}x(21))\n"
            ),
            "after": (
                f"def {fn}(n):\n    return n * 2\n\n"
                f"print({fn}(21))\n"
            ),
            "symbol_graph_value": fn,
            "py": True,
        })
    # AttributeError на str.
    for fn in FUNCS:
        bad = fn[:4] + "_x"
        out.append({
            "code": "AttributeError", "category": "BLOCKING", "error_class": "STRUCTURAL",
            "file": "main.py", "line": 2,
            "message": f"'str' object has no attribute '{bad}'",
            "before": f"def run(s):\n    return s.{bad}()\n\nprint(run('hi'))\n",
            "after": "def run(s):\n    return s.upper()\n\nprint(run('hi'))\n",
            "py": True,
        })
    # ZeroDivisionError.
    for fn in FUNCS:
        out.append({
            "code": "ZeroDivisionError", "category": "BLOCKING", "error_class": "STRUCTURAL",
            "file": "main.py", "line": 2,
            "message": "division by zero",
            "before": f"def {fn}(a, b):\n    return a / b\n\nprint({fn}(1, 0))\n",
            "after": (
                f"def {fn}(a, b):\n    if b == 0:\n        return 0\n"
                f"    return a / b\n\nprint({fn}(1, 0))\n"
            ),
            "py": True,
        })
    # KeyError typo.
    for good, bad in FIELDS:
        out.append({
            "code": "KeyError", "category": "BLOCKING", "error_class": "STRUCTURAL",
            "file": "main.py", "line": 2,
            "message": f"'{bad}'",
            "before": (
                f"def get(d):\n    return d['{bad}']\n\n"
                f"print(get({{'{good}': 1}}))\n"
            ),
            "after": (
                f"def get(d):\n    return d['{good}']\n\n"
                f"print(get({{'{good}': 1}}))\n"
            ),
            "py": True,
        })
    return out


def _js_templates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # no-unused-vars.
    for var in JS_VARS:
        out.append({
            "code": "no-unused-vars", "category": "CLEANUP", "error_class": "CLEANUP",
            "file": "main.js", "line": 2,
            "message": f"'{var}' is assigned a value but never used",
            "before": (
                f"function run() {{\n    const {var} = 42;\n"
                f"    console.log('hi');\n}}\nrun();\n"
            ),
            "after": "function run() {\n    console.log('hi');\n}\nrun();\n",
            "js": True,
        })
    # eqeqeq.
    for var in JS_VARS:
        out.append({
            "code": "eqeqeq", "category": "WARNING", "error_class": "WARNING",
            "file": "main.js", "line": 2,
            "message": "Expected '===' and instead saw '=='",
            "before": (
                f"function check({var}) {{\n    return {var} == 0;\n}}\n"
                f"check(1);\n"
            ),
            "after": (
                f"function check({var}) {{\n    return {var} === 0;\n}}\n"
                f"check(1);\n"
            ),
            "js": True,
        })
    # no-var.
    for var in JS_VARS:
        out.append({
            "code": "no-var", "category": "WARNING", "error_class": "WARNING",
            "file": "main.js", "line": 1,
            "message": "Unexpected var, use let or const instead",
            "before": f"var {var} = 0;\n{var} += 1;\nconsole.log({var});\n",
            "after": f"let {var} = 0;\n{var} += 1;\nconsole.log({var});\n",
            "js": True,
        })
    # no-console.
    for var in JS_VARS:
        out.append({
            "code": "no-console", "category": "WARNING", "error_class": "WARNING",
            "file": "main.js", "line": 2,
            "message": "Unexpected console statement",
            "before": (
                f"function dbg({var}) {{\n    console.log({var});\n"
                f"    return {var};\n}}\ndbg(5);\n"
            ),
            "after": f"function dbg({var}) {{\n    return {var};\n}}\ndbg(5);\n",
            "js": True,
        })
    # prefer-const.
    for var in JS_VARS:
        out.append({
            "code": "prefer-const", "category": "WARNING", "error_class": "WARNING",
            "file": "main.js", "line": 1,
            "message": f"'{var}' is never reassigned. Use 'const' instead",
            "before": f"let {var} = 42;\nconsole.log({var});\n",
            "after": f"const {var} = 42;\nconsole.log({var});\n",
            "js": True,
        })
    return out


def _lang_of(tpl: Dict[str, Any]) -> str:
    if tpl.get("rust"):
        return "rust"
    if tpl.get("py"):
        return "python"
    return "javascript"


def _variant_name(tpl: Dict[str, Any], idx: int) -> str:
    code = tpl["code"].replace("::", "_").replace("-", "_").lower()
    return f"gen_{code}_{idx:03d}"


def generate(limit: Optional[int] = None) -> int:
    templates = _rust_templates() + _python_templates() + _js_templates()
    if limit is not None:
        templates = templates[:limit]
    count = 0
    per_code_idx: Dict[str, int] = {}
    for tpl in templates:
        lang = _lang_of(tpl)
        key = (lang, tpl["code"])
        idx = per_code_idx.get(key, 0)
        per_code_idx[key] = idx + 1
        name = _variant_name(tpl, idx)
        case_dir = CASES / lang / name
        before_dir = case_dir / "before"
        after_dir = case_dir / "after"
        before_dir.mkdir(parents=True, exist_ok=True)
        after_dir.mkdir(parents=True, exist_ok=True)

        file_rel = tpl["file"]
        (before_dir / file_rel).parent.mkdir(parents=True, exist_ok=True)
        (after_dir / file_rel).parent.mkdir(parents=True, exist_ok=True)
        (before_dir / file_rel).write_text(tpl["before"], encoding="utf-8")
        (after_dir / file_rel).write_text(tpl["after"], encoding="utf-8")

        if lang == "rust":
            (before_dir / "Cargo.toml").write_text(_rust_cargo(), encoding="utf-8")
            (after_dir / "Cargo.toml").write_text(_rust_cargo(), encoding="utf-8")

        expected: Dict[str, Any] = {
            "description": f"[generated] {tpl['code']}: {tpl['message'][:80]}",
            "language": lang,
            "category": tpl["category"],
            "generated": True,
            "primary_error": {
                "code": tpl["code"],
                "file": file_rel,
                "line": tpl["line"],
                "error_class": tpl["error_class"],
                "message": tpl["message"],
            },
            "acceptable_fixes": ["see after/ for the canonical fix"],
        }
        if "constraints_test" in tpl:
            expected["constraints_test"] = tpl["constraints_test"]
        if "symbol_graph_value" in tpl:
            expected["symbol_graph_test"] = {
                "must_find_definition_of": tpl["symbol_graph_value"]
            }
        (case_dir / "expected.json").write_text(
            json.dumps(expected, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        count += 1
    return count


def clean() -> int:
    removed = 0
    for lang_dir in CASES.iterdir():
        if not lang_dir.is_dir():
            continue
        for case_dir in lang_dir.iterdir():
            if case_dir.is_dir() and case_dir.name.startswith("gen_"):
                shutil.rmtree(case_dir, ignore_errors=True)
                removed += 1
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="remove all gen_* cases")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if args.clean:
        n = clean()
        print(f"removed {n} generated cases")
        return 0
    n = generate(limit=args.limit)
    print(f"generated {n} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
