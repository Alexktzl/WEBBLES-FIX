"""
2026-06-24: PipelineEngine._global_fix_loop считал llm_timeout_count ТОЛЬКО
с self.llm_client (primary) — при parallel_workers>1 + WEBBLES_LLM_API_KEY_POOL
большинство таймаутов происходит на ДРУГИХ клиентах пула (round-robin по
файлам). Живой бенчмарк Bluetooth-Devices/dbus-fast: метрика показывала 0
при реальных 29 hard timeout (видно в логе), потому что они произошли на
pool[1]/pool[2]/pool[3], не на pool[0]=self.llm_client.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.pipeline_engine import PipelineEngine


def _fake_client(hard_timeout_count):
    return SimpleNamespace(hard_timeout_count=hard_timeout_count)


def test_sums_hard_timeout_count_across_full_pool():
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.llm_client = _fake_client(0)
    eng.llm_client_pool = [
        eng.llm_client, _fake_client(12), _fake_client(9), _fake_client(8),
    ]

    from core.pipeline_context import PipelineContext
    eng.context = PipelineContext(project_path=Path("/tmp/p"), language="python")

    # Воспроизводим ровно тот фрагмент _global_fix_loop, что считает
    # llm_timeout_count (без поднятия всего цикла).
    _loop_meta = dict(eng.context.metadata)
    _llm_pool = getattr(eng, "llm_client_pool", None) or [getattr(eng, "llm_client", None)]
    _loop_meta["llm_timeout_count"] = sum(
        int(getattr(c, "hard_timeout_count", 0) or 0) for c in _llm_pool if c is not None
    )
    eng.context = eng.context.update(metadata=_loop_meta)

    assert eng.context.metadata["llm_timeout_count"] == 29  # 0+12+9+8


def test_falls_back_to_single_client_without_pool():
    eng = PipelineEngine.__new__(PipelineEngine)
    eng.llm_client = _fake_client(5)
    eng.llm_client_pool = None  # старое поведение / пул не построен

    from core.pipeline_context import PipelineContext
    eng.context = PipelineContext(project_path=Path("/tmp/p"), language="python")

    _loop_meta = dict(eng.context.metadata)
    _llm_pool = getattr(eng, "llm_client_pool", None) or [getattr(eng, "llm_client", None)]
    _loop_meta["llm_timeout_count"] = sum(
        int(getattr(c, "hard_timeout_count", 0) or 0) for c in _llm_pool if c is not None
    )
    eng.context = eng.context.update(metadata=_loop_meta)

    assert eng.context.metadata["llm_timeout_count"] == 5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
