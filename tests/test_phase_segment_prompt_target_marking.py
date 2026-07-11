"""
build_segment_prompt() сваливал ВСЕ ошибки сегмента в один список без
разметки — на плотных участках кода (10+ соседних flake8-находок) модель
путала целевую ошибку с соседними и чинила более простую/заметную вместо
нужной (whitespace вместо security-находки), патч уходил как нерелевантный.

Обнаружено при контрольном прогоне pygenda (control series 13) — в реальном
промпте, отправленном LLM, "🔴 ERROR TO FIX" чётко называл security-ошибку
на строке 518, но "ERRORS RELEVANT TO THIS SEGMENT" содержал 13 других
несвязанных находок без разметки, и модель сгенерировала патч для E231
(пробел после запятой) на той же строке — технически "релевантно" по
файлу/строке, но не решает реальную задачу.

Фикс: build_segment_prompt(target_error=...) — целевая ошибка помечается
отдельно ("👉 TARGET ERROR"), остальные явно отмечены как "context only —
do NOT fix these".
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fixers.file_segmenter import FileSegmenter


def _segment():
    return {"start_line": 498, "end_line": 538, "text": "    def f():\n        pass\n"}


def _errors():
    return [
        {"file": "pygenda_gui.py", "line": 498, "code": "E501", "message": "line too long"},
        {"file": "pygenda_gui.py", "line": 505, "code": "E231", "message": "missing whitespace after ','"},
        {"file": "pygenda_gui.py", "line": 518, "code": "E231", "message": "missing whitespace after ','"},
        {
            "file": "pygenda_gui.py", "line": 518,
            "code": "python.lang.security.audit.non-literal-import.non-literal-import",
            "message": "Untrusted user input in importlib.import_module()",
        },
        {"file": "pygenda_gui.py", "line": 532, "code": "E501", "message": "line too long"},
    ]


def test_target_error_marked_distinctly():
    seg = FileSegmenter()
    target = {
        "file": "pygenda_gui.py", "line": 518,
        "code": "python.lang.security.audit.non-literal-import.non-literal-import",
        "message": "Untrusted user input in importlib.import_module()",
    }
    prompt = seg.build_segment_prompt(_segment(), "", _errors(), target_error=target)

    assert "TARGET ERROR" in prompt
    assert "non-literal-import" in prompt.split("TARGET ERROR")[1].split("\n\n")[0]
    assert "do NOT fix these" in prompt


def test_other_errors_present_but_marked_as_context_only():
    seg = FileSegmenter()
    target = {"file": "pygenda_gui.py", "line": 518, "code": "E231", "message": "x"}
    prompt = seg.build_segment_prompt(_segment(), "", _errors(), target_error=target)

    # все 5 ошибок всё ещё видны модели (контекст важен для не-разрушения кода),
    # но только ОДНА помечена как цель.
    assert prompt.count("Line 498") == 1
    assert prompt.count("Line 532") == 1
    other_block = prompt.split("Other errors nearby")[1]
    assert "Line 498" in other_block
    assert "Line 532" in other_block
    # целевая (line 518, E231) НЕ дублируется в "other" блоке
    target_block = prompt.split("TARGET ERROR")[1].split("Other errors nearby")[0]
    assert "E231" in target_block


def test_no_target_error_falls_back_to_old_flat_list():
    """Обратная совместимость: без target_error (старые вызовы) — старое
    поведение, без разметки, без падений."""
    seg = FileSegmenter()
    prompt = seg.build_segment_prompt(_segment(), "", _errors())
    assert "TARGET ERROR" not in prompt
    assert "Line 498" in prompt and "Line 518" in prompt


def test_target_error_not_found_in_segment_no_crash():
    """target_error указывает на строку ВНЕ этого сегмента (например,
    GENERATING_PATCH вызвал build_segment_prompt для соседнего сегмента
    по ошибке) — не должно ломаться, просто нет TARGET-секции."""
    seg = FileSegmenter()
    target = {"file": "pygenda_gui.py", "line": 9999, "code": "E999", "message": "x"}
    prompt = seg.build_segment_prompt(_segment(), "", _errors(), target_error=target)
    assert "TARGET ERROR" not in prompt
    assert "Line 498" in prompt


def test_empty_errors_list_unaffected():
    seg = FileSegmenter()
    prompt = seg.build_segment_prompt(_segment(), "", [], target_error=None)
    assert "No specific errors in this segment." in prompt


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
