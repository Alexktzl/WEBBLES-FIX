"""
Stage I — smoke / regression: cross-file reasoning (ProjectSymbolIndex).

Покрывает:
  I.1 build + definitions: индексирует определения по всем файлам (py/rust/js).
  I.3 references / impact_files / undefined_symbols / cross_file_context.

Чистый regex-индекс (tree-sitter в среде нет) — детерминирован, без сети/LLM.
Запуск: python3 tests/test_phase_i_crossfile.py
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from analysis.project_symbol_index import ProjectSymbolIndex, SymbolDef

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def _mkproj(files):
    d = tempfile.mkdtemp()
    for rel, content in files.items():
        p = Path(d) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


# --- Python multi-file ----------------------------------------------
def test_python_index():
    proj = _mkproj({
        "models.py": "class Player:\n    pass\n\ndef compute(x):\n    return x\n",
        "main.py": "from models import Player, compute\n\np = Player()\nprint(compute(5))\n",
        "util.py": "def helper():\n    return compute(1)\n",
        "skip/__pycache__/x.py": "def ghost(): pass\n",  # в служебном каталоге
    })
    idx = ProjectSymbolIndex.build(proj, "python")

    defs = idx.definitions("Player")
    check("py_def_found", len(defs) == 1 and defs[0].kind == "class")
    check("py_def_file_line", defs[0].file == "models.py" and defs[0].line == 1)
    check("py_compute_is_function", idx.definitions("compute")[0].kind == "function")

    # ссылки на compute: models.py(def excl) + main.py + util.py
    refs = idx.references("compute")
    ref_files = {f for f, _ in refs}
    check("py_refs_crossfile", "main.py" in ref_files and "util.py" in ref_files)
    check("py_refs_exclude_def", ("models.py", 4) not in refs)  # строка def исключена

    # impact: кто пострадает от изменения compute (без файла-определения)
    impact = idx.impact_files("compute")
    check("py_impact", "main.py" in impact and "util.py" in impact and "models.py" not in impact)

    # служебный каталог не индексирован
    check("py_skips_pycache", not idx.is_defined("ghost"))

    # неопределённый символ
    check("py_undefined", idx.undefined_symbols(["Player", "Nonexistent"]) == ["Nonexistent"])


# --- Rust multi-file ------------------------------------------------
def test_rust_index():
    proj = _mkproj({
        "src/player.rs": "pub struct Player {\n    name: String,\n}\n\npub fn spawn() -> Player {\n    Player { name: String::new() }\n}\n",
        "src/main.rs": "mod player;\nuse player::Player;\n\nfn main() {\n    let p = player::spawn();\n}\n",
    })
    idx = ProjectSymbolIndex.build(proj, "rust")
    pdef = idx.definitions("Player")
    check("rs_struct_def", len(pdef) == 1 and pdef[0].kind == "struct")
    check("rs_fn_def", idx.definitions("spawn")[0].kind == "fn")
    check("rs_impact_spawn", "src/main.rs" in [p.replace("\\", "/") for p in idx.impact_files("spawn")])
    ctx = idx.cross_file_context("Player").replace("\\", "/")
    check("rs_context_has_def", "src/player.rs:1" in ctx and "struct" in ctx)
    check("rs_context_has_refs", "src/main.rs" in ctx)


# --- JS multi-file --------------------------------------------------
def test_js_index():
    proj = _mkproj({
        "math.js": "export function add(a, b) {\n  return a + b;\n}\n",
        "app.js": "import { add } from './math.js';\nconst r = add(1, 2);\n",
    })
    idx = ProjectSymbolIndex.build(proj, "javascript")
    check("js_fn_def", idx.definitions("add")[0].kind == "function")
    check("js_impact", "app.js" in idx.impact_files("add"))


# --- edge cases -----------------------------------------------------
def test_edges():
    idx = ProjectSymbolIndex.build(_mkproj({"a.py": "x = 1\n"}), "python")
    check("edge_no_defs", idx.definitions("nope") == [])
    check("edge_context_empty", idx.cross_file_context("nope") == "")
    check("edge_refs_empty_name", idx.references("") == [])
    # неподдерживаемый язык → пустой индекс, без падения
    idx2 = ProjectSymbolIndex.build(_mkproj({"a.cob": "x"}), "cobol")
    check("edge_unsupported_lang", idx2.definitions("x") == [])


if __name__ == "__main__":
    print("Stage I — cross-file reasoning smoke:")
    test_python_index()
    test_rust_index()
    test_js_index()
    test_edges()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage I: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
