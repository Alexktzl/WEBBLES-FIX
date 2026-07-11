"""
O.19 — анти-порча данных при mypy value/data-фиксах (2026-07-02).

Живой прогон pyca/bcrypt (детерминированно, два прогона): LLM «чинила» mypy
list-item/arg-type/assignment в tests/test_bcrypt.py, ПОРТЯ тестовые векторы
вместо исправления типов (b"salt"→"salt", 4→"4", hex-bytes→plaintext-str).
Каждый патч честно снимал mypy-ошибку и не порождал новой flake8-ошибки:
target_recheck подтверждал «исправлено», net_delta_check не запускался
(счётчик не рос), O.14/O.17 (символы) и O.18 (Any/cast) не при чём —
per-patch решатель принимал порчу, финальный аудит откатывал постфактум.

Инвариант, отличающий порчу от честного фикса (0 collateral по всему
архиву learning_cases: dbus-fast arg-type ×16, pycfmodel, pytils,
more-itertools, anyio, httpx — все СОХРАНЯЮТ данные-литералы дословно):
суммарное число bytes-/числовых литералов в файле НЕ должно уменьшаться.

Запуск: python tests/test_phase_mypy_data_mangling_guard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from analysis.data_literal_guard import (  # noqa: E402
    check_data_literal_mangling,
    DATA_MYPY_CODES,
)
from core.pipeline_context import PipelineContext  # noqa: E402
from core.pipeline_stage import PipelineStage  # noqa: E402
from core.stages.decide_stage import DecideStage  # noqa: E402

# --- Реальные фрагменты из runtime/learning_cases.jsonl (bcrypt, 2026-07-02) ---

_VECTORS_BEFORE = (
    "_VECTORS = [\n"
    "    [\n"
    "        4,\n"
    '        b"password",\n'
    '        b"salt",\n'
    '        b"\\x60\\x51\\xbe\\x18\\xc2\\xf4\\xf8\\x2c",\n'
    "    ],\n"
    "]\n"
)


# ==================== (а) bcrypt-сценарий → НЕ ACCEPT ====================

def test_bytes_to_str_demotion_flagged():
    """b"salt" → "salt" (L1042/L1047/L1051): bytes-литерал понижен до str."""
    after = _VECTORS_BEFORE.replace('b"salt"', '"salt"')
    out = check_data_literal_mangling(_VECTORS_BEFORE, after, "list-item")
    assert out["ok"] is False, out
    assert "bytes_literal_demoted" in out["markers"], out


def test_int_to_str_demotion_flagged():
    """4 → "4" (L995/L1041/L1050): числовой литерал понижен до str."""
    after = _VECTORS_BEFORE.replace("        4,\n", '        "4",\n')
    out = check_data_literal_mangling(_VECTORS_BEFORE, after, "list-item")
    assert out["ok"] is False, out
    assert "numeric_literal_demoted" in out["markers"], out


def test_hex_bytes_to_plaintext_str_flagged():
    """b"\\x60..." → "6051be18..." (L1046/L1052): содержимое bytes переписано в str."""
    after = _VECTORS_BEFORE.replace(
        'b"\\x60\\x51\\xbe\\x18\\xc2\\xf4\\xf8\\x2c"', '"6051be18c2f4f82c"'
    )
    out = check_data_literal_mangling(_VECTORS_BEFORE, after, "list-item")
    assert out["ok"] is False, out
    assert "bytes_literal_demoted" in out["markers"], out


def test_arg_type_code_also_in_scope():
    """arg-type (L1044-класс) — тот же вектор порчи данных, входит в scope."""
    after = _VECTORS_BEFORE.replace('b"password"', '"password"')
    out = check_data_literal_mangling(_VECTORS_BEFORE, after, "arg-type")
    assert out["ok"] is False, out


# ============ (б) легитимные mypy-фиксы → путь к ACCEPT не тронут ============

def test_add_annotation_not_flagged():
    """pytils L663: `H = [(0,0)]` → `H: list[...] = [(0,0)]` — литералы целы."""
    before = "H = [(0, 0)]\nfor i in range(3):\n    pass\n"
    after = "H: list[tuple[int, int]] = [(0, 0)]\nfor i in range(3):\n    pass\n"
    out = check_data_literal_mangling(before, after, "list-item")
    assert out["ok"] is True, out


def test_add_cast_isinstance_not_flagged():
    """httpx/anyio-класс: добавлен assert isinstance — данные не тронуты."""
    before = "def f(s):\n    return s.read()\n"
    after = "def f(s):\n    assert isinstance(s, bytes)\n    return s.read()\n"
    out = check_data_literal_mangling(before, after, "arg-type")
    assert out["ok"] is True, out


def test_dict_unpack_to_kwargs_not_flagged():
    """pycfmodel L630: Statement(**{"Effect": "Allow"}) → Statement(Effect="Allow").

    Строковые КЛЮЧИ становятся kwargs (str-константы исчезают), но bytes/
    числовые литералы не трогаются — guard не срабатывает (str не считаем)."""
    before = 'x = Statement(**{"Effect": "Allow", "Action": "ec2:Run*"})\n'
    after = 'x = Statement(Effect="Allow", Action="ec2:Run*")\n'
    out = check_data_literal_mangling(before, after, "arg-type")
    assert out["ok"] is True, out


def test_wrap_call_preserving_bytes_not_flagged():
    """dbus-fast L671: обёртка io.BytesIO(b"") в BufferedRWPair — b"" сохранён."""
    before = 'x = Unmarshaller(io.BytesIO(b"")).unmarshall()\n'
    after = 'x = Unmarshaller(io.BufferedRWPair(io.BytesIO(b""), io.BytesIO())).unmarshall()\n'
    out = check_data_literal_mangling(before, after, "arg-type")
    assert out["ok"] is True, out


# ======================== (в) краевые случаи ========================

def test_non_mypy_code_out_of_scope():
    """Порча литерала под НЕ-mypy кодом (E501) — guard не в scope (молчит)."""
    after = _VECTORS_BEFORE.replace('b"salt"', '"salt"')
    out = check_data_literal_mangling(_VECTORS_BEFORE, after, "E501")
    assert out["ok"] is True, out
    assert out["reason"] == "code_not_in_scope", out


def test_empty_patch_not_flagged():
    """Идентичный before/after — нечего понижать."""
    out = check_data_literal_mangling(_VECTORS_BEFORE, _VECTORS_BEFORE, "list-item")
    assert out["ok"] is True, out


def test_literal_demotion_in_production_code_also_flagged():
    """Порча литерала в НЕ-тестовом коде тоже ловится: guard намеренно
    path-agnostic — понижение bytes→str в data-mypy фиксе подозрительно везде,
    а по архиву 0 collateral (production assignment-фиксы dbus/anyio не
    понижали литералов)."""
    before = "DEFAULT = b'\\x00\\x01\\x02'\n"
    after = "DEFAULT = '\\x00\\x01\\x02'\n"
    out = check_data_literal_mangling(before, after, "assignment")
    assert out["ok"] is False, out
    assert "bytes_literal_demoted" in out["markers"], out


def test_non_string_input_safe():
    out = check_data_literal_mangling(None, "x = 1\n", "list-item")
    assert out["ok"] is True, out


def test_unparseable_after_bytes_fallback():
    """AST-парс падает → regex-фолбэк всё равно ловит потерю bytes-литерала."""
    before = 'row = [b"a", b"b", 4  # broken\n'  # не парсится
    after = 'row = [b"a", "b", 4  # broken\n'
    out = check_data_literal_mangling(before, after, "list-item")
    assert out["ok"] is False, out
    assert "bytes_literal_demoted" in out["markers"], out


# ============ Интеграция: флаг из ValidateStage уводит ACCEPT → NEEDS_REVIEW ============

_ERR = {"file": "tests/test_bcrypt.py", "line": 359, "code": "list-item",
        "message": 'List item 0 has incompatible type "int"; expected "str"'}


def _accept_context(work: Path, extra_meta: dict) -> PipelineContext:
    """Контекст, попадающий в ветку target_fixed_reviewer_ok_count_unchanged
    (before==after, target_recheck present=False, review ok, conf>=0.85)."""
    (work / "tests").mkdir(exist_ok=True)
    (work / "tests" / "test_bcrypt.py").write_text(_VECTORS_BEFORE, encoding="utf-8")
    meta = {
        "patch_source": "structured_llm_blocking",
        "confidence": 1.0,
        "review": {"verdict": "ok"},
        "patch_snapshots": [],
        "_pre_patch_content": {"tests/test_bcrypt.py": _VECTORS_BEFORE},
    }
    meta.update(extra_meta)
    return PipelineContext(
        project_path=work, language="python", working_path=work,
        selected_error=dict(_ERR),
        current_errors=(),
        validation_results=MappingProxyType({
            "error_count_before": 59, "error_count_after": 59,
            "current_errors_before": [],
            "target_recheck": {"performed": True, "tool": "mypy", "present": False},
        }),
        metadata=meta,
    )


def test_wiring_mangling_flag_downgrades_to_needs_review():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        # NR-retry уже израсходован → терминальное решение (без второго шанса)
        sig = PipelineStage._static_signature(dict(_ERR))
        ctx = _accept_context(work, {
            "data_literal_mangling": {
                "file": "tests/test_bcrypt.py",
                "markers": ["numeric_literal_demoted"],
            },
            f"_nr_retry_{sig}": 1,
        })
        out = DecideStage(quality_evaluator=None, analyzer=None).execute(ctx)
        assert out.metadata.get("last_decision") == "NEEDS_REVIEW", (
            f"порча данных обязана уводить в NEEDS_REVIEW, а не ACCEPT: "
            f"{out.metadata.get('last_decision')}"
        )


def test_wiring_no_flag_still_accepts():
    """Тот же контекст БЕЗ флага порчи — путь к ACCEPT не изменился (легит-фикс)."""
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ctx = _accept_context(work, {})
        out = DecideStage(quality_evaluator=None, analyzer=None).execute(ctx)
        assert out.metadata.get("last_decision") == "ACCEPT", (
            f"легитимный mypy-фикс без порчи данных обязан приниматься: "
            f"{out.metadata.get('last_decision')}"
        )


def test_scope_set_sanity():
    assert {"list-item", "arg-type", "assignment"} <= DATA_MYPY_CODES
    assert "attr-defined" not in DATA_MYPY_CODES  # покрывается O.18/isinstance-путём


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
    print(f"\nData-mangling guard: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
