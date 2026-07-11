"""
Control series 2026-06-21, находка #2 (зависание после финального отчёта):
LLMClient._hard_timeout_call использовал concurrent.futures.ThreadPoolExecutor
для единственного persistent воркера. ThreadPoolExecutor регистрирует свои
рабочие потоки в атексит-хуке `concurrent.futures.thread._python_exit`, который
БЕЗУСЛОВНО join'ит их при завершении интерпретатора — НЕЗАВИСИМО от daemon-флага.
Если фоновый вызов (например, зависший/очень медленный LLM HTTP-запрос или его
собственная retry-цепочка) переживает foreground hard_limit, процесс не может
завершиться, пока этот воркер не закончит работу — даже после того, как весь
видимый Python-код (включая финальный отчёт run_agent.py) уже отработал.

Фикс: _SingleDaemonWorker — raw threading.Thread(daemon=True) + queue вместо
ThreadPoolExecutor. Daemon-потоки НЕ регистрируются в этом atexit-хуке —
интерпретатор просто бросает их при выходе.

Тест ниже проверяет это РЕАЛЬНЫМ поведением процесса (не моками) — единственный
надёжный способ для такого класса бага: spawn'им subprocess, который создаёт
LLMClient, отправляет в фон функцию, которая "висит" дольше, чем готов ждать
process exit, и завершается. Если фикс работает — subprocess завершается быстро
(daemon-поток брошен). Если бы вернули старое поведение (ThreadPoolExecutor) —
subprocess завис бы на длительность фоновой задачи.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SUBPROCESS_SCRIPT = '''
import sys
sys.path.insert(0, r"{root}")
from fixers.llm_client import LLMClient

client = LLMClient(config={{"llm": {{"provider": "openai", "timeout": 0.05}}}})

def _hangs_much_longer_than_hard_limit():
    import time
    time.sleep(20)  # дольше, чем готов ждать тест ниже
    return "should never be observed"

result, err = client._hard_timeout_call(_hangs_much_longer_than_hard_limit)
assert result is None
assert err is not None and err.startswith("hard_timeout_")
print("FOREGROUND_DONE", flush=True)
# Скрипт завершается здесь — фоновая задача (20с sleep) ещё "выполняется"
# в воркере. Если это daemon-поток — процесс должен завершиться немедленно.
'''


def test_process_exits_promptly_despite_stuck_background_call():
    """Реальная проверка поведения процесса: subprocess должен завершиться
    за секунды, НЕ за ~20 секунд (длительность зависшей фоновой задачи)."""
    script = _SUBPROCESS_SCRIPT.format(root=str(ROOT).replace("\\", "\\\\"))
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    start = time.monotonic()
    try:
        stdout, stderr = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(
            f"Subprocess не завершился за 8с — старая регрессия "
            f"(ThreadPoolExecutor блокирует выход) вернулась. "
            f"stdout={stdout!r} stderr={stderr[-2000:]!r}"
        )
    elapsed = time.monotonic() - start

    assert "FOREGROUND_DONE" in stdout, f"stderr: {stderr[-2000:]}"
    assert elapsed < 8, f"Процесс завершился за {elapsed:.1f}с — подозрительно долго для daemon-потока"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
