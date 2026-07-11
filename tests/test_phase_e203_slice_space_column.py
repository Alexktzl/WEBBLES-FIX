r"""
2026-06-25 (направление "rule-based расширение", learning_cases.jsonl):
E203 — 19 попыток через LLM (95% успеха), 0 через rule_based, хотя
правило `_py_slice_space` существует с 2026-06-16. Root cause: старый
regex `\[\s*(\w+)\s*:\s*(\w+)\s*\]` требовал, чтобы ОБА операнда слайса
были простым identifier'ом — реальные слайсы часто содержат ВЫРАЖЕНИЯ
(`data[i : i + CHUNK_SIZE]`) — regex не матчился вообще, кейс ВСЕГДА
уходил в LLM. Теперь используем error["column"] (flake8 репортит точную
позицию пробела перед ':') — работает независимо от сложности выражений.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.rule_based_fixer import RuleBasedFixer


def _run(line_text: str, col: int):
    fixer = RuleBasedFixer()
    return fixer._py_slice_space(
        {"file": "t.py", "line": 1, "column": col, "code": "E203",
         "message": "whitespace before ':'"},
        line_text,
    )


def test_simple_identifier_slice_still_works():
    """Регрессия: исходный (простой identifier) случай не должен сломаться."""
    out = _run("x = data[1 : 3]\n", 11)
    assert out is not None
    assert out.edits[0].new == "x = data[1: 3]\n"


def test_complex_expression_on_both_sides_now_fixed():
    """Реальный кейс, который старый regex не матчил: выражение
    `i + CHUNK_SIZE` справа от ':' — теперь чинится через column."""
    line = "chunks = [data[i : i + CHUNK_SIZE] for i in range(10)]\n"
    out = _run(line, 17)
    assert out is not None
    assert out.edits[0].new == "chunks = [data[i: i + CHUNK_SIZE] for i in range(10)]\n"


def test_complex_expression_both_sides():
    """dum[k + 1 : 2 * k + 1] — выражения с обеих сторон."""
    line = "sym = data[k + 1 : 2 * k + 1]\n"
    out = _run(line, 17)
    assert out is not None
    assert out.edits[0].new == "sym = data[k + 1: 2 * k + 1]\n"


def test_only_leading_space_removed_trailing_space_kept():
    """Принципиально: убираем ТОЛЬКО пробел ПЕРЕД ':' (то, на что жалуется
    E203) — пробел ПОСЛЕ не трогаем (принадлежит соседнему выражению,
    не двоеточию; black использует именно такой паттерн для сложных
    слайсов — трогать его не наша забота, E203 про него не жалуется)."""
    line = "x = data[a : b + 1]\n"
    out = _run(line, 11)
    assert out is not None
    new = out.edits[0].new
    assert new == "x = data[a: b + 1]\n"
    assert " b + 1" in new  # пробел после ':' остался нетронутым


def test_real_flake8_confirms_e203_resolved_and_no_new_e2_findings():
    """Поведенческая проверка реальным flake8 (не мок): после фикса не
    должно остаться E203, и не должно появиться НИКАКИХ новых E2xx находок."""
    import tempfile
    content = (
        "data = [1, 2, 3, 4, 5]\n"
        "CHUNK_SIZE = 2\n"
        "k = 1\n"
        "chunks = [data[i : i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]\n"
    )
    fixer = RuleBasedFixer()
    error = {"file": "t.py", "line": 4, "column": 17, "code": "E203",
             "message": "whitespace before ':'"}
    out = fixer._py_slice_space(error, content)
    assert out is not None
    patched = content.replace(content.splitlines()[3], out.edits[0].new.rstrip("\n"))

    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "t.py"
        target.write_text(patched, encoding="utf-8")
        result = subprocess.run(
            ["flake8", "--select=E2", str(target)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"новые/оставшиеся E2xx находки: {result.stdout}"


def test_column_out_of_range_returns_none():
    out = _run("x = 1\n", 99)
    assert out is None


def test_no_colon_at_expected_position_returns_none():
    """Если по указанной колонке после пробелов НЕ оказывается ':' —
    несоответствие данных, тихо отступаем."""
    out = _run("x = a + b\n", 6)
    assert out is None


def test_no_actual_whitespace_at_column_returns_none():
    out = _run("x = data[1:3]\n", 11)
    assert out is None
