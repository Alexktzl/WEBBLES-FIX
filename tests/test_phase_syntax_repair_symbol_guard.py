"""
Regress H1 (PROJECT_AUDIT_REPORT 2026-07-01): SyntaxRepairStage писала
результат ремонта (включая LLM-реконструкцию региона) на диск и регистрировала
ACCEPT без единой символьной проверки — а before с E999 не парсится, поэтому
per-patch guard и финальный аудит были слепы (C2). LLM-«ремонт», стирающий
половину функций, проходил все рубежи.

Фикс: перед записью — check_symbol_regression (с regex-фолбэком C2);
при потере имён файл не пишется и ACCEPT не регистрируется.

Запуск: python tests/test_phase_syntax_repair_symbol_guard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")

from core.pipeline_context import PipelineContext  # noqa: E402
from core.stages.syntax_repair_stage import SyntaxRepairStage  # noqa: E402


_BROKEN = (
    "import os\n"
    "\n"
    "def keep_me(x):\n"
    "    return x + 1\n"
    "\n"
    "def lose_me(y):\n"
    "    return y * 2\n"
    "\n"
    "def broken(:\n"  # SyntaxError
)


class _LLMDeletesFunctions:
    """LLM, чей «ремонт» возвращает регион без lose_me (типичный сценарий H1)."""

    def complete_text(self, system: str, user: str) -> str:
        return (
            "import os\n"
            "\n"
            "def keep_me(x):\n"
            "    return x + 1\n"
            "\n"
            "def broken(z):\n"
            "    return z\n"
        )


class _LLMKeepsFunctions:
    """LLM, чинящий только сломанную строку (все имена сохранены)."""

    def complete_text(self, system: str, user: str) -> str:
        return (
            "import os\n"
            "\n"
            "def keep_me(x):\n"
            "    return x + 1\n"
            "\n"
            "def lose_me(y):\n"
            "    return y * 2\n"
            "\n"
            "def broken(z):\n"
            "    return z\n"
        )


def _ctx(work: Path) -> PipelineContext:
    return PipelineContext(
        project_path=work, language="python",
        config={"pipeline": {"python_version_detection": False}},
        working_path=work,
    )


def _run(work: Path, llm) -> tuple:
    stage = SyntaxRepairStage()
    ctx = _ctx(work)
    return stage.execute(ctx, work, llm_client=llm)


# --- 1. Ремонт, стирающий функцию, отклоняется: файл не изменён, ACCEPT нет ---
def test_llm_repair_losing_def_rejected():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "app.py").write_text(_BROKEN, encoding="utf-8")

        ctx, accepted, n_failed = _run(work, _LLMDeletesFunctions())

        assert (work / "app.py").read_text(encoding="utf-8") == _BROKEN, (
            "H1: ремонт с потерей lose_me не должен быть записан на диск"
        )
        assert accepted == [], f"ACCEPT не должен регистрироваться: {accepted}"
        assert not ctx.accepted_patches, "context.accepted_patches должен остаться пуст"
        assert n_failed == 1


# --- 2. Ремонт, сохранивший все имена, по-прежнему применяется ---
def test_llm_repair_keeping_names_applied():
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        (work / "app.py").write_text(_BROKEN, encoding="utf-8")

        ctx, accepted, n_failed = _run(work, _LLMKeepsFunctions())

        new_content = (work / "app.py").read_text(encoding="utf-8")
        assert "def lose_me" in new_content and "def broken(z):" in new_content, (
            f"валидный ремонт должен примениться, получили:\n{new_content}"
        )
        assert len(accepted) == 1, accepted
        assert n_failed == 0


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
    print(f"\nSyntaxRepair symbol guard: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
