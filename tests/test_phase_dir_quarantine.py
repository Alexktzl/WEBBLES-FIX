"""
Dir-карантин fixture-корпусов (2026-07-07, Delgan/loguru).

Инцидент: tests/exceptions/source/ у loguru — корпус из 99 НАМЕРЕННО
сломанных файлов-фикстур (проект тестирует на них форматирование
исключений: PEP 750 t-строки, exception groups, специальные F821/B018/E999).
Прогон series5_loguru_20260707 сжёг 1871s и 160 LLM-вызовов при
cycles_run=0 и нуле доставленных фиксов: все per-file защиты (unanchorable
fast-fail порог 3 НА ФАЙЛ, FileAntiLoop) не агрегируют по каталогу, а
76 CRITICAL_SYNTAX-откатов размазались по ~1-2 на файл.

Инвариант: если FileAntiLoop заблокировал >= DIR_QUARANTINE_THRESHOLD (3)
файлов одного каталога — остальные ошибки этого каталога И его подкаталогов
уходят в NEEDS_REVIEW без LLM-генерации, ВКЛЮЧАЯ синтаксические (карантин
означает «файлы не поддались даже с ремонтом синтаксиса» — это сломанные
ДАННЫЕ, не код проекта). Сравнение каталогов — по границе сегмента
(source_extra не квараентинится карантином source). Корень проекта
не квараентинится никогда.

Запуск: python tests/test_phase_dir_quarantine.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from core.anti_loop import FileAntiLoop  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    # строгий assert — иначе pytest-коллекция «проходит» тест с FAIL внутри
    # (падение видел только __main__-запуск; расхождение поймано 2026-07-08)
    assert cond, f"{name}: {note}"


def _block_via_streak(fal: FileAntiLoop, file_path: str) -> bool:
    """Блокирует файл через stuck-стрик (3 одинаковые причины подряд).
    Возвращает True, если файл реально заблокирован."""
    blocked = False
    for _ in range(3):
        if not fal.record_attempt(file_path, 1, failure_reason="SAME_REASON: stable"):
            blocked = True
    return blocked


def _block_via_total_limit(fal: FileAntiLoop, file_path: str) -> bool:
    """Блокирует файл через общий лимит попыток (причины разные — стрик
    не копится)."""
    blocked = False
    for i in range(fal.max_total_attempts + 1):
        if not fal.record_attempt(file_path, 1, failure_reason=f"reason variant {i}"):
            blocked = True
    return blocked


# ---------------------------------------------------------------------------
# 1. 3 файла одного каталога, разные пути блокировки → каталог в карантине
# ---------------------------------------------------------------------------

def test_three_blocked_files_quarantine_dir():
    fal = FileAntiLoop()
    b1 = _block_via_streak(fal, "tests/exceptions/source/a.py")
    b2 = _block_via_streak(fal, "tests\\exceptions\\source\\b.py")  # backslash-путь
    b3 = _block_via_total_limit(fal, "tests/exceptions/source/c.py")
    check("all_three_actually_blocked", b1 and b2 and b3)
    check(
        "dir_quarantined_at_3",
        "tests/exceptions/source" in fal.quarantined_dirs(),
        f"dirs: {fal.quarantined_dirs()}",
    )


def test_blocks_in_sibling_subdirs_aggregate_to_ancestor():
    """2026-07-08 (series5_loguru2): корпус разбит на подкаталоги
    (backtrace/diagnose/modern) — блокировки в РАЗНЫХ подкаталогах обязаны
    суммироваться у общего предка. Первая версия писала только прямого
    родителя, порог не набирался нигде."""
    fal = FileAntiLoop()
    _block_via_streak(fal, "tests/exceptions/source/backtrace/a.py")
    _block_via_streak(fal, "tests/exceptions/source/diagnose/b.py")
    _block_via_streak(fal, "tests/exceptions/source/modern/c.py")
    q = fal.quarantined_dirs()
    check("ancestor_source_quarantined", "tests/exceptions/source" in q, f"dirs: {q}")
    check("ancestor_exceptions_quarantined", "tests/exceptions" in q, f"dirs: {q}")
    # подкаталоги с 1 файлом — сами по себе НЕ в карантине
    check("subdir_with_one_not_quarantined", "tests/exceptions/source/backtrace" not in q)


def test_toplevel_dir_needs_double_threshold():
    """Каталог верхнего уровня (корневой пакет проекта) карантинится только
    при УДВОЕННОМ пороге — 3 неудачных файла в разных подкаталогах пакета
    не должны глушить конверсию на остальном живом коде."""
    fal = FileAntiLoop()
    _block_via_streak(fal, "loguru/sub1/a.py")
    _block_via_streak(fal, "loguru/sub2/b.py")
    _block_via_streak(fal, "loguru/sub3/c.py")
    check("toplevel_not_quarantined_at_3", "loguru" not in fal.quarantined_dirs())
    _block_via_streak(fal, "loguru/sub4/d.py")
    _block_via_streak(fal, "loguru/sub5/e.py")
    _block_via_streak(fal, "loguru/sub6/f.py")
    check("toplevel_quarantined_at_6", "loguru" in fal.quarantined_dirs(),
          f"dirs: {fal.quarantined_dirs()}")


# ---------------------------------------------------------------------------
# 2. 2 файла — ниже порога
# ---------------------------------------------------------------------------

def test_two_blocked_files_not_quarantined():
    fal = FileAntiLoop()
    _block_via_streak(fal, "tests/exceptions/source/a.py")
    _block_via_streak(fal, "tests/exceptions/source/b.py")
    check("two_files_below_threshold", fal.quarantined_dirs() == set())


# ---------------------------------------------------------------------------
# 3. Файлы разных каталогов не суммируются
# ---------------------------------------------------------------------------

def test_blocked_files_in_different_dirs_do_not_aggregate():
    fal = FileAntiLoop()
    _block_via_streak(fal, "pkg/a/x.py")
    _block_via_streak(fal, "pkg/b/y.py")
    _block_via_streak(fal, "pkg/c/z.py")
    check("different_dirs_not_quarantined", fal.quarantined_dirs() == set())


# ---------------------------------------------------------------------------
# 4. Один файл, заблокированный дважды, считается один раз
# ---------------------------------------------------------------------------

def test_same_file_blocked_twice_counts_once():
    fal = FileAntiLoop()
    _block_via_streak(fal, "tests/src/a.py")
    # повторные блокировки того же файла
    fal.record_attempt("tests/src/a.py", 1, failure_reason="SAME_REASON: stable")
    _block_via_streak(fal, "tests/src/b.py")
    check("same_file_once", fal.quarantined_dirs() == set())


# ---------------------------------------------------------------------------
# 5. Корень проекта не квараентинится
# ---------------------------------------------------------------------------

def test_project_root_never_quarantined():
    fal = FileAntiLoop()
    for name in ("a.py", "b.py", "c.py", "d.py"):
        _block_via_streak(fal, name)  # parent == "."
    check("root_not_quarantined", fal.quarantined_dirs() == set())


# ---------------------------------------------------------------------------
# 6-7. Гейт в GeneratePatchStage: NR без LLM (в т.ч. E999), сосед проходит,
#      граница сегмента
# ---------------------------------------------------------------------------

class _GateCtx:
    """Минимальный fake-context для входа в execute() до гейта."""

    def __init__(self, error, metadata=None):
        self.selected_error = error
        self.metadata = dict(metadata or {})
        self.processed_errors = {}
        self.language = "python"
        self.project_path = Path(".")
        self.working_path = Path(".")
        self.history_states = []

    def update(self, metadata=None, processed_errors=None, **_kw):
        if metadata is not None:
            self.metadata = metadata
        if processed_errors is not None:
            self.processed_errors = processed_errors
        return self

    def record_processed_error(self, sig):
        return self

    def add_unfixable_error(self, e):
        return self

    def add_state_to_history(self, state):
        self.history_states.append(state)
        return self


def _mk_stage_with_quarantine(qdir_files):
    """GeneratePatchStage с моками и предзаполненным карантином."""
    from core.stages.generate_patch_stage import GeneratePatchStage
    stage = GeneratePatchStage.__new__(GeneratePatchStage)
    stage.llm_client = MagicMock()
    fal = FileAntiLoop()
    for f in qdir_files:
        _block_via_streak(fal, f)
    stage.file_anti_loop = fal
    return stage


def _run_gate(stage, error):
    """Воспроизводит логику гейта на минимальном контексте (юнит уровня
    гейта: полный execute() тянет весь пайплайн). Логика зеркалит
    generate_patch_stage.py — если гейт изменится, тест на quarantined_dirs
    выше поймает расхождение порога, а сквозная проверка остаётся за живым
    прогоном loguru."""
    from core.state_machine import State
    file_norm = str(error.get("file", "")).replace("\\", "/")
    from pathlib import PurePath
    err_dir = str(PurePath(file_norm).parent).replace("\\", "/")
    q_dirs = stage.file_anti_loop.quarantined_dirs()
    hit = next((q for q in q_dirs if err_dir == q or err_dir.startswith(q + "/")), None)
    return hit


def test_gate_quarantines_including_syntax_and_respects_boundaries():
    stage = _mk_stage_with_quarantine([
        "tests/exceptions/source/a.py",
        "tests/exceptions/source/b.py",
        "tests/exceptions/source/c.py",
    ])
    # порядок обхода set-а недетерминирован — гейт может вернуть любого
    # карантинного предка (source или exceptions), оба валидны
    _valid_hits = {"tests/exceptions/source", "tests/exceptions"}
    # E999 из карантинного каталога — квараентинится (отличие от соседних гейтов)
    hit_syntax = _run_gate(stage, {"file": "tests/exceptions/source/d.py", "code": "E999"})
    check("gate_hits_syntax_error_in_dir", hit_syntax in _valid_hits, f"hit: {hit_syntax}")
    # вложенный подкаталог — квараентинится (родитель в карантине)
    hit_nested = _run_gate(stage, {"file": "tests/exceptions/source/modern/e.py", "code": "F821"})
    check("gate_hits_nested_subdir", hit_nested in _valid_hits, f"hit: {hit_nested}")
    # другой каталог — проходит
    hit_other = _run_gate(stage, {"file": "loguru/_logger.py", "code": "F401"})
    check("gate_passes_other_dirs", hit_other is None)


def test_gate_respects_segment_boundary_without_quarantined_ancestor():
    """Чистая граница сегмента: 'pkg/source' в карантине (3 файла, depth 2),
    общий родитель 'pkg' — верхнего уровня (нужно 6, НЕ в карантине).
    'pkg/source_extra' совпадает с 'pkg/source' по префиксу ИМЕНИ, но не по
    сегменту — не должен квараентиниться. (Сосед source_extra при
    карантинном ПРЕДКЕ tests/exceptions — отдельный случай: он квараентинится
    легитимно, через родителя, а не через префикс — см. тест агрегации.)"""
    stage = _mk_stage_with_quarantine([
        "pkg/source/a.py",
        "pkg/source/b.py",
        "pkg/source/c.py",
    ])
    check("pkg_source_is_quarantined",
          "pkg/source" in stage.file_anti_loop.quarantined_dirs())
    check("pkg_toplevel_not_quarantined",
          "pkg" not in stage.file_anti_loop.quarantined_dirs())
    hit_prefix = _run_gate(stage, {"file": "pkg/source_extra/f.py", "code": "F821"})
    check("gate_respects_segment_boundary", hit_prefix is None,
          f"hit: {hit_prefix}")


# ---------------------------------------------------------------------------
# 8. Мягкий карантин по ширине anchor-fail (series5_loguru3, 2026-07-08)
# ---------------------------------------------------------------------------

def test_widespread_anchor_fails_quarantine_dir():
    """Размазанный корпус: 5 РАЗНЫХ файлов каталога с 1 форматным откатом
    каждый (per-file пороги не набраны!) → каталог в мягком карантине."""
    from core.stages.generate_patch_stage import GeneratePatchStage as GPS
    afc = {f"tests/exceptions/source/modern/f{i}.py": 1 for i in range(5)}
    dirs = GPS._dirs_with_widespread_anchor_fails(afc)
    check("widespread_nested_dir_hit", "tests/exceptions/source/modern" in dirs, f"dirs: {dirs}")
    check("widespread_ancestor_hit", "tests/exceptions/source" in dirs, f"dirs: {dirs}")


def test_widespread_four_files_below_threshold():
    from core.stages.generate_patch_stage import GeneratePatchStage as GPS
    afc = {f"tests/data/f{i}.py": 2 for i in range(4)}
    check("four_files_not_enough", GPS._dirs_with_widespread_anchor_fails(afc) == set())


def test_widespread_toplevel_needs_double():
    """Корневой пакет (depth 1): нужно 10 разных файлов, не 5 — не глушить
    конверсию на живом коде после локальной серии неудач."""
    from core.stages.generate_patch_stage import GeneratePatchStage as GPS
    afc5 = {f"loguru/f{i}.py": 1 for i in range(5)}
    check("toplevel_5_not_enough", "loguru" not in GPS._dirs_with_widespread_anchor_fails(afc5))
    afc10 = {f"loguru/f{i}.py": 1 for i in range(10)}
    check("toplevel_10_enough", "loguru" in GPS._dirs_with_widespread_anchor_fails(afc10))


def test_widespread_one_file_many_fails_is_not_breadth():
    """Один файл с 10 откатами — это per-file сигнал (unanchorable), не
    корпусный: ширина считается по числу РАЗНЫХ файлов."""
    from core.stages.generate_patch_stage import GeneratePatchStage as GPS
    afc = {"tests/data/single.py": 10}
    check("depth_not_breadth", GPS._dirs_with_widespread_anchor_fails(afc) == set())


if __name__ == "__main__":
    test_three_blocked_files_quarantine_dir()
    test_blocks_in_sibling_subdirs_aggregate_to_ancestor()
    test_toplevel_dir_needs_double_threshold()
    test_two_blocked_files_not_quarantined()
    test_blocked_files_in_different_dirs_do_not_aggregate()
    test_same_file_blocked_twice_counts_once()
    test_project_root_never_quarantined()
    test_gate_quarantines_including_syntax_and_respects_boundaries()
    test_gate_respects_segment_boundary_without_quarantined_ancestor()
    test_widespread_anchor_fails_quarantine_dir()
    test_widespread_four_files_below_threshold()
    test_widespread_toplevel_needs_double()
    test_widespread_one_file_many_fails_is_not_breadth()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"dir_quarantine: {passed}/{len(results)} passed")
    if failed:
        for name, note in failed:
            print(f"  FAIL: {name}" + (f" ({note})" if note else ""))
        sys.exit(1)
    sys.exit(0)
