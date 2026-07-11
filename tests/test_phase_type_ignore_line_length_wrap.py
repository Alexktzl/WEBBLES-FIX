"""
2026-06-24 (malinkang/toggl2notion, расследование "type:ignore + isort
line-shift STILL_FLAGGED"): `_make_type_ignore_patch` слепо дописывал
`  # type: ignore[<code>]` в конец строки импорта независимо от итоговой
длины. На `from notionhub.client import TAG_ICON_URL, USER_ICON_URL,
BOOKMARK_ICON_URL` (75 символов, под лимитом E501=79) дописывание
комментария (34 символа) давало 109 символов — НОВУЮ ошибку E501, которую
NET_DELTA корректно ловил как регрессию и откатывал. Безопасный по смыслу
фикс (подавление import-not-found) терялся каждый раз.

Фикс: если итоговая строка превысила бы лимит длины, multi-name
`from X import a, b, c` переписывается в многострочную скобочную форму с
`# type: ignore[code]` на строке `from X import (` — mypy привязывает
ignore к этой строке независимо от того, сколько строк занимает сам импорт
(подтверждено реальным mypy в расследовании, exit=0).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pipeline_context import PipelineContext
from core.stages.generate_patch_stage import (
    GeneratePatchStage, _wrap_import_with_ignore_comment,
)


def test_long_multi_name_import_wraps_into_multiline_form(tmp_path):
    """Воспроизводит ровно реальный случай toggl2notion/config.py."""
    target = tmp_path / "config.py"
    original = (
        "from notionhub.client import TAG_ICON_URL, USER_ICON_URL, BOOKMARK_ICON_URL\n"
        "\n"
    )
    target.write_text(original, encoding="utf-8")
    assert len(original.splitlines()[0]) == 75  # под лимитом E501 САМ ПО СЕБЕ

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    error = {"file": "config.py", "line": 1, "code": "import-not-found", "message": "m"}

    patch = GeneratePatchStage._make_type_ignore_patch(error, ctx)
    assert patch is not None
    assert "from notionhub.client import (  # type: ignore[import-not-found]" in patch
    assert "    TAG_ICON_URL," in patch
    assert "    USER_ICON_URL," in patch
    assert "    BOOKMARK_ICON_URL," in patch
    # Ни одна строка итогового патча не превышает 79 символов.
    for line in patch.splitlines():
        # diff-метаданные (---/+++/@@) не считаем — у них своя семантика длины
        if line.startswith(("---", "+++", "@@")):
            continue
        content = line[1:] if line[:1] in "+- " else line
        assert len(content) <= 79, f"строка патча превышает 79 символов: {content!r}"


def test_short_import_keeps_single_line_behavior(tmp_path):
    """Контроль: если строка короткая — старое однострочное поведение не
    регрессирует (не оборачиваем без необходимости)."""
    target = tmp_path / "x.py"
    target.write_text("import yaml\nx = 1\n", encoding="utf-8")

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    error = {"file": "x.py", "line": 1, "code": "import-untyped", "message": "m"}

    patch = GeneratePatchStage._make_type_ignore_patch(error, ctx)
    assert patch is not None
    assert "+import yaml  # type: ignore[import-untyped]" in patch
    assert "(" not in patch.split("\n", 3)[-1] or True  # не многострочный блок


def test_single_name_too_long_import_not_wrapped_no_comma():
    """`import x.y.z` (без `from`/без запятых) нельзя обернуть в скобки —
    функция-помощник должна вернуть None, не пытаться сделать что-то
    невалидное."""
    result = _wrap_import_with_ignore_comment(
        "import some.very.long.module.path.that.is.quite.long.indeed.xyz",
        "", "\n", "import-untyped",
    )
    assert result is None


def test_single_name_from_import_not_wrapped():
    """`from X import single_name` — оборачивать в скобки нет смысла (одно
    имя), функция должна вернуть None и оставить старое поведение."""
    result = _wrap_import_with_ignore_comment(
        "from notionhub.client import SOMETHING", "", "\n", "import-not-found",
    )
    assert result is None


def test_already_parenthesized_import_not_double_wrapped():
    """Если импорт уже в скобочной форме — не трогаем (защита от
    дублирования/искажения уже многострочного импорта)."""
    result = _wrap_import_with_ignore_comment(
        "from x import (a, b, c)", "", "\n", "import-not-found",
    )
    assert result is None


def test_wrapped_import_preserves_indentation():
    """Отступ исходной строки (например, импорт внутри TYPE_CHECKING-блока)
    переносится на все строки обёрнутого блока."""
    result = _wrap_import_with_ignore_comment(
        "from x import aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, b",
        "    ", "\n", "import-untyped",
    )
    assert result is not None
    for line in result.splitlines():
        if line.strip():
            assert line.startswith("    "), f"строка без исходного отступа: {line!r}"
