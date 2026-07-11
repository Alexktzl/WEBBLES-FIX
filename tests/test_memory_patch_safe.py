"""
Regress: pre-apply валидатор memory-патча `_memory_patch_safe`.

Защищает от ситуации, когда memory сохранил патч в одном состоянии файла,
а сейчас файл изменился — apply бы прошёл «технически», но получил бы кашу
с откатом постфактум.

Сценарии:
  - контекст-линии совпали + баланс ок → safe
  - файл изменился, контекст НЕ совпадает → unsafe (rust-патч в python-файле
    тоже сюда: контекст линий разный)
  - патч ломает баланс скобок → unsafe
  - пустой/невалидный патч → unsafe

Запуск: python3 tests/test_memory_patch_safe.py
"""

import importlib.util
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# generate_patch_stage транзитивно тянет tomlkit через fixers/toml_patcher.
# В средах без него подсовываем заглушку — функционал валидатора tomlkit
# не использует.
try:
    import tomlkit  # noqa
except ImportError:
    sys.modules["tomlkit"] = types.ModuleType("tomlkit")


def _load_generate_stage_class():
    """Загружаем GeneratePatchStage напрямую (минуя core.stages.__init__,
    который тянет тяжёлые зависимости)."""
    spec = importlib.util.spec_from_file_location(
        "gps_for_test", str(ROOT / "core" / "stages" / "generate_patch_stage.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.GeneratePatchStage


def _make_stage():
    GPS = _load_generate_stage_class()
    from fixers.patch_engine import PatchEngine
    # Мокаем тяжёлые deps, нам нужен только self.patch_engine для _parse_patch.
    stage = GPS(
        llm_client=MagicMock(),
        memory=MagicMock(),
        patch_engine=PatchEngine(),
    )
    return stage


def _mk_file(content: str) -> Path:
    d = Path(tempfile.mkdtemp())
    f = d / "src.rs"
    f.write_text(content, encoding="utf-8")
    return f


# === 1. Совпавший контекст, простая замена → safe ===
def test_context_matches_and_balanced_safe():
    stage = _make_stage()
    f = _mk_file("fn main() {\n    println!(\"a\");\n}\n")
    # Заменяем "a" на "b" — контекст линий совпадает с файлом
    patch = (
        "--- a/src.rs\n+++ b/src.rs\n"
        "@@ -1,3 +1,3 @@\n"
        " fn main() {\n"
        "-    println!(\"a\");\n"
        "+    println!(\"b\");\n"
        " }\n"
    )
    assert stage._memory_patch_safe(patch, f) is True


# === 2. Файл изменился — контекст НЕ совпадает → unsafe ===
def test_context_mismatch_unsafe():
    stage = _make_stage()
    # Файл уже изменён (println другой) — patch ожидает старое содержимое
    f = _mk_file("fn main() {\n    println!(\"DIFFERENT\");\n}\n")
    patch = (
        "--- a/src.rs\n+++ b/src.rs\n"
        "@@ -1,3 +1,3 @@\n"
        " fn main() {\n"
        "-    println!(\"a\");\n"
        "+    println!(\"b\");\n"
        " }\n"
    )
    assert stage._memory_patch_safe(patch, f) is False


# === 3. Патч ломает баланс { } → unsafe ===
def test_brace_imbalance_unsafe():
    stage = _make_stage()
    f = _mk_file("fn main() {\n    x();\n}\n")
    # Удаляем закрывающий } — баланс ломается
    patch = (
        "--- a/src.rs\n+++ b/src.rs\n"
        "@@ -1,3 +1,3 @@\n"
        " fn main() {\n"
        "     x();\n"
        "-}\n"
        "+\n"
    )
    assert stage._memory_patch_safe(patch, f) is False


# === 4. Пустой патч → unsafe ===
def test_empty_patch_unsafe():
    stage = _make_stage()
    f = _mk_file("fn main() {}\n")
    assert stage._memory_patch_safe("", f) is False
    assert stage._memory_patch_safe("--- a/x\n+++ b/x\n", f) is False  # без ханков


# === 5. Файла не существует → unsafe ===
def test_nonexistent_file_unsafe():
    stage = _make_stage()
    fake = Path(tempfile.mkdtemp()) / "nope.rs"
    patch = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    assert stage._memory_patch_safe(patch, fake) is False


# === 6. Rust-патч в python-файл (контекст совершенно другой) → unsafe ===
def test_cross_language_context_unsafe():
    stage = _make_stage()
    # Файл — python, патч про rust код
    f = Path(tempfile.mkdtemp()) / "x.py"
    f.write_text("def main():\n    print('hi')\n", encoding="utf-8")
    rust_patch = (
        "--- a/x.py\n+++ b/x.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-fn main() {\n"
        "+pub fn main() {\n"
        "     println!(\"hi\");\n"
    )
    assert stage._memory_patch_safe(rust_patch, f) is False


if __name__ == "__main__":
    tests = [
        ("context_matches_and_balanced_safe", test_context_matches_and_balanced_safe),
        ("context_mismatch_unsafe", test_context_mismatch_unsafe),
        ("brace_imbalance_unsafe", test_brace_imbalance_unsafe),
        ("empty_patch_unsafe", test_empty_patch_unsafe),
        ("nonexistent_file_unsafe", test_nonexistent_file_unsafe),
        ("cross_language_context_unsafe", test_cross_language_context_unsafe),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"Memory patch safe (dry-run) regression: {len(tests) - failed}/{len(tests)} pass")
    sys.exit(0 if failed == 0 else 1)
