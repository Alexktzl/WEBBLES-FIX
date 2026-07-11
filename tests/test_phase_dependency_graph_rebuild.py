"""
2026-06-25: DependencyGraph строился ОДИН раз в PipelineEngine.__init__ от
project_path (снимок ДО первого патча) и никогда не обновлялся —
RootCauseStage.cascade_score со временем считался по всё более устаревшей
структуре импортов, хотя реальные правки идут в working_path (отдельная
песочница, не project_path). DependencyGraph.rebuild() позволяет
пересобрать граф с нуля (опционально — от нового пути); PipelineEngine
теперь вызывает его раз в макро-цикл (_global_fix_loop), используя tmp_dir
(working_path) — НЕ на каждый отдельный pick ошибки (RootCauseStage
вызывается много раз за цикл, это было бы расточительно).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.dependency_graph import DependencyGraph


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_rebuild_picks_up_new_import_added_after_initial_build(tmp_path):
    """Граф строится при old-состоянии (a.py не импортирует b.py), потом
    файл меняется (добавляется импорт) — rebuild() должен это увидеть,
    старый граф этого не знал бы."""
    _write(tmp_path / "a.py", "x = 1\n")
    _write(tmp_path / "b.py", "y = 2\n")

    graph = DependencyGraph(tmp_path, "python")
    assert graph.get_dependents("b.py") == []

    # Имитируем правку: a.py теперь импортирует b.py.
    _write(tmp_path / "a.py", "import b\nx = 1\n")

    assert graph.get_dependents("b.py") == [], (
        "без rebuild() граф должен оставаться старым (доказывает, что "
        "тест действительно проверяет ОБНОВЛЕНИЕ, а не случайную свежесть)"
    )

    graph.rebuild()

    assert graph.get_dependents("b.py") == ["a.py"], (
        "после rebuild() граф должен увидеть новый импорт"
    )


def test_rebuild_with_new_path_switches_root(tmp_path):
    """rebuild(new_path) должен полностью переключить project_path —
    имитирует реальный сценарий: граф изначально строился от project_path
    (снимок до патчей), потом пересобирается от working_path (песочница,
    где реально применяются патчи)."""
    original = tmp_path / "original"
    working = tmp_path / "working"
    _write(original / "a.py", "x = 1\n")
    _write(working / "a.py", "import b\nx = 1\n")
    _write(working / "b.py", "y = 2\n")

    graph = DependencyGraph(original, "python")
    assert graph.get_dependents("b.py") == []

    graph.rebuild(working)

    assert graph.project_path == working
    assert graph.get_dependents("b.py") == ["a.py"]


def test_rebuild_clears_stale_edges_not_just_appends():
    """Если зависимость была удалена (a.py больше не импортирует b.py),
    rebuild() должен ОЧИСТИТЬ старое ребро, не накапливать дубли."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        _write(tmp_path / "a.py", "import b\nx = 1\n")
        _write(tmp_path / "b.py", "y = 2\n")

        graph = DependencyGraph(tmp_path, "python")
        assert graph.get_dependents("b.py") == ["a.py"]

        _write(tmp_path / "a.py", "x = 1\n")  # импорт убран
        graph.rebuild()

        assert graph.get_dependents("b.py") == []


def test_pipeline_engine_global_fix_loop_calls_dep_graph_rebuild_per_cycle(tmp_path, monkeypatch):
    """Интеграционная проверка: _global_fix_loop реально вызывает
    self.dep_graph.rebuild(tmp_dir) — не просто метод существует
    изолированно, а реально подключён в продакшен-путь."""
    import inspect
    from core.pipeline_engine import PipelineEngine

    src = inspect.getsource(PipelineEngine._global_fix_loop)
    assert "self.dep_graph.rebuild(tmp_dir)" in src, (
        "_global_fix_loop должен вызывать self.dep_graph.rebuild(tmp_dir) "
        "раз в макро-цикл — иначе граф остаётся статичным снимком от старта"
    )
