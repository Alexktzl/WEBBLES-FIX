"""
Pip dependency inference — детерминированный pre-pipeline слой для Python.

По аналогии с `analysis/dependency_inference.py` (Rust, Stage M.1):
  * сканирует все `*.py` в проекте — собирает имена top-level импортов;
  * курируемая таблица `KNOWN_PACKAGES` маппит import-имя → имя дистрибутива
    (то, что пишется в `requirements.txt` — иногда не совпадает с import-именем:
    `bs4` → `beautifulsoup4`, `yaml` → `pyyaml`, `PIL` → `pillow`, и т.п.);
  * `apply` дописывает недостающие в `requirements.txt` (создаёт файл, если
    его не было);
  * `recover` — оркестрирует: backup → infer → apply → опц. валидация
    `pip install --dry-run` → откат при ошибке.

Этот модуль НЕ ставит пакеты. Цель — сделать `requirements.txt` корректным,
чтобы при следующем `pip install -r requirements.txt` `nonexistent_module`-кейсы
ушли. Никогда не угадывает: если import не в `KNOWN_PACKAGES` и не похож на
дистрибутив 1-в-1 — пропускаем (запись только при явном matche).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# stdlib (защита от записи std-модулей в requirements.txt)
# ----------------------------------------------------------------------
def _stdlib_modules() -> Set[str]:
    """Возвращает набор имён модулей stdlib для текущего интерпретатора.

    Python 3.10+ имеет `sys.stdlib_module_names`; на более старых — берём
    курируемый минимум. На production-машине пользователя стоит >=3.10.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names)
    # fallback — то, что точно есть в любом Python ≥3.7
    return {
        "abc", "argparse", "ast", "asyncio", "base64", "binascii", "bisect",
        "calendar", "collections", "concurrent", "contextlib", "copy", "csv",
        "ctypes", "datetime", "decimal", "difflib", "dis", "email", "enum",
        "errno", "fnmatch", "fractions", "functools", "gc", "getpass", "glob",
        "gzip", "hashlib", "heapq", "hmac", "html", "http", "importlib",
        "inspect", "io", "ipaddress", "itertools", "json", "logging", "math",
        "mimetypes", "multiprocessing", "operator", "os", "pathlib", "pickle",
        "platform", "pprint", "queue", "random", "re", "secrets", "select",
        "selectors", "shelve", "shlex", "shutil", "signal", "site", "smtplib",
        "socket", "sqlite3", "ssl", "stat", "string", "struct", "subprocess",
        "sys", "tarfile", "tempfile", "textwrap", "threading", "time", "token",
        "tokenize", "traceback", "types", "typing", "unicodedata", "unittest",
        "urllib", "uuid", "warnings", "weakref", "xml", "zipfile", "zlib",
    }


# ----------------------------------------------------------------------
# Курируемая таблица KNOWN_PACKAGES
#   import_name → (distribution_name, suggested_version_spec | None)
# Только устоявшиеся, широко известные пакеты. Если в проекте встретится
# `import foo`, и `foo` не в таблице и не похож «один-в-один» на dist-имя —
# мы не угадываем, оставляем хвост на LLM/review.
# ----------------------------------------------------------------------
KNOWN_PACKAGES: Dict[str, Tuple[str, Optional[str]]] = {
    # web/api
    "requests":      ("requests", ">=2.31.0"),
    "httpx":         ("httpx", ">=0.27.0"),
    "aiohttp":       ("aiohttp", ">=3.9.0"),
    "fastapi":       ("fastapi", ">=0.110.0"),
    "starlette":     ("starlette", None),
    "uvicorn":       ("uvicorn", ">=0.27.0"),
    "flask":         ("flask", ">=3.0.0"),
    "django":        ("django", ">=4.2.0"),
    "werkzeug":      ("werkzeug", None),
    "jinja2":        ("jinja2", ">=3.1.0"),
    # data
    "numpy":         ("numpy", ">=1.26.0"),
    "scipy":         ("scipy", ">=1.11.0"),
    "pandas":        ("pandas", ">=2.1.0"),
    "matplotlib":    ("matplotlib", ">=3.8.0"),
    "seaborn":       ("seaborn", None),
    "sklearn":       ("scikit-learn", ">=1.4.0"),
    "torch":         ("torch", None),
    "transformers":  ("transformers", None),
    # parsers / serialization
    "yaml":          ("pyyaml", ">=6.0"),
    "toml":          ("tomli", None),
    "tomlkit":       ("tomlkit", ">=0.12.0"),
    "ruamel":        ("ruamel.yaml", None),
    "orjson":        ("orjson", None),
    "msgpack":       ("msgpack", None),
    "lxml":          ("lxml", None),
    "bs4":           ("beautifulsoup4", ">=4.12.0"),
    # imaging
    "PIL":           ("pillow", ">=10.0.0"),
    "cv2":           ("opencv-python", None),
    # db
    "psycopg2":      ("psycopg2-binary", None),
    "pymysql":       ("PyMySQL", None),
    "sqlalchemy":    ("SQLAlchemy", ">=2.0.0"),
    "alembic":       ("alembic", None),
    "redis":         ("redis", ">=5.0.0"),
    "pymongo":       ("pymongo", None),
    # cli / config
    "click":         ("click", ">=8.1.0"),
    "typer":         ("typer", None),
    "dotenv":        ("python-dotenv", ">=1.0.0"),
    "rich":          ("rich", None),
    "pydantic":      ("pydantic", ">=2.6.0"),
    "attrs":         ("attrs", None),
    # test / dev
    "pytest":        ("pytest", ">=8.0.0"),
    "hypothesis":    ("hypothesis", ">=6.0.0"),
    # auth / crypto
    "jose":          ("python-jose", None),
    "jwt":           ("PyJWT", None),
    "bcrypt":        ("bcrypt", None),
    "passlib":       ("passlib", None),
    # ai/llm sdk
    "openai":        ("openai", ">=1.0.0"),
    "anthropic":     ("anthropic", ">=0.18.0"),
    # ui / desktop
    "eel":           ("eel", None),
    # ops
    "telegram":      ("python-telegram-bot", ">=20.0"),
    "duckduckgo_search": ("duckduckgo-search", ">=6.0.0"),
    "semgrep":       ("semgrep", ">=1.0.0"),
    "flake8":        ("flake8", ">=6.0.0"),
}


# ----------------------------------------------------------------------
# Результат
# ----------------------------------------------------------------------
@dataclass
class PythonDepResult:
    missing: Dict[str, Tuple[str, Optional[str]]] = field(default_factory=dict)
    has_changes: bool = False
    explanations: List[str] = field(default_factory=list)
    requirements_path: Optional[Path] = None
    backup: Optional[str] = None  # содержимое до правки, для отката


# ----------------------------------------------------------------------
# Сканер импортов
# ----------------------------------------------------------------------
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w\.]*)\s+import\b|import\s+([A-Za-z_][\w\.,\s]*))"
)


def _top_module(name: str) -> str:
    """Top-level имя из dotted-path. Пустая строка для relative imports.

    Relative `from . import x` / `from .sub import y` дают `name=""`/`.sub`,
    и `_top_module("")`/`.sub` должен вернуть `""` — мы их не пишем в
    requirements (это внутренние модули проекта).
    """
    name = (name or "").strip()
    if not name or name.startswith("."):
        return ""
    return name.split(".", 1)[0]


def collect_imports(project_path: Path, *, max_files: int = 1500,
                    skip_dirs: Optional[Set[str]] = None) -> Set[str]:
    """Идёт по `*.py` в проекте, возвращает множество top-level import-имён."""
    skip = set(skip_dirs or {
        "target", "node_modules", ".git", "venv", ".venv",
        "__pycache__", ".webbles_fix", ".webbles", ".webbles_backups",
        "dist", "build", ".idea", ".vscode", ".tox", ".pytest_cache",
    })
    out: Set[str] = set()
    root = Path(project_path).resolve()
    n = 0
    for p in root.rglob("*.py"):
        if any(part in skip for part in p.parts):
            continue
        n += 1
        if n > max_files:
            logger.info("collect_imports: лимит %d файлов достигнут", max_files)
            break
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            m = _IMPORT_RE.match(line)
            if not m:
                continue
            if m.group(1):  # `from X import ...`
                top = _top_module(m.group(1))
                if top:  # пустая строка = relative-import, не пишем в requirements
                    out.add(top)
            else:           # `import A, B as B2, C.D`
                for piece in (m.group(2) or "").split(","):
                    piece = piece.strip()
                    if not piece:
                        continue
                    # отрезаем `... as alias`
                    piece = piece.split(" as ", 1)[0].strip()
                    top = _top_module(piece)
                    if top:
                        out.add(top)
    return out


def _local_top_modules(project_path: Path,
                       skip_dirs: Optional[Set[str]] = None) -> Set[str]:
    """Имена пакетов/модулей самого проекта (их в requirements не пишем).

    Идём рекурсивно: любой `__init__.py` означает Python-пакет — его имя и все
    промежуточные части пути добавляем как локальные. `*.py` в корне — тоже
    локальный модуль. Для глубоких подпакетов хранится TOP имя — это
    защищает от ложных попыток записать `from app.routes import x` в
    requirements (`app` — локальный пакет).
    """
    skip = set(skip_dirs or {
        "target", "node_modules", ".git", "venv", ".venv",
        "__pycache__", ".webbles_fix", ".webbles", ".webbles_backups",
        "dist", "build", ".idea", ".vscode", ".tox", ".pytest_cache",
    })
    out: Set[str] = set()
    root = Path(project_path).resolve()
    if not root.exists():
        return out
    # 1) `*.py` в корне → top-level модули.
    try:
        for p in root.iterdir():
            if p.is_file() and p.suffix == ".py" and not p.name.startswith("."):
                out.add(p.stem)
    except Exception:
        pass
    # 2) Любой `__init__.py` в проекте → берём первый компонент пути от корня.
    for init in root.rglob("__init__.py"):
        try:
            rel = init.parent.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        if any(p in skip for p in parts):
            continue
        out.add(parts[0])
    return out


# ----------------------------------------------------------------------
# Анализ + правка requirements.txt
# ----------------------------------------------------------------------
def _read_requirements(req_path: Path) -> List[str]:
    if not req_path.exists():
        return []
    try:
        return req_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def _dist_in_requirements(lines: List[str], dist: str) -> bool:
    """True если в requirements уже есть запись для дистрибутива (PEP 508)."""
    canon = re.sub(r"[-_.]+", "-", dist.strip().lower())
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        # отрезаем спецификатор/экстра/комментарий
        spec = re.split(r"[<>=!~;\[]", s, maxsplit=1)[0].strip()
        spec_canon = re.sub(r"[-_.]+", "-", spec.lower())
        if spec_canon == canon:
            return True
    return False


def infer_missing(project_path: Path) -> Dict[str, Tuple[str, Optional[str]]]:
    """Возвращает словарь `dist_name → (dist_name, version_spec)` пакетов,
    которые есть в импортах, не в stdlib, не локальные и ещё не записаны в
    `requirements.txt`.
    """
    imports = collect_imports(project_path)
    stdlib = _stdlib_modules()
    local = _local_top_modules(project_path)
    req_path = project_path / "requirements.txt"
    req_lines = _read_requirements(req_path)

    missing: Dict[str, Tuple[str, Optional[str]]] = {}
    for imp in sorted(imports):
        if imp in stdlib or imp in local:
            continue
        if imp.startswith("_"):
            continue
        match = KNOWN_PACKAGES.get(imp)
        if match is None:
            # не угадываем: если имя совпадает с распространённой dist-нормой,
            # пропускаем (нет уверенности).
            continue
        dist, ver = match
        if _dist_in_requirements(req_lines, dist):
            continue
        missing[dist] = (dist, ver)
    return missing


def apply_to_requirements(
    project_path: Path,
    missing: Dict[str, Tuple[str, Optional[str]]],
) -> Tuple[bool, Optional[str], Path]:
    """Дописывает недостающие пакеты в `requirements.txt`.

    Возвращает `(changed, backup_text, req_path)`. `backup_text` — содержимое
    файла ДО правки (для отката), None если файла не было.
    """
    req_path = project_path / "requirements.txt"
    backup = None
    if req_path.exists():
        try:
            backup = req_path.read_text(encoding="utf-8")
        except Exception:
            backup = ""
    if not missing:
        return (False, backup, req_path)
    lines = (backup or "").splitlines()
    if lines and lines[-1].strip() != "":
        lines.append("")  # пустая строка как разделитель
    if not any(l.strip().startswith("# auto-added") for l in lines):
        lines.append("# auto-added by Webbles Fix (Python dep-inference)")
    for dist, (_, ver) in sorted(missing.items()):
        line = f"{dist}{ver}" if ver else dist
        lines.append(line)
    new_text = "\n".join(lines) + "\n"
    try:
        req_path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        logger.warning("Не удалось записать requirements.txt: %s", e)
        return (False, backup, req_path)
    return (True, backup, req_path)


# ----------------------------------------------------------------------
# Высокоуровневый recover()
# ----------------------------------------------------------------------
class PythonDependencyInference:
    """API, идентичный `DependencyInference` (Rust) для единообразия в
    `controller._recover_dependencies`."""

    def infer(self, project_path: Path) -> Dict[str, Tuple[str, Optional[str]]]:
        return infer_missing(project_path)

    def recover(self, project_path: Path, *,
                run_pip_check: bool = False) -> PythonDepResult:
        """Полный цикл: backup → apply → опц. `pip install --dry-run` → откат.

        `run_pip_check` по умолчанию False — pip нередко медленный и требует
        сети. Включается флагом `pipeline.dependency_recovery_pip_check`.
        """
        res = PythonDepResult()
        missing = self.infer(project_path)
        res.missing = dict(missing)
        if not missing:
            res.explanations.append("dep-inference (python): новых импортов нет")
            return res

        changed, backup, req_path = apply_to_requirements(project_path, missing)
        res.has_changes = changed
        res.backup = backup
        res.requirements_path = req_path
        if changed:
            res.explanations.append(
                f"dep-inference (python): добавлены {', '.join(sorted(missing))}"
            )
        if not changed:
            return res

        if run_pip_check:
            ok = self._dry_install(project_path)
            if not ok:
                # откат
                try:
                    if backup is None:
                        req_path.unlink(missing_ok=True)
                    else:
                        req_path.write_text(backup, encoding="utf-8")
                    res.explanations.append(
                        "dep-inference (python): pip --dry-run упал — откат"
                    )
                    res.has_changes = False
                except Exception as e:
                    res.explanations.append(f"откат не удался: {e}")
        return res

    def _dry_install(self, project_path: Path, *, timeout: int = 90) -> bool:
        """Запускает `pip install --dry-run -r requirements.txt`. False при
        отсутствии pip / сети / ошибки разрешения версий.
        """
        if not shutil.which("pip"):
            return True  # нет pip — не блокируем правку (граcefulно)
        try:
            r = subprocess.run(
                ["pip", "install", "--dry-run",
                 "-r", str(project_path / "requirements.txt")],
                cwd=str(project_path),
                capture_output=True, text=True, timeout=timeout,
            )
            return r.returncode == 0
        except Exception as e:
            logger.debug("pip dry-run упал: %s", e)
            return True  # сеть/timeout — не блокируем
