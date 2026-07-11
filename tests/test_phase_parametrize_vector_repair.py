"""
Rule-based ремонт «vector/parametrize»-блоков — list-item/arg-type
(2026-07-03, pyca/bcrypt tests/test_bcrypt.py).

Toxic-класс из «исключаем» в «чиним»: тест-векторы вида list-of-lists с
ГЕТЕРОГЕННЫМИ элементами (`[4, b"password", b"salt", b"\\x.."]`) mypy инферит
как list[object] (list ИНВАРИАНТЕН) и в типовом контексте pytest.parametrize
это даёт `arg-type ... incompatible type "list[object]"` /
`list-item ... expected "tuple[...]"`. Честный фикс — внутренние списки →
КОРТЕЖИ: tuple КОВАРИАНТЕН и хранит типы по позициям, mypy доволен. Ни один
литерал НЕ меняется — чистая структурная замена `[` `]` → `(` `)` по позициям
AST-узлов (НЕ ast.unparse: он потерял бы форматирование, комментарии, hex).

Инвариант, который кодируют эти тесты:
  * ВСЕ внутренние списки целевого L0 → кортежи (тип ряда становится
    однородным List[Tuple]);
  * мультимножество всех Constant-литералов до/после совпадает ТОЧНО
    (внутренний O.19-супергард фиксера);
  * строгий триггер: НЕ срабатывает на гомогенных списках, списках с
    именами/вызовами, dict/set, list-of-tuples, вложенности глубже 2,
    одноэлементных внутренних списках.

Запуск: python tests/test_phase_parametrize_vector_repair.py
"""

from __future__ import annotations

import ast
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.rule_based_fixer import RuleBasedFixer  # noqa: E402


# Реальный фрагмент из pyca/bcrypt tests/test_bcrypt.py (test_kdf):
# hex-конкатенация через границу строк (implicit concat) + комментарий
# ВНУТРИ вектора + trailing comma. Всё это обязано пережить правку дословно.
BCRYPT_FRAGMENT = '''\
import pytest


@pytest.mark.parametrize(
    ("rounds", "password", "salt", "expected"),
    [
        [
            4,
            b"password",
            b"salt",
            b"\\x5b\\xbf\\x0c\\xc2\\x93\\x58\\x7f\\x1c\\x36\\x35\\x55\\x5c\\x27\\x79\\x65\\x98"
            b"\\xd4\\x7e\\x57\\x90\\x71\\xbf\\x42\\x7e\\x9d\\x8f\\xbe\\x84\\x2a\\xba\\x34\\xd9",
        ],
        [
            # nul bytes in password and string
            4,
            b"password\\x00",
            b"salt\\x00",
            b"\\x74\\x10\\xe4\\x4c\\xf4\\xfa\\x07\\xbf\\xaa\\xc8\\xa9\\x28\\xb1\\x72\\x7f\\xac"
            b"\\x00\\x13\\x75\\xe7\\xbf\\x73\\x84\\x37\\x0f\\x48\\xef\\xd1\\x21\\x74\\x30\\x50",
        ],
    ],
)
def test_kdf(rounds, password, salt, expected):
    assert rounds
'''


def _constants(src):
    c = collections.Counter()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Constant):
            c[(type(n.value).__name__, n.value)] += 1
    return c


def _apply(src, code="list-item", line=None):
    """Пробует фиксер на КАЖДОЙ строке src (номера строк в юнит-тесте не
    привязаны к оригинальному файлу) и возвращает изменённый контент или None."""
    f = RuleBasedFixer()
    cand_lines = [line] if line else range(1, src.count("\n") + 2)
    for cand in cand_lines:
        es = f.try_fix({"code": code, "line": cand, "file": "x.py"}, src, "python")
        if es:
            out = es.apply({"x.py": src})
            return out["x.py"]
    return None


# ---------------------------------------------------------------------------
# Позитив: реальный bcrypt-фрагмент
# ---------------------------------------------------------------------------
def test_bcrypt_fragment_inner_lists_become_tuples():
    out = _apply(BCRYPT_FRAGMENT, code="arg-type")
    assert out is not None, "фиксер обязан сработать на bcrypt-фрагменте"
    tree = ast.parse(out)  # должен парситься
    # Найти argvalues-список (2-й позиционный арг parametrize) и убедиться,
    # что ВСЕ его элементы теперь Tuple, а не List.
    outer_lists = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.List) and n.elts
        and all(isinstance(e, (ast.List, ast.Tuple)) for e in n.elts)
    ]
    assert outer_lists, "внешний список рядов не найден"
    for L0 in outer_lists:
        assert all(isinstance(e, ast.Tuple) for e in L0.elts), (
            "все внутренние ряды должны стать кортежами"
        )


def test_bcrypt_fragment_literals_byte_identical():
    out = _apply(BCRYPT_FRAGMENT, code="list-item")
    assert out is not None
    assert _constants(BCRYPT_FRAGMENT) == _constants(out), (
        "ни один литерал не должен измениться (внутренний O.19-супергард)"
    )
    # И побайтово меняются ТОЛЬКО скобки [ ] → ( ).
    assert len(out) == len(BCRYPT_FRAGMENT)
    kinds = collections.Counter(
        (a, b) for a, b in zip(BCRYPT_FRAGMENT, out) if a != b
    )
    assert set(kinds) <= {("[", "("), ("]", ")")}, kinds


def test_bcrypt_fragment_comment_and_hexconcat_preserved():
    out = _apply(BCRYPT_FRAGMENT, code="arg-type")
    assert out is not None
    assert "# nul bytes in password and string" in out
    # implicit bytes concatenation через границу строк сохранена дословно
    assert 'b"\\x74\\x10\\xe4\\x4c' in out
    assert 'b"\\x00\\x13\\x75\\xe7' in out


def test_mypy_silent_after_repair():
    """Опциональный E2E: если mypy доступен — list-item должен исчезнуть.

    Контекст, эквивалентный pytest.parametrize (Sequence[tuple[...]]):
    list-ряд флагается, tuple-ряд — нет."""
    import shutil
    import subprocess
    import tempfile
    if shutil.which("mypy") is None and not Path(sys.executable).exists():
        return
    src = (
        "from typing import Sequence\n\n\n"
        "def take(rows: Sequence[tuple[int, bytes]]) -> None:\n"
        "    for r in rows:\n"
        "        print(r)\n\n\n"
        "take(\n"
        "    [\n"
        "        [\n"
        "            4,\n"
        '            b"password",\n'
        "        ],\n"
        "        [\n"
        "            8,\n"
        '            b"salt",\n'
        "        ],\n"
        "    ]\n"
        ")\n"
    )
    out = _apply(src, code="list-item")
    assert out is not None

    def mypy_errors(text):
        d = tempfile.mkdtemp()
        p = Path(d) / "m.py"
        p.write_text(text, encoding="utf-8")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "mypy", "--no-error-summary",
                 "--show-error-codes", str(p)],
                capture_output=True, text=True, timeout=90,
            )
        except Exception:
            return None
        return [ln for ln in r.stdout.splitlines() if "error:" in ln]

    before = mypy_errors(src)
    after = mypy_errors(out)
    if before is None or after is None:
        return  # mypy недоступен — E2E пропущен
    assert before, "исходный фрагмент обязан флагаться mypy (list-item)"
    assert not after, f"после ремонта mypy обязан молчать, а вернул: {after}"


# ---------------------------------------------------------------------------
# Негативы: строгий zero-collateral триггер
# ---------------------------------------------------------------------------
def test_negative_homogeneous_list_untouched():
    src = "data = [\n    [1, 2],\n    [3, 4],\n]\n"
    assert _apply(src) is None, "гомогенный список mypy не флагает — не трогаем"


def test_negative_list_with_variable_untouched():
    src = 'X = b"a"\ndata = [\n    [1, X],\n    [2, b"b"],\n]\n'
    assert _apply(src) is None, "список с именем (не константой) — не наш случай"


def test_negative_list_with_call_untouched():
    src = 'data = [\n    [1, bytes(3)],\n    [2, b"b"],\n]\n'
    assert _apply(src) is None


def test_negative_list_of_tuples_untouched():
    # pytils-подобное: ряды уже кортежи — конвертировать нечего.
    src = 'data = [\n    (1, b"a"),\n    (2, b"b"),\n]\n'
    assert _apply(src) is None


def test_negative_dict_and_set_untouched():
    assert _apply('data = {\n    "k": [1, b"a"],\n}\n') is None
    assert _apply('data = {1, b"a", 2}\n') is None


def test_negative_call_arg_single_list_untouched():
    src = 'foo([1, b"a", 2])\n'
    assert _apply(src, code="arg-type") is None


def test_negative_depth_greater_than_2_untouched():
    src = (
        "data = [\n"
        '    [[1, b"a"], [2, b"b"]],\n'
        '    [[3, b"c"], [4, b"d"]],\n'
        "]\n"
    )
    assert _apply(src) is None, "вложенность глубже 2 не трогаем"


def test_negative_single_element_inner_untouched():
    # [x] -> (x) изменило бы семантику (кортеж vs скаляр) — фиксер отступает.
    src = 'data = [\n    [b"a"],\n    [b"b"],\n]\n'
    assert _apply(src) is None


def test_negative_mixed_outer_elements_untouched():
    src = 'data = [\n    [1, b"a"],\n    99,\n]\n'
    assert _apply(src) is None, "не все элементы L0 — списки"


# ---------------------------------------------------------------------------
# Самопроверка: гард ловит порчу литерала
# ---------------------------------------------------------------------------
def test_self_check_multiset_detects_literal_change():
    a = _constants('x = [1, b"a"]')
    b = _constants('x = [1, "a"]')  # bytes → str: порча O.19-класса
    assert a != b, "мультимножество обязано различать b'a' и 'a'"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
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
    print(f"\nparametrize vector repair: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
