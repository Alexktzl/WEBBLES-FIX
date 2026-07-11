"""
LLM circuit breaker на подряд идущие transport_error/empty (2026-07-10).

ИНЦИДЕНТ (C++ серия, tinyxml2): DeepSeek возвращал empty/рвал соединение
(transport_error) — 272 «неответа» из 398 вызовов за 30 мин, 0 доставки. Старый
breaker (is_unresponsive) считал ТОЛЬКО hard-timeout'ы (llm_timeout_count=0 при
transport_error), поэтому не срабатывал: прогон жёг весь project_timeout, а
результат маскировался под «project_timeout / 0 delivery» — не отличить от
реального бага движка.

ИНВАРИАНТ: подряд идущие transport_error/empty/timeout (LLM не ответил вовсе)
после порога → is_unresponsive() → вызывающий код fail-fast'ит, прогон
завершается быстро с честной причиной llm_unavailable. bad_format/parser_fail
(LLM ЖИВ, но контент кривой) НЕ считаются недоступностью и СБРАСЫВАЮТ счётчик.

Запуск: python tests/test_phase_llm_no_response_breaker.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from fixers.llm_client import LLMClient  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


def _client(threshold=6):
    return LLMClient(config={"llm": {"timeout": 5, "max_retries": 1,
                                     "no_response_threshold": threshold}})


def test_transport_errors_trip_breaker():
    c = _client(threshold=6)
    check("healthy_at_start", not c.is_unresponsive())
    for i in range(5):
        c._note_response_outcome(None, "transport_error")
    check("not_tripped_below_threshold", not c.is_unresponsive(), f"count={c.consecutive_no_response}")
    c._note_response_outcome(None, "transport_error")  # 6-й
    check("tripped_at_threshold", c.is_unresponsive(), f"count={c.consecutive_no_response}")


def test_empty_and_timeout_also_count():
    c = _client(threshold=3)
    c._note_response_outcome(None, "empty")
    c._note_response_outcome(None, "timeout")
    c._note_response_outcome(None, "transport_error")
    check("mixed_no_response_trips", c.is_unresponsive())


def test_real_response_resets():
    c = _client(threshold=3)
    c._note_response_outcome(None, "transport_error")
    c._note_response_outcome(None, "transport_error")
    check("count_built_up", c.consecutive_no_response == 2)
    c._note_response_outcome("some raw json", None)  # LLM ответил
    check("reset_on_real_response", c.consecutive_no_response == 0)
    check("not_unresponsive_after_reset", not c.is_unresponsive())


def test_bad_format_does_not_count():
    """LLM ЖИВ (ответил), но контент не собрался — это НЕ недоступность."""
    c = _client(threshold=3)
    for _ in range(10):
        c._note_response_outcome(None, "bad_format")
        c._note_response_outcome(None, "parser_fail")
    check("bad_format_no_increment", c.consecutive_no_response == 0)
    check("bad_format_not_unresponsive", not c.is_unresponsive())


def test_hard_timeout_path_still_trips():
    """Старый путь (consecutive_hard_timeouts) не сломан."""
    c = _client(threshold=99)  # no_response порог высок — проверяем именно timeout-путь
    c.consecutive_hard_timeouts = c.llm_unresponsive_threshold
    check("hard_timeout_still_trips", c.is_unresponsive())


if __name__ == "__main__":
    test_transport_errors_trip_breaker()
    test_empty_and_timeout_also_count()
    test_real_response_resets()
    test_bad_format_does_not_count()
    test_hard_timeout_path_still_trips()
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"llm_no_response_breaker: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
