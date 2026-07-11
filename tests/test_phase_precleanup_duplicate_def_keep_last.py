"""
2026-06-23 (control series, RussellDash332/pytils): `_py_remove_duplicate_lines`
(fixers/rule_based_fixer.py, PreCleanup pass 2 — несмежные дубли def/class)
хранило ПЕРВОЕ определение и удаляло остальные — backwards относительно
семантики Python. При повторном `def foo(...)` на модульном уровне имя
перепривязывается, реально исполняется ПОСЛЕДНЕЕ определение; предыдущие —
уже мёртвый код (shadowed).

На pytils/fast_fourier_transform.py файл легитимно содержал ДВА разных
`def div` (вторая — расширенная версия с остатком, не дубль-баг, а
намеренное переопределение). Старая логика удаляла ВТОРУЮ (реально
вызываемую) версию вместе с вложенной `sub`, меняя фактическое поведение
кода для любого вызова `div(...)` извне (control series verify_accepts.py
обнаружил unsafe_accept: missing_defs=["sub"]).
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from fixers.rule_based_fixer import RuleBasedFixer


def _run(content: str):
    fixer = RuleBasedFixer()
    return fixer._py_remove_duplicate_lines(
        {"file": "t.py", "line": 0, "code": "DUPLICATE_LINES", "message": ""}, content,
    )


def test_redefined_function_keeps_last_not_first():
    """Минимальная репродукция pytils-кейса: вторая def — то, что
    реально вызывается в Python, и должна остаться."""
    src = (
        "def div(u, v):\n"
        "    return 'old_simple_version'\n"
        "\n"
        "def div(P, Q):\n"
        "    def sub(u, v):\n"
        "        return 'helper'\n"
        "    return sub(P, Q), 'remainder'\n"
    )
    out = _run(src)
    assert out is not None
    new_content = out.edits[0].new
    ast.parse(new_content)
    assert "def sub(" in new_content
    assert "'remainder'" in new_content
    assert "'old_simple_version'" not in new_content


def test_true_accidental_duplicate_still_removed():
    """Истинный дубль (две БУКВАЛЬНО одинаковые функции — типичный
    copy-paste баг) всё ещё чистится, просто сохраняется последняя
    (для идентичных тел это не имеет значения)."""
    src = (
        "def helper():\n"
        "    return 42\n"
        "\n"
        "def helper():\n"
        "    return 42\n"
    )
    out = _run(src)
    assert out is not None
    new_content = out.edits[0].new
    assert new_content.count("def helper():") == 1


def test_no_duplicates_returns_none():
    src = "def a():\n    return 1\n\ndef b():\n    return 2\n"
    out = _run(src)
    assert out is None


def test_triple_redefinition_keeps_only_last():
    src = (
        "def f():\n"
        "    return 'v1'\n"
        "\n"
        "def f():\n"
        "    return 'v2'\n"
        "\n"
        "def f():\n"
        "    return 'v3'\n"
    )
    out = _run(src)
    assert out is not None
    new_content = out.edits[0].new
    assert new_content.count("def f():") == 1
    assert "'v3'" in new_content
    assert "'v1'" not in new_content
    assert "'v2'" not in new_content


def test_real_pytils_fast_fourier_transform_file():
    """Полная репродукция реального файла из контрольной серии."""
    # Синтетическая, но структурно идентичная репродукция настоящего
    # fast_fourier_transform.py (две def div, вторая с вложенной sub).
    src = (
        "def div(u, v):\n"
        "    b = 0\n"
        "    return mult(u, v)\n"
        "\n"
        "# General division, possibly with remainders\n"
        "def div(P, Q):\n"
        "    def sub(u, v):\n"
        "        z = [*u]\n"
        "        return z\n"
        "    return mult(P, Q), sub(P, Q)\n"
    )
    out = _run(src)
    assert out is not None
    new_content = out.edits[0].new
    ast.parse(new_content)
    assert "def sub(" in new_content
    assert "General division" in new_content


def test_overload_decorated_functions_are_never_deduplicated():
    """2026-06-25 (control series, agronholm/anyio): @overload-декорированные
    функции ЛЕГИТИМНО делят одно имя (единственный способ выразить
    несколько сигнатур в Python typing) — старая логика считала их
    "дублями" и удаляла все, кроме последней (реальной реализации),
    теряя 4 из 5 перегрузок connect_tcp/started/cache/... systemically
    по всему проекту. Все @overload-вхождения должны остаться целыми."""
    src = (
        "from typing import overload\n"
        "\n"
        "@overload\n"
        "def connect_tcp(host: str, *, tls: bool) -> int: ...\n"
        "\n"
        "@overload\n"
        "def connect_tcp(host: str) -> str: ...\n"
        "\n"
        "def connect_tcp(host, *, tls=False):\n"
        "    return host\n"
    )
    out = _run(src)
    assert out is None, (
        "правило не должно трогать файл вообще — все три connect_tcp "
        "являются легитимными @overload-перегрузками/реализацией, не дублями"
    )


def test_overload_functions_coexist_with_real_duplicate_in_same_file():
    """@overload-перегрузки одного имени НЕ должны мешать дедупликации
    НАСТОЯЩЕГО дубля другого имени в том же файле."""
    src = (
        "from typing import overload\n"
        "\n"
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "@overload\n"
        "def f(x: str) -> str: ...\n"
        "def f(x):\n"
        "    return x\n"
        "\n"
        "def g():\n"
        "    return 'old'\n"
        "\n"
        "def g():\n"
        "    return 'new'\n"
    )
    out = _run(src)
    assert out is not None
    new_content = out.edits[0].new
    ast.parse(new_content)
    # Все 3 'f' (2 overload + 1 реализация) должны остаться
    assert new_content.count("def f(") == 3
    # 'g' должен дедуплицироваться — остаётся только последняя ('new')
    assert new_content.count("def g():") == 1
    assert "'new'" in new_content
    assert "'old'" not in new_content
