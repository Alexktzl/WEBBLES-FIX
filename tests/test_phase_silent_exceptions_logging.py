"""
Tech debt audit 2026-06-21, находка #5 (P2): ~15+ мест `except Exception: pass`
без логирования (decide_stage.py, apply_patch_stage.py, analyze_stage.py
_use_*_enabled()). Поведение НЕ менялось (try/except остаётся осознанным
fail-safe) — добавлено только logger.debug/warning в каждый except, чтобы
будущая отладка "почему флаг конфига не сработал" не начиналась с нуля.

Тест ниже подтверждает на представительном случае (_use_mypy_enabled), что
логирование реально срабатывает при сбое чтения конфига, а возвращаемое
default-значение не изменилось.
"""

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.stages.analyze_stage import AnalyzeStage


class _BrokenConfig:
    """config, у которого .get() бросает исключение — имитирует сбой чтения."""
    def get(self, *_a, **_kw):
        raise RuntimeError("симулированный сбой чтения конфига")


class _FakeContext:
    def __init__(self, config):
        self.config = config


def test_use_mypy_enabled_logs_on_config_failure(caplog):
    ctx = _FakeContext(config=_BrokenConfig())
    with caplog.at_level(logging.DEBUG, logger="core.stages.analyze_stage"):
        result = AnalyzeStage._use_mypy_enabled(ctx)
    assert result is False  # default не изменился
    assert any("_use_mypy_enabled" in r.message for r in caplog.records)


def test_use_mypy_enabled_normal_path_unaffected():
    ctx = _FakeContext(config={"pipeline": {"use_mypy": True}})
    assert AnalyzeStage._use_mypy_enabled(ctx) is True
    ctx2 = _FakeContext(config={"pipeline": {}})
    assert AnalyzeStage._use_mypy_enabled(ctx2) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
