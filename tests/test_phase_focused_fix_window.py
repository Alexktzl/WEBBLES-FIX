"""
Focused-fix режим (2026-06-22) — anti-hallucination guard для structured_llm.

Найдено живьём (мини-серия 5 проектов после empty_response диагностики):
запрос на ОДНУ конкретную ошибку (E501, конкретная строка) — модель
возвращает EditSet с 6-9 правками, "доисправляя" все похожие длинные
строки в файле, и придумывает несуществующий "# noqa: E501" в anchor.match
для каждой такой лишней правки (подтверждено сверкой с реальным файлом
geopython/pygeofilter — noqa там не было). Из-за файловой атомарности
to_unified_diff (одна несовпавшая правка топит ВЕСЬ файл) даже ВАЛИДНЫЙ
фикс целевой строки терялся.

Фикс: EditSet.filter_to_target_window отбрасывает edits вне окна вокруг
target_line ДО сборки диффа — целевая правка сохраняется, остальные
дисквалифицируются явно, не топя весь файл.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline_context import PipelineContext
from core.state_machine import State
from core.stages.generate_patch_stage import GeneratePatchStage
from fixers.patch_engine import PatchEngine
from fixers.structured_edit import Anchor, Edit, EditSet
from memory.learning import MemoryLearning


# ---------------------------------------------------------------------------
# EditSet.filter_to_target_window — модульные тесты
# ---------------------------------------------------------------------------

def test_filter_keeps_edit_on_target_line():
    es = EditSet(edits=[Edit(file="a.py", anchor=Anchor(line=10, match="x = 1"),
                               kind="replace", new="x = 2\n")])
    filtered, dropped = es.filter_to_target_window("a.py", target_line=10)
    assert len(filtered.edits) == 1
    assert dropped == []


def test_filter_keeps_edit_within_window_radius():
    es = EditSet(edits=[Edit(file="a.py", anchor=Anchor(line=12, match="y = 2"),
                               kind="replace", new="y = 3\n")])
    filtered, dropped = es.filter_to_target_window("a.py", target_line=10, window_radius=3)
    assert len(filtered.edits) == 1
    assert dropped == []


def test_filter_drops_edit_outside_window():
    es = EditSet(edits=[Edit(file="a.py", anchor=Anchor(line=50, match="z = 3"),
                               kind="replace", new="z = 4\n")])
    filtered, dropped = es.filter_to_target_window("a.py", target_line=10, window_radius=3)
    assert len(filtered.edits) == 0
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "outside_target_window"


def test_filter_drops_e501_noqa_hallucination_on_other_line():
    """E501-специфика: anchor.match с "noqa" НЕ на target_line отбрасывается,
    даже если формально внутри окна."""
    es = EditSet(edits=[Edit(
        file="a.py",
        anchor=Anchor(line=11, match="    long_line = 1  # noqa: E501"),
        kind="replace", new="    long_line = 1\n",
    )])
    filtered, dropped = es.filter_to_target_window(
        "a.py", target_line=10, error_code="E501", window_radius=3,
    )
    assert len(filtered.edits) == 0
    assert dropped[0]["reason"] == "noqa_outside_target"


def test_filter_keeps_e501_noqa_edit_exactly_on_target_line():
    """noqa-эдит РАЗРЕШЁН, если это и есть запрошенная строка (модель может
    легитимно добавить noqa к самой целевой ошибке)."""
    es = EditSet(edits=[Edit(
        file="a.py",
        anchor=Anchor(line=10, match="    long_line = 1"),
        kind="replace", new="    long_line = 1  # noqa: E501\n",
    )])
    filtered, dropped = es.filter_to_target_window(
        "a.py", target_line=10, error_code="E501", window_radius=3,
    )
    assert len(filtered.edits) == 1
    assert dropped == []


def test_filter_does_not_touch_other_files():
    """Кросс-файловые edits (манифесты и т.п.) вне области этой защиты."""
    es = EditSet(edits=[
        Edit(file="a.py", anchor=Anchor(line=10, match="x"), kind="replace", new="y\n"),
        Edit(file="Cargo.toml", anchor=Anchor(line=99, match="version"), kind="replace", new="version2\n"),
    ])
    filtered, dropped = es.filter_to_target_window("a.py", target_line=10, window_radius=3)
    assert len(filtered.edits) == 2
    assert dropped == []


def test_filter_matches_by_basename_when_path_separator_differs():
    es = EditSet(edits=[Edit(
        file="pygeofilter\\backends\\solr\\evaluate.py",
        anchor=Anchor(line=10, match="x"), kind="replace", new="y\n",
    )])
    filtered, dropped = es.filter_to_target_window(
        "pygeofilter/backends/solr/evaluate.py", target_line=10, window_radius=3,
    )
    assert len(filtered.edits) == 1
    assert dropped == []


# ---------------------------------------------------------------------------
# Воспроизведение реального кейса: geopython/pygeofilter
# ---------------------------------------------------------------------------

def test_geopython_pygeofilter_case_keeps_valid_target_drops_hallucinations(tmp_path):
    """Точное воспроизведение живой находки: target — E501 на строке 198
    pygeofilter/backends/solr/evaluate.py. Модель вернула ОДНУ валидную
    правку для целевой строки + 8 лишних правок на других строках того же
    файла, каждая с придуманным "# noqa: E501" (которого в реальном файле
    нет). Система должна: применить ТОЛЬКО целевую правку, не терять её
    из-за лишних edits."""
    target_file = "evaluate.py"
    real_content = (
        "line1\n" * 197
        + "        for Geo3D fields). Non-spatial queries are combined in bool.must as before.\n"
        + "line199\n" * 50
    )
    (tmp_path / target_file).write_text(real_content, encoding="utf-8")

    target_line_text = (
        "        for Geo3D fields). Non-spatial queries are combined in bool.must as before."
    )
    bogus_edit_set = EditSet(
        intent="Fix all E501 line length violations",
        confidence=1.0,
        edits=[
            # Целевая правка — РЕАЛЬНО присутствует в файле.
            Edit(file=target_file, anchor=Anchor(line=198, match=target_line_text),
                 kind="replace",
                 new="        for Geo3D fields). Non-spatial queries are combined\n"
                     "        in bool.must as before.\n"),
            # 8 "лишних" правок на других строках — с придуманным noqa,
            # которого в реальном файле НЕТ (см. docstring модуля).
            Edit(file=target_file, anchor=Anchor(line=81, match='    """Docstring.  # noqa: E501'),
                 kind="replace", new="    pass\n"),
            Edit(file=target_file, anchor=Anchor(line=114, match='    # comment.  # noqa: E501'),
                 kind="replace", new="    pass\n"),
            Edit(file=target_file, anchor=Anchor(line=196, match='    filters live...  # noqa: E501'),
                 kind="replace", new="    pass\n"),
            Edit(file=target_file, anchor=Anchor(line=197, match='    not be merged...  # noqa: E501'),
                 kind="replace", new="    pass\n"),
            Edit(file=target_file, anchor=Anchor(line=437, match='    # Rectangular...  # noqa: E501'),
                 kind="replace", new="    pass\n"),
            Edit(file=target_file, anchor=Anchor(line=438, match='    # WKT polygons...  # noqa: E501'),
                 kind="replace", new="    pass\n"),
            Edit(file=target_file, anchor=Anchor(line=439, match='    # poles...  # noqa: E501'),
                 kind="replace", new="    pass\n"),
            Edit(file=target_file, anchor=Anchor(line=440, match='    # or mishandle...  # noqa: E501'),
                 kind="replace", new="    pass\n"),
        ],
    )

    llm = MagicMock()
    llm.generate_structured_fix.return_value = bogus_edit_set
    llm.last_failure_category = None
    llm.last_raw_response = '{"intent":"...","edits":[...]}'
    llm.generate_fix.return_value = ""

    stage = GeneratePatchStage(llm_client=llm, memory=MemoryLearning(), patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({
        "file": target_file, "line": 198, "code": "E501",
        "message": "line too long (89 > 79 characters)", "error_class": "UNKNOWN",
    })

    result = stage.execute(ctx)

    # Целевая правка должна СОХРАНИТЬСЯ и попасть в patch — не REJECT,
    # не infra-failure, не потеря всего файла из-за лишних edits.
    assert result.generated_patch is not None
    assert "Geo3D fields" in result.generated_patch
    assert len(result.rejected_patches) == 0
    assert result.metadata.get("llm_infra_failure_count", 0) == 0

    structured_edit = result.metadata.get("structured_edit") or {}
    assert len(structured_edit.get("edits", [])) == 1
    dropped = result.metadata.get("focused_fix_dropped_edits", [])
    assert len(dropped) == 8
    assert all(d["reason"] == "noqa_outside_target" for d in dropped)


def test_only_hallucinated_edits_no_target_match_is_honest_reject(tmp_path):
    """Если ПОСЛЕ фильтрации не осталось ни одной правки (целевая строка
    тоже не совпала, или все edits были на других строках) — честный
    REJECT (focused_fix_rejected), не потеря молча и не падение пайплайна."""
    (tmp_path / "a.py").write_text("x = 1\n" * 20, encoding="utf-8")
    bogus_edit_set = EditSet(
        intent="fix", confidence=0.9,
        edits=[
            Edit(file="a.py", anchor=Anchor(line=50, match="far away line"),
                 kind="replace", new="y\n"),
        ],
    )
    llm = MagicMock()
    llm.generate_structured_fix.return_value = bogus_edit_set
    llm.last_failure_category = None
    llm.last_raw_response = "{}"
    llm.generate_fix.return_value = ""

    stage = GeneratePatchStage(llm_client=llm, memory=MemoryLearning(), patch_engine=PatchEngine())
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "E501",
                                   "message": "m", "error_class": "UNKNOWN"})

    result = stage.execute(ctx)

    assert result.current_state == State.NEXT_ERROR
    assert len(result.rejected_patches) == 1
    assert result.rejected_patches[0]["reason"] == "empty_response_focused_fix_rejected"
    assert result.metadata.get("llm_infra_failure_count", 0) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
