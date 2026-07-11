"""
Stage C — regression (F.3-extension): «вести LLM за ручку» через case-file.

Покрывает закрытые подзадачи Stage C без тяжёлых импортов:

  C.1  SymbolGraph.find_related_definitions — находит определение символа,
       упомянутого в error.message, в другом файле проекта.
  C.2  error_constraints.get_constraints / is_known — DO/DON'T для известных
       кодов, ([], []) для неизвестных.
  C.3  Memory.get_similar_fixes — прецеденты для few-shot в case-file.
  C.4  PromptBuilder.build_case_file — секции (SUMMARY/SPAN/RELATED/
       CONSTRAINTS/SIMILAR), деградация на пустом вводе.
  I.3  Секция PROJECT USAGE (cross_file) появляется/отсутствует.

Запуск: python3 tests/test_phase_c_casefile.py
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from analysis.symbol_graph import SymbolGraph
from analysis.constraints.error_constraints import get_constraints, is_known
from fixers.prompt_builder import PromptBuilder
from memory.learning import MemoryLearning

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- C.1: SymbolGraph.find_related_definitions ----------------------
def test_symbol_graph():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src").mkdir()
        (root / "src" / "player.rs").write_text(
            "pub struct Player {\n    name: String,\n    attempt: u32,\n}\n",
            encoding="utf-8")
        (root / "src" / "main.rs").write_text(
            "fn main() {\n    let p = Player { player_name: \"x\".into() };\n}\n",
            encoding="utf-8")

        sg = SymbolGraph(root, "rust")
        error = {"file": "src/main.rs", "code": "E0560",
                 "message": "struct `Player` has no field named `player_name`"}
        out = sg.find_related_definitions(error, working_path=root, max_symbols=5)
        check("symgraph_finds_player", "Player" in out)
        check("symgraph_returns_struct_def", "struct Player" in out)

        empty = sg.find_related_definitions(
            {"file": "src/main.rs", "code": "E0106", "message": "missing lifetime specifier"},
            working_path=root)
        check("symgraph_no_symbols_empty", isinstance(empty, str) and "struct Player" not in empty)


# --- C.2: constraints table -----------------------------------------
def test_constraints():
    do, dont = get_constraints("E0382")
    check("constraints_e0382_nonempty", len(do) >= 1 and len(dont) >= 1)
    check("constraints_e0382_no_unsafe", any("unsafe" in x.lower() for x in dont))

    do9, dont9 = get_constraints("E0609")
    check("constraints_e0609_dont_add_field",
          any("field" in x.lower() for x in dont9))

    check("constraints_unknown_empty", get_constraints("ZZZ999") == ([], []))
    check("constraints_is_known", is_known("E0382") and not is_known("ZZZ999"))


# --- C.3: memory similar fixes (few-shot) ---------------------------
def test_similar_fixes():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryLearning(Path(d) / "mem.json")
        sig = "src/a.rs::E0382::moved"
        m.record_success(sig, "patch-A", score=0.7, intent="use ref")
        m.record_success(sig, "patch-B", score=0.95, intent="clone")
        top = m.get_similar_fixes(sig, n=2)
        check("similar_returns_two", len(top) == 2)
        check("similar_sorted_by_score", top[0].get("patch") == "patch-B")
        check("similar_unknown_empty", m.get_similar_fixes("nope", n=2) == [])


# --- C.4 + I.3: build_case_file -------------------------------------
def test_build_case_file():
    pb = PromptBuilder()
    error = {"file": "src/lib.rs", "line": 2, "code": "E0609",
             "error_class": "BLOCKING",
             "message": "no field `foo` on type `Bar`"}
    content = "struct Bar { baz: u32 }\nfn f(b: Bar) { b.foo; }\n"
    do, dont = get_constraints("E0609")
    cf = pb.build_case_file(
        error=error, file_content=content,
        related_defs="### Bar (src/lib.rs:1)\nstruct Bar { baz: u32 }",
        constraints=(do, dont),
        similar_fixes=[{"intent": "use existing field", "patch": "- b.foo\n+ b.baz",
                        "confidence": 0.9}],
    )
    check("casefile_has_summary", "## SUMMARY" in cf and "[E0609]" in cf)
    check("casefile_has_span", "## SPAN" in cf and "src/lib.rs:2" in cf)
    check("casefile_has_related", "## RELATED DEFINITIONS" in cf)
    check("casefile_has_constraints", "## CONSTRAINTS" in cf and "DON'T" in cf)
    check("casefile_has_similar", "## SIMILAR FIXES" in cf)
    check("casefile_header", cf.startswith("# CASE FILE"))

    # Stage I: cross_file → секция PROJECT USAGE (и нет её без аргумента)
    cf_xf = pb.build_case_file(
        error=error, file_content=content,
        cross_file="### `Bar` — project-wide\n- def [struct] src/lib.rs:1\nused in 2 place(s):\n  - src/x.rs:5",
    )
    check("casefile_has_project_usage", "## PROJECT USAGE" in cf_xf and "project-wide" in cf_xf)
    check("casefile_no_project_usage_default", "## PROJECT USAGE" not in cf)

    # пустой error → пустая строка (нечего наполнять)
    check("casefile_empty_input", pb.build_case_file({}) == "")


if __name__ == "__main__":
    print("Stage C — case-file regression:")
    test_symbol_graph()
    test_constraints()
    test_similar_fixes()
    test_build_case_file()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage C regression: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
