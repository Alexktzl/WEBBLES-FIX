"""
2026-06-25 (направление "rule-based расширение", learning_cases.jsonl):
F541 (f-string без placeholder'ов) — 19 попыток через LLM в накопленной
истории, 100% успеха, чисто механический фикс (снять лишний `f`-префикс).
ruff --fix умеет это сам и корректно ПРОПУСКАЕТ f-строки С placeholder'ами
(проверено вручную) — добавлен в _SAFE_CODES вместо того, чтобы тратить
LLM-вызов на код с единственным детерминированным решением.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from core.stages.ruff_autofix_stage import _SAFE_CODES


def test_f541_in_safe_codes():
    codes = [c for c in _SAFE_CODES.split(",") if c]
    assert "F541" in codes


def test_ruff_fix_removes_f_prefix_without_placeholders(tmp_path):
    """Поведенческая проверка реального ruff --fix (не мок) — подтверждает
    механику, на которую опирается решение включить F541 в safe-набор."""
    target = tmp_path / "t.py"
    target.write_text(
        'x = f"hello world"\n'
        'y = f"with {1} placeholder"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["ruff", "check", "--fix", f"--select={_SAFE_CODES}", str(target)],
        capture_output=True, text=True, timeout=30,
    )
    new_content = target.read_text(encoding="utf-8")

    assert 'x = "hello world"' in new_content, (
        "f-строка без placeholder'ов должна потерять лишний f-префикс"
    )
    assert 'y = f"with {1} placeholder"' in new_content, (
        "f-строка С placeholder'ом не должна быть тронута"
    )
