"""
Stage J — smoke / regression для External example search.

Покрывает:
  1. Example round-trip (to_dict / from_dict).
  2. ExampleCache: round-trip + TTL-протухание + битый файл.
  3. RustcExplainProvider: парсинг зафиксированного --explain текста (мок subprocess),
     не-rust / не-Exxxx → [].
  4. ExampleSearchService: оркестрация мок-провайдеров, агрегация, сортировка по score,
     enabled=False → [], кэш используется (повторный вызов без провайдеров).
  5. from_config: по умолчанию только rustc_explain, enabled=True.
  6. PromptBuilder.build_case_file: секция EXTERNAL EXAMPLES появляется при непустом
     списке и отсутствует при пустом.

Сеть НЕ дёргается: github/stackoverflow здесь не вызываются вживую.
Запуск: python3 tests/test_phase_j_external.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Гарантируем импорт пакета из корня репо.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

from analysis.external_examples.provider import (
    Example, ExampleSearchProvider, ExampleSearchService,
)
from analysis.external_examples.cache import ExampleCache
from analysis.external_examples.python_explain import PythonExplainProvider
from analysis.external_examples.rustc_explain import RustcExplainProvider
from fixers.prompt_builder import PromptBuilder


results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- мок-провайдеры -------------------------------------------------
class _MockProvider(ExampleSearchProvider):
    def __init__(self, name, examples, avail=True):
        self.name = name
        self._examples = examples
        self._avail = avail

    def available(self):
        return self._avail

    def search(self, error, language, max_results=3):
        return list(self._examples)[:max_results]


class _BoomProvider(ExampleSearchProvider):
    name = "boom"

    def available(self):
        return True

    def search(self, error, language, max_results=3):
        raise RuntimeError("provider exploded")


# --- 1. Example round-trip ------------------------------------------
def test_example_roundtrip():
    e = Example(source="rustc_explain", title="t", snippet="code", url="u", score=0.9)
    check("example_roundtrip", Example.from_dict(e.to_dict()) == e)
    # дефолты
    e2 = Example(source="s", title="", snippet="")
    check("example_defaults", e2.url == "" and e2.score == 0.0)


# --- 2. ExampleCache -------------------------------------------------
def test_cache():
    with tempfile.TemporaryDirectory() as d:
        cache = ExampleCache(Path(d) / "example_cache", ttl_days=7)
        check("cache_miss_returns_none", cache.get("E0382") is None)

        exs = [Example("rustc_explain", "title", "snippet", "url", 0.9)]
        cache.put("E0382", exs)
        got = cache.get("E0382")
        check("cache_roundtrip", got is not None and len(got) == 1
              and got[0].snippet == "snippet")

        # TTL: запись «из прошлого» с ttl=0 дней → протухла.
        cache0 = ExampleCache(Path(d) / "example_cache", ttl_days=0)
        # сохраняем свежую, но читаем с ttl=0 → now-saved_at > 0 → протухла
        # (saved_at чуть в прошлом из-за времени выполнения)
        import time
        time.sleep(0.01)
        check("cache_ttl_expired", cache0.get("E0382") is None)

        # битый файл → None, без исключения
        bad = Path(d) / "example_cache" / "E9999.json"
        bad.write_text("{not json", encoding="utf-8")
        check("cache_corrupt_returns_none", cache.get("E9999") is None)

        # коды с небезопасными символами не ломают путь
        cache.put("clippy::needless_return", exs)
        check("cache_safe_code", cache.get("clippy::needless_return") is not None)


# --- 3. RustcExplainProvider ----------------------------------------
_EXPLAIN_TEXT = """\
This error occurs because a value was moved and then used again.

Erroneous code example:

```rust
let x = String::from("hi");
let y = x;
println!("{}", x); // error: borrow of moved value
```

To fix this, clone the value or borrow it.
"""


def test_rustc_parse():
    p = RustcExplainProvider()
    # _parse — статический, тестируем напрямую (без subprocess).
    snippet, title = p._parse(_EXPLAIN_TEXT, "E0382")
    check("rustc_parse_snippet_is_code",
          "let y = x;" in snippet and "borrow of moved value" in snippet)
    check("rustc_parse_title_is_first_line",
          title.startswith("This error occurs"))

    # язык не rust → []
    check("rustc_non_rust_empty",
          p.search({"code": "E0382"}, "python", 3) == [])
    # код не Exxxx → []
    check("rustc_bad_code_empty",
          p.search({"code": "F401"}, "rust", 3) == [])

    # search с замоканным _run_explain → один Example с правильным url
    p._run_explain = lambda code: _EXPLAIN_TEXT
    out = p.search({"code": "E0382"}, "rust", 3)
    check("rustc_search_builds_example",
          len(out) == 1 and out[0].source == "rustc_explain"
          and out[0].url.endswith("E0382.html"))


# --- 4. ExampleSearchService ----------------------------------------
def test_service():
    err = {"code": "E0382"}

    # enabled=False → []
    svc_off = ExampleSearchService(providers=[_MockProvider("m", [Example("m", "t", "s")])],
                                   enabled=False)
    check("service_disabled_empty", svc_off.search(err, "rust") == [])

    # агрегация двух провайдеров + сортировка по score
    p1 = _MockProvider("a", [Example("a", "t1", "s1", "", 0.3)])
    p2 = _MockProvider("b", [Example("b", "t2", "s2", "", 0.8)])
    svc = ExampleSearchService(providers=[p1, p2], cache=None, enabled=True)
    out = svc.search(err, "rust")
    check("service_aggregates_two", len(out) == 2)
    check("service_sorts_by_score", out[0].source == "b" and out[1].source == "a")

    # недоступный провайдер пропускается; падающий не роняет
    p_off = _MockProvider("off", [Example("off", "t", "s")], avail=False)
    svc2 = ExampleSearchService(providers=[p_off, _BoomProvider(), p2], enabled=True)
    out2 = svc2.search(err, "rust")
    check("service_skips_unavail_and_boom", len(out2) == 1 and out2[0].source == "b")

    # кэш: первый вызов наполняет, второй — берёт из кэша даже без провайдеров
    with tempfile.TemporaryDirectory() as d:
        cache = ExampleCache(Path(d), ttl_days=7)
        svc3 = ExampleSearchService(providers=[p2], cache=cache, enabled=True)
        first = svc3.search(err, "rust")
        check("service_cache_fills", len(first) == 1)
        # теперь у сервиса НЕТ провайдеров, но код тот же → из кэша
        svc4 = ExampleSearchService(providers=[], cache=cache, enabled=True)
        second = svc4.search(err, "rust")
        check("service_cache_hit_no_providers",
              len(second) == 1 and second[0].source == "b")


# --- 5. from_config --------------------------------------------------
def test_from_config():
    with tempfile.TemporaryDirectory() as d:
        svc = ExampleSearchService.from_config({}, d)
        names = [p.name for p in svc.providers]
        # P.3: добавили python_explain в дефолт — для python-кодов даёт
        # ruff rule / mypy описание. rustc_explain остался первым.
        check("from_config_default_rustc_only",
              names == ["rustc_explain", "python_explain"])
        check("from_config_default_enabled", svc.enabled is True)

        svc2 = ExampleSearchService.from_config(
            {"case_file": {"external_search": {"enabled": False,
                                               "providers": ["rustc_explain", "github"]}}},
            d)
        names2 = sorted(p.name for p in svc2.providers)
        check("from_config_custom_providers", names2 == ["github", "rustc_explain"])
        check("from_config_disabled", svc2.enabled is False)


# --- 6. build_case_file: EXTERNAL EXAMPLES --------------------------
def test_case_file_section():
    pb = PromptBuilder()
    error = {"code": "E0382", "message": "borrow of moved value", "file": "src/m.rs", "line": 3}
    content = "fn main() {\n    let x = 1;\n    let y = x;\n}\n"

    # с примерами — секция есть
    ext = [{"source": "rustc_explain", "title": "E0382 explained",
            "snippet": "let y = &x;", "url": "https://doc.rust-lang.org/E0382.html"}]
    cf = pb.build_case_file(error=error, file_content=content, external_examples=ext)
    check("case_file_has_external_section", "## EXTERNAL EXAMPLES" in cf)
    check("case_file_external_shows_source", "[rustc_explain]" in cf)
    check("case_file_external_shows_snippet", "let y = &x;" in cf)

    # без примеров — секции нет
    cf2 = pb.build_case_file(error=error, file_content=content, external_examples=[])
    check("case_file_no_external_when_empty", "## EXTERNAL EXAMPLES" not in cf2)

    # None тоже безопасно
    cf3 = pb.build_case_file(error=error, file_content=content)
    check("case_file_external_none_safe", "## EXTERNAL EXAMPLES" not in cf3)


# --- 7. PythonExplainProvider ----------------------------------------
def test_python_explain_provider():
    provider = PythonExplainProvider()

    # available() always True — mypy table is built-in, no PATH check needed
    check("python_explain_available_always_true", provider.available() is True)

    # non-python language → []
    check("python_explain_non_python_empty",
          provider.search({"code": "F401"}, "rust") == [])
    check("python_explain_non_python_js",
          provider.search({"code": "F401"}, "javascript") == [])

    # empty / None code → []
    check("python_explain_empty_code",
          provider.search({"code": ""}, "python") == [])
    check("python_explain_none_code",
          provider.search({"code": None}, "python") == [])

    # mypy known code (name-defined) → Example from built-in table
    out = provider.search({"code": "name-defined"}, "python")
    check("python_explain_mypy_known_code",
          len(out) == 1 and out[0].source == "python_explain" and out[0].score == 0.80)

    # mypy known code using "py" language alias
    out_py = provider.search({"code": "arg-type"}, "py")
    check("python_explain_mypy_py_language_variant",
          len(out_py) == 1 and "Argument" in out_py[0].snippet)

    # invalid-syntax: matches _MYPY_CODE_RE but NOT in _MYPY_DESCRIPTIONS → []
    check("python_explain_invalid_syntax_fallback",
          provider.search({"code": "invalid-syntax"}, "python") == [])

    # unknown mypy-style code not in table → []
    check("python_explain_mypy_unknown_code",
          provider.search({"code": "unknown-error"}, "python") == [])

    # ruff code (F401) when ruff not in PATH → []
    with patch("shutil.which", return_value=None):
        check("python_explain_ruff_not_in_path",
              provider.search({"code": "F401"}, "python") == [])

    # E999 (flake8/ruff code) — matches _RUFF_CODE_RE, ruff not in PATH → []
    with patch("shutil.which", return_value=None):
        check("python_explain_E999_no_ruff",
              provider.search({"code": "E999"}, "python") == [])

    # ruff subprocess raises (e.g. timeout) → []
    with patch("shutil.which", return_value="/usr/bin/ruff"), \
         patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(["ruff", "rule", "F401"], 10)):
        check("python_explain_ruff_subprocess_raises",
              provider.search({"code": "F401"}, "python") == [])

    # ruff returncode != 0 (code unknown to ruff, e.g. W291) → []
    mock_fail = MagicMock()
    mock_fail.returncode = 1
    mock_fail.stdout = ""
    with patch("shutil.which", return_value="/usr/bin/ruff"), \
         patch("subprocess.run", return_value=mock_fail):
        check("python_explain_ruff_nonzero_rc",
              provider.search({"code": "W291"}, "python") == [])

    # ruff returncode == 0 but empty stdout → []
    mock_empty = MagicMock()
    mock_empty.returncode = 0
    mock_empty.stdout = ""
    with patch("shutil.which", return_value="/usr/bin/ruff"), \
         patch("subprocess.run", return_value=mock_empty):
        check("python_explain_ruff_empty_stdout",
              provider.search({"code": "F401"}, "python") == [])

    # ruff returns valid output → Example with correct source and score
    _RUFF_TEXT = "F401: Module `os` imported but unused.\n\n```python\nimport os\n```\n"
    mock_ok = MagicMock()
    mock_ok.returncode = 0
    mock_ok.stdout = _RUFF_TEXT
    with patch("shutil.which", return_value="/usr/bin/ruff"), \
         patch("subprocess.run", return_value=mock_ok):
        out_ruff = provider.search({"code": "F401"}, "python")
        check("python_explain_ruff_valid_output",
              len(out_ruff) == 1 and out_ruff[0].source == "python_explain"
              and out_ruff[0].score == 0.85)


if __name__ == "__main__":
    print("Stage J — External example search smoke:")
    test_example_roundtrip()
    test_cache()
    test_rustc_parse(None)
    test_service()
    test_from_config()
    test_case_file_section()
    test_python_explain_provider()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nStage J: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
