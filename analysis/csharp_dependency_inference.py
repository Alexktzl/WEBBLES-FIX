"""
Детерминированный слой инференса NuGet-зависимостей C# (без LLM, без сети).

Параллель `analysis/dependency_inference.py` (Rust) для C#:
  * сканер `using` в `.cs` файлах;
  * курируемая таблица `USING_TO_PACKAGE` (NuGet-пакеты с известным
    namespace-mapping'ом);
  * добавление `<PackageReference Include="X" Version="Y" />` в `.csproj`
    через text-edit XML (комментарии/форматирование сохраняем; существующие
    PackageReference не трогаем);
  * `recover()` оркеструет: бэкап → infer → apply → опц. `dotnet build`
    валидация → откат при ухудшении.

Принципы те же: детерминированно, без сети/угадывания версий, untagged
неизвестные `using` пропускаем (оставляем пайплайну/в review).
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

# Стандартные .NET namespace-корни, которые НЕ требуют NuGet (BCL).
_BCL_ROOTS: Set[str] = {
    "System", "Microsoft.Win32", "Microsoft.CSharp", "Microsoft.VisualBasic",
}

_SKIP_DIRS = {"bin", "obj", "node_modules", ".git", "packages", ".vs",
              ".webbles_fix"}

_MAX_FILES = 2000
_MAX_FILE_BYTES = 1_000_000

# ---------------------------------------------------------------------------
# Курируемая таблица: первый сегмент namespace → (NuGet-пакет, версия).
# Версии — мажор-консервативные. Расширяется по мере востребованности.
# ---------------------------------------------------------------------------
USING_TO_PACKAGE: Dict[str, Tuple[str, str]] = {
    "Newtonsoft":             ("Newtonsoft.Json", "13.0.3"),
    "Serilog":                ("Serilog", "3.1.1"),
    "AutoMapper":             ("AutoMapper", "12.0.1"),
    "FluentValidation":       ("FluentValidation", "11.9.0"),
    "Polly":                  ("Polly", "8.2.0"),
    "Dapper":                 ("Dapper", "2.1.28"),
    "MediatR":                ("MediatR", "12.2.0"),
    "MoreLinq":               ("MoreLinq", "4.0.0"),
    "Humanizer":              ("Humanizer.Core", "2.14.1"),
    "RestSharp":              ("RestSharp", "110.2.0"),
    "NUnit":                  ("NUnit", "4.0.1"),
    "Xunit":                  ("xunit", "2.6.6"),
    "FluentAssertions":       ("FluentAssertions", "6.12.0"),
    "Moq":                    ("Moq", "4.20.70"),
}

# Прямой mapping FQN → пакет (для namespace, которые НЕ совпадают
# с первым сегментом — например `Microsoft.EntityFrameworkCore`).
FQN_TO_PACKAGE: Dict[str, Tuple[str, str]] = {
    "Microsoft.EntityFrameworkCore":         ("Microsoft.EntityFrameworkCore", "8.0.1"),
    "Microsoft.AspNetCore":                  ("Microsoft.AspNetCore.App", "8.0.0"),
    "Microsoft.Extensions.Hosting":          ("Microsoft.Extensions.Hosting", "8.0.0"),
    "Microsoft.Extensions.DependencyInjection": ("Microsoft.Extensions.DependencyInjection", "8.0.0"),
    "Microsoft.Extensions.Logging":          ("Microsoft.Extensions.Logging", "8.0.0"),
    "Microsoft.Extensions.Configuration":    ("Microsoft.Extensions.Configuration", "8.0.0"),
}


@dataclass
class PackageSpec:
    name: str
    version: str

    def to_xml(self) -> str:
        return f'<PackageReference Include="{self.name}" Version="{self.version}" />'


@dataclass
class CsharpInferenceResult:
    used_namespaces: Set[str] = field(default_factory=set)
    existing_packages: Set[str] = field(default_factory=set)
    missing: Dict[str, PackageSpec] = field(default_factory=dict)
    skipped_unknown: Set[str] = field(default_factory=set)
    explanations: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.missing)


_RE_USING = re.compile(
    r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:[A-Za-z_][\w]*\s*=\s*)?"
    r"((?:[A-Za-z_][\w]*\.?)+)\s*;"
)


def _strip_comment(line: str) -> str:
    idx = line.find("//")
    return line[:idx] if idx >= 0 else line


class CsharpDependencyInference:
    """Детерминированный инференс отсутствующих NuGet-пакетов в .csproj."""

    def __init__(self):
        self.using_to_pkg = USING_TO_PACKAGE
        self.fqn_to_pkg = FQN_TO_PACKAGE

    # ---- скан исходников -------------------------------------------------
    def _iter_cs_files(self, root: Path):
        count = 0
        for path in sorted(root.rglob("*.cs")):
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

    def _scan_usings(self, root: Path) -> Set[str]:
        """Возвращает множество полных namespace из `using X.Y.Z;` директив."""
        usings: Set[str] = set()
        for _path, text in self._iter_cs_files(root):
            for raw in text.splitlines():
                line = _strip_comment(raw)
                m = _RE_USING.match(line)
                if m:
                    usings.add(m.group(1).rstrip("."))
        return usings

    # ---- .csproj ---------------------------------------------------------
    @staticmethod
    def _find_csproj(root: Path) -> Optional[Path]:
        for p in root.rglob("*.csproj"):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            return p
        return None

    @staticmethod
    def _existing_packages(csproj_text: str) -> Set[str]:
        """Имена уже подключённых PackageReference (case-insensitive)."""
        pkgs = set()
        rx = re.compile(r'<PackageReference\s+Include\s*=\s*"([^"]+)"', re.IGNORECASE)
        for m in rx.finditer(csproj_text or ""):
            pkgs.add(m.group(1).strip().lower())
        return pkgs

    # ---- инференс --------------------------------------------------------
    def _pick_package(self, ns: str) -> Optional[PackageSpec]:
        """Подбирает PackageSpec для namespace по двум таблицам.

        Сначала ищет точный FQN-префикс (более длинный → выигрывает),
        затем по первому сегменту в using_to_pkg.
        """
        # FQN: ищем самый длинный префикс
        best_key = None
        best_len = -1
        for key in self.fqn_to_pkg:
            if ns == key or ns.startswith(key + "."):
                if len(key) > best_len:
                    best_len = len(key)
                    best_key = key
        if best_key:
            name, ver = self.fqn_to_pkg[best_key]
            return PackageSpec(name=name, version=ver)
        # first segment
        first = ns.split(".", 1)[0]
        hit = self.using_to_pkg.get(first)
        if hit:
            return PackageSpec(name=hit[0], version=hit[1])
        return None

    def infer(self, root) -> CsharpInferenceResult:
        root = Path(root)
        res = CsharpInferenceResult()
        try:
            usings = self._scan_usings(root)
            res.used_namespaces = usings

            csproj = self._find_csproj(root)
            existing = set()
            if csproj is not None and csproj.is_file():
                try:
                    existing = self._existing_packages(csproj.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    existing = set()
            res.existing_packages = existing

            # Кандидаты: namespace не из BCL и не покрытый существующим пакетом.
            added: Set[str] = set()  # уже добавленные пакеты (по имени)
            for ns in sorted(usings):
                first = ns.split(".", 1)[0]
                if first in _BCL_ROOTS and not any(ns.startswith(k + ".") for k in self.fqn_to_pkg):
                    # это BCL (System.*) и не покрыто прямым FQN-pattern → не нужен пакет
                    continue
                spec = self._pick_package(ns)
                if spec is None:
                    res.skipped_unknown.add(ns)
                    continue
                if spec.name.lower() in existing or spec.name in added:
                    continue
                added.add(spec.name)
                res.missing[spec.name] = spec
                res.explanations.append(
                    f"{spec.name}: использован `using {ns};`, нет в .csproj → "
                    f"добавляю {spec.to_xml()}"
                )
            for sk in sorted(res.skipped_unknown):
                res.explanations.append(
                    f"{sk}: namespace не в курируемой таблице → пропущен (версию не угадываем)"
                )
        except Exception as e:
            logger.warning("CsharpDependencyInference.infer упал: %s", e)
        return res

    # ---- правка .csproj --------------------------------------------------
    def apply_to_csproj(self, csproj_path: Path, missing: Dict[str, PackageSpec]) -> bool:
        """Добавляет недостающие PackageReference в подходящий ItemGroup
        (или создаёт новый). Не трогает существующие версии и комментарии.
        """
        if not missing or not csproj_path.is_file():
            return False
        try:
            original = csproj_path.read_text(encoding="utf-8")
        except OSError:
            return False

        # Готовим строки PackageReference (с табуляцией для читаемости).
        indent = "    "
        new_refs = [f'{indent}{indent}{spec.to_xml()}' for spec in missing.values()]

        # Ищем существующий <ItemGroup> с PackageReference; если нет — создаём перед </Project>.
        # Простой regex-подход (формат .csproj стандартен).
        ig_rx = re.compile(
            r"(<ItemGroup[^>]*>[\s\S]*?<PackageReference[\s\S]*?</ItemGroup>)",
            re.IGNORECASE,
        )
        m = ig_rx.search(original)
        if m:
            # вставляем перед </ItemGroup> существующей группы пакетов
            block = m.group(1)
            insert_pos = block.rfind("</ItemGroup>")
            if insert_pos < 0:
                return False
            new_block = (
                block[:insert_pos]
                + "\n".join(new_refs) + "\n"
                + indent
                + block[insert_pos:]
            )
            new_text = original[:m.start(1)] + new_block + original[m.end(1):]
        else:
            # создаём новый ItemGroup перед закрывающим </Project>
            close = original.rfind("</Project>")
            if close < 0:
                return False
            ig = (
                f"\n{indent}<ItemGroup>\n"
                + "\n".join(new_refs) + "\n"
                + f"{indent}</ItemGroup>\n"
            )
            new_text = original[:close] + ig + original[close:]

        if new_text == original:
            return False
        try:
            csproj_path.write_text(new_text, encoding="utf-8", newline="")
            return True
        except OSError as e:
            logger.warning("CsharpDependencyInference: запись .csproj упала: %s", e)
            return False

    # ---- оркестрация пред-пайплайн фазы ---------------------------------
    @staticmethod
    def _dotnet_error_count(root: Path) -> Optional[int]:
        if not shutil.which("dotnet"):
            return None
        try:
            proc = subprocess.run(
                ["dotnet", "build", "--nologo", "-clp:ErrorsOnly", "/p:WarningLevel=0"],
                cwd=str(root), capture_output=True, text=True, timeout=240,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return sum(1 for ln in out.splitlines() if re.search(r"\b[Ee]rror\s+(CS\d+|\w+):", ln))

    def recover(self, root, run_build_check: bool = True) -> CsharpInferenceResult:
        """Пред-пайплайн фаза: добавить отсутствующие NuGet-пакеты в .csproj.

        Аддитивно. Откат при ухудшении (`dotnet build` показал больше ошибок).
        Никогда не роняет пайплайн.
        """
        root = Path(root)
        res = self.infer(root)
        if not res.has_changes:
            return res
        csproj = self._find_csproj(root)
        if csproj is None or not csproj.is_file():
            res.explanations.append(".csproj не найден — пропуск C# dependency recovery")
            res.missing = {}
            return res
        try:
            backup = csproj.read_text(encoding="utf-8")
        except OSError:
            return res

        before = self._dotnet_error_count(root) if run_build_check else None

        if not self.apply_to_csproj(csproj, res.missing):
            res.explanations.append("Правка .csproj не применилась")
            res.missing = {}
            return res

        if run_build_check and before is not None:
            after = self._dotnet_error_count(root)
            if after is not None and after > before:
                try:
                    csproj.write_text(backup, encoding="utf-8", newline="")
                except OSError:
                    pass
                res.explanations.append(
                    f"ROLLBACK: после правки ошибок стало больше ({before}->{after}) — откат .csproj"
                )
                res.missing = {}
                return res
            res.explanations.append(f"ACCEPT: добавлено {len(res.missing)} пакетов; ошибок {before}->{after}")
        else:
            res.explanations.append(
                "ACCEPT (без dotnet-валидации): добавлено " + ", ".join(res.missing.keys())
            )
        return res
