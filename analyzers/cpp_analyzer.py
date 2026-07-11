"""
Анализатор C++ для Webbles Fix.
Использует g++ для поиска ошибок.
"""

import json
import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# O.12: пропускаем служебные/бэкап-каталоги при rglob по .cpp/.cxx/.cc.
# Без этого g++ компилирует копии повреждённых файлов из .webbles_backups
# и движок чинит копии вместо реальных файлов проекта.
_CPP_SKIP_DIRS = {
    "build", "cmake-build-debug", "cmake-build-release", "out",
    ".git", "node_modules", ".vs", ".vscode", ".idea",
    "vcpkg_installed", ".webbles_fix",
    ".webbles_backups", ".webbles", "target", "dist", "__pycache__",
    # 2026-07-10: наш собственный cmake-build каталог — cmake кладёт туда
    # временные .cpp (CompilerIdCXX и пр.) + бинарники; без skip анализатор
    # глобил их как исходники (fmt: 2309 фантомов) и ломал чтение в других
    # стадиях (permission/decode). Теперь билд генерится ВНЕ проекта, но
    # skip оставляем на случай готового/старого каталога.
    ".webbles_cmake_build", "CMakeFiles",
    # 2026-07-10: вендорный/сторонний код — не наш, не чиним и не тратим на
    # него анализ. gtest/gmock — тестовый фреймворк (fmt: вендорный
    # gmock-gtest-all.cc в 30k строк один давал 31.7с скана и 64 «находки»
    # в чужом коде). third_party/extern/external/vendor — общий контейнер
    # чужих зависимостей.
    "gtest", "gmock", "googletest", "googlemock",
    "third_party", "third-party", "extern", "external", "vendor", "vendored",
}


def _cpp_excluded(path: Path) -> bool:
    return any(part in _CPP_SKIP_DIRS for part in path.parts)


class CppAnalyzer:
    """Анализирует код C++ с помощью g++."""

    # Суффиксы единиц трансляции (translation unit) — то, что реально
    # компилируется. Только их безопасно пере-анализировать инкрементально:
    # правка .cc/.c влияет на диагностику ТОЛЬКО своей TU. Заголовок (.h/.hpp)
    # трансляционной единицей НЕ является и, будучи включён в N файлов, меняет
    # их все — инкрементал по одному заголовку неверен (см. analyze(files=)).
    _TU_SUFFIXES = (".cpp", ".cxx", ".cc", ".c")

    def analyze(self, project_path: Path,
                files: Optional[List] = None,
                static_extra: bool = True,
                **_ignored) -> List[Dict[str, Any]]:
        """Анализ C++ через g++ + опциональные статические анализаторы
        (`cppcheck`/`clang-tidy`).

        Параметр `static_extra=True` (default) подключает `cpp_static_extra.run_all`,
        который ловит UB-паттерны и стилевые проблемы, которые g++ не видит
        (`new`/`delete` асимметрия, висячие ссылки, неиниц. память). Тулы
        опциональны: если нет в PATH — модуль вернёт `[]` без падения.

        ИНКРЕМЕНТАЛЬНЫЙ режим (`files=[...]`, 2026-07-10): пере-анализ ТОЛЬКО
        указанных файлов вместо всего проекта — снимает главный перф-хвост
        C++ (ValidateStage пере-сканировал ~140 файлов cmake+cppcheck+clang-tidy
        после каждого патча, минуты за цикл). БЕЗОПАСНОСТЬ (главный инвариант,
        `merge_file_errors` ждёт ошибки ТОЛЬКО целевого файла):
        1. Инкрементал допустим лишь для единиц трансляции (`_TU_SUFFIXES`);
           не-TU (заголовок/`.py`) в `files` → возвращаем результат ПОЛНОГО
           скана (fail-closed: лучше медленно и верно, чем быстро и с false
           ACCEPT). Заголовок влияет на все включающие его TU — по одному его
           не пересчитать.
        2. Результат жёстко фильтруется до запрошенных файлов — ошибки в чужих
           заголовках, всплывшие при компиляции целевой TU, отбрасываются
           (их слайс в снимке принадлежит другим файлам и не трогается).
        Вызывающий (ValidateStage/DecideStage) обязан звать `files=` ТОЛЬКО с
        C++-единицами трансляции; иначе см. п.1.
        """
        errors: List[Dict[str, Any]] = []

        # --- Инкрементальный режим: ограничиваем анализ целевыми TU ---
        incremental_targets: Optional[List[Path]] = None
        if files:
            _reqs = [Path(f) if not isinstance(f, Path) else f for f in files]
            # относительные пути резолвим от project_path
            _abs = [(project_path / p) if not p.is_absolute() else p for p in _reqs]
            _tu = [p for p in _abs if p.suffix.lower() in self._TU_SUFFIXES
                   and not _cpp_excluded(p)]
            if len(_tu) != len(_abs):
                # среди запрошенных есть не-TU (заголовок/.py/служебный) —
                # инкрементал небезопасен, откатываемся на полный скан.
                logger.info("C++ incremental: среди %d целей есть не-TU — "
                            "полный скан (fail-closed)", len(_abs))
            else:
                incremental_targets = _tu

        # C++ (.cpp/.cxx/.cc) и C (.c) — раздельно: C компилируется gcc,
        # C++ — g++ (2026-07-09: раньше .c не сканировались вовсе).
        if incremental_targets is not None:
            cpp_files = [p for p in incremental_targets if p.suffix.lower() != ".c"]
            c_files = [p for p in incremental_targets if p.suffix.lower() == ".c"]
        else:
            cpp_files = [
                p for p in (
                    list(project_path.rglob("*.cpp"))
                    + list(project_path.rglob("*.cxx"))
                    + list(project_path.rglob("*.cc"))
                )
                if not _cpp_excluded(p)
            ]
            c_files = [p for p in project_path.rglob("*.c") if not _cpp_excluded(p)]

        if not cpp_files and not c_files:
            logger.warning("Не найдены .c/.cpp файлы, пропускаем анализ C/C++")
            return errors

        # 2026-07-09 (постройка C++-этапа): БЕЗ include-путей `-fsyntax-only`
        # не находит заголовки проекта → шквал GCC_INCLUDE «no such file»,
        # тонущий реальные диагностики. Авто-детект каталогов с заголовками
        # (+ типовые include/, src/) и раздача через -I резко чистит шум:
        # внутренние хедеры резолвятся, остаётся только реально внешнее.
        include_flags = self._collect_include_flags(project_path)

        # ОБЪЕКТИВНЫЙ путь (2026-07-09, требование Алекса): точные per-file
        # флаги из compile_commands.json (реальные -I/-D/-std проекта). Без
        # него g++ даёт фантомы. Если БД нет — fallback на эвристику
        # (-std из CMakeLists + авто-include + фильтр шума).
        cdb_path = self._find_or_gen_compile_db(project_path)
        compile_db = self._parse_compile_db(cdb_path) if cdb_path else {}

        if compile_db:
            logger.info("C++: анализ по compile_commands.json (%d файлов с точными флагами)",
                        len(compile_db))
            errors.extend(self._analyze_with_db(project_path, cpp_files + c_files, compile_db))
        else:
            logger.info("C++: compile_commands.json недоступен — эвристика (-std+include)")
            cpp_std = self._detect_std(project_path, cpp=True)
            c_std = self._detect_std(project_path, cpp=False)
            for compiler, files, std in (("g++", cpp_files, cpp_std),
                                         ("gcc", c_files, c_std)):
                if not files:
                    continue
                try:
                    cmd = ([compiler, f"-std={std}", "-Wall", "-Wextra", "-fsyntax-only"]
                           + include_flags + [str(f) for f in files])
                    result = subprocess.run(
                        cmd, cwd=project_path, capture_output=True, text=True, timeout=300,
                    )
                    parsed = self._parse_gcc_output(result.stderr, project_path)
                    errors.extend(self._filter_build_config_noise(parsed))
                except subprocess.TimeoutExpired:
                    logger.error("%s timed out", compiler)
                except FileNotFoundError:
                    logger.warning("%s не найден в PATH", compiler)
                except Exception as e:
                    logger.warning("Ошибка анализа %s: %s", compiler, e)

        # Дополнительно: статические анализаторы (UB/leaks/style/perf).
        # Граcefully: если cppcheck/clang-tidy не установлены — вернётся [].
        if static_extra:
            try:
                from analyzers.cpp_static_extra import run_all as run_static
                # Прокидываем каталог compile_commands.json в clang-tidy (-p):
                # точные флаги проекта убирают ложные clang-diagnostic-error.
                _db_dir = cdb_path.parent if cdb_path else None
                # Инкрементал: cppcheck/clang-tidy только по целевым TU, не по
                # всему дереву (главный источник медленных циклов).
                # №2 (2026-07-10): даже в ПОЛНОМ пути ограничиваем static
                # файлами из compile_commands (реальный билд, ~51 у fmt), а не
                # всеми ~140 .cc в дереве. Не-билд файлы (docs/examples/fuzzing/
                # вендор) анализировать без флагов всё равно нельзя (фантомы),
                # а сканирование их — основная трата времени полного скана.
                if incremental_targets is not None:
                    _only = [str(p) for p in incremental_targets]
                elif compile_db:
                    _only = self._db_source_files(compile_db)
                else:
                    _only = None  # эвристика без БД — обходим всё дерево
                extra = run_static(project_path, compile_db_dir=_db_dir,
                                   only_files=_only)
                if extra:
                    # Дедуп по (file, line, code, message)
                    seen = {(e.get("file", ""), e.get("line", 0),
                             e.get("code", ""), e.get("message", "")[:120])
                            for e in errors}
                    for e in extra:
                        key = (e.get("file", ""), e.get("line", 0),
                               e.get("code", ""), e.get("message", "")[:120])
                        if key not in seen:
                            seen.add(key)
                            errors.append(e)
                    logger.info("C++ static extras добавили %d findings", len(extra))
            except Exception as e:
                logger.warning("cpp_static_extra пропущен: %s", e)

        # ИНКРЕМЕНТАЛ (safety-фильтр): при компиляции целевой TU g++/cppcheck/
        # clang-tidy могут отдать диагностику из ВКЛЮЧАЕМЫХ ею чужих заголовков.
        # Их слайс в снимке ошибок принадлежит другим файлам — `merge_file_errors`
        # заменяет только слайс целевого файла, поэтому чужие записи здесь
        # отбрасываем: иначе они задвоятся/исказят учёт (→ риск false ACCEPT).
        if incremental_targets is not None:
            _want = set()
            for p in incremental_targets:
                _want.add(str(p).replace("\\", "/").lstrip("/"))
                try:
                    _want.add(str(p.resolve()).replace("\\", "/").lstrip("/"))
                except Exception:
                    pass
                try:
                    _want.add(str(p.relative_to(project_path)).replace("\\", "/").lstrip("/"))
                except Exception:
                    pass

            def _match(ef: str) -> bool:
                efn = str(ef).replace("\\", "/").lstrip("/")
                if efn in _want:
                    return True
                # basename-совпадение (sandbox/относительные расхождения путей)
                base = efn.rsplit("/", 1)[-1]
                return any(w.rsplit("/", 1)[-1] == base and base for w in _want)

            before = len(errors)
            errors = [e for e in errors if _match(e.get("file", ""))]
            if before != len(errors):
                logger.info("C++ incremental: отфильтровано %d ошибок вне целевых "
                            "файлов (диагностика чужих заголовков)", before - len(errors))

        logger.info(f"Всего найдено ошибок C++: {len(errors)}")
        return errors

    # Каталоги с заголовками, которые почти всегда нужны в -I.
    _HEADER_SUFFIXES = (".h", ".hpp", ".hxx", ".hh", ".h++")
    _MAX_INCLUDE_DIRS = 200  # защита от гигантских монреп

    def _collect_include_flags(self, project_path: Path) -> List[str]:
        """Список -I<dir> для каталогов проекта, содержащих заголовки, плюс
        корень и типовые include/. Убирает GCC_INCLUDE-шум от внутренних
        хедеров (2026-07-09, постройка C++). Служебные каталоги пропускаем."""
        dirs = set()
        dirs.add(project_path)
        for common in ("include", "src", "inc", "headers"):
            d = project_path / common
            if d.is_dir():
                dirs.add(d)
        try:
            for suf in self._HEADER_SUFFIXES:
                for h in project_path.rglob(f"*{suf}"):
                    if _cpp_excluded(h):
                        continue
                    dirs.add(h.parent)
                    if len(dirs) >= self._MAX_INCLUDE_DIRS:
                        break
                if len(dirs) >= self._MAX_INCLUDE_DIRS:
                    break
        except Exception as e:
            logger.debug("collect_include_flags: %s", e)
        flags: List[str] = []
        for d in sorted(dirs, key=lambda p: str(p)):
            flags.append(f"-I{d}")
        return flags

    # ---- compile_commands.json — точная билд-конфигурация ----
    # Флаги, которые НЕ переносим в -fsyntax-only прогон (вывод/линковка/
    # оптимизация/зависимости-выхлоп мешают или бессмысленны при синтакс-чеке).
    _CDB_DROP_PREFIXES = ("-o", "-c", "-MMD", "-MF", "-MT", "-MD", "-MP",
                          "-flto", "-fprofile", "-Werror")

    def _analyze_with_db(self, project_path: Path, files: List[Path],
                         compile_db: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        """Компилирует каждый файл с ЕГО точными флагами из compile_commands
        (+ -fsyntax-only -Wall -Wextra). Только файлы, которые есть в БД
        (остальные — не в билде, шум). C → gcc, C++ → g++."""
        errs: List[Dict[str, Any]] = []
        for f in files:
            try:
                key = str(f.resolve())
            except Exception:
                key = str(f)
            flags = compile_db.get(key)
            if flags is None:
                # sandbox-копия: абсолютный путь не совпал — по basename
                flags = compile_db.get("::name::" + f.name)
            if flags is None:
                continue  # файла нет в билд-графе — не наш
            compiler = "gcc" if f.suffix == ".c" else "g++"
            try:
                cmd = ([compiler] + flags
                       + ["-fsyntax-only", "-Wall", "-Wextra", str(f)])
                r = subprocess.run(cmd, cwd=project_path, capture_output=True,
                                   text=True, timeout=120)
                errs.extend(self._parse_gcc_output(r.stderr, project_path))
            except subprocess.TimeoutExpired:
                logger.debug("C++: %s timeout на %s", compiler, f.name)
            except Exception as e:
                logger.debug("C++: %s ошибка на %s: %s", compiler, f.name, e)
        return errs

    def _find_or_gen_compile_db(self, project_path: Path) -> Optional[Path]:
        """compile_commands.json: готовый в проекте/build/, иначе генерим через
        cmake (-DCMAKE_EXPORT_COMPILE_COMMANDS=ON). Без него C++-анализ
        необъективен — точные -I/-D/-std берутся только отсюда
        (2026-07-09, требование Алекса). None если ни готового, ни cmake."""
        for cand in (project_path / "compile_commands.json",
                     project_path / "build" / "compile_commands.json",
                     project_path / "out" / "compile_commands.json"):
            if cand.is_file():
                logger.info("C++: найден готовый compile_commands.json: %s", cand)
                return cand
        if not (project_path / "CMakeLists.txt").is_file():
            return None
        # 2026-07-10: билд генерим ВНЕ проекта (temp), иначе cmake засыпает
        # клон временными .cpp/бинарниками → анализатор их глобит (fmt: 2309
        # фантомов) + другие стадии падают на permission/decode. compile_commands
        # ссылается на ИСХОДНИКИ проекта абсолютными путями — вне-проектный
        # билд этому не мешает. Кешируем по имени проекта.
        import hashlib
        import tempfile
        _key = hashlib.sha1(str(project_path.resolve()).encode()).hexdigest()[:12]
        build_dir = Path(tempfile.gettempdir()) / "webbles_cmake" / _key
        cached = build_dir / "compile_commands.json"
        if cached.is_file():
            return cached
        # Устаревший build_dir от ПРОВАЛЬНОГО прошлого configure (есть CMakeCache,
        # но нет compile_commands.json) — CMakeCache фиксирует старую (сломанную)
        # конфигурацию и повторный configure унаследует её. Чистим, чтобы retry
        # с новыми флагами (-DBUILD_TESTING=OFF и пр.) прошёл начисто (2026-07-10).
        if build_dir.exists() and (build_dir / "CMakeCache.txt").is_file():
            import shutil as _sh
            try:
                _sh.rmtree(build_dir, ignore_errors=True)
            except Exception as e:
                logger.debug("не удалось очистить устаревший build_dir: %s", e)
        # На Windows cmake по умолчанию берёт Visual Studio-генератор, который
        # (а) НЕ создаёт compile_commands.json, (б) выбирает MSVC (cl.exe), чьи
        # флаги несовместимы с g++. Форсим Ninja (fallback MinGW Makefiles) и
        # gcc/g++ — тогда БД генерится с флагами под наш анализ-компилятор.
        import shutil as _shutil
        generator = "Ninja" if _shutil.which("ninja") else "MinGW Makefiles"
        try:
            logger.info("C++: генерирую compile_commands.json через cmake (%s, gcc)...",
                        generator)
            # -D*_BUILD_TESTS=OFF и пр. — не тянуть внешние тест-зависимости
            # (GTest/Catch2/benchmark), роняющие configure → нет compile_commands.
            _disable = self._cmake_disable_flags(project_path)
            r = subprocess.run(
                ["cmake", "-S", str(project_path.resolve()), "-B", str(build_dir.resolve()),
                 "-G", generator, "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                 "-DCMAKE_C_COMPILER=gcc", "-DCMAKE_CXX_COMPILER=g++", *_disable],
                capture_output=True, text=True, timeout=300,
            )
            cdb = build_dir / "compile_commands.json"
            if cdb.is_file():
                logger.info("C++: cmake сгенерировал compile_commands.json")
                return cdb
            logger.warning("C++: cmake configure не дал compile_commands.json: %s",
                           (r.stderr or "")[:200])
        except subprocess.TimeoutExpired:
            logger.warning("C++: cmake configure таймаут (300s) — fallback на эвристику")
        except FileNotFoundError:
            logger.warning("C++: cmake не найден — fallback на эвристику")
        except Exception as e:
            logger.warning("C++: cmake configure ошибка: %s — fallback", e)
        return None

    # Паттерн имён cmake-option, отвечающих за сборку тестов/примеров/бенчмарков.
    # Их отключение убирает частую причину провала configure — поиск внешних
    # тест-зависимостей (GTest/Catch2/benchmark), которых нет в системе
    # (2026-07-10, leveldb: option(LEVELDB_BUILD_TESTS ON) → find_package(GTest)
    # FAILED → нет compile_commands → эвристика-флуд 299+ фантомов).
    _CMAKE_TEST_OPT_RE = re.compile(
        r"option\s*\(\s*([A-Za-z0-9_]*(?:BUILD_TESTS?|_TESTS?|_TESTING|"
        r"BUILD_EXAMPLES?|_EXAMPLES?|BUILD_BENCHMARKS?|_BENCHMARKS?|"
        r"BUILD_DOCS?|_DOCS?)[A-Za-z0-9_]*)",
        re.IGNORECASE,
    )

    def _cmake_disable_flags(self, project_path: Path) -> List[str]:
        """`-D<OPT>=OFF` для проектных option-ов тестов/примеров/бенчмарков +
        стандартный `-DBUILD_TESTING=OFF`. Убирает внешние тест-зависимости,
        роняющие cmake configure (и потому compile_commands)."""
        flags = ["-DBUILD_TESTING=OFF"]
        try:
            cml = project_path / "CMakeLists.txt"
            if cml.is_file():
                txt = cml.read_text(encoding="utf-8", errors="replace")
                seen = set()
                for m in self._CMAKE_TEST_OPT_RE.finditer(txt):
                    name = m.group(1)
                    if name and name.upper() not in seen:
                        seen.add(name.upper())
                        flags.append(f"-D{name}=OFF")
        except Exception as e:
            logger.debug("_cmake_disable_flags: %s", e)
        return flags

    def _parse_compile_db(self, cdb_path: Path) -> Dict[str, List[str]]:
        """{абсолютный путь файла → список флагов для -fsyntax-only}."""
        out: Dict[str, List[str]] = {}
        try:
            entries = json.loads(cdb_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            logger.warning("C++: не разобрал compile_commands.json: %s", e)
            return out
        for ent in entries:
            f = ent.get("file")
            if not f:
                continue
            if "arguments" in ent and ent["arguments"]:
                argv = list(ent["arguments"])
            else:
                argv = shlex.split(ent.get("command", ""), posix=False)
            flags: List[str] = []
            skip_next = False
            for i, a in enumerate(argv[1:], 1):  # [0] — сам компилятор
                if skip_next:
                    skip_next = False
                    continue
                if a in ("-o", "-c", "-MF", "-MT"):
                    skip_next = a in ("-o", "-MF", "-MT")
                    continue
                if any(a.startswith(p) for p in self._CDB_DROP_PREFIXES):
                    continue
                if a == f or a.endswith((".o", ".obj")):
                    continue
                flags.append(a)
            # Ключуем и по абсолютному пути, и по basename — пайплайн копирует
            # проект в sandbox, и абсолютные пути в БД (из клона, где гонялся
            # cmake) не совпадут с sandbox-копией; basename-fallback спасает
            # (2026-07-10). Коллизии basename редки; при коллизии берём первый.
            try:
                key = str(Path(f).resolve())
            except Exception:
                key = f
            out[key] = flags
            bn = Path(f).name
            out.setdefault("::name::" + bn, flags)
        return out

    def _db_source_files(self, compile_db: Dict[str, List[str]]) -> List[str]:
        """Список путей исходников из compile_db (без basename-дублей
        `::name::`), для ограничения static-анализа файлами реального билда
        (№2, 2026-07-10). Вендорные/служебные каталоги отсеиваем _cpp_excluded."""
        files: List[str] = []
        for key in compile_db:
            if key.startswith("::name::"):
                continue
            p = Path(key)
            if p.suffix.lower() not in self._TU_SUFFIXES:
                continue
            if _cpp_excluded(p):
                continue
            files.append(key)
        return files

    def _detect_std(self, project_path: Path, cpp: bool) -> str:
        """Стандарт языка: из CMakeLists (CXX_STANDARD/C_STANDARD) или дефолт.
        gnu-варианты permissive (меньше ложного syntax-шума)."""
        default = "gnu++17" if cpp else "gnu11"
        try:
            cml = project_path / "CMakeLists.txt"
            if cml.is_file():
                txt = cml.read_text(encoding="utf-8", errors="replace")
                key = "CXX_STANDARD" if cpp else "C_STANDARD"
                m = re.search(key + r"\s+(\d{2})", txt)
                if not m:
                    m = re.search((r"c\+\+" if cpp else r"\bc") + r"(\d{2})\b", txt, re.IGNORECASE)
                if m:
                    ver = m.group(1)
                    return f"gnu++{ver}" if cpp else f"gnu{ver}"
        except Exception as e:
            logger.debug("_detect_std: %s", e)
        return default

    # Порог: файл с таким числом build-config-ошибок (undeclared/syntax/include)
    # почти наверняка страдает от отсутствия билд-флагов, а не реальных багов.
    _BUILD_NOISE_CODES = frozenset({"GCC_UNDECLARED", "GCC_SYNTAX", "GCC_INCLUDE"})
    _BUILD_NOISE_PER_FILE = 8

    def _filter_build_config_noise(self, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Отсекает файлы, где лавина undeclared/syntax/include — признак
        отсутствия билд-конфигурации (std/defines/пути), а не настоящих багов
        (2026-07-09, fmt: 1306 фантомов без -std). Такой файл в отчёт не
        попадает целиком — иначе движок «чинит» несуществующие ошибки.
        Файлы с единичными диагностиками (реальные warning-и/баги) проходят."""
        from collections import Counter
        by_file_noise: Counter = Counter()
        for e in errors:
            if e.get("code") in self._BUILD_NOISE_CODES:
                by_file_noise[e.get("file", "")] += 1
        toxic = {f for f, n in by_file_noise.items() if n >= self._BUILD_NOISE_PER_FILE}
        if toxic:
            logger.info("C++: пропускаю %d файлов с build-config-шумом (нет std/defines): %s",
                        len(toxic), sorted(toxic)[:5])
        return [e for e in errors if e.get("file", "") not in toxic]

    def _parse_gcc_output(self, output: str, project_path: Path) -> List[Dict[str, Any]]:
        errors = []
        # Формат: main.cpp:10:5: error: expected ';' before 'return'
        # Также ловим 'fatal error' (g++ для отсутствующих заголовков и т.п.)
        pattern = re.compile(r'^(.+?):(\d+):(\d+):\s+(fatal error|error|warning):\s+(.+)$', re.MULTILINE)

        for match in pattern.finditer(output):
            file_path = match.group(1)
            line = int(match.group(2))
            column = int(match.group(3))
            severity = match.group(4)
            message = match.group(5)

            # g++ не выдаёт MSVC-коды (CXXXX), поэтому строим синтетический
            # код из текстовых паттернов сообщения. Используется и для
            # error_type, и для правил/каскад-детекта в cpp_support.
            code = self._extract_synthetic_code(message)

            try:
                rel_path = Path(file_path).relative_to(project_path)
            except ValueError:
                rel_path = Path(file_path).name

            errors.append({
                "file": str(rel_path),
                "line": line,
                "column": column,
                "message": message,
                "code": code,
                "severity": severity,
                "error_type": self._classify_error(code, message),
            })

        return errors

    @staticmethod
    def _extract_synthetic_code(message: str) -> str:
        """Сопоставляет текст сообщения g++ синтетическому коду.

        Возвращает строку вида 'GCC_*', либо '' если паттерн не распознан.
        Эти коды используются `_classify_error` и правилами в `cpp_support`.
        """
        m = message.lower()
        if "expected ';'" in m or "missing terminating" in m:
            return "GCC_SEMI"
        if "expected '}'" in m or "expected '{'" in m:
            return "GCC_BRACE"
        if "expected ')'" in m or "expected '('" in m:
            return "GCC_PAREN"
        if "no such file" in m:
            return "GCC_INCLUDE"
        if "was not declared" in m or "undeclared" in m:
            return "GCC_UNDECLARED"
        if "redefinition" in m:
            return "GCC_REDEFINITION"
        if ("expected unqualified-id" in m or "expected primary-expression" in m
                or "expected expression" in m or "unterminated" in m or "unclosed" in m):
            return "GCC_SYNTAX"
        return ""

    @staticmethod
    def _classify_error(code: str, message: str) -> str:
        if code in ("GCC_SEMI", "GCC_BRACE", "GCC_PAREN", "GCC_SYNTAX"):
            return "syntax"
        if code == "GCC_INCLUDE":
            return "dependency"
        if code in ("GCC_UNDECLARED", "GCC_REDEFINITION"):
            return "compile"
        # обратная совместимость с MSVC (на случай вывода cl.exe)
        if code in ("C2143", "C2059"):
            return "syntax"
        if code == "C1083":
            return "dependency"
        if code == "C2065":
            return "compile"
        return "unknown"