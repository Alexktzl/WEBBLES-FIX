"""
Контрактные тесты на границы ответственности движков (2026-06-24, усилены
после ревью на "не декоративность" — добавлены behavioral spy/mock-проверки
и канарейка-тесты, доказывающие, что ассерты гарантированно ловят нарушение
границы, а не просто совпадают с текущим поведением случайно):

- GeneratePatchStage — только ГЕНЕРИРУЕТ кандидата патча, никогда не
  пишет в реальный файл проекта (внутренняя sandbox-проверка кандидата
  в validate_in_sandbox — на временной копии, не на work_dir-файле).
- ApplyPatchStage — единственная стадия, которая пишет НОВЫЙ контент в
  целевой файл (применяет уже сгенерированный кандидат), и никогда не
  выставляет LAST_DECISION/не переходит в DECIDING/COMPLETED сама.
- ValidateStage — только проверяет; единственная мутация файла, которую
  ей разрешено делать — ROLLBACK к содержимому ДО патча (отмена), но
  никогда не пишет НОВЫЙ/иной контент.
- DecideStage — структурно не имеет llm_client/patch_engine (не может
  генерировать или применять патчи), и НИКОГДА не инстанцирует их и
  GeneratePatchStage инлайн (через context/helper/import) даже при
  полном execute(); на REJECT может только восстановить файл из снапшота.
- PipelineEngine — оркестрирует, делегируя каждый State исключительно
  зарегистрированной стадии, не реализуя эквивалентную бизнес-логику инлайн,
  и никогда не вызывает стадию не для текущего state.

Тесты используют минимальные стабы/моки и не меняют основную логику
проекта — только фиксируют существующий (проверенный по коду) контракт.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.state_machine import State


# ---------------------------------------------------------------------
# GeneratePatchStage: только генерирует кандидата, никогда не пишет файл.
# ---------------------------------------------------------------------

def test_generate_patch_stage_never_writes_target_file_on_success(tmp_path):
    """Детерминированный путь _make_type_ignore_patch (import-untyped) не
    требует реального LLM-вызова и гарантированно производит кандидат —
    удобен для проверки границы без сложного мокинга LLM."""
    from core.stages.generate_patch_stage import GeneratePatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "mod.py"
    original = "from foo import bar, baz\n"
    target.write_text(original, encoding="utf-8")

    stage = GeneratePatchStage(
        llm_client=MagicMock(), memory=MagicMock(), patch_engine=PatchEngine(),
    )
    error = {"file": "mod.py", "line": 1, "code": "import-untyped", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)

    result = stage.execute(ctx)

    assert target.read_text(encoding="utf-8") == original, (
        "GeneratePatchStage не должен писать в реальный целевой файл — "
        "только производить кандидат патча"
    )
    assert result.generated_patch, "ожидался сгенерированный кандидат патча"


def test_generate_patch_stage_never_writes_target_file_on_no_patch(tmp_path):
    """Тот же инвариант должен держаться и когда генерация НЕ дала
    кандидата (LLM вернул пусто) — отсутствие патча не повод что-то
    записать в файл."""
    from core.stages.generate_patch_stage import GeneratePatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "mod.py"
    original = "x = 1\n"
    target.write_text(original, encoding="utf-8")

    llm_client = MagicMock()
    llm_client.generate_fix.return_value = (None, "empty_response")
    llm_client.generate_structured_fix.return_value = (None, "empty_response")

    stage = GeneratePatchStage(
        llm_client=llm_client, memory=MagicMock(), patch_engine=PatchEngine(),
    )
    error = {"file": "mod.py", "line": 1, "code": "E999", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)

    stage.execute(ctx)

    assert target.read_text(encoding="utf-8") == original


def test_generate_patch_stage_validate_merged_patch_spy_never_targets_real_file(tmp_path):
    """Behavioral spy (не строковый grep по исходнику!) на
    PatchEngine.apply_patch — единственный путь, которым GeneratePatchStage
    мог бы физически записать контент в work_dir, вызывается из
    _validate_merged_patch (self-check кандидата перед тем, как вернуть
    его как итоговый патч сегментного слияния). Перехватываем РЕАЛЬНЫЙ
    вызов и проверяем: он целится в sandbox-копию (validate_in_sandbox
    создаёт tempfile.mkdtemp(prefix="webbles_sandbox_")), не в реальный
    файл проекта. Если кто-то завтра поменяет _validate_merged_patch на
    прямой apply к work_dir-файлу "для оптимизации" — этот тест поймает
    это немедленно, в отличие от строкового grep по исходнику (он бы
    остался зелёным даже при добавлении ВТОРОГО, небезопасного вызова)."""
    from core.stages.generate_patch_stage import GeneratePatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "mod.py"
    original = "x = 1\n"
    target.write_text(original, encoding="utf-8")

    patch_engine = PatchEngine()
    real_apply = patch_engine.apply_patch
    calls = []

    def spy_apply(file_path, patch_text, *a, **kw):
        calls.append(Path(file_path))
        return real_apply(file_path, patch_text, *a, **kw)

    patch_engine.apply_patch = spy_apply

    stage = GeneratePatchStage(
        llm_client=MagicMock(), memory=MagicMock(), patch_engine=patch_engine,
    )
    error = {"file": "mod.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    merged_patch = "--- a/mod.py\n+++ b/mod.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"

    with patch.object(PatchEngine, "is_patch_relevant", return_value=True):
        ok = stage._validate_merged_patch(merged_patch, ctx, error, original, segments=[])

    assert ok is True, "ожидали, что sandbox self-check кандидата пройдёт успешно"
    assert calls, (
        "ожидался хотя бы один внутренний self-check вызов apply_patch "
        "(validate_in_sandbox) — если их 0, тест не покрывает то, что "
        "должен, и сам по себе бесполезен"
    )
    real_target = target.resolve()
    for call_path in calls:
        assert call_path.resolve() != real_target, (
            f"GeneratePatchStage вызвал apply_patch НАПРЯМУЮ на реальном файле "
            f"проекта {call_path} — нарушение границы 'только кандидат, "
            f"никогда не применяет к work_dir'"
        )
    # И главное — реальный файл проекта не тронут результатом self-check.
    assert target.read_text(encoding="utf-8") == original


def test_generate_patch_stage_canary_sandbox_bypass_is_detected(tmp_path):
    """Канарейка: ИСКУССТВЕННО ломаем границу (подменяем
    validation.syntax_validator.validate_in_sandbox так, чтобы он применял
    modification_fn НАПРЯМУЮ к реальному файлу, как если бы кто-то по
    ошибке убрал sandboxing) и подтверждаем, что инвариант 'файл не тронут'
    из теста выше СЛОМАЛСЯ БЫ — то есть что проверка реально чувствительна
    к этому классу регрессии, а не зелёная при любом исходе."""
    import validation.syntax_validator as sv_mod
    from core.stages.generate_patch_stage import GeneratePatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "mod.py"
    original = "x = 1\n"
    target.write_text(original, encoding="utf-8")

    def bypass_sandbox(project_path, file_path, modification_fn, language):
        # Симулируем регрессию: применяем прямо к реальному file_path,
        # без временной копии.
        modification_fn(file_path)
        return True, file_path.read_text(encoding="utf-8"), None

    error = {"file": "mod.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    merged_patch = "--- a/mod.py\n+++ b/mod.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"

    stage = GeneratePatchStage(
        llm_client=MagicMock(), memory=MagicMock(), patch_engine=PatchEngine(),
    )
    with patch.object(sv_mod, "validate_in_sandbox", bypass_sandbox), \
         patch.object(PatchEngine, "is_patch_relevant", return_value=True):
        stage._validate_merged_patch(merged_patch, ctx, error, original, segments=[])

    # Без sandbox-изоляции файл ДЕЙСТВИТЕЛЬНО меняется — подтверждает,
    # что граница в реальном коде держится именно за счёт sandboxing,
    # и что отсутствие этого механизма тест выше поймал бы как провал.
    assert target.read_text(encoding="utf-8") != original, (
        "канарейка не сработала — bypass_sandbox должен был дать "
        "наблюдаемое изменение реального файла, иначе тест выше "
        "(test_generate_patch_stage_validate_merged_patch_spy_never_targets_real_file) "
        "ничего не доказывает о роли sandbox-изоляции"
    )


# ---------------------------------------------------------------------
# ApplyPatchStage: единственная стадия, реально записывающая новый контент.
# ---------------------------------------------------------------------

def test_apply_patch_stage_writes_new_content_and_advances_to_validating(tmp_path):
    from core.stages.apply_patch_stage import ApplyPatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    patch_text = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
    )
    stage = ApplyPatchStage(patch_engine=PatchEngine())
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(generated_patch=patch_text)

    result = stage.execute(ctx)

    assert target.read_text(encoding="utf-8") == "x = 2\n", (
        "ApplyPatchStage должен записать новый контент из сгенерированного патча"
    )
    assert result.current_state == State.VALIDATING, (
        "ApplyPatchStage не принимает решений ACCEPT/REJECT сама — после "
        "успешного применения управление должно перейти к ValidateStage"
    )


def _assert_apply_patch_decision_contract(result):
    """Общий ассерт, переиспользуемый и для настоящего результата, и для
    искусственно испорченного (см. канарейку ниже) — единственное место,
    где определена граница 'ApplyPatchStage не решает ACCEPT/REJECT'."""
    assert result.accepted_patches == [], "ApplyPatchStage не должен класть accepted_patches"
    assert result.rejected_patches == [], "ApplyPatchStage не должен класть rejected_patches"
    assert result.metadata.get(MetadataKeys.LAST_DECISION) is None, (
        "ApplyPatchStage не должен выставлять last_decision — это решает "
        "только DecideStage; _global_fix_loop читает именно этот ключ, "
        "чтобы понять, что патч принят"
    )
    assert result.current_state not in (State.DECIDING, State.COMPLETED), (
        "ApplyPatchStage не должен сам прыгать в DECIDING/COMPLETED, минуя "
        "ValidateStage/ReviewStage"
    )


def test_apply_patch_stage_does_not_decide_accept_or_reject(tmp_path):
    """ApplyPatchStage не должно само класть что-либо в accepted_patches/
    rejected_patches, выставлять last_decision или скакать в DECIDING/
    COMPLETED — всё это исключительно прерогатива DecideStage."""
    from core.stages.apply_patch_stage import ApplyPatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    patch_text = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"

    stage = ApplyPatchStage(patch_engine=PatchEngine())
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(generated_patch=patch_text)

    result = stage.execute(ctx)

    _assert_apply_patch_decision_contract(result)


def test_apply_patch_stage_canary_decision_violation_is_detected(tmp_path):
    """Канарейка: берём РЕАЛЬНЫЙ (валидный) результат ApplyPatchStage и
    искусственно ИСПОРТИМ его так, как выглядел бы баг (стадия сама
    выставила ACCEPT и проставила last_decision), затем подтверждаем, что
    наш ассерт-хелпер выше гарантированно падает на этой порче — то есть
    что он не "всегда зелёный" по конструкции."""
    from core.stages.apply_patch_stage import ApplyPatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    patch_text = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"

    stage = ApplyPatchStage(patch_engine=PatchEngine())
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(generated_patch=patch_text)

    result = stage.execute(ctx)

    # Симулируем гипотетический баг: ApplyPatchStage "решила" принять патч сама.
    corrupted = result.add_accepted_patch({"error": error, "reason": "fake_self_accept"})
    corrupted = corrupted.update(
        metadata=dict(corrupted.metadata, **{MetadataKeys.LAST_DECISION: "ACCEPT"}),
    )

    with pytest.raises(AssertionError):
        _assert_apply_patch_decision_contract(corrupted)


# ---------------------------------------------------------------------
# ValidateStage: либо не трогает файл, либо откатывает к ОРИГИНАЛУ (никогда
# не пишет третий, новый вариант контента).
# ---------------------------------------------------------------------

def _make_validate_stage():
    compiler = MagicMock()
    linter = MagicMock()
    security = MagicMock()
    compiler.run.return_value = (True, [])
    linter.run.return_value = (True, [])
    security.run.return_value = (True, [])
    from analyzers.python_analyzer import PythonAnalyzer
    from core.stages.validate_stage import ValidateStage
    return ValidateStage(compiler=compiler, linter=linter, security=security,
                          analyzer=PythonAnalyzer(), degradation=MagicMock())


def _validate_ctx(tmp_path, patched_content, original_content, target_file="a.py"):
    config = {"pipeline": {"use_mypy": False, "use_bandit": False, "run_tests": False,
                            "logic_guard": False}}
    (tmp_path / target_file).write_text(patched_content, encoding="utf-8")
    ctx = PipelineContext(project_path=tmp_path, language="python", config=config,
                           working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": target_file, "line": 1, "code": "E501",
                                  "message": "x", "error_class": "CLEANUP"})
    ctx = ctx.update(generated_patch="--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n")
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "_pre_patch_content": {target_file: original_content},
    }))
    return ctx


def test_validate_stage_leaves_applied_content_untouched_on_clean_pass(tmp_path):
    """Успешная проверка (нет net-delta регрессии) — ValidateStage не
    должна сама что-то менять в файле, он остаётся таким, каким его
    оставил ApplyPatchStage."""
    stage = _make_validate_stage()
    patched = "y\n"
    ctx = _validate_ctx(tmp_path, patched_content=patched, original_content="x\n")

    from core.stages.net_delta_check import NetDeltaClassification
    clean = NetDeltaClassification(regression=[], unmasked=[], uncertain=[], changed_lines={1})
    with patch("core.stages.net_delta_check.classify_net_delta", return_value=clean):
        stage.execute(ctx)

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == patched, (
        "ValidateStage не должна изменять контент файла на чистом проходе"
    )


def test_validate_stage_rollback_restores_exact_original_not_novel_content(tmp_path):
    """NET_DELTA regression → rollback должен восстановить РОВНО
    _pre_patch_content (контент ДО патча), не какой-то другой/новый текст —
    единственная разрешённая мутация файла внутри ValidateStage — отмена."""
    stage = _make_validate_stage()
    original = "x\n"
    patched = "y\n"
    ctx = _validate_ctx(tmp_path, patched_content=patched, original_content=original)
    sig_key = "_net_delta_error_retries"
    from core.pipeline_stage import PipelineStage as _PS
    sig = _PS._static_signature(ctx.selected_error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{sig_key: {sig: 1}}))

    from core.stages.net_delta_check import NetDeltaClassification
    regression = NetDeltaClassification(
        regression=[{"file": "a.py", "line": 1, "code": "E999"}],
        unmasked=[], uncertain=[], changed_lines={1},
    )
    with patch("core.stages.net_delta_check.classify_net_delta", return_value=regression):
        stage.execute(ctx)

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == original, (
        "rollback должен восстановить ИМЕННО pre_patch_content — не "
        "оставить applied-вариант и не записать что-то третье"
    )


def test_validate_stage_write_spy_every_write_equals_pre_patch_or_noop(tmp_path):
    """Behavioral spy на Path.write_text ВО ВРЕМЯ execute(): какой бы путь
    исполнения ни сработал (clean pass или rollback), КАЖДАЯ запись в файл,
    если она случилась, должна совпасть РОВНО с original — никогда не
    какой-то третий контент. В отличие от теста выше (читает финальное
    состояние файла один раз), это перехватывает ВСЕ промежуточные записи,
    включая случай 'записали novel-контент, а потом ещё раз откатили'."""
    stage = _make_validate_stage()
    original = "x\n"
    patched = "y\n"
    ctx = _validate_ctx(tmp_path, patched_content=patched, original_content=original)
    from core.pipeline_stage import PipelineStage as _PS
    sig = _PS._static_signature(ctx.selected_error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{"_net_delta_error_retries": {sig: 1}}))

    written = []
    real_write_text = Path.write_text

    def spy_write_text(self, data, *a, **kw):
        written.append((str(self), data))
        return real_write_text(self, data, *a, **kw)

    from core.stages.net_delta_check import NetDeltaClassification
    regression = NetDeltaClassification(
        regression=[{"file": "a.py", "line": 1, "code": "E999"}],
        unmasked=[], uncertain=[], changed_lines={1},
    )
    with patch("core.stages.net_delta_check.classify_net_delta", return_value=regression), \
         patch.object(Path, "write_text", spy_write_text):
        stage.execute(ctx)

    assert written, "ожидался хотя бы один write (rollback) — иначе spy ничего не проверил"
    for path_str, data in written:
        assert data == original, (
            f"ValidateStage записал в {path_str} контент {data!r}, отличный "
            f"от pre_patch_content {original!r} — разрешён только откат к "
            "РОВНО оригинальному содержимому"
        )


def test_validate_stage_canary_novel_write_is_detected(tmp_path):
    """Канарейка: подкладываем в _pre_patch_content контент, отличный от
    реального 'before' (имитируя баг, где rollback восстановил бы НЕ то,
    что было до патча), и убеждаемся, что spy/файловая проверка это
    обнаруживает — то есть что обе проверки выше чувствительны к разнице,
    а не сравнивают файл с самим собой."""
    stage = _make_validate_stage()
    real_original = "x\n"
    patched = "y\n"
    wrong_snapshot = "THIS IS NOT THE REAL ORIGINAL\n"
    ctx = _validate_ctx(tmp_path, patched_content=patched, original_content=wrong_snapshot)
    from core.pipeline_stage import PipelineStage as _PS
    sig = _PS._static_signature(ctx.selected_error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{"_net_delta_error_retries": {sig: 1}}))

    from core.stages.net_delta_check import NetDeltaClassification
    regression = NetDeltaClassification(
        regression=[{"file": "a.py", "line": 1, "code": "E999"}],
        unmasked=[], uncertain=[], changed_lines={1},
    )
    with patch("core.stages.net_delta_check.classify_net_delta", return_value=regression):
        stage.execute(ctx)

    result_content = (tmp_path / "a.py").read_text(encoding="utf-8")
    assert result_content != real_original, (
        "канарейка не сработала — ожидали, что rollback восстановит "
        "испорченный снапшот (wrong_snapshot), доказывая, что проверка "
        "'== original' выше реально завязана на содержимое снапшота, "
        "а не на побочное совпадение"
    )
    assert result_content == wrong_snapshot


# ---------------------------------------------------------------------
# DecideStage: структурно не может генерировать/применять; на REJECT —
# только откат к снапшоту (тот же принцип, что у ValidateStage). Особое
# внимание — обход границы через инлайн-импорт/локальную инстанциацию
# внутри метода, а не только через конструктор.
# ---------------------------------------------------------------------

def test_decide_stage_has_no_llm_or_patch_apply_capability():
    """Структурная проверка границы: конструктор DecideStage НЕ принимает
    llm_client/patch_engine — он физически не может ни сгенерировать новый
    патч, ни применить произвольный контент к файлу через них."""
    import inspect
    from core.stages.decide_stage import DecideStage

    sig = inspect.signature(DecideStage.__init__)
    params = set(sig.parameters) - {"self"}
    assert "llm_client" not in params
    assert "patch_engine" not in params


def _make_unmask_ctx(tmp_path, verdict, conf, target_still_present, target_file="src/game.py"):
    """Минимальный, но РЕАЛЬНЫЙ execute()-ready PipelineContext, доводящий
    до ACCEPT/REJECT через настоящую логику (не через приватный helper) —
    взят из паттерна test_phase_d_trichotomy.py."""
    target_error = {"file": target_file, "line": 2, "code": "E0432", "message": "x"}
    current_errors = [target_error] if target_still_present else [
        {"file": target_file, "line": 15, "code": "E0609", "message": "y"}
    ]
    meta = {
        MetadataKeys.CONFIDENCE: conf,
        MetadataKeys.PATCH_SOURCE: "structured_llm",
        "review": {"verdict": verdict, "confidence_adjustment": 0.0},
        "patch_snapshots": [{"file": target_file, "original_content": "ORIGINAL\n"}],
    }
    return PipelineContext(
        project_path=tmp_path, language="python", working_path=tmp_path,
        metadata=meta, selected_error=target_error,
        current_errors=current_errors,
        validation_results={
            "error_count_before": 1, "error_count_after": 8,
            "project_error_count_before": 1, "project_error_count_after": 8,
            "current_errors_before": [target_error],
        },
    )


def test_decide_stage_execute_never_instantiates_llm_or_patch_engine_on_accept(tmp_path):
    """Полный execute() (не только _dispatch_after_success/rollback-helper
    напрямую) на ACCEPT-ветке — спай на КОНСТРУКТОРЫ LLMClient/PatchEngine/
    GeneratePatchStage, который гарантированно провалит тест, если
    DecideStage когда-нибудь обойдёт отсутствие этих коллабораторов в
    __init__ через инлайн-импорт+инстанциацию внутри какого-либо метода
    (то, что чисто структурная проверка конструктора выше НЕ видит)."""
    import fixers.llm_client as llm_mod
    import fixers.patch_engine as pe_mod
    import core.stages.generate_patch_stage as gps_mod
    from core.stages.decide_stage import DecideStage

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "game.py").write_text("PATCHED\n", encoding="utf-8")

    ds = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(tmp_path, verdict="ok", conf=1.0, target_still_present=False)

    def _boom(*a, **kw):
        raise AssertionError(
            "DecideStage инстанцировал коллаборатора генерации/применения "
            "патчей — это нарушение границы 'только решает ACCEPT/REJECT/"
            "NEEDS_REVIEW'"
        )

    with patch.object(llm_mod, "LLMClient", side_effect=_boom), \
         patch.object(pe_mod, "PatchEngine", side_effect=_boom), \
         patch.object(gps_mod, "GeneratePatchStage", side_effect=_boom):
        out = ds.execute(ctx)

    assert out.metadata.get(MetadataKeys.LAST_DECISION) in ("ACCEPT", "NEEDS_REVIEW")


def test_decide_stage_execute_never_instantiates_llm_or_patch_engine_on_reject(tmp_path):
    """Та же спай-проверка на REJECT-ветке (verdict=wrong → откат) —
    решение и откат файла не должны требовать ни LLMClient, ни PatchEngine."""
    import fixers.llm_client as llm_mod
    import fixers.patch_engine as pe_mod
    from core.stages.decide_stage import DecideStage

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "game.py").write_text("PATCHED\n", encoding="utf-8")

    ds = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(tmp_path, verdict="wrong", conf=1.0, target_still_present=False)

    def _boom(*a, **kw):
        raise AssertionError("DecideStage инстанцировал LLMClient/PatchEngine на REJECT-пути")

    with patch.object(llm_mod, "LLMClient", side_effect=_boom), \
         patch.object(pe_mod, "PatchEngine", side_effect=_boom):
        out = ds.execute(ctx)

    assert out.metadata.get(MetadataKeys.LAST_DECISION) == "REJECT" or any(
        (p.get("reason") or "").startswith(("reviewer_verdict_wrong", "error_count_not_decreased"))
        for p in (out.rejected_patches or [])
    )
    # REJECT обязан откатить файл к снапшоту, не оставить PATCHED-контент.
    assert (tmp_path / "src" / "game.py").read_text(encoding="utf-8") == "ORIGINAL\n"


def test_decide_stage_write_spy_accept_path_never_writes_file(tmp_path):
    """ACCEPT-путь НЕ должен писать в файл вообще — файл остаётся таким,
    каким его оставил ApplyPatchStage/ValidateStage."""
    from core.stages.decide_stage import DecideStage

    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "game.py"
    target.write_text("PATCHED\n", encoding="utf-8")

    ds = DecideStage(quality_evaluator=MagicMock())
    ctx = _make_unmask_ctx(tmp_path, verdict="ok", conf=1.0, target_still_present=False)

    written = []
    real_write_text = Path.write_text

    def spy_write_text(self, data, *a, **kw):
        written.append((str(self), data))
        return real_write_text(self, data, *a, **kw)

    with patch.object(Path, "write_text", spy_write_text):
        ds.execute(ctx)

    assert written == [], (
        f"DecideStage записал в файл(ы) на ACCEPT-пути: {written} — "
        "ACCEPT не должен трогать файл вообще"
    )
    assert target.read_text(encoding="utf-8") == "PATCHED\n"


def test_decide_stage_reject_rollback_restores_exact_snapshot(tmp_path):
    """REJECT-решение откатывает файл к original_content из снапшота —
    не пишет какой-либо иной контент (та же дисциплина 'только отмена',
    что у ValidateStage)."""
    from core.stages.decide_stage import DecideStage

    target = tmp_path / "a.py"
    patched = "y\n"
    original = "x\n"
    target.write_text(patched, encoding="utf-8")

    ds = DecideStage(quality_evaluator=MagicMock())
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "patch_snapshots": [{"file": "a.py", "original_content": original}],
    }))

    result = ds._rollback_file_from_snapshot(ctx, error)

    assert target.read_text(encoding="utf-8") == original
    # Метод отката не должен сам класть запись в accepted/rejected —
    # это прерогатива вызывающего _dispatch_after_success, не утилиты отката.
    assert result.metadata.get("patch_snapshots") == []


def test_decide_stage_rollback_canary_wrong_snapshot_is_detected(tmp_path):
    """Канарейка: испорченный snapshot (не совпадает с тем, что РЕАЛЬНО
    было до патча) — подтверждает, что проверка 'restores exact snapshot'
    завязана на содержимое снапшота, а не на побочное совпадение строк."""
    from core.stages.decide_stage import DecideStage

    target = tmp_path / "a.py"
    target.write_text("y\n", encoding="utf-8")
    real_original = "x\n"
    wrong_snapshot = "NOT THE REAL ORIGINAL\n"

    ds = DecideStage(quality_evaluator=MagicMock())
    error = {"file": "a.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        "patch_snapshots": [{"file": "a.py", "original_content": wrong_snapshot}],
    }))

    ds._rollback_file_from_snapshot(ctx, error)

    result_content = target.read_text(encoding="utf-8")
    assert result_content != real_original
    assert result_content == wrong_snapshot, (
        "канарейка не сработала — rollback должен был восстановить РОВНО "
        "то, что лежит в snapshot (даже если оно 'неправильное'), что "
        "доказывает, что основной тест проверяет содержимое снапшота, "
        "а не случайное совпадение"
    )


# ---------------------------------------------------------------------
# PipelineEngine: оркестрирует, делегируя каждый State зарегистрированной
# стадии — не реализует эквивалентную бизнес-логику инлайн, и никогда не
# зовёт стадию, не относящуюся к текущему state.
# ---------------------------------------------------------------------

def _setup_mock_engine(tmp_path, stages):
    from core.pipeline_engine import PipelineEngine

    engine = PipelineEngine.__new__(PipelineEngine)
    engine.project_path = tmp_path
    engine.language = "python"
    engine.config = {"pipeline": {}}
    engine.dry_run = False
    engine.event_emitter = None
    engine.reporter = None
    engine.circuit_breaker = MagicMock(is_open=MagicMock(return_value=False))
    engine.anti_loop = MagicMock(should_break=MagicMock(return_value=False))
    engine.stability_governor = MagicMock(
        get_params=MagicMock(return_value={}), step=MagicMock(return_value={}),
    )
    engine.health_evaluator = MagicMock()
    engine.step_times = {}
    engine.dynamic_params = {}
    engine._emit_after_stage = MagicMock()
    engine._save_state = MagicMock()
    engine._build_result = lambda status_override=None: {"status_override": status_override}
    engine._error_signature = staticmethod(lambda e: f"{e.get('file')}::{e.get('code')}")
    engine.stages = stages
    return engine


def test_pipeline_engine_single_run_delegates_each_state_to_its_own_stage(tmp_path):
    """Заменяем ВСЕ стадии моками и вызываем НАСТОЯЩИЙ _single_run —
    убеждаемся, что он только вызывает stage.execute(context) для текущего
    state и переходит дальше по тому, что вернул мок, без альтернативной
    бизнес-логики внутри самого PipelineEngine. Дополнительно регистрируем
    'чужую' стадию на состояние, через которое прогон вообще не проходит
    (REVIEWING зарегистрирован, но реальный путь его не задевает в этом
    сценарии было бы избыточно — здесь все 6 состояний посещаются, поэтому
    'чужой' стадией используем ANALYZING, не входящий в маршрут вовсе)."""
    from core.pipeline_engine import PipelineEngine

    call_log = []

    def make_stage(name, next_state):
        stage = MagicMock()

        def _execute(ctx):
            call_log.append(name)
            return ctx.add_state_to_history(next_state)

        stage.execute.side_effect = _execute
        return stage

    unrelated_stage = make_stage("analyzing_should_never_be_called", State.GENERATING_PATCH)

    stages = {
        State.GENERATING_PATCH: make_stage("generate", State.APPLYING_PATCH),
        State.APPLYING_PATCH: make_stage("apply", State.VALIDATING),
        State.VALIDATING: make_stage("validate", State.REVIEWING),
        State.REVIEWING: make_stage("review", State.DECIDING),
        State.DECIDING: make_stage("decide", State.NEXT_ERROR),
        State.NEXT_ERROR: make_stage("next_error", State.COMPLETED),
        State.ANALYZING: unrelated_stage,
    }
    engine = _setup_mock_engine(tmp_path, stages)

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "E1", "message": "x"})
    ctx = ctx.add_state_to_history(State.GENERATING_PATCH)
    engine.context = ctx

    engine._single_run()

    assert call_log == ["generate", "apply", "validate", "review", "decide", "next_error"], (
        "PipelineEngine._single_run должен делегировать КАЖДЫЙ state "
        "единственной зарегистрированной для него стадии, в правильном "
        "порядке — никакой инлайн-логики, дублирующей стадии"
    )
    for state, stage in stages.items():
        if state is State.ANALYZING:
            continue
        stage.execute.assert_called_once()
    unrelated_stage.execute.assert_not_called()


def test_pipeline_engine_canary_extra_stage_call_is_detected(tmp_path):
    """Канарейка: если бы _single_run по ошибке дёрнул ЕЩЁ одну стадию для
    того же state (двойной вызов — характерный симптом дублирующей инлайн-
    логики), assert_called_once() из теста выше обязан упасть. Симулируем
    это здесь явно, без второго прогона реального _single_run."""
    stage = MagicMock()
    stage.execute.side_effect = lambda ctx: ctx.add_state_to_history(State.APPLYING_PATCH)

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error({"file": "a.py", "line": 1, "code": "E1", "message": "x"})

    stage.execute(ctx)
    stage.execute(ctx)  # имитируем баг "дёрнули стадию дважды"

    with pytest.raises(AssertionError):
        stage.execute.assert_called_once()
