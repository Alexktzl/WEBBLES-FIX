"""
NEW: блок на правку .json (кроме манифестов) и .md в LLM-патчах.

Кейс: на реальном прогоне LLM начала чинить README.md как код, выдавая
невалидные diff'ы. Документация и data-json-файлы не должны редактироваться
авто-фиксером — это побочные эффекты галлюцинаций LLM.

Тест проверяет статический classmethod `_is_blocked_for_llm_edit`:
  * `.md` всегда блокируется (кроме случая когда сам error в этом md).
  * `.json` блокируется, кроме whitelist'а манифестов
    (package.json, tsconfig.json, composer.json, deno.json, ...).
  * `.jsonc` — то же правило.
  * Path / case / разделители — все варианты нормализованы.

Запуск: python3 tests/test_phase_block_json_md.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True

try:
    import tomlkit  # noqa: F401
except ImportError:
    import types as _t
    sys.modules["tomlkit"] = _t.ModuleType("tomlkit")

from core.stages.apply_patch_stage import ApplyPatchStage

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


cls = ApplyPatchStage


# ---------------------------------------------------------------------
# 1. Markdown — всегда блокируется (если не целевой файл ошибки).
# ---------------------------------------------------------------------

def test_md_blocked():
    check("md: README.md blocked",
          cls._is_blocked_for_llm_edit("README.md", "src/main.py") is True)
    check("md: docs/CHANGELOG.md blocked",
          cls._is_blocked_for_llm_edit("docs/CHANGELOG.md", "src/main.py") is True)
    check("md: API_NOTES.md blocked",
          cls._is_blocked_for_llm_edit("API_NOTES.md", "src/main.py") is True)


def test_md_allowed_if_target():
    """Если сам error на этом .md — блокировку снимаем."""
    check("md: README.md NOT blocked when error is on README.md",
          cls._is_blocked_for_llm_edit("README.md", "README.md") is False)
    check("md: nested case",
          cls._is_blocked_for_llm_edit("docs/INTRO.md", "docs/INTRO.md") is False)


# ---------------------------------------------------------------------
# 2. JSON — блокируется, кроме манифестов.
# ---------------------------------------------------------------------

def test_json_data_blocked():
    """Данные/конфиги, не входящие в whitelist."""
    for path in ("data/users.json", "config/app.json", "i18n/ru.json",
                 "fixtures/sample.json", "src/components/styles.json"):
        check(f"json: {path} blocked",
              cls._is_blocked_for_llm_edit(path, "src/main.py") is True)


def test_json_manifests_allowed():
    """Известные манифесты пакетов — РАЗРЕШЕНЫ."""
    for path in ("package.json", "package-lock.json",
                 "frontend/package.json",
                 "tsconfig.json", "tsconfig.app.json", "tsconfig.base.json",
                 "composer.json", "deno.json", "jsr.json"):
        check(f"json(manifest): {path} allowed",
              cls._is_blocked_for_llm_edit(path, "src/main.py") is False)


def test_jsonc_data_blocked():
    check("jsonc: random.jsonc blocked",
          cls._is_blocked_for_llm_edit("config/foo.jsonc", "src/main.py") is True)


def test_jsonc_deno_allowed():
    check("jsonc: deno.jsonc allowed",
          cls._is_blocked_for_llm_edit("deno.jsonc", "src/main.py") is False)


def test_json_target_override():
    """Если сам error на этом json — блокировку снимаем."""
    check("json: data.json NOT blocked when target",
          cls._is_blocked_for_llm_edit("data/x.json", "data/x.json") is False)


# ---------------------------------------------------------------------
# 3. Не-blocked расширения проходят свободно.
# ---------------------------------------------------------------------

def test_other_extensions_allowed():
    for path in ("src/main.py", "lib/foo.rs", "app.ts", "index.js",
                 "Cargo.toml", "pyproject.toml", "requirements.txt",
                 "Makefile", "Dockerfile"):
        check(f"other: {path} allowed",
              cls._is_blocked_for_llm_edit(path, "src/main.py") is False)


# ---------------------------------------------------------------------
# 4. Edge: пустые/None
# ---------------------------------------------------------------------

def test_edge_cases():
    check("edge: empty path -> not blocked",
          cls._is_blocked_for_llm_edit("", "src/main.py") is False)
    check("edge: None target -> md still blocked",
          cls._is_blocked_for_llm_edit("README.md", None) is True)


# ---------------------------------------------------------------------
# 5. Path separators / case-insensitive
# ---------------------------------------------------------------------

def test_path_normalization():
    check("path: backslash separator",
          cls._is_blocked_for_llm_edit("docs\\README.md", "src/main.py") is True)
    check("path: leading ./",
          cls._is_blocked_for_llm_edit("./README.md", "src/main.py") is True)


def test_uppercase_extension():
    check("case: README.MD also blocked",
          cls._is_blocked_for_llm_edit("README.MD", "src/main.py") is True)
    check("case: Package.JSON manifest still allowed",
          cls._is_blocked_for_llm_edit("Package.JSON", "src/main.py") is False)


if __name__ == "__main__":
    print("NEW: block .json (non-manifest) + .md edits:")
    test_md_blocked()
    test_md_allowed_if_target()
    test_json_data_blocked()
    test_json_manifests_allowed()
    test_jsonc_data_blocked()
    test_jsonc_deno_allowed()
    test_json_target_override()
    test_other_extensions_allowed()
    test_edge_cases()
    test_path_normalization()
    test_uppercase_extension()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nNEW block: {passed}/{total} pass")
    sys.exit(0 if passed == total else 1)
