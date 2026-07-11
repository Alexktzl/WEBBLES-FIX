"""
Детерминированный слой инференса C++ зависимостей (без LLM, без сети).

Параллель `analysis/dependency_inference.py` (Rust) и
`analysis/csharp_dependency_inference.py` (C#) для C++:
  * сканер `#include <X/Y.hpp>` в `.cpp/.cc/.cxx/.hpp/.h/.hxx` файлах;
  * курируемая таблица `INCLUDE_TO_CMAKE_PACKAGE` (Boost/fmt/gtest/OpenCV
    и пр., с готовым `find_package` + `target_link_libraries` рецептом);
  * правка `CMakeLists.txt` (или `Makefile` через `-l...`): добавление
    `find_package(...)` блока И `target_link_libraries(... PRIVATE ...)`,
    без замены существующих;
  * `recover()` оркеструет: бэкап → infer → apply → опц. `cmake -B build`
    или `make -n` валидация → откат при ухудшении.

Honest-ограничение: C++ build-системы разнообразны (CMake/Make/Bazel/Meson/
Conan/vcpkg). Сейчас поддерживаем CMake (наиболее распространён) и базовый
Makefile. Bazel/Meson — оставляем пайплайну/в review.
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

# Стандартные C/C++ заголовки — НЕ требуют пакета.
_STDLIB_PREFIXES: Set[str] = {
    "iostream", "vector", "string", "map", "unordered_map", "set",
    "unordered_set", "array", "deque", "list", "forward_list", "stack",
    "queue", "algorithm", "memory", "functional", "utility", "tuple",
    "optional", "variant", "any", "type_traits", "limits", "numeric",
    "iterator", "ranges", "concepts", "compare", "chrono", "thread",
    "mutex", "condition_variable", "future", "atomic", "filesystem",
    "fstream", "sstream", "iomanip", "cstdio", "cstdlib", "cstring",
    "cmath", "cstdint", "cstddef", "cctype", "cassert", "cerrno",
    "exception", "stdexcept", "system_error", "new", "typeinfo",
    "initializer_list", "regex", "random", "bitset", "complex",
    "valarray", "locale", "codecvt", "charconv", "string_view",
    "span", "format", "source_location", "stop_token", "barrier",
    "latch", "semaphore", "syncstream", "expected",
    # C-style heads with .h
    "stdio.h", "stdlib.h", "string.h", "math.h", "stdint.h", "stddef.h",
    "ctype.h", "assert.h", "errno.h", "limits.h", "float.h", "time.h",
    "signal.h", "setjmp.h", "stdarg.h", "stdbool.h", "fcntl.h", "unistd.h",
    "sys/types.h", "sys/stat.h", "sys/wait.h", "sys/socket.h",
    # platform-specific noise — не пакетируем
    "windows.h", "winsock2.h",
}

_SKIP_DIRS = {"build", "cmake-build-debug", "cmake-build-release", "out",
              ".git", "node_modules", ".vs", ".vscode", "vcpkg_installed",
              ".webbles_fix"}

_MAX_FILES = 2000
_MAX_FILE_BYTES = 1_000_000

# Расширения C/C++ source/header.
_CXX_EXTS = {".cpp", ".cc", ".cxx", ".c", ".hpp", ".h", ".hxx", ".inl"}


@dataclass
class CmakePackage:
    """Готовый рецепт подключения пакета в CMake."""
    name: str                       # find_package(NAME ...)
    components: Optional[List[str]] = None   # COMPONENTS X Y
    link_targets: List[str] = field(default_factory=list)  # target_link_libraries

    def find_package_line(self) -> str:
        if self.components:
            comp = " ".join(self.components)
            return f"find_package({self.name} REQUIRED COMPONENTS {comp})"
        return f"find_package({self.name} REQUIRED)"


# ---------------------------------------------------------------------------
# Курируемая таблица: префикс include → CmakePackage.
# Префикс — первый сегмент пути (до /). Расширяется.
# ---------------------------------------------------------------------------
INCLUDE_TO_CMAKE_PACKAGE: Dict[str, CmakePackage] = {
    "boost":     CmakePackage(name="Boost", components=["system", "filesystem"],
                              link_targets=["Boost::system", "Boost::filesystem"]),
    "fmt":       CmakePackage(name="fmt", link_targets=["fmt::fmt"]),
    "spdlog":    CmakePackage(name="spdlog", link_targets=["spdlog::spdlog"]),
    "nlohmann":  CmakePackage(name="nlohmann_json", link_targets=["nlohmann_json::nlohmann_json"]),
    "gtest":     CmakePackage(name="GTest", link_targets=["GTest::gtest", "GTest::gtest_main"]),
    "gmock":     CmakePackage(name="GTest", link_targets=["GTest::gmock"]),
    "opencv2":   CmakePackage(name="OpenCV", link_targets=["${OpenCV_LIBS}"]),
    "Eigen":     CmakePackage(name="Eigen3", link_targets=["Eigen3::Eigen"]),
    "Qt5":       CmakePackage(name="Qt5", components=["Core"],
                              link_targets=["Qt5::Core"]),
    "Qt6":       CmakePackage(name="Qt6", components=["Core"],
                              link_targets=["Qt6::Core"]),
    "GLFW":      CmakePackage(name="glfw3", link_targets=["glfw"]),
    "glm":       CmakePackage(name="glm", link_targets=["glm::glm"]),
    "asio":      CmakePackage(name="asio", link_targets=["asio::asio"]),
    "yaml-cpp":  CmakePackage(name="yaml-cpp", link_targets=["yaml-cpp"]),
    "tinyxml2":  CmakePackage(name="tinyxml2", link_targets=["tinyxml2::tinyxml2"]),
    "cpprest":   CmakePackage(name="cpprestsdk", link_targets=["cpprestsdk::cpprest"]),
    "absl":      CmakePackage(name="absl", link_targets=["absl::base", "absl::strings"]),
    "protobuf":  CmakePackage(name="Protobuf", link_targets=["protobuf::libprotobuf"]),
    "grpcpp":    CmakePackage(name="gRPC", link_targets=["gRPC::grpc++"]),
    "curl":      CmakePackage(name="CURL", link_targets=["CURL::libcurl"]),
}


@dataclass
class CppInferenceResult:
    used_includes: Set[str] = field(default_factory=set)
    existing_find_packages: Set[str] = field(default_factory=set)
    missing: Dict[str, CmakePackage] = field(default_factory=dict)
    skipped_unknown: Set[str] = field(default_factory=set)
    explanations: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.missing)


_RE_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def _strip_comment(line: str) -> str:
    idx = line.find("//")
    if idx >= 0:
        line = line[:idx]
    return line


class CppDependencyInference:
    """Детерминированный инференс отсутствующих C++ пакетов в CMakeLists.txt."""

    def __init__(self):
        self.table = INCLUDE_TO_CMAKE_PACKAGE

    # ---- скан исходников -------------------------------------------------
    def _iter_cpp_files(self, root: Path):
        count = 0
        for path in sorted(root.rglob("*")):
            if count >= _MAX_FILES:
                break
            if not path.is_file() or path.suffix.lower() not in _CXX_EXTS:
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            count += 1
            yield path, text

    def _scan_includes(self, root: Path) -> Set[str]:
        """Возвращает множество include-путей (как написано в `#include <...>`)."""
        seen: Set[str] = set()
        for _path, text in self._iter_cpp_files(root):
            for raw in text.splitlines():
                line = _strip_comment(raw)
                m = _RE_INCLUDE.match(line)
                if m:
                    seen.add(m.group(1).strip())
        return seen

    @staticmethod
    def _include_prefix(inc: str) -> str:
        """Первый сегмент пути include (до '/')."""
        return inc.split("/", 1)[0]

    # ---- CMakeLists.txt --------------------------------------------------
    @staticmethod
    def _find_cmakelists(root: Path) -> Optional[Path]:
        cml = root / "CMakeLists.txt"
        if cml.is_file():
            return cml
        for p in root.rglob("CMakeLists.txt"):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            return p
        return None

    @staticmethod
    def _existing_find_packages(cmake_text: str) -> Set[str]:
        rx = re.compile(r"^\s*find_package\s*\(\s*([A-Za-z_][\w]*)", re.MULTILINE | re.IGNORECASE)
        return {m.group(1).lower() for m in rx.finditer(cmake_text or "")}

    # ---- инференс --------------------------------------------------------
    def infer(self, root) -> CppInferenceResult:
        root = Path(root)
        res = CppInferenceResult()
        try:
            includes = self._scan_includes(root)
            res.used_includes = includes
            cml = self._find_cmakelists(root)
            existing: Set[str] = set()
            if cml is not None:
                try:
                    existing = self._existing_find_packages(cml.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    existing = set()
            res.existing_find_packages = existing

            added: Set[str] = set()
            for inc in sorted(includes):
                if inc in _STDLIB_PREFIXES:
                    continue
                # Проверка по полному пути сначала (например, "boost/asio.hpp"):
                # ключи таблицы — префиксы.
                prefix = self._include_prefix(inc)
                # Также пробуем без точечного префикса (например, "Eigen/Dense" → Eigen)
                pkg = self.table.get(prefix) or self.table.get(inc.split(".", 1)[0])
                if pkg is None:
                    # отбрасываем системные/полные пути типа "/usr/include/..."
                    if "/" not in inc and inc not in _STDLIB_PREFIXES:
                        res.skipped_unknown.add(inc)
                    continue
                if pkg.name.lower() in existing or pkg.name in added:
                    continue
                added.add(pkg.name)
                res.missing[pkg.name] = pkg
                res.explanations.append(
                    f"{pkg.name}: #include <{inc}>, нет find_package — добавляю {pkg.find_package_line()}"
                )
            for sk in sorted(res.skipped_unknown):
                res.explanations.append(
                    f"{sk}: include не в курируемой таблице → пропущен"
                )
        except Exception as e:
            logger.warning("CppDependencyInference.infer упал: %s", e)
        return res

    # ---- правка CMakeLists.txt -------------------------------------------
    def apply_to_cmakelists(self, cml_path: Path, missing: Dict[str, CmakePackage]) -> bool:
        """Добавляет find_package(...) перед первой add_executable/add_library
        (или в конец, если их нет). Не трогает существующие команды.

        target_link_libraries для существующих target'ов — best-effort: если
        находим первую `add_executable(<NAME> ...)` или `add_library(<NAME> ...)`,
        добавляем `target_link_libraries(<NAME> PRIVATE <links...>)` сразу
        после неё. Это покрывает 80% типовых CMakeLists.
        """
        if not missing or not cml_path.is_file():
            return False
        try:
            original = cml_path.read_text(encoding="utf-8")
        except OSError:
            return False

        # Имя первого target'а (если найдено).
        target_rx = re.compile(
            r"^\s*add_(?:executable|library)\s*\(\s*([A-Za-z_][\w]*)",
            re.MULTILINE | re.IGNORECASE,
        )
        m = target_rx.search(original)
        target_name = m.group(1) if m else None

        find_lines = [pkg.find_package_line() for pkg in missing.values()]
        link_lines: List[str] = []
        if target_name:
            for pkg in missing.values():
                if pkg.link_targets:
                    link_lines.append(
                        f"target_link_libraries({target_name} PRIVATE "
                        + " ".join(pkg.link_targets) + ")"
                    )

        block_find = "\n".join(find_lines) + "\n"
        block_link = ("\n".join(link_lines) + "\n") if link_lines else ""

        new_text = original
        if m:
            # вставляем find_package блок ПЕРЕД add_executable/add_library
            insert_pos = m.start()
            new_text = (
                original[:insert_pos]
                + "# Added by webbles-fix CppDependencyInference\n"
                + block_find
                + "\n"
                + original[insert_pos:]
            )
            if block_link:
                # вставляем link-команды сразу после строки add_*
                eol = new_text.find("\n", new_text.find(m.group(0), insert_pos))
                if eol > 0:
                    new_text = (
                        new_text[:eol + 1]
                        + block_link
                        + new_text[eol + 1:]
                    )
        else:
            tail = "" if original.endswith("\n") else "\n"
            new_text = (
                original + tail
                + "\n# Added by webbles-fix CppDependencyInference\n"
                + block_find
                + block_link
            )

        if new_text == original:
            return False
        try:
            cml_path.write_text(new_text, encoding="utf-8", newline="")
            return True
        except OSError as e:
            logger.warning("CppDependencyInference: запись CMakeLists.txt упала: %s", e)
            return False

    # ---- оркестрация -----------------------------------------------------
    @staticmethod
    def _cmake_error_count(root: Path) -> Optional[int]:
        """Считаем ошибки `cmake -S . -B build` (если cmake доступен).
        Не делаем полный `cmake --build` — это слишком долго."""
        if not shutil.which("cmake"):
            return None
        try:
            proc = subprocess.run(
                ["cmake", "-S", str(root), "-B", str(root / ".webbles_cmake_check")],
                cwd=str(root), capture_output=True, text=True, timeout=180,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return sum(1 for ln in out.splitlines() if re.search(r"CMake Error", ln))

    def recover(self, root, run_cmake_check: bool = True) -> CppInferenceResult:
        """Пред-пайплайн фаза: добавить отсутствующие find_package/link.

        Аддитивно, с возможной cmake-валидацией и откатом.
        Никогда не роняет пайплайн.
        """
        root = Path(root)
        res = self.infer(root)
        if not res.has_changes:
            return res
        cml = self._find_cmakelists(root)
        if cml is None or not cml.is_file():
            res.explanations.append("CMakeLists.txt не найден — пропуск C++ dependency recovery")
            res.missing = {}
            return res
        try:
            backup = cml.read_text(encoding="utf-8")
        except OSError:
            return res

        before = self._cmake_error_count(root) if run_cmake_check else None

        if not self.apply_to_cmakelists(cml, res.missing):
            res.explanations.append("Правка CMakeLists.txt не применилась")
            res.missing = {}
            return res

        if run_cmake_check and before is not None:
            after = self._cmake_error_count(root)
            if after is not None and after > before:
                try:
                    cml.write_text(backup, encoding="utf-8", newline="")
                except OSError:
                    pass
                res.explanations.append(
                    f"ROLLBACK: после правки cmake-ошибок стало больше ({before}->{after}) — откат"
                )
                res.missing = {}
                return res
            res.explanations.append(f"ACCEPT: добавлено {len(res.missing)} пакетов; cmake-ошибок {before}->{after}")
        else:
            res.explanations.append(
                "ACCEPT (без cmake-валидации): добавлено " + ", ".join(res.missing.keys())
            )
        return res
