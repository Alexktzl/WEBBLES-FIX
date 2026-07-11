"""
Regress: применение МНОГО-ФАЙЛОВОГО unified diff.

Баг (до фикса): PatchEngine.apply_patch собирал ханки ВСЕХ файлов из патча и
применял их к одному целевому файлу (по номерам строк целевого). Кросс-файловый
контекст генерит много-файловый diff (например game.rs + Cargo.toml), и ханки
Cargo.toml уезжали в game.rs — `[package]` оказывался в .rs, сигнатуры функций
ломались, структурный анкор откатывал патч. Фикс: apply_patch берёт ТОЛЬКО
секцию своего файла (по `+++ b/<path>`), а ApplyPatchStage применяет каждый файл
по своему адресу.

Без сети/LLM. Запуск: python3 tests/test_phase_multifile_apply.py
"""

import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fixers.patch_engine import PatchEngine
from fixers.structured_edit import EditSet, Edit, Anchor

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("[OK ] " if cond else "[FAIL] ") + name)


def _mk_project():
    d = Path(tempfile.mkdtemp())
    (d / "src").mkdir()
    (d / "src" / "game.rs").write_text(
        "// src/game.rs\n"
        "use rand::Rng;\n"
        "\n"
        "pub fn run() {\n"
        "    let secret = rand::thread_rng().gen_range(1..=100);\n"
        "    let mut player = Player::new(\"p\".to_string());\n"
        "    player.attempts += 1;\n"
        "}\n",
        encoding="utf-8",
    )
    (d / "Cargo.toml").write_text(
        "# Cargo.toml\n[package]\nname = \"g\"\nversion = \"0.1.0\"\nedition = \"2021\"\n\n[dependencies]\n",
        encoding="utf-8",
    )
    return d


def _multifile_diff(d):
    fc = {
        "src/game.rs": (d / "src" / "game.rs").read_text(),
        "Cargo.toml": (d / "Cargo.toml").read_text(),
    }
    es = EditSet(
        intent="fix field + add dep",
        edits=[
            Edit(file="src/game.rs", anchor=Anchor(line=7, match="player.attempts"),
                 kind="replace", new="    player.attempt += 1;", rationale=""),
            Edit(file="Cargo.toml", anchor=Anchor(line=7, match="[dependencies]"),
                 kind="insert_after", new='rand = "0.8"', rationale=""),
        ],
        risks=[], confidence=0.9,
    )
    return es.to_unified_diff(fc), fc


# --- files_in_patch ---
d = _mk_project()
diff, fc = _multifile_diff(d)
check("files_in_patch находит обе секции",
      PatchEngine.files_in_patch(diff) == ["src/game.rs", "Cargo.toml"])

# --- _parse_patch фильтрует по целевому файлу ---
pe = PatchEngine()
g_hunks = pe._parse_patch(diff, target_file=str(d / "src" / "game.rs"))
c_hunks = pe._parse_patch(diff, target_file=str(d / "Cargo.toml"))
check("game.rs получает ровно 1 ханк (свой)", len(g_hunks) == 1)
check("Cargo.toml получает ровно 1 ханк (свой)", len(c_hunks) == 1)
check("без target — оба ханка (обратная совместимость)",
      len(pe._parse_patch(diff)) == 2)

# --- применение по адресам ---
for rel in PatchEngine.files_in_patch(diff):
    pe.apply_patch(d / rel, diff)
g = (d / "src" / "game.rs").read_text()
c = (d / "Cargo.toml").read_text()
check("fn run() сохранена в game.rs", "pub fn run()" in g)
check("поле attempt исправлено в game.rs", "player.attempt += 1;" in g)
check("[package] НЕ протёк в game.rs", "[package]" not in c.join("") or "[package]" not in g)
check("game.rs: баланс { } сохранён", g.count("{") == g.count("}"))
check("rand добавлен в Cargo.toml", 'rand = "0.8"' in c)
check("rust-код НЕ протёк в Cargo.toml", "pub fn run" not in c)

# --- обратная совместимость: одно-файловый патч ---
d2 = Path(tempfile.mkdtemp())
f = d2 / "a.rs"
f.write_text("l1\nold\nl3\n", encoding="utf-8")
single = "--- a/a.rs\n+++ b/a.rs\n@@ -2,1 +2,1 @@\n-old\n+new\n"
check("одно-файловый патч применяется", pe.apply_patch(f, single) and f.read_text() == "l1\nnew\nl3\n")

# --- патч без файловых хедеров (legacy) применяется ---
f2 = d2 / "b.rs"
f2.write_text("x\nold2\nz\n", encoding="utf-8")
headerless = "@@ -2,1 +2,1 @@\n-old2\n+new2\n"
check("headerless патч применяется", pe.apply_patch(f2, headerless) and f2.read_text() == "x\nnew2\nz\n")

# --- патч только для чужого файла не трогает целевой ---
f3 = d2 / "c.rs"
f3.write_text("keep\n", encoding="utf-8")
foreign = "--- a/other.rs\n+++ b/other.rs\n@@ -1,1 +1,1 @@\n-keep\n+changed\n"
before = f3.read_text()
res = pe.apply_patch(f3, foreign)
check("патч чужого файла отклонён, цель цела", (res is False) and f3.read_text() == before)

passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"\nMulti-file apply regression: {passed}/{total} pass")
sys.exit(0 if passed == total else 1)
