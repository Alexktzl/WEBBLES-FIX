"""
Дополнительные статические анализаторы для C/C++: `cppcheck` и `clang-tidy`.

ЗАЧЕМ. `g++`/`clang` (через `cpp_analyzer`) ловит только то, что мешает
компиляции. UB-паттерны (`new`/`delete` асимметрия, висячие ссылки, неиниц.
память, выход за границы, race conditions) компиляторы пропускают — а
статические анализаторы ловят. Это «акулы C/C++», про которые писали:
ловим их БЕЗ запуска программы (sanitizers — отдельный шаг, требует тестов).

ЧТО ДЕЛАЕТ. Запускает (по очереди, что найдено):
  1. `cppcheck --enable=warning,style,performance,portability` с XML-выводом;
  2. `clang-tidy` со стандартными чек-листами.
Парсит вывод в наш стандартный `error_dict` контракт:
    {file, line, column, message, code, severity, error_type}

Коды нормализуются:
  * cppcheck: `cppcheck::<id>` (например `cppcheck::nullPointer`).
  * clang-tidy: оставляем как есть (`clang-analyzer-*`, `cppcoreguidelines-*`,
    `readability-*`, `performance-*`, ...).

Graceful: если тула нет в PATH — возвращаем `[]`, не падаем. Все исключения
ловятся и логируются.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"build", "cmake-build-debug", "cmake-build-release", "out",
              ".git", "node_modules", ".vs", ".vscode", "vcpkg_installed",
              ".webbles_fix", ".webbles_backups", ".webbles", ".idea",
              "target", "dist", "__pycache__"}
_CXX_EXTS = {".cpp", ".cc", ".cxx", ".c", ".hpp", ".h", ".hxx"}

# cppcheck-ID заведомого стилевого/оптимизационного шума и build-config-шума.
# ПОЧЕМУ денилист, а не сужение `--enable` (2026-07-10): в категории `style`
# лежит и высокосигнальный `noExplicitConstructor` (реальный доставленный фикс
# на argparse), и вал `functionStatic`/`useInitializationList` — сужать по
# категории значит потерять сигнал. Гасим точечно только шум: оптимизационные
# советы (perf), «could be static», и config-зависимые ложняки (unknownMacro
# без -D). Направление фазы: чиним БАГИ, не навязываем стиль зрелой библиотеке.
_CPPCHECK_NOISE_IDS = frozenset({
    "functionStatic", "functionConst", "passedByValue",
    "useInitializationList", "constParameter", "constParameterPointer",
    "constParameterReference", "constVariable", "constVariablePointer",
    "constVariableReference", "unusedFunction", "unusedStructMember",
    "unusedVariable", "unmatchedSuppression", "missingInclude",
    "missingIncludeSystem", "unknownMacro", "toomanyconfigs",
    "normalCheckLevelMaxBranches", "checkersReport", "purgedConfiguration",
    "ConfigurationNotChecked", "noValidConfiguration",
    # 2026-07-10 (tinyxml2, серия): чисто СТИЛЕВЫЕ конвенции, не дефекты —
    # missingOverride (17 на tinyxml2, аналог modernize-use-override, который
    # мы уже глушим в clang-tidy), cstyleCast (C-cast vs static_cast).
    # Навязывать их чужому коду = стилевая политика, а не ремонт бага.
    "missingOverride", "cstyleCast",
})

# Маппинг важности cppcheck → наш `severity`.
_CPPCHECK_SEVERITY_MAP = {
    "error": "error",
    "warning": "warning",
    "style": "warning",
    "performance": "warning",
    "portability": "warning",
    "information": "note",
    "debug": "note",
}

# clang-tidy: line format
#   path/to/file.cpp:LINE:COL: severity: message [check-name,check-name2]
_TIDY_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<severity>error|warning|note):\s+(?P<msg>.+?)"
    r"\s*\[(?P<check>[\w\-\.,\s]+)\]\s*$"
)


def _has_cxx_sources(root: Path) -> bool:
    for p in root.rglob("*"):
        if p.suffix.lower() in _CXX_EXTS and not any(
            part in _SKIP_DIRS for part in p.parts
        ):
            return True
    return False


def _safe_rel(file_path: str, project_path: Path) -> str:
    try:
        return str(Path(file_path).resolve().relative_to(project_path.resolve()))
    except (ValueError, OSError):
        return Path(file_path).name


# ---------------------------------------------------------------------------
# cppcheck
# ---------------------------------------------------------------------------
def run_cppcheck(project_path: Path,
                 timeout: int = 180,
                 only_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Запускает `cppcheck` с XML-выводом и парсит результат.

    `only_files` (инкрементал) — анализировать только эти файлы, а не весь
    проект: снимает медленный полный обход дерева на каждый цикл валидации.

    Возвращает [] если cppcheck не найден / нет .cxx-файлов / таймаут / ошибка.
    """
    if not shutil.which("cppcheck"):
        logger.debug("cppcheck не найден в PATH — пропускаем")
        return []
    project_path = Path(project_path)
    if only_files:
        targets = [f for f in only_files if Path(f).is_file()]
        if not targets:
            return []
    else:
        if not _has_cxx_sources(project_path):
            return []
        targets = [str(project_path)]
    cmd = [
        "cppcheck",
        "--enable=warning,style,performance,portability",
        "--inline-suppr",
        "--quiet",
        "--xml", "--xml-version=2",
        "--language=c++",
        "-j", "2",
        *targets,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("cppcheck не запустился: %s", e)
        return []
    # cppcheck XML идёт в stderr.
    xml_text = proc.stderr or ""
    errors: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text) if xml_text.strip() else None
    except ET.ParseError as e:
        logger.debug("cppcheck XML parse fail: %s", e)
        return []
    if root is None:
        return []
    for err in root.iter("error"):
        cid = err.get("id", "unknown")
        if cid in _CPPCHECK_NOISE_IDS:
            continue  # стилевой/оптимизационный/config-шум — см. _CPPCHECK_NOISE_IDS
        sev = err.get("severity", "warning")
        msg = err.get("msg", "") or err.get("verbose", "")
        loc = err.find("location")
        if loc is None:
            continue
        f = loc.get("file", "")
        try:
            line = int(loc.get("line", "0") or 0)
        except ValueError:
            line = 0
        try:
            col = int(loc.get("column", "0") or 0)
        except ValueError:
            col = 0
        errors.append({
            "file": _safe_rel(f, project_path),
            "line": line,
            "column": col,
            "message": msg.strip(),
            "code": f"cppcheck::{cid}",
            "severity": _CPPCHECK_SEVERITY_MAP.get(sev, "warning"),
            "error_type": "static",
        })
    logger.info("cppcheck: %d findings", len(errors))
    return errors


# ---------------------------------------------------------------------------
# clang-tidy
# ---------------------------------------------------------------------------
# Bug-ориентированный чек-сет clang-tidy. ПОЧЕМУ жёстко задаём (2026-07-10,
# fmt-инцидент): по умолчанию/из проектного `.clang-tidy` включаются семейства
# `modernize-*`/`readability-*` — это СТИЛЕВЫЕ политики автора для своих
# контрибьюторов, а не дефекты. На header-only fmt один только
# `modernize-use-trailing-return-type` дал 2078 «находок» (`auto f()->int`
# вместо `int f()`) — реальный сигнал (clang-diagnostic-error, реальные UB)
# тонул, initial_error_count=2315, движок уходил в project_timeout. Прямой
# аналог python-решения «E501-wrapper too noisy»: наша система чинит БАГИ, а не
# навязывает чужому коду стиль. Ведущий `-*` глушит всё (в т.ч. проектный
# конфиг, т.к. CLI-checks применяются ПОСЛЕ файла), затем включаем только
# семейства реальных дефектов; шумные под-проверки внутри них гасим точечно.
# Оставляем ТОЛЬКО семейства настоящих дефектов: `bugprone-*` (паттерны
# реальных багов) и `clang-analyzer-*` (path-sensitive: null-deref, утечки,
# UB). Семейства `modernize-/readability-/misc-/performance-/cppcoreguidelines-`
# сознательно НЕ включаем — они стилевые/оптимизационные (fmt: misc и
# cppcoreguidelines дали ещё ~2000 стилевых нитов сверх modernize). Наша система
# чинит БАГИ, а не навязывает чужому коду стиль. `clang-diagnostic-error` (реальные
# ошибки компиляции) приходит независимо от `--checks`. Внутри bugprone гасим
# заведомо шумные эвристики, дающие ложный вал на нормальном коде.
_TIDY_CHECKS = ",".join([
    "-*",
    "bugprone-*",
    "clang-analyzer-*",
    "-bugprone-easily-swappable-parameters",
    "-bugprone-narrowing-conversions",
    "-bugprone-exception-escape",
    "-bugprone-reserved-identifier",
])


def run_clang_tidy(project_path: Path,
                   timeout: int = 180,
                   max_files: int = 200,
                   compile_db_dir: Optional[Path] = None,
                   only_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Запускает `clang-tidy` по найденным .cpp/.cc/.cxx файлам.

    Чек-сет жёстко задан bug-ориентированным (`_TIDY_CHECKS`), а проектный
    `.clang-tidy`/дефолт со стилевыми `modernize-*`/`readability-*`
    подавляется ведущим `-*` — см. коммент к `_TIDY_CHECKS` (fmt-инцидент).

    `compile_db_dir` — каталог с compile_commands.json: clang-tidy берёт
    per-file флаги проекта через `-p`, что убирает ложные
    `clang-diagnostic-error` (без билд-конфига clang не находит include/define
    и штампует фантомные ошибки компиляции). Без БД — fallback на `-std -I`.

    `only_files` (инкрементал) — анализировать только эти TU вместо обхода
    всего дерева (главный источник медленных циклов валидации C++).
    """
    if not shutil.which("clang-tidy"):
        logger.debug("clang-tidy не найден в PATH — пропускаем")
        return []
    project_path = Path(project_path)
    if only_files:
        cxx_files = [f for f in only_files
                     if Path(f).suffix.lower() in {".cpp", ".cc", ".cxx", ".c"}
                     and Path(f).is_file()]
    else:
        cxx_files = []
        for p in project_path.rglob("*"):
            if len(cxx_files) >= max_files:
                break
            if p.suffix.lower() not in {".cpp", ".cc", ".cxx", ".c"}:
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            cxx_files.append(str(p))
    if not cxx_files:
        return []
    # `--checks` жёстко перекрывает стилевой шум; `--header-filter=$^` (регексп,
    # не матчащий ничего) глушит диагностику из чужих заголовков header-only
    # библиотек, чей проектный конфиг ставит HeaderFilterRegex='.*'.
    cmd = ["clang-tidy", "--quiet", f"--checks={_TIDY_CHECKS}",
           "--header-filter=$^"]
    _use_db = compile_db_dir and (Path(compile_db_dir) / "compile_commands.json").exists()
    if _use_db:
        # точные per-file флаги проекта → нет фантомных clang-diagnostic-error
        cmd += ["-p", str(compile_db_dir)]
    cmd += [*cxx_files, "--"]
    if not _use_db:
        # Fallback без БД: минимальные подсказки компилятору.
        cmd += ["-std=c++17", "-I" + str(project_path)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("clang-tidy не запустился: %s", e)
        return []
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    errors: List[Dict[str, Any]] = []
    for raw in out.splitlines():
        m = _TIDY_LINE.match(raw.strip())
        if not m:
            continue
        sev = m.group("severity")
        if sev == "note":
            continue
        check = m.group("check").split(",")[0].strip()
        # `clang-diagnostic-*` — это clang-tidy пытается САМ скомпилировать файл
        # и спотыкается на файлах вне compile_commands (тесты/примеры/иной билд-
        # таргет): без точных -D/-I он штампует фантомные «ошибки компиляции».
        # Авторитет по реальным compile-ошибкам — g++ через compile_db в
        # cpp_analyzer; дубли/фантомы от clang-tidy отбрасываем (2026-07-10).
        if check.startswith("clang-diagnostic"):
            continue
        errors.append({
            "file": _safe_rel(m.group("file"), project_path),
            "line": int(m.group("line")),
            "column": int(m.group("col")),
            "message": m.group("msg").strip(),
            "code": check,
            "severity": sev,
            "error_type": "static",
        })
    logger.info("clang-tidy: %d findings", len(errors))
    return errors


# ---------------------------------------------------------------------------
# Объединение
# ---------------------------------------------------------------------------
def run_all(project_path: Path,
            compile_db_dir: Optional[Path] = None,
            only_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Запускает оба анализатора и возвращает объединённый список ошибок.
    Дедуп по (file, line, code, message).

    `compile_db_dir` (каталог compile_commands.json) прокидывается в clang-tidy
    через `-p` — точные флаги проекта убирают ложные clang-diagnostic-error.
    `only_files` (инкрементал) — анализ только этих TU, не всего дерева.
    """
    project_path = Path(project_path)
    combined: List[Dict[str, Any]] = []
    seen = set()
    backends = (
        lambda p: run_cppcheck(p, only_files=only_files),
        lambda p: run_clang_tidy(p, compile_db_dir=compile_db_dir,
                                 only_files=only_files),
    )
    for backend in backends:
        try:
            for e in backend(project_path):
                key = (e.get("file", ""), e.get("line", 0),
                       e.get("code", ""), e.get("message", "")[:120])
                if key in seen:
                    continue
                seen.add(key)
                combined.append(e)
        except Exception as ex:
            logger.warning("cpp_static_extra backend упал: %s", ex)
    return combined
