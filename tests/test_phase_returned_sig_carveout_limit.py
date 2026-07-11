"""
2026-07-07 (разбор класса UNCHANGED_FILE на httpx/bcrypt): карваут
«принятая сигнатура вернулась в current_errors → не дубль, разбаниваем»
в outer-петле PipelineEngine был безусловным. Паттерн «ACCEPT корёжит
структуру, ошибка мигает» гонял одну сигнатуру по кругу:
- bcrypt tests/test_bcrypt.py::370::arg-type — ACCEPT×2 (каждый патч
  искажал тестовые векторы) → осцилляция поймана только 3-м заходом →
  финальный аудит откатил файл целиком (UNCHANGED_FILE, работа выброшена);
- httpx _models.py::999::attr-defined (серия 02.07) — ACCEPT×3 подряд.

Инвариант: карваут одноразовый per-signature. Первый возврат — легитимный
доразбор (сосед задел файл). Повторный возврат той же принятой сигнатуры —
осцилляция: сигнатура НЕ разбанивается и уходит в _ban_oscillating_signatures
без нового LLM-захода (ложный бан лучше ложного ACCEPT, §4 CLAUDE.md).

Запуск: python tests/test_phase_returned_sig_carveout_limit.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from core.pipeline_engine import PipelineEngine  # noqa: E402

SIG = "tests/test_bcrypt.py::370::arg-type"
OTHER = "httpx/_urls.py::10::F401"

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))


def test_first_return_is_allowed():
    """Первый возврат принятой сигнатуры — легитимный доразбор: разбан."""
    counts = {}
    allowed, dupes = PipelineEngine._filter_returned_sigs({SIG}, {SIG}, counts)
    check("first_return_allowed", allowed == {SIG})
    check("first_return_no_dupes", dupes == set())
    check("first_return_counted", counts.get(SIG) == 1)


def test_second_return_goes_to_oscillation_ban():
    """Повторный возврат той же сигнатуры — карваут исчерпан: в real_dupes."""
    counts = {SIG: 1}  # первый возврат уже был
    allowed, dupes = PipelineEngine._filter_returned_sigs({SIG}, {SIG}, counts)
    check("second_return_not_allowed", allowed == set())
    check("second_return_is_dupe", dupes == {SIG})
    check("second_return_counted", counts.get(SIG) == 2)


def test_not_returned_sig_is_plain_dupe():
    """Сигнатура повторно принята, но НЕ вернулась в current_errors —
    классическая осцилляция (два патча взаимно отменились): в real_dupes,
    счётчик возвратов не трогается."""
    counts = {}
    allowed, dupes = PipelineEngine._filter_returned_sigs({SIG}, set(), counts)
    check("non_returned_not_allowed", allowed == set())
    check("non_returned_is_dupe", dupes == {SIG})
    check("non_returned_not_counted", SIG not in counts)


def test_mixed_signatures_split_independently():
    """Счётчик per-signature: исчерпание карваута одной сигнатуры не
    задевает первый возврат другой."""
    counts = {SIG: 1}
    repeated = {SIG, OTHER}
    current = {SIG, OTHER}
    allowed, dupes = PipelineEngine._filter_returned_sigs(repeated, current, counts)
    check("mixed_other_allowed", allowed == {OTHER})
    check("mixed_sig_is_dupe", dupes == {SIG})


def test_counts_persist_across_calls():
    """Счётчик живёт у вызывающего на весь прогон: две последовательные
    outer-итерации дают разбан → бан для одной и той же сигнатуры."""
    counts = {}
    allowed1, _ = PipelineEngine._filter_returned_sigs({SIG}, {SIG}, counts)
    allowed2, dupes2 = PipelineEngine._filter_returned_sigs({SIG}, {SIG}, counts)
    check("persist_first_allowed", allowed1 == {SIG})
    check("persist_second_banned", allowed2 == set() and dupes2 == {SIG})


if __name__ == "__main__":
    test_first_return_is_allowed()
    test_second_return_goes_to_oscillation_ban()
    test_not_returned_sig_is_plain_dupe()
    test_mixed_signatures_split_independently()
    test_counts_persist_across_calls()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"returned_sig_carveout_limit: {passed}/{len(results)} passed")
    if failed:
        for name, note in failed:
            print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
        sys.exit(1)
    sys.exit(0)
