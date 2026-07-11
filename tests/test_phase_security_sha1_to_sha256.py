"""
2026-06-25 (направление "rule-based расширение", learning_cases.jsonl):
python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1 —
6 попыток через LLM, 100% успеха, всегда одна и та же простая
детерминированная замена hashlib.sha1(...) -> hashlib.sha256(...) с
сохранением аргументов вызова (включая usedforsecurity=False).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from fixers.rule_based_fixer import RuleBasedFixer

_CODE = "python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1"


def _run(line: str):
    fixer = RuleBasedFixer()
    return fixer.try_fix(
        {"file": "t.py", "line": 1, "code": _CODE, "message": "x"}, line, "python",
    )


def test_dispatch_registered_for_full_semgrep_check_id():
    """error['code'] для semgrep — ПОЛНЫЙ dotted check_id (см.
    core/stages/analyze_stage.py: finding.get('check_id')) — dispatch
    должен матчиться буквально на эту строку."""
    fixer = RuleBasedFixer()
    handler = fixer._dispatch(_CODE, "python")
    assert handler is not None
    assert handler == fixer._py_sha1_to_sha256


def test_real_case_with_usedforsecurity_kwarg_preserved():
    """Реальный кейс из requests/auth.py: usedforsecurity=False должен
    остаться нетронутым — меняется только имя алгоритма."""
    line = "                return hashlib.sha1(x, usedforsecurity=False).hexdigest()\n"
    out = _run(line)
    assert out is not None
    assert out.edits[0].new == "                return hashlib.sha256(x, usedforsecurity=False).hexdigest()\n"


def test_real_case_simple_url_hash():
    """Реальный кейс из mister-companion/ra_image_cache.py."""
    line = '    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()\n'
    out = _run(line)
    assert out is not None
    assert out.edits[0].new == '    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()\n'


def test_no_sha1_call_returns_none():
    out = _run("x = hashlib.sha256(data).hexdigest()\n")
    assert out is None


def test_sha1_as_substring_of_other_name_not_matched():
    """\\b границы слова — не должно матчить, например, my_sha1_helper()."""
    out = _run("x = my_sha1_helper(data)\n")
    assert out is None
