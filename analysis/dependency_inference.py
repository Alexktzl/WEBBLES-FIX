"""
Детерминированный слой инференса зависимостей Rust (без LLM, без сети).

Назначение: ДО основного auto-fix пайплайна найти крейты, которые код реально
использует, но которых нет в `Cargo.toml`, и добавить их детерминированно. Это
снимает целый каскад ошибок (E0432 `unresolved import` / E0433 `cannot find
crate` → следом E0425 `cannot find value` и т.п.), которые иначе LLM пытается
чинить по одной и галлюцинирует не те крейты.

Принципы (жёсткие ограничения, согласованы с пользователем):
  * ДЕТЕРМИНИРОВАННО / ВОСПРОИЗВОДИМО / БЫСТРО — regex-скан + курируемая таблица.
  * НЕ LLM, НЕ network, НЕ crates.io API, НЕ угадывание версий.
  * Версии и features берутся ТОЛЬКО из курируемой таблицы `KNOWN_CRATES`.
  * Неизвестный внешний крейт (нет в таблице) — ПРОПУСКАЕМ (оставляем пайплайну
    / на review), не добавляем с выдуманной версией.
  * Правка `Cargo.toml` не ломает форматирование/комментарии и НЕ трогает уже
    существующие версии (tomlkit, с аккуратным текстовым fallback).

Honest-ограничение: regex-уровень (как `security_scanner`/`symbol_graph`) — это
MVP. Не полноценный AST (`syn`). Ловит обычные `use`/`path::`/derive/attr; не
разбирает макро-сахар и сложные пере-экспорты.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Корни путей, которые НЕ являются внешними крейтами.
_NON_CRATE_ROOTS: Set[str] = {
    "crate", "self", "super", "std", "core", "alloc", "Self",
}

# Служебные каталоги, которые не сканируем.
_SKIP_DIRS = {"target", "node_modules", ".git", ".webbles_fix", ".webbles",
              ".webbles_backups", "dist", "build"}

# Встроенные атрибуты Rust — не крейты.
_BUILTIN_ATTRS: Set[str] = {
    "derive", "cfg", "cfg_attr", "allow", "warn", "deny", "forbid", "test",
    "ignore", "should_panic", "inline", "repr", "doc", "non_exhaustive",
    "must_use", "deprecated", "macro_use", "macro_export", "path", "no_std",
    "no_mangle", "used", "link", "global_allocator", "panic_handler",
    "automatically_derived", "track_caller", "cold", "proc_macro",
    "proc_macro_derive", "proc_macro_attribute",
}

_MAX_FILES = 2000
_MAX_FILE_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Курируемая таблица крейтов: версия + дефолтные features.
# Версии — мажор-консервативные строки (как пишут в Cargo.toml вручную).
# features здесь — базовые; динамические features добавляет feature-инференс.
# ---------------------------------------------------------------------------
KNOWN_CRATES: Dict[str, Dict[str, object]] = {
    "serde":        {"version": "1",     "features": []},
    "serde_json":   {"version": "1",     "features": []},
    "serde_yaml":   {"version": "0.9",   "features": []},
    "tokio":        {"version": "1",     "features": ["full"]},
    "clap":         {"version": "4",     "features": []},
    "anyhow":       {"version": "1",     "features": []},
    "thiserror":    {"version": "1",     "features": []},
    "rand":         {"version": "0.8",   "features": []},
    "regex":        {"version": "1",     "features": []},
    "log":          {"version": "0.4",   "features": []},
    "env_logger":   {"version": "0.11",  "features": []},
    "tracing":      {"version": "0.1",   "features": []},
    "tracing_subscriber": {"version": "0.3", "features": []},
    "chrono":       {"version": "0.4",   "features": []},
    "reqwest":      {"version": "0.11",  "features": []},
    "futures":      {"version": "0.3",   "features": []},
    "lazy_static":  {"version": "1",     "features": []},
    "once_cell":    {"version": "1",     "features": []},
    "itertools":    {"version": "0.12",  "features": []},
    "uuid":         {"version": "1",     "features": []},
    "bytes":        {"version": "1",     "features": []},
    "async_trait":  {"version": "0.1",   "features": []},
    "num_cpus":     {"version": "1",     "features": []},
    "rayon":        {"version": "1",     "features": []},
}

# Crate-имя в коде использует '_', а в Cargo.toml — может быть с '-'.
# Сопоставляем по нормализованному виду (заменяем '-' на '_').
def _norm(name: str) -> str:
    return name.replace("-", "_")


# derive-трейт → крейт + feature, который он включает.
DERIVE_TO_CRATE: Dict[str, Tuple[str, Optional[str]]] = {
    "Serialize":     ("serde", "derive"),
    "Deserialize":   ("serde", "derive"),
    "Parser":        ("clap", "derive"),
    "Subcommand":    ("clap", "derive"),
    "Args":          ("clap", "derive"),
    "ValueEnum":     ("clap", "derive"),
    "Error":         ("thiserror", None),  # #[derive(Error)] из thiserror
}

# attribute-путь (`#[serde(...)]`, `#[tokio::main]`) → крейт + feature.
ATTR_TO_CRATE: Dict[str, Tuple[str, Optional[str]]] = {
    "serde":  ("serde", None),
    "tokio":  ("tokio", None),
    "clap":   ("clap", "derive"),
    "async_trait": ("async_trait", None),
}


@dataclass
class CrateSpec:
    """Что добавить в [dependencies] для одного крейта."""
    name: str
    version: str
    features: List[str] = field(default_factory=list)

    def to_toml_value(self) -> str:
        """Правая часть `name = <...>` для Cargo.toml."""
        if self.features:
            feats = ", ".join(f'"{f}"' for f in self.features)
            return f'{{ version = "{self.version}", features = [{feats}] }}'
        return f'"{self.version}"'

    def to_toml_line(self) -> str:
        return f"{self.name} = {self.to_toml_value()}"


@dataclass
class InferenceResult:
    used_external: Set[str] = field(default_factory=set)
    local_modules: Set[str] = field(default_factory=set)
    existing_deps: Set[str] = field(default_factory=set)
    missing: Dict[str, CrateSpec] = field(default_factory=dict)
    skipped_unknown: Set[str] = field(default_factory=set)
    explanations: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.missing)


# ---------------------------------------------------------------------------
# Регулярки скана
# ---------------------------------------------------------------------------
_RE_USE = re.compile(r"^\s*(?:pub\s+(?:\([^)]*\)\s+)?)?use\s+(?:::)?([A-Za-z_][A-Za-z0-9_]*)")
_RE_MOD = re.compile(r"^\s*(?:pub\s+(?:\([^)]*\)\s+)?)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*[;{]")
_RE_EXTERN_CRATE = re.compile(r"^\s*(?:pub\s+)?extern\s+crate\s+([A-Za-z_][A-Za-z0-9_]*)")
# Путь в коде: первый сегмент перед '::' начинается со строчной буквы (крейты
# snake_case). Type/enum пути (Player::new) начинаются с заглавной — отсекаются.
_RE_PATH = re.compile(r"\b([a-z_][a-z0-9_]*)::")
_RE_DERIVE = re.compile(r"#\[\s*derive\s*\(([^)]*)\)\s*\]")
_RE_ATTR = re.compile(r"#\[\s*([a-z_][a-z0-9_]*)\b")


def _strip_line_comment(line: str) -> str:
    """Грубо отрезает '//'-комментарий (MVP, как в security_scanner). Строки
    внутри — не разбираем; для инференса крейтов это приемлемо."""
    idx = line.find("//")
    return line[:idx] if idx >= 0 else line


class DependencyInference:
    """Детерминированный инференс отсутствующих Rust-зависимостей."""

    def __init__(self):
        self.known = KNOWN_CRATES

    # ---- скан исходников -------------------------------------------------
    def _iter_rs_files(self, root: Path):
        count = 0
        for path in sorted(root.rglob("*.rs")):
            if count >= _MAX_FILES:
                break
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            count += 1
            yield path, text

    def _local_modules(self, root: Path) -> Set[str]:
        """Локальные модули проекта: объявленные `mod X` + имена файлов/каталогов
        под src (минус main/lib/mod)."""
        local: Set[str] = set()
        src = root / "src"
        base = src if src.is_dir() else root
        for path in base.rglob("*.rs"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            stem = path.stem
            if stem not in ("main", "lib", "mod"):
                local.add(stem)
            # каталог с mod.rs → имя каталога локальный модуль
            if stem == "mod":
                local.add(path.parent.name)
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    m = _RE_MOD.match(line)
                    if m:
                        local.add(m.group(1))
            except OSError:
                continue
        return local

    def _scan(self, root: Path):
        """Возвращает (used_roots, derives, attr_paths, shadowed).

        `shadowed` — имена, втянутые в скоуп через `use std::X` / `use crate::X`
        и т.п. (root ∈ non-crate). Их `X::` НЕ должен считаться крейтом — иначе
        `use std::time; time::now()` ошибочно добавил бы крейт `time`."""
        used: Set[str] = set()
        derives: Set[str] = set()
        attr_crates: Set[str] = set()
        shadowed: Set[str] = set()
        for _path, text in self._iter_rs_files(root):
            for raw in text.splitlines():
                line = _strip_line_comment(raw)
                if not line.strip():
                    continue
                m = _RE_USE.match(line)
                if m:
                    first = m.group(1)
                    used.add(first)
                    # Если use идёт от не-крейтового корня, все идентификаторы
                    # пути попадают в скоуп как НЕ крейты.
                    if first in _NON_CRATE_ROOTS:
                        body = line.split("use", 1)[1].split(";", 1)[0]
                        for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body):
                            shadowed.add(ident)
                m = _RE_EXTERN_CRATE.match(line)
                if m:
                    used.add(m.group(1))
                for pm in _RE_PATH.finditer(line):
                    used.add(pm.group(1))
                for dm in _RE_DERIVE.finditer(line):
                    for name in dm.group(1).split(","):
                        name = name.strip()
                        if name:
                            derives.add(name)
                for am in _RE_ATTR.finditer(line):
                    attr = am.group(1)
                    if attr not in _BUILTIN_ATTRS:
                        attr_crates.add(attr)
        # корни (std/...) сами по себе из shadowed убираем — они и так
        # отфильтруются _NON_CRATE_ROOTS, но leaf-имена важны.
        shadowed -= _NON_CRATE_ROOTS
        return used, derives, attr_crates, shadowed

    # ---- Cargo.toml ------------------------------------------------------
    @staticmethod
    def _existing_deps(cargo_text: str) -> Set[str]:
        """Имена зависимостей из [dependencies] / [dev-dependencies] /
        [build-dependencies] (нормализованные '-'→'_'). Грубый TOML-скан —
        достаточно знать, что крейт уже объявлен."""
        deps: Set[str] = set()
        in_deps = False
        for raw in cargo_text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                in_deps = section.endswith("dependencies")
                continue
            if not in_deps or not line or line.startswith("#"):
                continue
            # `name = ...` или `name.workspace = true`
            m = re.match(r'([A-Za-z0-9_\-]+)\s*[.=]', line)
            if m:
                deps.add(_norm(m.group(1)))
        return deps

    # ---- инференс --------------------------------------------------------
    def infer(self, root) -> InferenceResult:
        root = Path(root)
        res = InferenceResult()
        try:
            used, derives, attr_crates, shadowed = self._scan(root)
            local = self._local_modules(root)
            res.local_modules = local

            cargo = root / "Cargo.toml"
            cargo_text = ""
            if cargo.is_file():
                try:
                    cargo_text = cargo.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    cargo_text = ""
            res.existing_deps = self._existing_deps(cargo_text)

            # Кандидаты во внешние крейты.
            candidates: Set[str] = set()
            for u in used:
                if u in _NON_CRATE_ROOTS:
                    continue
                if u in local:
                    continue
                if u in shadowed:
                    continue  # имя втянуто через use std::... — не крейт
                candidates.add(u)
            # из derive и attr добавляем целевые крейты явно
            for d in derives:
                hit = DERIVE_TO_CRATE.get(d)
                if hit:
                    candidates.add(hit[0])
            for a in attr_crates:
                hit = ATTR_TO_CRATE.get(a)
                if hit:
                    candidates.add(hit[0])
                elif _norm(a) in self.known:
                    candidates.add(_norm(a))

            res.used_external = {c for c in candidates}

            # Чего нет в Cargo.toml.
            for crate in sorted(candidates):
                ncrate = _norm(crate)
                if ncrate in res.existing_deps:
                    continue
                if ncrate not in self.known:
                    res.skipped_unknown.add(crate)
                    continue
                spec = self._build_spec(ncrate, derives, attr_crates, used)
                res.missing[ncrate] = spec
                res.explanations.append(
                    f"{ncrate}: используется в коде, нет в Cargo.toml → "
                    f"добавляю `{spec.to_toml_line()}`"
                )
            for sk in sorted(res.skipped_unknown):
                res.explanations.append(
                    f"{sk}: внешний крейт не в курируемой таблице → пропущен "
                    f"(оставлен пайплайну/review, версию не угадываем)"
                )
        except Exception as e:  # инференс никогда не роняет пайплайн
            logger.warning("DependencyInference.infer упал: %s", e)
        return res

    def _build_spec(self, ncrate: str, derives: Set[str],
                    attr_crates: Set[str], used: Set[str]) -> CrateSpec:
        meta = self.known[ncrate]
        version = str(meta.get("version", "*"))
        features = list(meta.get("features", []) or [])

        # Feature-инференс по детерминированным правилам.
        # serde: derive-трейты Serialize/Deserialize → feature "derive".
        if ncrate == "serde":
            if {"Serialize", "Deserialize"} & derives:
                if "derive" not in features:
                    features.append("derive")
        # clap: #[derive(Parser/...)] → "derive".
        if ncrate == "clap":
            if {"Parser", "Subcommand", "Args", "ValueEnum"} & derives:
                if "derive" not in features:
                    features.append("derive")
        # tokio: базово "full" (покрывает macros+rt) уже в таблице.
        return CrateSpec(name=ncrate, version=version, features=features)

    # ---- правка Cargo.toml ----------------------------------------------
    def apply_to_cargo_toml(self, cargo_path, missing: Dict[str, CrateSpec]) -> bool:
        """Добавляет недостающие зависимости в [dependencies]. Не трогает
        существующие версии/комментарии. tomlkit → точно; текстовый fallback —
        аккуратная вставка под [dependencies]. Возвращает True при изменении."""
        cargo_path = Path(cargo_path)
        if not missing or not cargo_path.is_file():
            return False
        try:
            original = cargo_path.read_text(encoding="utf-8")
        except OSError:
            return False

        new_text = self._apply_tomlkit(original, missing)
        if new_text is None:
            new_text = self._apply_text(original, missing)
        if new_text is None or new_text == original:
            return False
        try:
            cargo_path.write_text(new_text, encoding="utf-8", newline="")
            return True
        except OSError as e:
            logger.warning("DependencyInference: запись Cargo.toml упала: %s", e)
            return False

    def _apply_tomlkit(self, original: str, missing: Dict[str, CrateSpec]) -> Optional[str]:
        try:
            import tomlkit
            from tomlkit import inline_table
        except Exception:
            return None
        try:
            doc = tomlkit.parse(original)
            deps = doc.get("dependencies")
            if deps is None:
                deps = tomlkit.table()
                doc["dependencies"] = deps
            for ncrate, spec in missing.items():
                # уже есть (на всякий случай) — не трогаем
                if spec.name in deps:
                    continue
                if spec.features:
                    it = inline_table()
                    it["version"] = spec.version
                    it["features"] = spec.features
                    deps[spec.name] = it
                else:
                    deps[spec.name] = spec.version
            return tomlkit.dumps(doc)
        except Exception as e:
            logger.debug("DependencyInference: tomlkit-путь не сработал: %s", e)
            return None

    def _apply_text(self, original: str, missing: Dict[str, CrateSpec]) -> Optional[str]:
        """Текстовый fallback: вставить строки под существующий [dependencies]
        или добавить секцию в конец. Комментарии/версии не трогаем."""
        lines = original.splitlines(keepends=True)
        add_lines = [spec.to_toml_line() + "\n" for spec in missing.values()]
        # ищем заголовок [dependencies]
        dep_idx = None
        for i, ln in enumerate(lines):
            if ln.strip() == "[dependencies]":
                dep_idx = i
                break
        if dep_idx is None:
            # секции нет — добавляем в конец
            tail = "" if (not lines or lines[-1].endswith("\n")) else "\n"
            block = tail + "\n[dependencies]\n" + "".join(add_lines)
            return original + block
        # вставляем сразу после всех уже идущих строк секции (перед следующей [..]
        # или концом файла), чтобы не разрывать существующие записи/комментарии.
        insert_at = len(lines)
        for j in range(dep_idx + 1, len(lines)):
            if lines[j].lstrip().startswith("[") and lines[j].strip().endswith("]"):
                insert_at = j
                break
        # подложим перевод строки, если предыдущая строка без него
        prefix = ""
        if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
            prefix = "\n"
        new_lines = lines[:insert_at] + [prefix] + add_lines + lines[insert_at:]
        return "".join(new_lines)

    # ---- оркестрация пред-пайплайн фазы ---------------------------------
    @staticmethod
    def _cargo_error_count(root: Path) -> Optional[int]:
        """Число ошибок компиляции по `cargo check`. None — если cargo нет /
        таймаут / сбой (тогда валидацию пропускаем, не угадываем)."""
        if not shutil.which("cargo"):
            return None
        try:
            proc = subprocess.run(
                ["cargo", "check", "--message-format=short"],
                cwd=str(root), capture_output=True, text=True, timeout=180,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("DependencyInference: cargo check не запустился: %s", e)
            return None
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        # строки вида `error[E0432]: ...` или `error: ...`
        return sum(1 for ln in out.splitlines()
                   if re.match(r"\s*error(\[E\d+\])?:", ln))

    def recover(self, root, run_cargo_check: bool = True) -> InferenceResult:
        """Пред-пайплайн фаза: добавить отсутствующие зависимости в Cargo.toml.

        Порядок (как в задумке): [cargo check] → инференс+правка → [cargo check].
        Изменение АДДИТИВНОЕ (только добавляем недостающие крейты из таблицы).
        Если cargo доступен и после правки ошибок стало БОЛЬШЕ — откатываем
        (значит версия из таблицы не подошла). Если cargo нет — применяем без
        валидации (детерминированно и аддитивно), это honest-ограничение.

        Никогда не роняет пайплайн (всё в try). Возвращает InferenceResult;
        `explanations` дополняется итогом ACCEPT/ROLLBACK.
        """
        root = Path(root)
        res = self.infer(root)
        if not res.has_changes:
            return res
        cargo_path = root / "Cargo.toml"
        if not cargo_path.is_file():
            res.explanations.append("Cargo.toml не найден — пропуск dependency recovery")
            res.missing = {}
            return res
        try:
            backup = cargo_path.read_text(encoding="utf-8")
        except OSError:
            return res

        before = self._cargo_error_count(root) if run_cargo_check else None

        if not self.apply_to_cargo_toml(cargo_path, res.missing):
            res.explanations.append("Правка Cargo.toml не применилась (нет изменений)")
            res.missing = {}
            return res

        if run_cargo_check and before is not None:
            after = self._cargo_error_count(root)
            if after is not None and after > before:
                # стало хуже — откатываем (версия из таблицы не подошла)
                try:
                    cargo_path.write_text(backup, encoding="utf-8", newline="")
                except OSError:
                    pass
                res.explanations.append(
                    f"ROLLBACK: после правки ошибок стало больше ({before}→{after}) — откат Cargo.toml"
                )
                res.missing = {}
                return res
            res.explanations.append(
                f"ACCEPT: добавлено {len(res.missing)} зав-тей; ошибок {before}→{after}"
            )
        else:
            res.explanations.append(
                f"ACCEPT (без cargo-валидации): добавлено {len(res.missing)} зав-тей: "
                + ", ".join(res.missing.keys())
            )
        return res
