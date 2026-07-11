"""
C# анализатор — достройка этапа (2026-07-10): включение Roslyn-анализаторов,
дедуп, фильтр стилевого шума, чистка message, nullable-классификация.

Мотивация: голый `dotnet build` даёт только CS-коды компилятора; баг-паттерны
(CA1508 мёртвый код, CS8602 nullable-deref) ловят Roslyn-анализаторы, которые
по умолчанию выключены. Включаем `-p:AnalysisMode=All` + `--no-incremental`
(иначе инкрементальный билд молчит). Стиль (IDE*/SA*/опинион-CA) — денилист,
как в C++ (чиним баги, не стиль).

Запуск: python tests/test_phase_csharp_buildout.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from analyzers.csharp_analyzer import CsharpAnalyzer  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond), note))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" ({note})" if note and not cond else ""))
    assert cond, f"{name}: {note}"


A = CsharpAnalyzer()

# Реальный формат вывода dotnet build (с хвостом [project.csproj], дублями,
# смесью баг/стиль-кодов). Пути внутри project_root — иначе фильтр «вне проекта»
# их отбросит (диагностика из .nuget/SDK — не наш код).
def _dotnet_out(root):
    r = str(root)
    return (
        f"{r}\\Program.cs(6,27): warning CS8602: Dereference of a possibly null reference. [{r}\\proj.csproj]\n"
        f"{r}\\Program.cs(8,13): warning CA1508: 'list == null' is always 'false'. [{r}\\proj.csproj::TargetFramework=net10.0]\n"
        f"{r}\\Program.cs(6,27): warning CS8602: Dereference of a possibly null reference. [{r}\\proj.csproj]\n"
        f"{r}\\Program.cs(3,9): warning CA1303: Do not pass literals as localized parameters. [{r}\\proj.csproj]\n"
        f"{r}\\Util.cs(1,1): warning IDE0055: Fix formatting. [{r}\\proj.csproj]\n"
        f"{r}\\Util.cs(5,10): warning SA1200: Using directive should appear within a namespace. [{r}\\proj.csproj]\n"
        f"{r}\\Bad.cs(10,5): error CS1002: ; expected [{r}\\proj.csproj]\n"
        f"{r}\\Res.cs(4,9): warning CA2000: Dispose object before losing scope. [{r}\\proj.csproj]\n"
    )


def test_drops_findings_outside_project(tmp_path):
    """Находки из .nuget/SDK (вне project_path) отбрасываются."""
    out = (f"{tmp_path}\\Own.cs(1,1): warning CA1508: dead. [{tmp_path}\\p.csproj]\n"
           r"C:\Users\u\.nuget\packages\polyshim\2.0\File.cs(5,3): warning CS8602: null. [x.csproj]" + "\n")
    errs = A._parse_dotnet_output(out, tmp_path)
    files = {e["file"] for e in errs}
    check("own_kept", any("Own.cs" in f for f in files), f"files={files}")
    check("nuget_dropped", not any("File.cs" in f or "polyshim" in f for f in files), f"files={files}")


def test_parse_strips_project_tail(tmp_path):
    errs = A._parse_dotnet_output(_dotnet_out(tmp_path), tmp_path)
    cs8602 = [e for e in errs if e["code"] == "CS8602"][0]
    check("tail_stripped", "csproj" not in cs8602["message"], f"msg={cs8602['message']!r}")
    check("message_clean", cs8602["message"] == "Dereference of a possibly null reference.")


def test_dedup_removes_doubles(tmp_path):
    errs = A._dedup(A._parse_dotnet_output(_dotnet_out(tmp_path), tmp_path))
    cs8602 = [e for e in errs if e["code"] == "CS8602"]
    check("cs8602_deduped", len(cs8602) == 1, f"got {len(cs8602)}")


def test_noise_filter_drops_style_keeps_bugs(tmp_path):
    errs = A._filter_noise(A._dedup(A._parse_dotnet_output(_dotnet_out(tmp_path), tmp_path)))
    codes = {e["code"] for e in errs}
    # баги сохранены
    check("keeps_cs8602_nullable", "CS8602" in codes)
    check("keeps_ca1508_deadcode", "CA1508" in codes)
    check("keeps_cs1002_syntax", "CS1002" in codes)
    check("keeps_ca2000_dispose", "CA2000" in codes)
    # шум отброшен
    check("drops_ca1303_localization", "CA1303" not in codes)
    check("drops_ide0055_style", "IDE0055" not in codes)
    check("drops_sa1200_stylecop", "SA1200" not in codes)


def test_analyze_resolves_relative_path(tmp_path, monkeypatch):
    """analyze() резолвит относительный project_path в абсолютный ДО rglob/cwd —
    иначе относительный target + относительный cwd удваивают путь → dotnet не
    находит проект → тихий 0 (2026-07-10, класс C++ doubled-path)."""
    import subprocess as _sp
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Lib.csproj").write_text("<Project/>", encoding="utf-8")
    captured = {}

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        return _R()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_sp, "run", fake_run)
    # относительный путь
    CsharpAnalyzer().analyze(Path("."))
    # cwd переданный в dotnet — абсолютный
    check("cwd_is_absolute", captured.get("cwd") is not None
          and Path(captured["cwd"]).is_absolute(), f"cwd={captured.get('cwd')}")
    # target-csproj в команде — абсолютный путь
    target = [c for c in captured.get("cmd", []) if str(c).endswith(".csproj")]
    check("target_is_absolute", target and Path(target[0]).is_absolute(),
          f"target={target}")


def test_nullable_classification():
    check("cs8602_is_nullable", A._classify_error("CS8602", "deref") == "nullable")
    check("cs8618_is_nullable", A._classify_error("CS8618", "uninit") == "nullable")
    check("cs1002_is_syntax", A._classify_error("CS1002", "; expected") == "syntax")
    check("ca2000_is_lint", A._classify_error("CA2000", "dispose") == "lint")


def test_noise_prefix_only_matches_analyzer_codes():
    """IDE/SA — только когда за префиксом ИДУТ цифры (не 'SATURATE')."""
    check("ide0055_noise", A._filter_noise([{"code": "IDE0055"}]) == [])
    check("sa1200_noise", A._filter_noise([{"code": "SA1200"}]) == [])
    # не-анализаторный код с теми же буквами не отбрасывается
    kept = A._filter_noise([{"code": "CS8602"}, {"code": "CA2000"}])
    check("bug_codes_kept", len(kept) == 2)


def test_is_test_project_by_name(tmp_path):
    """Детект по STEM (имя файла) в НЕЙТРАЛЬНОМ каталоге 'app' — чтобы имя
    родителя не влияло (проверяем именно stem-логику)."""
    app = tmp_path / "app"
    app.mkdir()
    def mk(name, body="<Project/>"):
        p = app / name
        p.write_text(body, encoding="utf-8")
        return p
    check("tests_name", A._is_test_project(mk("Foo.Tests.csproj")))
    check("test_name", A._is_test_project(mk("Foo.Test.csproj")))
    check("benchmarks_name", A._is_test_project(mk("Foo.Benchmarks.csproj")))
    check("samples_name", A._is_test_project(mk("Foo.Samples.csproj")))
    check("lib_not_test", not A._is_test_project(mk("Foo.csproj")))
    check("core_not_test", not A._is_test_project(mk("Foo.Core.csproj")))


def test_is_test_project_by_dir(tmp_path):
    """Проект в каталоге tests/ — тест (детект по имени НЕПОСРЕДСТВЕННОГО дира)."""
    tdir = tmp_path / "tests"
    tdir.mkdir()
    p = tdir / "Neutral.csproj"
    p.write_text("<Project/>", encoding="utf-8")
    check("in_tests_dir_is_test", A._is_test_project(p))
    ldir = tmp_path / "src"
    ldir.mkdir()
    p2 = ldir / "Lib.csproj"
    p2.write_text("<Project/>", encoding="utf-8")
    check("in_src_dir_not_test", not A._is_test_project(p2))


def test_is_test_project_by_package(tmp_path):
    """Проект с ссылкой на тест-фреймворк — тест, даже если имя нейтрально."""
    app = tmp_path / "app"
    app.mkdir()
    p = app / "Neutral.csproj"
    p.write_text('<Project><ItemGroup><PackageReference Include="xunit" '
                 'Version="2.6.0"/></ItemGroup></Project>', encoding="utf-8")
    check("xunit_ref_is_test", A._is_test_project(p))
    p2 = app / "Lib.csproj"
    p2.write_text('<Project><ItemGroup><PackageReference Include="Newtonsoft.Json" '
                  'Version="13.0.0"/></ItemGroup></Project>', encoding="utf-8")
    check("normal_ref_not_test", not A._is_test_project(p2))


if __name__ == "__main__":
    import tempfile
    for fn in (test_drops_findings_outside_project,
               test_parse_strips_project_tail, test_dedup_removes_doubles,
               test_noise_filter_drops_style_keeps_bugs,
               test_is_test_project_by_name, test_is_test_project_by_dir,
               test_is_test_project_by_package):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))
    test_nullable_classification()
    test_noise_prefix_only_matches_analyzer_codes()
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, note) for n, ok, note in results if not ok]
    print(f"csharp_buildout: {passed}/{len(results)} passed")
    sys.exit(1 if failed else 0)
