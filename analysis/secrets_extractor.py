"""
Stage R — детерминированный экстрактор захардкоженных секретов.

Принципы (жёсткие ограничения):
  * БЕЗ LLM ни на одном шаге — только regex + прямая обработка строк.
  * Значения секретов НИКОГДА не попадают в логи или event-payload.
  * EditSet используется для замены строк в исходниках.
  * Результат: secrets.txt в корне проекта (dotenv-формат), env-lookups в коде.

Поддерживаемые языки: python, js/ts, rust.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex-паттерны (модульный уровень)
# ---------------------------------------------------------------------------

# Имена-маркеры секрета — зеркало security_scanner._SECRET_NAME
_SECRET_NAME = re.compile(
    r"(secret|passwd|password|pwd|token|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|auth[_-]?token|client[_-]?secret)",
    re.IGNORECASE,
)

# Признаки, что значение уже читается из окружения — такие строки не трогаем
_ENV_READ = re.compile(
    r"(os\.environ|os\.getenv|getenv|process\.env|std::env::var|"
    r"dotenv|environ\[)",
    re.IGNORECASE,
)

# Python: NAME = "value" или NAME = 'value'
# f-строки (SECRET = f"...") не совпадают: перед кавычкой стоит 'f' — паттерн
# требует ['"] сразу после =\s*, поэтому SECRET = f"..." не матчится.
_PY_ASSIGN = re.compile(
    r"""^\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*=\s*(['"])((?:(?!\2).){6,})\2"""
)

# JS/TS: const/let/var NAME = "..." или NAME = "..."
_JS_ASSIGN = re.compile(
    r"""(?:(?:const|let|var)\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(['"`])((?:(?!\2).){6,})\2"""
)

# Rust: let [mut] NAME [: Type] = "..." или const NAME: Type = "..."
_RUST_ASSIGN = re.compile(
    r'(?:let\s+(?:mut\s+)?|const\s+)([A-Za-z_][A-Za-z0-9_]*)'
    r'\s*(?::\s*[^\s=]+)?\s*=\s*"([^"]{4,})"'
)

# Расширение файла → язык
_EXT_LANG: Dict[str, str] = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".ts": "ts",
    ".tsx": "ts",
    ".rs": "rust",
}

# Каталоги, которые не сканируем
_SKIP_DIRS: Set[str] = {
    "node_modules", ".git", "venv", ".venv", "target",
    "__pycache__", ".webbles_backups", ".webbles",
    "dist", "build", ".tox", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".idea", ".vscode",
}

# Файлы, которые сами являются хранилищами секретов — не сканируем как исходник
_SKIP_FILENAMES: Set[str] = {
    "secrets.txt", "secrets.json", "secrets.yaml", "secrets.yml",
    ".env", ".env.local", ".env.development", ".env.production",
    ".env.test", ".envrc",
}


# ---------------------------------------------------------------------------
# Dataclass'ы
# ---------------------------------------------------------------------------

@dataclass
class SecretMatch:
    """Одно найденное захардкоженное значение."""

    name: str       # имя переменной, e.g. "SECRET_KEY"
    value: str      # само значение — ТОЛЬКО в памяти, НИКОГДА в логи
    line: int       # 1-based номер строки
    file: str       # относительный путь к файлу
    language: str   # "python" | "js" | "ts" | "rust"
    kind: str       # "assignment"

    def __repr__(self) -> str:
        # Значение намеренно скрыто — защита от случайного попадания в логи
        return (
            f"SecretMatch(name={self.name!r}, value='***', "
            f"line={self.line}, file={self.file!r}, language={self.language!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass
class ApplyResult:
    """Итог применения экстракции."""

    count: int
    names: List[str]           # только имена — значений нет
    files_modified: List[str]
    secrets_file: str
    gitignore_updated: bool
    dry_run: bool


# ---------------------------------------------------------------------------
# Внутренние вспомогательные функции
# ---------------------------------------------------------------------------

def _detect_language(suffix: str) -> Optional[str]:
    return _EXT_LANG.get(suffix.lower())


def _is_comment_line(line: str, language: str) -> bool:
    stripped = line.strip()
    if language == "python":
        return stripped.startswith("#")
    if language in ("js", "ts"):
        return stripped.startswith("//") or stripped.startswith("/*")
    if language == "rust":
        return stripped.startswith("//") or stripped.startswith("/*")
    return False


def _make_replacement(match: SecretMatch, orig_line: str) -> str:
    """Формирует строку-замену для данного языка."""
    indent = orig_line[: len(orig_line) - len(orig_line.lstrip())]

    if match.language == "python":
        return f'{indent}{match.name} = os.environ["{match.name}"]\n'

    if match.language in ("js", "ts"):
        # Сохраняем const/let/var-префикс если он есть
        m = re.match(r"\s*(const|let|var)\s+", orig_line)
        keyword = (m.group(1) + " ") if m else ""
        return f"{indent}{keyword}{match.name} = process.env.{match.name};\n"

    if match.language == "rust":
        # Сохраняем let/const-префикс
        m = re.match(r"\s*(let\s+(?:mut\s+)?|const\s+)", orig_line)
        prefix = m.group(1) if m else "let "
        return (
            f'{indent}{prefix}{match.name} = '
            f'std::env::var("{match.name}").expect("{match.name} not set");\n'
        )

    return orig_line


def _has_import_os(content: str) -> bool:
    return bool(re.search(
        r"^\s*import\s+os(\s*,\s*\w+)*\s*(?:as\s+\w+)?\s*(?:#.*)?$",
        content, re.MULTILINE,
    ))


def _inject_import_os(content: str) -> str:
    """Вставляет `import os` после последнего `import` в шапке файла."""
    lines = content.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) or stripped == "":
            insert_at = i + 1
        elif stripped and not stripped.startswith("#"):
            # Первая значимая не-import строка — вставляем прямо здесь
            insert_at = i
            break
    lines.insert(insert_at, "import os\n")
    return "".join(lines)


def _merge_secrets_file(
    secrets_path: Path,
    matches: List[SecretMatch],
    *,
    dry_run: bool,
) -> None:
    existing: Dict[str, str] = {}
    if secrets_path.exists():
        for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if "=" in raw_line and not raw_line.startswith("#"):
                k, _, v = raw_line.partition("=")
                existing[k.strip()] = v.strip()

    for m in matches:
        existing[m.name] = m.value   # значения только в файл, не в логи

    content = "\n".join(f"{k}={v}" for k, v in sorted(existing.items())) + "\n"
    if not dry_run:
        secrets_path.write_text(content, encoding="utf-8")


def _update_gitignore(gitignore_path: Path, *, dry_run: bool) -> bool:
    entries_needed = ["secrets.txt", ".env"]

    if gitignore_path.exists():
        existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        existing_patterns = {ln.strip() for ln in existing_lines}
    else:
        existing_lines = []
        existing_patterns = set()

    to_add = [e for e in entries_needed if e not in existing_patterns]
    if not to_add:
        return False

    if not dry_run:
        text = "\n".join(existing_lines)
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join(to_add) + "\n"
        gitignore_path.write_text(text, encoding="utf-8")

    return True


def _iter_source_files(
    project_root: Path,
    skip_dirs: Set[str],
    skip_filenames: Set[str],
) -> List[Path]:
    results: List[Path] = []
    for p in project_root.rglob("*"):
        if not p.is_file():
            continue
        # Пропускаем вложенные запрещённые каталоги
        if any(part in skip_dirs for part in p.parts):
            continue
        # Пропускаем файлы-хранилища секретов
        if p.name in skip_filenames:
            continue
        # Пропускаем файлы без поддерживаемого расширения
        if p.suffix.lower() not in _EXT_LANG:
            continue
        results.append(p)
    return results


# ---------------------------------------------------------------------------
# SecretsExtractor
# ---------------------------------------------------------------------------

class SecretsExtractor:
    """Детерминированный экстрактор захардкоженных секретов без LLM."""

    def scan(
        self,
        rel_path: str,
        content: str,
        language: str,
    ) -> List[SecretMatch]:
        """Сканирует содержимое файла и возвращает список найденных секретов."""
        matches: List[SecretMatch] = []
        lang = language.lower()

        for lineno, line in enumerate(content.splitlines(), 1):
            # Пропускаем комментарии
            if _is_comment_line(line, lang):
                continue

            # Пропускаем строки с уже готовым env-lookup
            if _ENV_READ.search(line):
                continue

            m = None
            value = ""
            name = ""

            if lang == "python":
                m = _PY_ASSIGN.match(line)
                if m:
                    name, value = m.group(1), m.group(3)

            elif lang in ("js", "ts"):
                m = _JS_ASSIGN.match(line)
                if m:
                    name, value = m.group(1), m.group(3)

            elif lang == "rust":
                m = _RUST_ASSIGN.search(line)
                if m:
                    name, value = m.group(1), m.group(2)

            if not m:
                continue
            if not _SECRET_NAME.search(name):
                continue

            matches.append(SecretMatch(
                name=name,
                value=value,
                line=lineno,
                file=rel_path,
                language=lang,
                kind="assignment",
            ))

        return matches

    def apply(
        self,
        matches: List[SecretMatch],
        project_root: Path,
        *,
        dry_run: bool = False,
    ) -> ApplyResult:
        """Применяет извлечение: заменяет литералы в коде, пишет secrets.txt."""
        from fixers.structured_edit import Anchor, Edit, EditSet

        # Группируем совпадения по файлу
        by_file: Dict[str, List[SecretMatch]] = {}
        for m in matches:
            by_file.setdefault(m.file, []).append(m)

        files_modified: List[str] = []

        for rel_path, file_matches in by_file.items():
            abs_path = project_root / rel_path
            if not abs_path.exists():
                logger.warning("secrets_extractor: файл не найден: %s", rel_path)
                continue

            try:
                original_content = abs_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("secrets_extractor: не удалось прочитать %s: %s", rel_path, e)
                continue

            orig_lines = original_content.splitlines(keepends=True)

            # Строим EditSet — только replace-правки (без import os)
            edits: List[Edit] = []
            for match in file_matches:
                if match.line < 1 or match.line > len(orig_lines):
                    logger.warning(
                        "secrets_extractor: номер строки %d вне диапазона для %s",
                        match.line, rel_path,
                    )
                    continue
                orig_line = orig_lines[match.line - 1]
                replacement = _make_replacement(match, orig_line)
                edits.append(Edit(
                    file=rel_path,
                    anchor=Anchor(line=match.line, match=orig_line.rstrip()),
                    kind="replace",
                    new=replacement,
                    rationale=f"extract {match.name} to environment variable",
                ))

            if not edits:
                continue

            edit_set = EditSet(
                intent=f"extract hardcoded secrets ({len(edits)} replacement(s))",
                edits=edits,
                confidence=1.0,
                risks=["startup fails if env var is not set"],
            )

            result_map = edit_set.apply({rel_path: original_content})
            if result_map is None or rel_path not in result_map:
                logger.warning(
                    "secrets_extractor: EditSet.apply не применился для %s — пропускаем",
                    rel_path,
                )
                continue

            new_content = result_map[rel_path]

            # Пост-процессинг Python: inject `import os` один раз если нужно
            language = file_matches[0].language
            if language == "python" and not _has_import_os(new_content):
                new_content = _inject_import_os(new_content)

            if not dry_run:
                try:
                    abs_path.write_text(new_content, encoding="utf-8")
                except Exception as e:
                    logger.warning("secrets_extractor: не удалось записать %s: %s", rel_path, e)
                    continue

            files_modified.append(rel_path)
            logger.info(
                "secrets_extractor: %s — заменено %d секрет(ов): %s",
                rel_path,
                len(file_matches),
                ", ".join(m.name for m in file_matches),   # имена — не значения
            )

        # Записываем/мержим secrets.txt
        secrets_path = project_root / "secrets.txt"
        _merge_secrets_file(secrets_path, matches, dry_run=dry_run)

        # Обновляем .gitignore
        gitignore_path = project_root / ".gitignore"
        gitignore_updated = _update_gitignore(gitignore_path, dry_run=dry_run)

        return ApplyResult(
            count=len(matches),
            names=[m.name for m in matches],    # имена только
            files_modified=files_modified,
            secrets_file=str(secrets_path),
            gitignore_updated=gitignore_updated,
            dry_run=dry_run,
        )

    def scan_and_apply(
        self,
        project_root: Path,
        *,
        dry_run: bool = False,
        language: Optional[str] = None,
    ) -> ApplyResult:
        """Сканирует все исходники в project_root и применяет замены."""
        project_root = Path(project_root)
        all_matches: List[SecretMatch] = []

        for src_file in _iter_source_files(project_root, _SKIP_DIRS, _SKIP_FILENAMES):
            # Фильтр по языку если задан
            detected_lang = _detect_language(src_file.suffix)
            if detected_lang is None:
                continue
            if language is not None and detected_lang != language.lower():
                continue

            try:
                content = src_file.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.debug("secrets_extractor: пропущен файл %s: %s", src_file, e)
                continue

            try:
                rel_path = str(src_file.relative_to(project_root))
            except ValueError:
                rel_path = str(src_file)

            file_matches = self.scan(rel_path, content, detected_lang)
            if file_matches:
                logger.debug(
                    "secrets_extractor: %s — найдено %d: %s",
                    rel_path,
                    len(file_matches),
                    ", ".join(m.name for m in file_matches),  # имена — не значения
                )
                all_matches.extend(file_matches)

        if not all_matches:
            return ApplyResult(
                count=0,
                names=[],
                files_modified=[],
                secrets_file=str(project_root / "secrets.txt"),
                gitignore_updated=False,
                dry_run=dry_run,
            )

        return self.apply(all_matches, project_root, dry_run=dry_run)
