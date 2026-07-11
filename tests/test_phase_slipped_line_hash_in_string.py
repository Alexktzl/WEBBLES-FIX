"""
Control series 16/20 (контрольная серия 12, 2026-06-22): PreCleanup ломал
синтаксис в десятках файлов на нескольких реальных проектах подряд
(angr/archinfo, ModOrganizer2/modorganizer-basic_games,
ClimateImpactLab/dodola) с ошибками вида "unterminated string literal"
и "'(' was never closed".

Корень: `_split_one_slipped_line`, паттерн 2 (`код#комментарий`), резал
строку по первому `#`, не проверяя `_string_ranges`/`_pos_inside_open_quote` —
в отличие от паттернов 1/3/5. Любой `#` внутри строкового литерала (URL
с якорем, текст "issue #123", докстринг с "# заголовком" и т.п.) считался
началом комментария и резался, вставляя реальный перевод строки внутрь
строкового литерала — "unterminated string literal". Если строка была
аргументом многострочного вызова, "комментарий" после разреза попадал на
закрывающую скобку вызова, давая "'(' was never closed".
"""
import ast

from fixers.rule_based_fixer import RuleBasedFixer


def _fixer():
    return RuleBasedFixer()


def test_hash_inside_string_not_split():
    line = 'comment = "# this is a fake comment inside a string"\n'
    assert _fixer()._split_one_slipped_line(line) is None


def test_hash_in_url_fragment_not_split():
    line = 'url = "http://example.com/page#anchor"\n'
    assert _fixer()._split_one_slipped_line(line) is None


def test_hash_in_issue_reference_not_split():
    line = 'msg = "see issue #123 for details"\n'
    assert _fixer()._split_one_slipped_line(line) is None


def test_real_inline_comment_still_split():
    line = "x=1#real comment\n"
    out = _fixer()._split_one_slipped_line(line)
    assert out == "x=1\n#real comment\n"


def test_real_comment_after_closed_string_still_split():
    line = 's = "closed"#real\n'
    out = _fixer()._split_one_slipped_line(line)
    assert out == 's = "closed"\n#real\n'


def test_whole_file_pass_preserves_validity_with_hash_in_string():
    src = (
        'def f():\n'
        '    comment = "# fake comment inside a string"\n'
        '    return comment\n'
    )
    fixer = _fixer()
    out = fixer._py_separate_slipped_lines(
        {"file": "t.py", "line": 0, "code": "SLIPPED_LINE", "message": ""}, src
    )
    # Раньше здесь возвращался EditSet, ломающий синтаксис; теперь — None
    # (нет изменений) либо валидный Python, если что-то всё же изменилось.
    if out is not None:
        ast.parse(out.edits[0].new)


def test_multiline_call_with_hash_in_string_argument_stays_valid():
    src = (
        "some_call(\n"
        '    "text with #anchor in url",\n'
        "    other_arg,\n"
        ")\n"
    )
    fixer = _fixer()
    out = fixer._py_separate_slipped_lines(
        {"file": "t.py", "line": 0, "code": "SLIPPED_LINE", "message": ""}, src
    )
    if out is not None:
        ast.parse(out.edits[0].new)
    else:
        ast.parse(src)


def test_pos_inside_open_quote_basic():
    fixer = _fixer()
    line = 'x = "abc#def" + y'
    hash_pos = line.index("#")
    assert fixer._pos_inside_open_quote(line, hash_pos) is True


def test_pos_inside_open_quote_after_close():
    fixer = _fixer()
    line = 'x = "abc"#def'
    hash_pos = line.index("#")
    assert fixer._pos_inside_open_quote(line, hash_pos) is False
