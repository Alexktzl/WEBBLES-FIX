"""
Stage F.3 regress — Phase B (structured edit + JSON).

Гарантирует базовые инварианты EditSet (B.1–B.2):
- round-trip dict → EditSet → dict стабилен;
- `to_unified_diff` собирает корректный diff из anchor.line + anchor.match;
- битый anchor → diff не собирается (логика B.2).

Запуск:
    pytest tests/test_phase_b_structured.py
    или
    python3 tests/test_phase_b_structured.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_editset_roundtrip():
    """B.1 — dict → EditSet → dict стабилен."""
    from fixers.structured_edit import EditSet
    payload = {
        "intent": "rename invalid field access",
        "edits": [{
            "file": "src/main.rs",
            "anchor": {"line": 6, "match": "p.player_name"},
            "kind": "replace",
            "new": "p.name",
            "rationale": "use existing field",
        }],
        "risks": ["may shadow"],
        "confidence": 0.72,
    }
    es = EditSet.from_json(__import__("json").dumps(payload))
    assert es is not None, "from_json must accept valid payload"
    out = es.to_dict()
    assert out["intent"] == payload["intent"]
    assert out["confidence"] == 0.72
    assert out["edits"][0]["new"] == "p.name"
    assert out["edits"][0]["anchor"]["line"] == 6


def test_editset_to_unified_diff_happy():
    """B.2 — to_unified_diff собирает корректный diff на валидном anchor."""
    from fixers.structured_edit import EditSet
    file_content = (
        "fn main() {\n"
        "    let p = Player { name: \"a\".into(), attempt: 0 };\n"
        "    println!(\"{}\", p.player_name);\n"
        "}\n"
    )
    es = EditSet.from_json('{"intent":"rename","edits":[{"file":"src/main.rs",'
                           '"anchor":{"line":3,"match":"p.player_name"},'
                           '"kind":"replace","new":"p.name","rationale":"existing"}],'
                           '"risks":[],"confidence":0.8}')
    diff = es.to_unified_diff({"src/main.rs": file_content})
    assert diff is not None and diff.strip(), "happy-path diff must be non-empty"
    # Diff содержит обе строки с -/+ и упоминание имени.
    assert "-" in diff and "+" in diff
    assert "p.player_name" in diff or "p.name" in diff


def test_editset_to_unified_diff_broken_anchor():
    """B.2 — anchor.match отсутствует в файле → None (не выдумываем diff)."""
    from fixers.structured_edit import EditSet
    es = EditSet.from_json('{"intent":"x","edits":[{"file":"src/main.rs",'
                           '"anchor":{"line":3,"match":"WORD_THAT_DOES_NOT_EXIST"},'
                           '"kind":"replace","new":"X","rationale":""}],'
                           '"risks":[],"confidence":0.5}')
    diff = es.to_unified_diff({"src/main.rs": "fn main() { println!(\"hi\"); }\n"})
    assert diff is None or not diff.strip(), \
        "broken anchor must NOT produce a diff (B.2 invariant)"


if __name__ == "__main__":
    # Простой runner без pytest.
    tests = [
        ("editset_roundtrip", test_editset_roundtrip),
        ("editset_to_unified_diff_happy", test_editset_to_unified_diff_happy),
        ("editset_to_unified_diff_broken_anchor", test_editset_to_unified_diff_broken_anchor),
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
    print(f"Phase B regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
