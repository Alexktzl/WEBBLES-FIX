"""
Regress: cascade collapse — универсальный пре-фильтр для списка ошибок.

Проверяет три эвристики:
  1) CRITICAL_SYNTAX cascade (per-file): синт. ошибка в строке N → всё ниже
     помечается derived.
  2) Missing-dependency cascade (per-file, по символу): missing import/use/
     include/using → undefined в том же файле с упоминанием символа.
  3) Symbol-use cluster: ≥3 ошибок про один backtick-символ → ранняя становится
     root, остальные derived.

Все случаи: collapse никогда не удаляет ошибки, только переупорядочивает и
аннотирует `_cascade_root` / `_cascade_derived_from`.

Запуск: python3 tests/test_phase_cascade_collapse.py
"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.error_cascade import collapse_cascades, group_cascades


def _err(file, line, code, message, error_class=""):
    return {"file": file, "line": line, "code": code,
            "message": message, "error_class": error_class}


# === 1. Пустой/одна ошибка → ничего не меняем ===
def test_empty_passthrough():
    assert collapse_cascades([]) == []


def test_single_error_no_annotation():
    e = _err("a.rs", 5, "E0432", "unresolved import `x`")
    out = collapse_cascades([e])
    assert len(out) == 1
    # Корень-одиночка: либо без annotation, либо _cascade_root=True — для одиночки
    # collapse может его пометить root'ом, но НЕ derived.
    assert not out[0].get("_cascade_derived_from")


# === 2. CRITICAL_SYNTAX cascade в Rust ===
def test_critical_syntax_cascade_rust():
    errs = [
        _err("src/game.rs", 7, "E0061", "wrong args"),                    # выше root → не derived
        _err("src/game.rs", 12, "GCC_BRACE", "unmatched brace", "CRITICAL_SYNTAX"),  # ROOT (но мы поставили rust-код)
        _err("src/game.rs", 15, "E0609", "no field val"),                 # ниже → derived
        _err("src/game.rs", 20, "E0425", "cannot find x"),                # ниже → derived
        _err("src/other.rs", 3, "E0432", "unresolved import"),            # другой файл — не трогаем
    ]
    out = collapse_cascades(errs)
    # Root оказался первым после реордера (root < derived)
    sigs = [(e["file"], e["line"]) for e in out]
    assert sigs[0] == ("src/game.rs", 12) or sigs[1] == ("src/game.rs", 12), \
        f"root должен быть в начале: {sigs}"
    # Стр. 15 и 20 — derivatives
    derived15 = next(e for e in out if e["file"] == "src/game.rs" and e["line"] == 15)
    derived20 = next(e for e in out if e["file"] == "src/game.rs" and e["line"] == 20)
    assert derived15.get("_cascade_derived_from")
    assert derived20.get("_cascade_derived_from")
    # Стр. 7 — НЕ derived (ВЫШЕ root'а)
    above = next(e for e in out if e["file"] == "src/game.rs" and e["line"] == 7)
    assert not above.get("_cascade_derived_from")
    # Другой файл не аннотируется как derived
    other = next(e for e in out if e["file"] == "src/other.rs")
    assert not other.get("_cascade_derived_from")


# === 3. Missing-import cascade: C++ (GCC_INCLUDE → undef) ===
def test_missing_include_cpp_cascade():
    errs = [
        _err("main.cpp", 2, "GCC_INCLUDE", "'cout' is not a member of 'std'"),
        _err("main.cpp", 5, "GCC_UNDECLARED", "'cout' was not declared in this scope"),
        _err("main.cpp", 8, "GCC_UNDECLARED", "'cout' was not declared in this scope"),
    ]
    out = collapse_cascades(errs)
    root = next(e for e in out if e["line"] == 2)
    der5 = next(e for e in out if e["line"] == 5)
    der8 = next(e for e in out if e["line"] == 8)
    assert root.get("_cascade_root") is True
    assert der5.get("_cascade_derived_from"), "стр.5 — derived от стр.2"
    assert der8.get("_cascade_derived_from"), "стр.8 — derived от стр.2"


# === 4. Missing-using cascade: C# ===
def test_missing_using_csharp_cascade():
    errs = [
        _err("Program.cs", 3, "CS0246", "The type or namespace name 'StringBuilder' could not be found"),
        _err("Program.cs", 7, "CS0103", "The name 'StringBuilder' does not exist in the current context"),
    ]
    out = collapse_cascades(errs)
    root = next(e for e in out if e["line"] == 3)
    der = next(e for e in out if e["line"] == 7)
    assert root.get("_cascade_root") is True
    assert der.get("_cascade_derived_from")


# === 5. Symbol-use cluster: 3+ ошибки про один символ → cluster ===
def test_symbol_cluster_cross_file():
    errs = [
        _err("a.rs", 10, "E0277", "the trait `Foo` is not implemented"),
        _err("b.rs", 5, "E0277", "the trait `Foo` is not implemented"),
        _err("c.rs", 15, "E0277", "the trait `Foo` is not implemented"),
    ]
    out = collapse_cascades(errs)
    # Самая ранняя по (file, line): a.rs:10
    root = next(e for e in out if e["file"] == "a.rs" and e["line"] == 10)
    others = [e for e in out if not (e["file"] == "a.rs" and e["line"] == 10)]
    assert root.get("_cascade_root") is True
    assert all(e.get("_cascade_derived_from") for e in others), \
        "оба других — derivatives кластера Foo"


# === 6. Reorder: roots+standalone сначала ===
def test_reorder_roots_first():
    errs = [
        _err("x.rs", 99, "E0609", "field"),                            # standalone
        _err("y.rs", 1, "GCC_BRACE", "brace", "CRITICAL_SYNTAX"),       # ROOT
        _err("y.rs", 5, "E0425", "downstream"),                        # DERIVED
        _err("z.rs", 10, "E0432", "unresolved `Foo`"),                 # standalone root
    ]
    out = collapse_cascades(errs)
    # Все derivatives должны быть В КОНЦЕ списка
    derived_indices = [i for i, e in enumerate(out) if e.get("_cascade_derived_from")]
    non_derived_indices = [i for i, e in enumerate(out) if not e.get("_cascade_derived_from")]
    if derived_indices and non_derived_indices:
        assert min(derived_indices) > max(non_derived_indices), \
            "derivatives должны идти после всех root/standalone"


# === 7. group_cascades — для debug-отчётов ===
def test_group_cascades_structure():
    errs = collapse_cascades([
        _err("main.cpp", 2, "GCC_INCLUDE", "'cout' is not a member of 'std'"),
        _err("main.cpp", 5, "GCC_UNDECLARED", "'cout' was not declared"),
    ])
    groups = group_cascades(errs)
    # Должна быть одна группа с одним derivative
    have_root = next((g for g in groups if g["root"]["line"] == 2), None)
    assert have_root is not None
    assert len(have_root["derivatives"]) == 1
    assert have_root["derivatives"][0]["line"] == 5


# === 8. Graceful: битые данные не роняют ===
def test_graceful_with_bad_input():
    # Без обязательных полей
    out = collapse_cascades([{}, {"file": "x", "line": "not_a_number", "code": "X", "message": ""}])
    assert isinstance(out, list)


if __name__ == "__main__":
    tests = [
        ("empty_passthrough", test_empty_passthrough),
        ("single_error_no_annotation", test_single_error_no_annotation),
        ("critical_syntax_cascade_rust", test_critical_syntax_cascade_rust),
        ("missing_include_cpp_cascade", test_missing_include_cpp_cascade),
        ("missing_using_csharp_cascade", test_missing_using_csharp_cascade),
        ("symbol_cluster_cross_file", test_symbol_cluster_cross_file),
        ("reorder_roots_first", test_reorder_roots_first),
        ("group_cascades_structure", test_group_cascades_structure),
        ("graceful_with_bad_input", test_graceful_with_bad_input),
    ]
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
    print()
    print(f"Cascade collapse regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
