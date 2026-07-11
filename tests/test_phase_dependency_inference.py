"""
Regress: детерминированный слой инференса зависимостей Rust.

Проверяет analysis/dependency_inference.py:
  * отсев std/core/local-модулей и имён, втянутых через `use std::...`;
  * добавление недостающего крейта из курируемой таблицы;
  * feature-инференс (serde/clap derive, tokio full);
  * пропуск неизвестных крейтов (версию не угадываем);
  * не трогаем уже существующую зависимость / её версию;
  * recover() аддитивен и идемпотентен; комментарии Cargo.toml сохраняются.

Без сети/LLM/cargo (в среде без cargo — применяет без валидации).
Запуск: python3 tests/test_phase_dependency_inference.py
"""

import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.dependency_inference import DependencyInference

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("[OK ] " if cond else "[FAIL] ") + name)


def _proj(files: dict) -> Path:
    d = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


di = DependencyInference()

# --- 1. guess-game: rand отсутствует, std/local отсеяны -------------------
d = _proj({
    "Cargo.toml": "# c\n[package]\nname=\"g\"\nversion=\"0.1.0\"\n\n[dependencies]\n",
    "src/main.rs": "mod game;\nmod player;\nfn main(){ game::run(); }\n",
    "src/game.rs": "use player::Player;\nuse rand::Rng;\nuse std::io;\n"
                   "pub fn run(){ let _=rand::thread_rng(); io::stdin(); }\n",
    "src/player.rs": "pub struct Player;\n",
})
res = di.infer(d)
check("rand определён как недостающий", "rand" in res.missing)
check("rand версия из таблицы (0.8)", res.missing.get("rand") and res.missing["rand"].version == "0.8")
check("локальные модули отсеяны (player)", "player" not in res.used_external)
check("std::io отсеян (shadowed)", "io" not in res.used_external and "io" not in res.skipped_unknown)

# --- 2. feature-инференс serde/clap/tokio ---------------------------------
d2 = _proj({
    "Cargo.toml": "[package]\nname=\"x\"\nversion=\"0.1.0\"\n\n[dependencies]\n",
    "src/main.rs": "use serde::{Serialize, Deserialize};\nuse clap::Parser;\n"
                   "#[derive(Serialize, Deserialize)]\nstruct C;\n"
                   "#[derive(Parser)]\nstruct A;\n#[tokio::main]\nasync fn main(){}\n",
})
r2 = di.infer(d2)
g2 = {k: v.to_toml_line() for k, v in r2.missing.items()}
check("serde с feature derive", g2.get("serde") == 'serde = { version = "1", features = ["derive"] }')
check("clap с feature derive", g2.get("clap") == 'clap = { version = "4", features = ["derive"] }')
check("tokio с feature full", g2.get("tokio") == 'tokio = { version = "1", features = ["full"] }')

# --- 3. неизвестный крейт пропускается ------------------------------------
d3 = _proj({
    "Cargo.toml": "[dependencies]\n",
    "src/main.rs": "use some_obscure_crate::Thing;\nfn main(){ some_obscure_crate::go(); }\n",
})
r3 = di.infer(d3)
check("неизвестный крейт не добавлен", "some_obscure_crate" not in r3.missing)
check("неизвестный крейт в skipped_unknown", "some_obscure_crate" in r3.skipped_unknown)

# --- 4. существующую зависимость не трогаем -------------------------------
d4 = _proj({
    "Cargo.toml": '[dependencies]\nrand = "0.7"  # уже стоит\n',
    "src/main.rs": "use rand::Rng;\nfn main(){ rand::random::<u8>(); }\n",
})
r4 = di.infer(d4)
check("уже существующий rand не в missing", "rand" not in r4.missing)

# --- 5. recover аддитивен, идемпотентен, комментарии целы -----------------
d5 = _proj({
    "Cargo.toml": "# важный коммент\n[package]\nname=\"g\"\nversion=\"0.1.0\"\n\n[dependencies]\n",
    "src/main.rs": "use rand::Rng;\nfn main(){ rand::random::<u8>(); }\n",
})
out1 = di.recover(d5, run_cargo_check=True)   # cargo может отсутствовать → без валидации
ct = (d5 / "Cargo.toml").read_text(encoding="utf-8")
check("recover добавил rand", 'rand = "0.8"' in ct)
check("recover сохранил комментарий", "# важный коммент" in ct)
out2 = di.recover(d5, run_cargo_check=True)
check("recover идемпотентен (повтор ничего не меняет)", not out2.has_changes)

# --- 6. CrateSpec рендер ---------------------------------------------------
from analysis.dependency_inference import CrateSpec
check("CrateSpec без features", CrateSpec("x", "1").to_toml_line() == 'x = "1"')
check("CrateSpec с features",
      CrateSpec("x", "1", ["a", "b"]).to_toml_line() == 'x = { version = "1", features = ["a", "b"] }')

passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"\nDependency inference regression: {passed}/{total} pass")
sys.exit(0 if passed == total else 1)
