"""
Регрессионный тест для Bug B: duplicate-accept loop.

Проверяет два инварианта исправления в _start_global_loop():
  1. accepted_patches считает только ДЕЛЬТУ каждого цикла,
     а не весь context.accepted_patches повторно.
  2. Если все принятые в цикле ошибки уже были приняты в предыдущих
     циклах — outer loop прерывается (не дублирует патчи бесконечно).
"""
from __future__ import annotations

import types
import pytest


# ---------------------------------------------------------------------------
# Вспомогательные классы-заглушки для теста без полного запуска pipeline
# ---------------------------------------------------------------------------

def _make_patch_info(file: str, line: int, code: str) -> dict:
    return {"error": {"file": file, "line": line, "code": code, "message": f"{code} at {file}:{line}"}}


def _sig(file: str, line: int, code: str) -> str:
    return f"{file}::{line}::{code}"


# ---------------------------------------------------------------------------
# Юнит-тест: логика delta-подсчёта и break при дубликатах
# ---------------------------------------------------------------------------

def _simulate_outer_loop(cycles: list[list[dict]]) -> tuple[list[dict], int, bool]:
    """
    Имитирует _start_global_loop: принимает список «циклов» (каждый цикл —
    список patch_info, добавляемых в этом цикле) и возвращает:
      (local_accepted_patches, iterations_done, duplicate_break_fired)
    """
    # Переменные состояния — те же, что в pipeline_engine.py после B-fix
    accepted_patches: list = []
    _outer_accepted_sigs: set = set()
    _ctx_accepted_before: int = 0

    # Симулируем накопительный context.accepted_patches
    ctx_accepted: list = []

    duplicate_break_fired = False
    iterations_done = 0

    for cycle_new_patches in cycles:
        # _single_run() добавляет новые патчи в context
        ctx_accepted.extend(cycle_new_patches)

        # --- копия кода из _start_global_loop (B-fix, порядок: сначала check, потом add) ---
        _new_accepted = ctx_accepted[_ctx_accepted_before:]
        _ctx_accepted_before = len(ctx_accepted)

        _cycle_sigs: set = set()
        for patch_info in _new_accepted:
            err = patch_info.get("error") or {}
            s = f"{err.get('file','')}::{err.get('line',0)}::{err.get('code','')}"
            _cycle_sigs.add(s)

        if _cycle_sigs and _cycle_sigs.issubset(_outer_accepted_sigs):
            duplicate_break_fired = True
            break  # break ДО добавления в local
        _outer_accepted_sigs |= _cycle_sigs

        for patch_info in _new_accepted:
            accepted_patches.append({
                "file": patch_info.get("error", {}).get("file", "unknown"),
                "message": patch_info.get("error", {}).get("message", "")[:100],
                "code": patch_info.get("error", {}).get("code", ""),
            })
        # --- конец копии ---

        iterations_done += 1

    return accepted_patches, iterations_done, duplicate_break_fired


# ---------------------------------------------------------------------------
# Тест 1: нет дубликатов — все циклы проходят, счётчик = суммарное кол-во
# ---------------------------------------------------------------------------

def test_no_duplicates_all_cycles_run():
    """Уникальные ошибки в каждом цикле — loop не прерывается досрочно."""
    error_a = _make_patch_info("a.py", 10, "E001")
    error_b = _make_patch_info("b.py", 20, "E002")
    error_c = _make_patch_info("c.py", 30, "E003")

    local, iters, dup_break = _simulate_outer_loop([
        [error_a],
        [error_b],
        [error_c],
    ])

    assert iters == 3, f"Должно быть 3 итерации, получено {iters}"
    assert not dup_break, "Дубликатов нет — досрочного прерывания не должно быть"
    assert len(local) == 3, f"local accepted должен содержать 3 записи, получено {len(local)}"


# ---------------------------------------------------------------------------
# Тест 2: один и тот же ACCEPT на 2-й цикл — loop прерывается
# ---------------------------------------------------------------------------

def test_duplicate_accept_breaks_loop():
    """Одна и та же ошибка принята в цикле 1 и снова в цикле 2 — loop прерывается."""
    error_a = _make_patch_info("django_app.py", 17, "hardcoded_secret")

    local, iters, dup_break = _simulate_outer_loop([
        [error_a],   # цикл 1: первый раз принят
        [error_a],   # цикл 2: тот же error — должен сработать break
        [error_a],   # цикл 3: не должен выполниться
    ])

    assert dup_break, "Должен сработать duplicate-break на 2-м цикле"
    # iterations_done — количество ПОЛНЫХ итераций (не считая цикл где break)
    assert iters == 1, f"Должна завершиться только 1 полная итерация, break на 2-й, получено {iters}"
    # break срабатывает ДО добавления в local → local содержит только патчи цикла 1
    assert len(local) == 1, f"local должен содержать 1 запись (только цикл 1, break до добавления), получено {len(local)}"


# ---------------------------------------------------------------------------
# Тест 3: дубликат + новая ошибка в одном цикле — loop НЕ прерывается
# ---------------------------------------------------------------------------

def test_duplicate_plus_new_does_not_break():
    """Цикл принял дубликат И новую ошибку — продолжаем, есть прогресс."""
    error_a = _make_patch_info("django_app.py", 17, "hardcoded_secret")
    error_b = _make_patch_info("tests.py", 56, "dangerous_eval")

    local, iters, dup_break = _simulate_outer_loop([
        [error_a],          # цикл 1: error_a принят
        [error_a, error_b], # цикл 2: дубликат + новая error_b
        [error_b],          # цикл 3: error_b дубликат → break
    ])

    # Цикл 3 — все дубликаты → break. Итераций выполнено: 2 (цикл 1 и 2 полные, цикл 3 прерван)
    assert dup_break, "Должен сработать break на 3-м цикле (error_b дубликат)"
    assert iters == 2, f"2 полных итерации (цикл 3 прерван), получено {iters}"
    # local: цикл 1 даёт [a], цикл 2 даёт [a, b] — итого 3 записи (break на цикле 3 до добавления)
    assert len(local) == 3, f"local должен содержать 3 записи (цикл1=1, цикл2=2), получено {len(local)}"


# ---------------------------------------------------------------------------
# Тест 4: нет принятых патчей в цикле — loop не прерывается по дублям
# ---------------------------------------------------------------------------

def test_empty_cycle_does_not_trigger_dup_break():
    """Цикл без новых ACCEPT не вызывает break (пустой _cycle_sigs)."""
    error_a = _make_patch_info("main.py", 5, "W291")

    local, iters, dup_break = _simulate_outer_loop([
        [error_a],  # цикл 1
        [],         # цикл 2: пусто — break по дублям НЕ должен срабатывать
        [],         # цикл 3: пусто
    ])

    assert not dup_break, "Пустой цикл не должен вызывать duplicate-break"
    assert iters == 3, f"Все 3 итерации должны выполниться, получено {iters}"
    assert len(local) == 1, f"Только 1 уникальный ACCEPT, получено {len(local)}"


# ---------------------------------------------------------------------------
# Тест 5: double-counting regression — local list = только дельта, не весь ctx
# ---------------------------------------------------------------------------

def test_no_double_counting_in_local_list():
    """
    До B-fix: каждый цикл добавлял ВЕСЬ context.accepted_patches в local list,
    что приводило к двойному счёту.
    После fix: добавляется только дельта.
    """
    error_a = _make_patch_info("file_a.py", 1, "CODE_A")
    error_b = _make_patch_info("file_b.py", 2, "CODE_B")
    error_c = _make_patch_info("file_c.py", 3, "CODE_C")

    local, iters, dup_break = _simulate_outer_loop([
        [error_a],
        [error_b],
        [error_c],
    ])

    # После fix: local = [a, b, c] (3 записи)
    # До fix (баг): local = [a, a+b, a+b+c] = 6 записей
    assert len(local) == 3, (
        f"local должен содержать ровно 3 записи (по 1 на цикл), получено {len(local)} "
        "(возможен double-counting если > 3)"
    )
    assert not dup_break
