"""
Stage R — тесты secrets-extractor.

Принципы:
  * БЕЗ LLM, БЕЗ сети.
  * Unit-тесты работают с in-memory строками; только интеграционные
    пишут во временные директории через tmp_path.
  * Значения секретов не должны попадать в repr/str совпадений.

Запуск: python tests/test_phase_r_secrets_extractor.py
"""
from __future__ import annotations

import os
import sys
import textwrap
import unittest
from pathlib import Path

# Добавляем корень проекта в sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from analysis.secrets_extractor import (
    ApplyResult,
    SecretMatch,
    SecretsExtractor,
    _has_import_os,
    _inject_import_os,
    _is_comment_line,
    _make_replacement,
    _merge_secrets_file,
    _update_gitignore,
)


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def _extractor() -> SecretsExtractor:
    return SecretsExtractor()


# ---------------------------------------------------------------------------
# 1. SecretMatch — repr и безопасность
# ---------------------------------------------------------------------------

class TestSecretMatchRepr(unittest.TestCase):

    def _match(self, **kw) -> SecretMatch:
        defaults = dict(
            name="API_KEY", value="super_secret_value_12345",
            line=3, file="config.py", language="python", kind="assignment",
        )
        defaults.update(kw)
        return SecretMatch(**defaults)

    def test_repr_redacts_value(self):
        m = self._match()
        r = repr(m)
        self.assertIn("***", r)
        self.assertNotIn("super_secret_value_12345", r)

    def test_str_redacts_value(self):
        m = self._match()
        self.assertNotIn("super_secret_value_12345", str(m))

    def test_repr_contains_name(self):
        m = self._match(name="TOKEN")
        self.assertIn("TOKEN", repr(m))

    def test_repr_contains_line(self):
        m = self._match(line=42)
        self.assertIn("42", repr(m))

    def test_dataclass_fields_accessible(self):
        m = self._match()
        self.assertEqual(m.name, "API_KEY")
        self.assertEqual(m.value, "super_secret_value_12345")
        self.assertEqual(m.line, 3)
        self.assertEqual(m.language, "python")
        self.assertEqual(m.kind, "assignment")


# ---------------------------------------------------------------------------
# 2. scan() — Python, совпадения
# ---------------------------------------------------------------------------

class TestScanPythonFinds(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def _scan(self, code: str) -> list[SecretMatch]:
        return self.ex.scan("config.py", textwrap.dedent(code), "python")

    def test_api_key_double_quote(self):
        r = self._scan('API_KEY = "abcdefghijkl"')
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].name, "API_KEY")
        self.assertEqual(r[0].value, "abcdefghijkl")

    def test_api_key_single_quote(self):
        r = self._scan("API_KEY = 'abcdefghijkl'")
        self.assertEqual(len(r), 1)

    def test_password_detected(self):
        r = self._scan('password = "mysecretpass"')
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].name, "password")

    def test_token_detected(self):
        r = self._scan('token = "ghp_abcdefghij123456789012"')
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].name, "token")

    def test_secret_key_detected(self):
        r = self._scan('SECRET_KEY = "abc123xyz789"')
        self.assertEqual(len(r), 1)

    def test_api_underscore_key(self):
        r = self._scan('my_api_key = "xxxxxxxxxxxxxxxxxxx"')
        self.assertEqual(len(r), 1)

    def test_mixed_case_name(self):
        r = self._scan('ApiKey = "longvaluehere123"')
        self.assertEqual(len(r), 1)

    def test_line_number_correct(self):
        code = "x = 1\ny = 2\nAPI_KEY = \"abcdefghijkl\"\n"
        r = self.ex.scan("f.py", code, "python")
        self.assertEqual(r[0].line, 3)

    def test_line_number_first_line(self):
        r = self._scan('SECRET = "verylongsecret1"')
        self.assertEqual(r[0].line, 1)

    def test_multiple_secrets_same_file(self):
        code = 'API_KEY = "abc123def456"\nPASSWORD = "xyzxyzxyzxyz"\n'
        r = self.ex.scan("f.py", code, "python")
        self.assertEqual(len(r), 2)
        names = {m.name for m in r}
        self.assertIn("API_KEY", names)
        self.assertIn("PASSWORD", names)


# ---------------------------------------------------------------------------
# 3. scan() — Python, пропуски
# ---------------------------------------------------------------------------

class TestScanPythonSkips(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def _scan(self, code: str) -> list[SecretMatch]:
        return self.ex.scan("config.py", textwrap.dedent(code), "python")

    def test_env_read_os_environ_skip(self):
        r = self._scan('API_KEY = os.environ["API_KEY"]')
        self.assertEqual(r, [])

    def test_env_read_os_getenv_skip(self):
        r = self._scan('API_KEY = os.getenv("API_KEY", "")')
        self.assertEqual(r, [])

    def test_fstring_skip(self):
        # f-строка: перед кавычкой стоит 'f', паттерн не матчится
        r = self._scan('SECRET = f"value_{x}"')
        self.assertEqual(r, [])

    def test_comment_line_skip(self):
        r = self._scan('# API_KEY = "abcdefghijkl"')
        self.assertEqual(r, [])

    def test_short_value_skip(self):
        # < 6 символов — слишком короткое, не считаем секретом
        r = self._scan('PASSWORD = "ab"')
        self.assertEqual(r, [])

    def test_non_secret_name_skip(self):
        # имя не содержит маркер секрета
        r = self._scan('GREETING = "hello_world_here"')
        self.assertEqual(r, [])

    def test_dotenv_skip(self):
        r = self._scan('API_KEY = dotenv.get("API_KEY")')
        self.assertEqual(r, [])

    def test_print_arg_not_secret(self):
        # аргумент функции без присваивания — паттерн требует NAME = "..."
        r = self._scan('print("password is very_wrong_here")')
        self.assertEqual(r, [])

    def test_already_os_environ_bracket_skip(self):
        r = self._scan("SECRET_KEY = environ['SECRET_KEY']")
        self.assertEqual(r, [])


# ---------------------------------------------------------------------------
# 4. scan() — JS/TS
# ---------------------------------------------------------------------------

class TestScanJS(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def test_const_api_key(self):
        r = self.ex.scan("app.js", 'const API_KEY = "abcdefghijkl";', "js")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].name, "API_KEY")

    def test_let_token(self):
        r = self.ex.scan("app.js", 'let token = "ghp_abc123def456789xyz";', "js")
        self.assertEqual(len(r), 1)

    def test_var_password(self):
        r = self.ex.scan("app.js", 'var password = "mysecretpassword";', "js")
        self.assertEqual(len(r), 1)

    def test_process_env_skip(self):
        r = self.ex.scan("app.js", 'const API_KEY = process.env.API_KEY;', "js")
        self.assertEqual(r, [])

    def test_linecomment_skip(self):
        r = self.ex.scan("app.js", '// const API_KEY = "abcdefghijkl";', "js")
        self.assertEqual(r, [])

    def test_ts_extension(self):
        r = self.ex.scan("app.ts", 'const apiKey = "abcdefghijkl";', "ts")
        self.assertEqual(len(r), 1)


# ---------------------------------------------------------------------------
# 5. scan() — Rust
# ---------------------------------------------------------------------------

class TestScanRust(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def test_let_binding(self):
        r = self.ex.scan("main.rs", 'let api_key: &str = "secretvalue1234";', "rust")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].name, "api_key")

    def test_let_no_type(self):
        r = self.ex.scan("main.rs", 'let token = "ghp_abcdefghij12345678";', "rust")
        self.assertEqual(len(r), 1)

    def test_const_binding(self):
        r = self.ex.scan("main.rs", 'const API_KEY: &str = "abcdefghijkl";', "rust")
        self.assertEqual(len(r), 1)

    def test_env_skip(self):
        r = self.ex.scan(
            "main.rs",
            'let api_key = std::env::var("API_KEY").unwrap();',
            "rust",
        )
        self.assertEqual(r, [])

    def test_linecomment_skip(self):
        r = self.ex.scan("main.rs", '// let api_key = "secretvalue1234";', "rust")
        self.assertEqual(r, [])


# ---------------------------------------------------------------------------
# 6. Вспомогательные функции
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_is_comment_python(self):
        self.assertTrue(_is_comment_line("# comment", "python"))
        self.assertFalse(_is_comment_line("x = 1", "python"))

    def test_is_comment_js(self):
        self.assertTrue(_is_comment_line("// comment", "js"))
        self.assertFalse(_is_comment_line("const x = 1;", "js"))

    def test_is_comment_rust(self):
        self.assertTrue(_is_comment_line("// comment", "rust"))

    def test_has_import_os_present(self):
        self.assertTrue(_has_import_os("import os\nx = 1\n"))
        self.assertTrue(_has_import_os("import os, sys\n"))

    def test_has_import_os_absent(self):
        self.assertFalse(_has_import_os("import sys\nx = 1\n"))

    def test_inject_import_os_no_imports(self):
        result = _inject_import_os("x = 1\n")
        self.assertIn("import os", result)
        self.assertIn("x = 1", result)

    def test_inject_import_os_after_existing_imports(self):
        src = "import sys\nimport re\nX = 1\n"
        result = _inject_import_os(src)
        self.assertIn("import os", result)
        self.assertIn("import sys", result)

    def test_inject_import_os_idempotent_call(self):
        # _inject_import_os вызывается только когда os ещё не импортирован
        src = "import sys\nX = 1\n"
        result = _inject_import_os(src)
        self.assertEqual(result.count("import os"), 1)

    def test_make_replacement_python(self):
        m = SecretMatch("API_KEY", "val", 1, "f.py", "python", "assignment")
        orig = 'API_KEY = "val"\n'
        r = _make_replacement(m, orig)
        self.assertEqual(r, 'API_KEY = os.environ["API_KEY"]\n')

    def test_make_replacement_python_indented(self):
        m = SecretMatch("TOKEN", "val", 1, "f.py", "python", "assignment")
        orig = '    TOKEN = "val"\n'
        r = _make_replacement(m, orig)
        self.assertTrue(r.startswith("    "))
        self.assertIn('os.environ["TOKEN"]', r)

    def test_make_replacement_js_const(self):
        m = SecretMatch("API_KEY", "val", 1, "f.js", "js", "assignment")
        orig = 'const API_KEY = "val";\n'
        r = _make_replacement(m, orig)
        self.assertIn("process.env.API_KEY", r)
        self.assertIn("const", r)

    def test_make_replacement_js_let(self):
        m = SecretMatch("TOKEN", "val", 1, "f.js", "js", "assignment")
        orig = 'let TOKEN = "val";\n'
        r = _make_replacement(m, orig)
        self.assertIn("let", r)
        self.assertIn("process.env.TOKEN", r)

    def test_make_replacement_rust(self):
        m = SecretMatch("api_key", "val", 1, "f.rs", "rust", "assignment")
        orig = 'let api_key: &str = "val";\n'
        r = _make_replacement(m, orig)
        self.assertIn('std::env::var("api_key")', r)


# ---------------------------------------------------------------------------
# 7. apply() — интеграционные (используют tmp_path-эмуляцию через tempfile)
# ---------------------------------------------------------------------------

import tempfile


def _make_project(files: dict[str, str]) -> Path:
    """Создаёт временный каталог с заданными файлами."""
    d = Path(tempfile.mkdtemp(prefix="r_test_"))
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


def _cleanup(root: Path) -> None:
    import shutil
    shutil.rmtree(str(root), ignore_errors=True)


class TestApplyPython(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def test_apply_replaces_literal(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            self.ex.apply([m], root)
            result = (root / "config.py").read_text(encoding="utf-8")
            self.assertIn('os.environ["API_KEY"]', result)
            self.assertNotIn('"abcdefghijkl"', result)
        finally:
            _cleanup(root)

    def test_apply_adds_import_os(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            self.ex.apply([m], root)
            result = (root / "config.py").read_text(encoding="utf-8")
            self.assertIn("import os", result)
        finally:
            _cleanup(root)

    def test_apply_no_dup_import_os(self):
        root = _make_project({"config.py": 'import os\nAPI_KEY = "abcdefghijkl"\n'})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 2, "config.py", "python", "assignment")
            self.ex.apply([m], root)
            result = (root / "config.py").read_text(encoding="utf-8")
            self.assertEqual(result.count("import os"), 1)
        finally:
            _cleanup(root)

    def test_apply_creates_secrets_txt(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            self.ex.apply([m], root)
            st = root / "secrets.txt"
            self.assertTrue(st.exists())
            self.assertIn("API_KEY=abcdefghijkl", st.read_text(encoding="utf-8"))
        finally:
            _cleanup(root)

    def test_apply_merges_secrets_txt(self):
        root = _make_project({
            "config.py": 'TOKEN = "newtokenvalue123"\n',
            "secrets.txt": "EXISTING_KEY=oldvalue\n",
        })
        try:
            m = SecretMatch("TOKEN", "newtokenvalue123", 1, "config.py", "python", "assignment")
            self.ex.apply([m], root)
            content = (root / "secrets.txt").read_text(encoding="utf-8")
            self.assertIn("TOKEN=newtokenvalue123", content)
            self.assertIn("EXISTING_KEY=oldvalue", content)
        finally:
            _cleanup(root)

    def test_apply_no_duplicates_in_secrets_txt(self):
        root = _make_project({
            "config.py": 'API_KEY = "abcdefghijkl"\n',
            "secrets.txt": "API_KEY=oldvalue\n",
        })
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            self.ex.apply([m], root)
            content = (root / "secrets.txt").read_text(encoding="utf-8")
            self.assertEqual(content.count("API_KEY="), 1)
        finally:
            _cleanup(root)

    def test_apply_creates_gitignore(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            result = self.ex.apply([m], root)
            self.assertTrue(result.gitignore_updated)
            gi = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("secrets.txt", gi)
        finally:
            _cleanup(root)

    def test_apply_updates_existing_gitignore(self):
        root = _make_project({
            "config.py": 'API_KEY = "abcdefghijkl"\n',
            ".gitignore": "*.pyc\n__pycache__/\n",
        })
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            self.ex.apply([m], root)
            gi = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("secrets.txt", gi)
            self.assertIn("*.pyc", gi)
        finally:
            _cleanup(root)

    def test_apply_no_dup_gitignore(self):
        root = _make_project({
            "config.py": 'API_KEY = "abcdefghijkl"\n',
            ".gitignore": "secrets.txt\n.env\n",
        })
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            result = self.ex.apply([m], root)
            self.assertFalse(result.gitignore_updated)
            gi = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(gi.count("secrets.txt"), 1)
        finally:
            _cleanup(root)

    def test_dry_run_no_file_written(self):
        original = 'API_KEY = "abcdefghijkl"\n'
        root = _make_project({"config.py": original})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            result = self.ex.apply([m], root, dry_run=True)
            # Файл не изменён
            self.assertEqual((root / "config.py").read_text(encoding="utf-8"), original)
            # secrets.txt не создан
            self.assertFalse((root / "secrets.txt").exists())
            self.assertTrue(result.dry_run)
            self.assertEqual(result.count, 1)
        finally:
            _cleanup(root)

    def test_dry_run_count_correct(self):
        root = _make_project({
            "a.py": 'API_KEY = "abcdefghijkl"\n',
            "b.py": 'PASSWORD = "xyzxyzxyzxyz"\n',
        })
        try:
            matches = [
                SecretMatch("API_KEY", "abcdefghijkl", 1, "a.py", "python", "assignment"),
                SecretMatch("PASSWORD", "xyzxyzxyzxyz", 1, "b.py", "python", "assignment"),
            ]
            result = self.ex.apply(matches, root, dry_run=True)
            self.assertEqual(result.count, 2)
            self.assertIn("API_KEY", result.names)
            self.assertIn("PASSWORD", result.names)
        finally:
            _cleanup(root)

    def test_apply_result_names_no_values(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "config.py", "python", "assignment")
            result = self.ex.apply([m], root)
            # names содержит только имена, не значения
            self.assertIn("API_KEY", result.names)
            self.assertNotIn("abcdefghijkl", result.names)
        finally:
            _cleanup(root)


# ---------------------------------------------------------------------------
# 8. apply() — JS
# ---------------------------------------------------------------------------

class TestApplyJS(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def test_apply_replaces_js_const(self):
        root = _make_project({"app.js": 'const API_KEY = "abcdefghijkl";\n'})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "app.js", "js", "assignment")
            self.ex.apply([m], root)
            result = (root / "app.js").read_text(encoding="utf-8")
            self.assertIn("process.env.API_KEY", result)
            self.assertNotIn('"abcdefghijkl"', result)
        finally:
            _cleanup(root)


# ---------------------------------------------------------------------------
# 9. apply() — Rust
# ---------------------------------------------------------------------------

class TestApplyRust(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def test_apply_replaces_rust_binding(self):
        root = _make_project({"main.rs": 'let api_key: &str = "secretvalue1234";\n'})
        try:
            m = SecretMatch("api_key", "secretvalue1234", 1, "main.rs", "rust", "assignment")
            self.ex.apply([m], root)
            result = (root / "main.rs").read_text(encoding="utf-8")
            self.assertIn('std::env::var("api_key")', result)
        finally:
            _cleanup(root)


# ---------------------------------------------------------------------------
# 10. scan_and_apply() — end-to-end
# ---------------------------------------------------------------------------

class TestScanAndApply(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def test_empty_project_no_secrets(self):
        root = _make_project({"main.py": "x = 1\nprint(x)\n"})
        try:
            result = self.ex.scan_and_apply(root)
            self.assertEqual(result.count, 0)
            self.assertEqual(result.files_modified, [])
            self.assertFalse((root / "secrets.txt").exists())
        finally:
            _cleanup(root)

    def test_finds_and_applies_secret(self):
        root = _make_project({"config.py": 'SECRET_KEY = "abc123xyz789"\n'})
        try:
            result = self.ex.scan_and_apply(root)
            self.assertEqual(result.count, 1)
            self.assertIn("SECRET_KEY", result.names)
            code = (root / "config.py").read_text(encoding="utf-8")
            self.assertIn("os.environ", code)
            self.assertTrue((root / "secrets.txt").exists())
        finally:
            _cleanup(root)

    def test_skips_venv_directory(self):
        root = _make_project({
            "config.py": "x = 1\n",
            "venv/lib/site-packages/config.py": 'API_KEY = "abcdefghijkl"\n',
        })
        try:
            result = self.ex.scan_and_apply(root)
            self.assertEqual(result.count, 0)
        finally:
            _cleanup(root)

    def test_skips_webbles_backups(self):
        root = _make_project({
            "config.py": "x = 1\n",
            ".webbles_backups/config.py": 'API_KEY = "abcdefghijkl"\n',
        })
        try:
            result = self.ex.scan_and_apply(root)
            self.assertEqual(result.count, 0)
        finally:
            _cleanup(root)

    def test_dry_run_no_writes(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            result = self.ex.scan_and_apply(root, dry_run=True)
            self.assertEqual(result.count, 1)
            self.assertTrue(result.dry_run)
            # Файл не изменён
            self.assertIn('"abcdefghijkl"', (root / "config.py").read_text(encoding="utf-8"))
            # secrets.txt не создан
            self.assertFalse((root / "secrets.txt").exists())
        finally:
            _cleanup(root)

    def test_gitignore_updated_after_scan(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            result = self.ex.scan_and_apply(root)
            self.assertTrue(result.gitignore_updated)
            self.assertIn("secrets.txt", (root / ".gitignore").read_text(encoding="utf-8"))
        finally:
            _cleanup(root)

    def test_language_filter(self):
        root = _make_project({
            "app.py": 'API_KEY = "abcdefghijkl"\n',
            "app.js": 'const token = "abcdefghijkl";\n',
        })
        try:
            result = self.ex.scan_and_apply(root, language="python")
            self.assertEqual(result.count, 1)
            self.assertIn("API_KEY", result.names)
        finally:
            _cleanup(root)

    def test_skips_secrets_txt_itself(self):
        root = _make_project({
            "config.py": "x = 1\n",
            "secrets.txt": "API_KEY=realvalue\n",
        })
        try:
            result = self.ex.scan_and_apply(root)
            self.assertEqual(result.count, 0)
        finally:
            _cleanup(root)

    def test_skips_env_file(self):
        root = _make_project({
            "config.py": "x = 1\n",
            ".env": "API_KEY=realvalue\n",
        })
        try:
            result = self.ex.scan_and_apply(root)
            self.assertEqual(result.count, 0)
        finally:
            _cleanup(root)


# ---------------------------------------------------------------------------
# 11. _update_gitignore и _merge_secrets_file — unit
# ---------------------------------------------------------------------------

class TestGitignoreUpdate(unittest.TestCase):

    def test_creates_new_gitignore(self):
        root = _make_project({})
        try:
            gi = root / ".gitignore"
            updated = _update_gitignore(gi, dry_run=False)
            self.assertTrue(updated)
            content = gi.read_text(encoding="utf-8")
            self.assertIn("secrets.txt", content)
            self.assertIn(".env", content)
        finally:
            _cleanup(root)

    def test_no_update_if_already_present(self):
        root = _make_project({".gitignore": "secrets.txt\n.env\n"})
        try:
            updated = _update_gitignore(root / ".gitignore", dry_run=False)
            self.assertFalse(updated)
        finally:
            _cleanup(root)

    def test_partial_update(self):
        root = _make_project({".gitignore": "secrets.txt\n"})
        try:
            updated = _update_gitignore(root / ".gitignore", dry_run=False)
            self.assertTrue(updated)
            content = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".env", content)
            self.assertEqual(content.count("secrets.txt"), 1)
        finally:
            _cleanup(root)

    def test_dry_run_no_write(self):
        root = _make_project({})
        try:
            updated = _update_gitignore(root / ".gitignore", dry_run=True)
            self.assertTrue(updated)
            self.assertFalse((root / ".gitignore").exists())
        finally:
            _cleanup(root)


class TestMergeSecretsFile(unittest.TestCase):

    def test_creates_file(self):
        root = _make_project({})
        try:
            m = SecretMatch("K", "v123456789", 1, "f.py", "python", "assignment")
            _merge_secrets_file(root / "secrets.txt", [m], dry_run=False)
            self.assertIn("K=v123456789", (root / "secrets.txt").read_text(encoding="utf-8"))
        finally:
            _cleanup(root)

    def test_merges_existing(self):
        root = _make_project({"secrets.txt": "OLD=x\n"})
        try:
            m = SecretMatch("NEW", "y123456789", 1, "f.py", "python", "assignment")
            _merge_secrets_file(root / "secrets.txt", [m], dry_run=False)
            content = (root / "secrets.txt").read_text(encoding="utf-8")
            self.assertIn("OLD=x", content)
            self.assertIn("NEW=y123456789", content)
        finally:
            _cleanup(root)

    def test_overwrites_same_key(self):
        root = _make_project({"secrets.txt": "KEY=oldval\n"})
        try:
            m = SecretMatch("KEY", "newval123456", 1, "f.py", "python", "assignment")
            _merge_secrets_file(root / "secrets.txt", [m], dry_run=False)
            content = (root / "secrets.txt").read_text(encoding="utf-8")
            self.assertEqual(content.count("KEY="), 1)
            self.assertIn("KEY=newval123456", content)
        finally:
            _cleanup(root)

    def test_dry_run_no_write(self):
        root = _make_project({})
        try:
            m = SecretMatch("K", "v123456789", 1, "f.py", "python", "assignment")
            _merge_secrets_file(root / "secrets.txt", [m], dry_run=True)
            self.assertFalse((root / "secrets.txt").exists())
        finally:
            _cleanup(root)


# ---------------------------------------------------------------------------
# 12. Corner cases
# ---------------------------------------------------------------------------

class TestCornerCases(unittest.TestCase):

    def setUp(self):
        self.ex = _extractor()

    def test_empty_file(self):
        r = self.ex.scan("empty.py", "", "python")
        self.assertEqual(r, [])

    def test_multiline_no_false_positive(self):
        # triple-quoted строки — MVP ограничение: не детектируем
        code = 'SECRET = """this is a very long triple quoted string"""\n'
        r = self.ex.scan("f.py", code, "python")
        # Паттерн ожидает ['"] а не """ — нет совпадения
        self.assertEqual(r, [])

    def test_editset_anchor_fail_graceful(self):
        # Если содержимое файла не совпадает с anchor — apply() возвращает gracefully
        root = _make_project({"config.py": "x = 1\n"})
        try:
            # Указываем несуществующую строку
            m = SecretMatch("API_KEY", "abcdefghijkl", 5, "config.py", "python", "assignment")
            result = self.ex.apply([m], root)
            # Файл не изменён, count всё равно равен числу matches переданных
            self.assertEqual((root / "config.py").read_text(encoding="utf-8"), "x = 1\n")
        finally:
            _cleanup(root)

    def test_apply_result_has_no_values_in_names(self):
        root = _make_project({"config.py": 'API_KEY = "abcdefghijkl"\n'})
        try:
            result = self.ex.scan_and_apply(root)
            for name in result.names:
                self.assertNotEqual(name, "abcdefghijkl")
        finally:
            _cleanup(root)

    def test_nonexistent_file_skipped_gracefully(self):
        root = _make_project({})
        try:
            m = SecretMatch("API_KEY", "abcdefghijkl", 1, "missing.py", "python", "assignment")
            result = self.ex.apply([m], root)
            self.assertEqual(result.files_modified, [])
        finally:
            _cleanup(root)

    def test_acceptance_test_python(self):
        """Acceptance: config.py с SECRET_KEY → secrets.txt + os.environ + .gitignore."""
        root = _make_project({
            "config.py": (
                "DEBUG = True\n"
                'SECRET_KEY = "abc123xyz789secret"\n'
                "ALLOWED_HOSTS = []\n"
            ),
        })
        try:
            result = self.ex.scan_and_apply(root)
            self.assertEqual(result.count, 1)
            self.assertIn("SECRET_KEY", result.names)

            # secrets.txt создан с правильным содержимым
            st = root / "secrets.txt"
            self.assertTrue(st.exists())
            st_content = st.read_text(encoding="utf-8")
            self.assertIn("SECRET_KEY=abc123xyz789secret", st_content)

            # Код заменён
            code = (root / "config.py").read_text(encoding="utf-8")
            self.assertIn('os.environ["SECRET_KEY"]', code)
            self.assertNotIn('"abc123xyz789secret"', code)
            self.assertIn("import os", code)

            # .gitignore обновлён
            gi = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("secrets.txt", gi)
        finally:
            _cleanup(root)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
