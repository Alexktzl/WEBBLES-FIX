"""
Анализатор C# для Webbles Fix.
Использует `dotnet build` с ВКЛЮЧЁННЫМИ Roslyn-анализаторами для поиска ошибок.

ЗАЧЕМ анализаторы (2026-07-10, достройка C#-этапа). Голый `dotnet build` даёт
только диагностику КОМПИЛЯТОРА (CS-коды: синтаксис, типы, nullable CS8xxx). Но
БАГ-паттерны (мёртвый код, утечки IDisposable, забытый результат, неверный
формат-стринг) ловят Roslyn-АНАЛИЗАТОРЫ (CA*), которые по умолчанию почти
все выключены. Включаем их через `-p:EnableNETAnalyzers=true -p:AnalysisMode=All`
(прямой аналог cppcheck/clang-tidy для C++). Стилевой шум (IDE*/SA*/StyleCop +
опинион-CA типа локализации/naming) отбрасываем — направление фазы: чиним
БАГИ, не навязываем стиль (см. `_CSHARP_NOISE_*`, аналог `_TIDY_CHECKS`).
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CsharpAnalyzer:
    """Анализирует код C# через `dotnet build` + Roslyn-анализаторы."""

    _SKIP = ("bin", "obj", ".git", ".vs", ".vscode", ".idea",
             ".webbles_fix", ".webbles_backups", ".webbles",
             "node_modules", "target", "__pycache__", "packages")

    # Пропсы, включающие баг-анализаторы (не стиль). AnalysisMode=All даёт весь
    # набор CA; шум отфильтруем денилистом. EnforceCodeStyleInBuild НЕ включаем —
    # это IDE*-стиль (форматирование), чистый шум для баг-ремонта.
    # ВАЖНО `--no-incremental`: `dotnet build` инкрементальный — на уже
    # собранном проекте НЕ пересобирает и анализаторы МОЛЧАТ (0 находок,
    # 2026-07-10). Форсим полную пересборку, иначе повторные скан-циклы
    # (валидация) не увидят диагностику. `-v:minimal` показывает warning'и
    # (в `-v:quiet` они могут не печататься построчно).
    _ANALYZER_PROPS = [
        "-p:EnableNETAnalyzers=true",
        "-p:AnalysisMode=All",
        "-p:EnforceCodeStyleInBuild=false",
        "-p:RunAnalyzersDuringBuild=true",
        "-p:TreatWarningsAsErrors=false",
        "--no-incremental",
        "-nologo",
        "-v:minimal",
    ]

    def analyze(self, project_path: Path,
                files: Optional[List] = None,
                **_ignored) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []

        # ОБЯЗАТЕЛЬНО абсолютный путь (2026-07-10): при относительном project_path
        # rglob даёт относительные target-пути, а `cwd=project_path` (тоже
        # относительный) удваивает путь → dotnet build не находит проект → тихий
        # 0-результат. Тот же класс, что C++ cmake doubled-path.
        project_path = Path(project_path).resolve()

        csproj_files = [p for p in project_path.rglob("*.csproj")
                        if not any(part in self._SKIP for part in p.parts)]

        if not csproj_files:
            logger.warning("Не найдены .csproj файлы, пропускаем анализ C#")
            return errors

        # Собираем БИБЛИОТЕЧНЫЕ проекты, НЕ тест/бенчмарк/сэмплы (2026-07-10,
        # Cronos): сборка .sln целиком тянет тест-проекты с тяжёлыми внешними
        # deps (xunit/benchmarkdotnet) → restore падает (особенно офлайн) и
        # шумит; это не наш код. Аналог C++ «static только по build-файлам».
        lib_projects = [p for p in csproj_files if not self._is_test_project(p)]
        targets: List[Path] = lib_projects or csproj_files  # fallback: всё, если только тесты

        # Английские диагностики (не локаль Windows) — стабильнее для парса и
        # промптов LLM. NuGet-restore не душим (нужен для build).
        import os as _os
        _env = dict(_os.environ)
        _env["DOTNET_CLI_UI_LANGUAGE"] = "en"

        raw_out: List[str] = []
        for target in targets:
            try:
                result = subprocess.run(
                    ["dotnet", "build", str(target), *self._ANALYZER_PROPS],
                    cwd=project_path, capture_output=True, text=True, timeout=600,
                    env=_env,
                )
                raw_out.append((result.stdout or "") + "\n" + (result.stderr or ""))
            except subprocess.TimeoutExpired:
                logger.error("dotnet build timed out на %s", target)
            except FileNotFoundError:
                logger.warning("dotnet не найден в PATH")
                return errors
            except Exception as e:
                logger.warning("Ошибка анализа C# на %s: %s", target, e)

        parsed = self._parse_dotnet_output("\n".join(raw_out), project_path)
        errors = self._filter_noise(self._dedup(parsed))
        logger.info("Всего найдено ошибок C#: %d (после дедупа/фильтра)", len(errors))
        return errors

    # Сигналы тест/бенчмарк/сэмпл-проекта: путь/имя или ссылки на тест-фреймворки.
    _TEST_NAME_RE = re.compile(
        r"(?:^|[./\\_-])(?:tests?|benchmarks?|samples?|specs?|e2e|fixtures?)"
        r"(?:[./\\_-]|$)", re.IGNORECASE)
    _TEST_PKG_RE = re.compile(
        r"(xunit|nunit|mstest|Microsoft\.NET\.Test\.Sdk|BenchmarkDotNet|"
        r"FluentAssertions|Moq|Shouldly|<IsTestProject>\s*true)", re.IGNORECASE)

    def _is_test_project(self, csproj: Path) -> bool:
        # По имени файла и имени НЕПОСРЕДСТВЕННОГО каталога (не по всему
        # абсолютному пути — родители вне проекта могут случайно содержать
        # 'test', напр. tmp-каталог pytest или .../MyTestApp/..., 2026-07-10).
        for seg in (csproj.stem, csproj.parent.name):
            if seg and self._TEST_NAME_RE.search(seg):
                return True
        # по содержимому (ссылки на тест-фреймворки)
        try:
            txt = csproj.read_text(encoding="utf-8", errors="replace")
            if self._TEST_PKG_RE.search(txt):
                return True
        except Exception:
            pass
        return False

    # Хвост "[C:\...\project.csproj]" / "[project.csproj::TargetFramework=net10.0]",
    # который dotnet приклеивает к сообщению — вырезаем (не часть диагностики).
    _MSG_PROJECT_TAIL = re.compile(r"\s*\[[^\]]*\.(?:csproj|sln|vbproj)[^\]]*\]\s*$")

    def _parse_dotnet_output(self, output: str, project_path: Path) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []
        # Формат: Program.cs(10,5): error CS1002: message [project.csproj]
        # Код-сегмент: 1-6 заглавных букв + 3-5 цифр (CS/CA/IDE/IDISP/SA/RCS/S...)
        pattern = re.compile(
            r'^(.+?)\((\d+),(\d+)\):\s+(error|warning)\s+([A-Z]{1,6}\d{3,5}):\s+(.+)$',
            re.MULTILINE,
        )
        for match in pattern.finditer(output):
            file_path = match.group(1).strip()
            line = int(match.group(2))
            column = int(match.group(3))
            severity = match.group(4)
            code = match.group(5)
            message = self._MSG_PROJECT_TAIL.sub("", match.group(6)).strip()

            # Диагностика в файле ВНЕ проекта (contentFiles NuGet-зависимостей
            # типа polyshim в ~/.nuget/packages/..., SDK-референсы) — не наш код,
            # чинить нельзя, отбрасываем (2026-07-10, CliWrap). Аналог C++
            # foreign-header/sandbox-фильтра. ОТНОСИТЕЛЬНЫЕ пути (dotnet иногда
            # печатает имя файла относительно проекта) — это наш код, оставляем.
            fp = Path(file_path)
            if fp.is_absolute():
                try:
                    rel_path = fp.resolve().relative_to(project_path.resolve())
                except (ValueError, OSError):
                    continue  # абсолютный путь вне проекта — nuget/SDK, дропаем
            else:
                rel_path = fp  # относительный = проектный, оставляем как есть

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
    def _dedup(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """dotnet печатает каждую диагностику дважды (билд каждого TargetFramework
        + summary). Дедуп по (file, line, column, code)."""
        seen = set()
        out = []
        for e in errors:
            key = (str(e.get("file", "")).replace("\\", "/"),
                   e.get("line", 0), e.get("column", 0), e.get("code", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        return out

    # ---- Шумовые коды: стиль/опинион, не баги (2026-07-10, аналог C++ денилиста) ----
    # Префиксы чисто-стилевых анализаторов: IDE (форматирование/refactor),
    # SA/SX (StyleCop). Отбрасываем целиком.
    _CSHARP_NOISE_PREFIXES = ("IDE", "SA", "SX")
    # Точечный денилист опинион/стиль/perf-CA, которые не являются дефектами:
    _CSHARP_NOISE_CODES = frozenset({
        "CA1303",  # не передавать литералы как локализуемые
        "CA1305", "CA1304", "CA1307", "CA1310", "CA1311",  # культура/сравнение — опинион
        "CA1822",  # пометить как static (perf/стиль)
        "CA1852",  # запечатать internal-тип (perf/стиль)
        "CA1812",  # неинстанцируемый internal-класс
        "CA2007",  # ConfigureAwait — опинион, вал в приложениях
        "CA1062",  # валидировать public-аргументы — опинион, очень шумно
        "CA1031",  # не ловить общий Exception — опинион
        "CA1002", "CA1051", "CA1707", "CA1710", "CA1711", "CA1716",
        "CA1720", "CA1724", "CA1725", "CA1801", "CA1805",  # naming/design-опинион
        "CA1848", "CA1727",  # LoggerMessage-delegate — опинион
        "CA5394",  # небезопасный Random — часто ложно-положителен в не-крипто
        "CA1014",  # CLSCompliant-атрибут сборки
        "CA1063", "CA1816",  # Dispose-паттерн-детали (стиль реализации)
        "CA1515",  # сделать public-типы internal — опинион про API-surface
    })

    def _filter_noise(self, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for e in errors:
            code = e.get("code", "")
            if code in self._CSHARP_NOISE_CODES:
                continue
            if any(code.startswith(p) and code[len(p):].isdigit()
                   for p in self._CSHARP_NOISE_PREFIXES):
                continue
            out.append(e)
        return out

    # Распространённые коды Roslyn, сгруппированные по природе ошибки.
    # И CS1xxx, и CS0xxx содержат и синтаксис, и семантику, поэтому
    # огульная классификация по префиксу даёт неверные результаты.
    _SYNTAX_CODES = frozenset({
        "CS1001", "CS1002", "CS1003", "CS1004", "CS1010", "CS1011", "CS1012",
        "CS1013", "CS1014", "CS1019", "CS1020", "CS1023", "CS1024", "CS1026",
        "CS1027", "CS1031", "CS1037", "CS1513", "CS1519", "CS1525",
    })
    _DEPENDENCY_CODES = frozenset({"CS0006", "CS0234", "CS0246"})
    _TYPE_CODES = frozenset({
        "CS0019", "CS0029", "CS0030", "CS0103", "CS0118", "CS0120", "CS0122",
        "CS0123", "CS0161", "CS0165", "CS0177", "CS0266",
        "CS1061", "CS1503", "CS1729",
    })
    # Nullable-flow диагностика (Nullable=enable): разыменование/присваивание
    # возможного null — реальные потенциальные NRE, важный класс баг-находок.
    _NULLABLE_CODES = frozenset({
        "CS8600", "CS8601", "CS8602", "CS8603", "CS8604", "CS8605",
        "CS8607", "CS8608", "CS8609", "CS8610", "CS8614", "CS8618",
        "CS8619", "CS8620", "CS8625", "CS8629", "CS8631", "CS8634",
    })

    # Префиксы Roslyn-анализаторов (не CS) — наша классификация по семейству.
    _ANALYZER_PREFIX_TO_TYPE = {
        "CA":    "lint",        # NetAnalyzers (style/perf/security)
        "IDE":   "style",       # IDE0001-IDE0500 — стилевые/refactor
        "IDISP": "resource",    # DisposableAnalyzers — IDisposable утечки
        "SA":    "style",       # StyleCop
        "SX":    "style",       # StyleCop extensions
        "RCS":   "lint",        # Roslynator
        "AD":    "lint",
        "ASYNC": "concurrency",
        "ASP":   "lint",        # ASP.NET-specific
        "NU":    "dependency",  # NuGet-related
        "VSTHRD": "concurrency",
        "S":     "lint",        # SonarAnalyzer.CSharp (S1234)
    }

    @classmethod
    def _classify_error(cls, code: str, message: str) -> str:
        if code in cls._SYNTAX_CODES:
            return "syntax"
        if code in cls._DEPENDENCY_CODES:
            return "dependency"
        if code in cls._NULLABLE_CODES:
            return "nullable"
        if code in cls._TYPE_CODES:
            return "type"
        # Префиксы Roslyn-анализаторов (CA*, IDE*, IDISP*, SA*, RCS*, ...)
        for prefix, etype in cls._ANALYZER_PREFIX_TO_TYPE.items():
            if code.startswith(prefix) and code[len(prefix):].isdigit():
                return etype
        # Эвристика по тексту: для незарегистрированных кодов.
        msg_low = message.lower()
        if "expected" in msg_low and any(
            ch in msg_low for ch in ("'{'", "'}'", "'('", "')'", "';'")
        ):
            return "syntax"
        if "does not exist" in msg_low or "could not be found" in msg_low:
            return "dependency"
        if ("cannot convert" in msg_low or "no overload" in msg_low
                or "does not contain a definition" in msg_low):
            return "type"
        # Большинство CS0xxx — семантика времени компиляции.
        if code.startswith("CS0"):
            return "compile"
        return "unknown"
