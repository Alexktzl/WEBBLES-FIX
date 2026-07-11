"""
Regression-тесты для трёх находок Control Series 12 (2026-06-20):

1. project_timeout не enforced внутри retry-петель одной ошибки
   (GeneratePatchStage._blocking_adaptive_strategy_no_sandbox /
   _generate_segmented_patches) — investdaytip застрял ~40+ минут.
2. LLM hang без circuit breaker (LLMClient) — cantools/textparser,
   20+ hard timeout подряд без единого ответа.
3. PreCleanupStage пересчитывает заведомо-невалидные правки на
   неизменившемся файле каждый global cycle — semiprime/pygenda.

Плюс 2 находки, всплывшие ПРИ верификации фикса №1 целевым прогоном
на cantools/textparser:
4. NextErrorStage не сбрасывал segmented_attempts между разными ошибками
   (см. test_next_error_stage_resets_segmented_attempts).
5. core/engine/parallel.py (parallel_workers>1) обходит весь error_list
   потока без единой проверки дедлайна — то, что чинили в
   GeneratePatchStage, не помогало, если ошибки шли через эту параллельную
   ветку (см. test_parallel_executor_stops_at_deadline).
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.contract import MetadataKeys
from core.pipeline_context import PipelineContext
from core.stages.generate_patch_stage import GeneratePatchStage
from core.stages.next_error_stage import NextErrorStage
from core.stages.pre_cleanup_stage import PreCleanupStage
from fixers.llm_client import LLMClient
from fixers.patch_engine import PatchEngine
from memory.learning import MemoryLearning


@pytest.fixture(autouse=True)
def _skip_llm_preflight_health_check(monkeypatch):
    """Эти тесты проверяют circuit breaker на ПОДРЯД идущих hard-timeout
    (находка #2), не pre-flight health-check (добавлен 2026-06-22,
    empty_response диагностика) — фейковый provider base_url="http://x"
    иначе триггерит pre-flight ДО самого теста, искажая стартовое
    consecutive_hard_timeouts."""
    monkeypatch.setattr(
        "fixers.local_llm_provider.LocalLLMProvider.health_check",
        lambda self, timeout=5.0: True,
    )


# ---------------------------------------------------------------------------
# Finding 1: project_timeout внутри retry-петель GeneratePatchStage
# ---------------------------------------------------------------------------

def _make_stage(llm_client=None):
    return GeneratePatchStage(
        llm_client=llm_client or MagicMock(),
        memory=MemoryLearning(),
        patch_engine=PatchEngine(),
    )


def _make_context(tmp_path, deadline=None):
    ctx = PipelineContext(project_path=tmp_path, language="python")
    meta = dict(ctx.metadata)
    if deadline is not None:
        meta[MetadataKeys.PROJECT_DEADLINE] = deadline
    return ctx.update(metadata=meta, working_path=tmp_path)


def test_deadline_exceeded_helper():
    stage = _make_stage()
    ctx_past = _make_context(Path("."), deadline=time.monotonic() - 1)
    ctx_future = _make_context(Path("."), deadline=time.monotonic() + 9999)
    ctx_none = _make_context(Path("."), deadline=None)
    assert stage._deadline_exceeded(ctx_past) is True
    assert stage._deadline_exceeded(ctx_future) is False
    assert stage._deadline_exceeded(ctx_none) is False


def test_blocking_strategy_stops_immediately_when_deadline_passed(tmp_path):
    """Воспроизводит investdaytip: deadline уже прошёл ДО входа в retry-петлю —
    она не должна делать ни одного LLM-вызова через цикл (только pre-pass,
    который идёт безусловно один раз до петли — это не регрессия)."""
    (tmp_path / "html_export.py").write_text("x = 1\n", encoding="utf-8")
    llm = MagicMock()
    llm.generate_structured_fix.return_value = None  # pre-pass ничего не даёт
    llm.generate_fix.return_value = None
    stage = _make_stage(llm_client=llm)

    ctx = _make_context(tmp_path, deadline=time.monotonic() - 1)
    error = {"file": "html_export.py", "line": 1, "code": "E501", "message": "x"}

    result = stage._blocking_adaptive_strategy_no_sandbox(
        ctx, error, "sig", "x = 1\n",
        failure_reason="", compiler_feedback="", segments=[], file_hash=hash("x = 1\n"),
    )

    # Цикл (generate_fix) не должен звониться вообще — deadline уже прошёл
    # до первой итерации while.
    assert llm.generate_fix.call_count == 0
    assert result.current_state.name == "NEXT_ERROR"


def test_segmented_strategy_stops_immediately_when_deadline_passed(tmp_path):
    """Воспроизводит ту же петлю на уровне _generate_segmented_patches:
    deadline прошёл до первого сегмента — generate_fix не вызывается."""
    content = "\n".join(f"line {i}" for i in range(50)) + "\n"
    (tmp_path / "big.py").write_text(content, encoding="utf-8")
    llm = MagicMock()
    llm.generate_fix.return_value = None
    stage = _make_stage(llm_client=llm)

    ctx = _make_context(tmp_path, deadline=time.monotonic() - 1)
    error = {"file": "big.py", "line": 25, "code": "E501", "message": "x"}

    patches = stage._generate_segmented_patches(
        error=error, file_content=content, context=ctx,
        failure_reason="", compiler_feedback="", segments=[], file_hash=hash(content),
    )

    assert llm.generate_fix.call_count == 0
    assert patches == []


def test_blocking_strategy_calls_llm_when_deadline_not_passed(tmp_path):
    """Контроль: без дедлайна (или с дедлайном в будущем) петля работает как
    раньше — это не false positive, не ломаем нормальный путь."""
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    llm = MagicMock()
    llm.generate_structured_fix.return_value = None
    llm.generate_fix.return_value = None
    stage = _make_stage(llm_client=llm)

    ctx = _make_context(tmp_path, deadline=time.monotonic() + 9999)
    error = {"file": "x.py", "line": 1, "code": "E501", "message": "x"}

    stage._blocking_adaptive_strategy_no_sandbox(
        ctx, error, "sig", "x = 1\n",
        failure_reason="", compiler_feedback="", segments=[], file_hash=hash("x = 1\n"),
    )
    assert llm.generate_fix.call_count > 0


# ---------------------------------------------------------------------------
# Finding 2: LLMClient circuit breaker на consecutive hard timeouts
# ---------------------------------------------------------------------------

def _llm_client_with_fake_provider(per_request_timeout=0.05, unresponsive_threshold=3):
    cfg = {
        "llm": {
            "timeout": per_request_timeout,
            "max_retries": 1,
            "unresponsive_threshold": unresponsive_threshold,
            "providers": [{"provider": "local", "model": "x", "base_url": "http://x"}],
        }
    }
    return LLMClient(config=cfg)


def test_llm_client_not_unresponsive_initially():
    client = _llm_client_with_fake_provider()
    assert client.is_unresponsive() is False
    assert client.consecutive_hard_timeouts == 0


def test_llm_client_trips_circuit_breaker_after_threshold():
    """Воспроизводит textparser: N последовательных hard timeout -> unresponsive."""
    client = _llm_client_with_fake_provider(per_request_timeout=0.02, unresponsive_threshold=3)

    def _hangs_forever(*_a, **_k):
        time.sleep(5)
        return "should never get here"

    for _ in range(3):
        result, err = client._hard_timeout_call(_hangs_forever)
        assert result is None
        assert err is not None and err.startswith("hard_timeout_")

    assert client.consecutive_hard_timeouts == 3
    assert client.is_unresponsive() is True


def test_llm_client_resets_consecutive_count_on_success():
    client = _llm_client_with_fake_provider(per_request_timeout=0.05, unresponsive_threshold=3)
    client.consecutive_hard_timeouts = 2
    result, err = client._hard_timeout_call(lambda: "ok")
    assert result == "ok"
    assert client.consecutive_hard_timeouts == 0
    assert client.is_unresponsive() is False


def test_call_with_retries_fails_fast_once_unresponsive():
    """После срабатывания circuit breaker _call_with_retries не должен даже
    пытаться звонить провайдеру — раньше пайплайн продолжал бы слать новые
    запросы зависшему серверу до конца project_timeout."""
    client = _llm_client_with_fake_provider(per_request_timeout=0.02, unresponsive_threshold=2)
    client.consecutive_hard_timeouts = 2  # уже сработавший breaker

    called = []
    client._call_provider_with_prompt = lambda provider, prompt: called.append(1) or "should not run"

    response, err = client._call_with_retries(client.providers[0], "prompt")
    assert response is None
    assert err == "llm_unresponsive"
    assert called == []  # ни одной реальной попытки


def test_call_for_json_with_retries_fails_fast_once_unresponsive():
    client = _llm_client_with_fake_provider(per_request_timeout=0.02, unresponsive_threshold=2)
    client.consecutive_hard_timeouts = 2

    called = []
    client._call_provider_for_json = lambda *a, **k: called.append(1) or "should not run"

    response, err = client._call_for_json_with_retries(client.providers[0], "sys", "user")
    assert response is None
    assert err == "llm_unresponsive"
    assert called == []


def test_hard_timeout_call_reuses_single_executor():
    """Старый код создавал новый ThreadPoolExecutor на каждый вызов и
    забывал его (zombie threads пилили локальный LLM-сервер конкурентно).
    Новый — один persistent executor на весь жизненный цикл клиента."""
    client = _llm_client_with_fake_provider()
    executor_before = client._executor
    client._hard_timeout_call(lambda: "ok")
    client._hard_timeout_call(lambda: "ok")
    assert client._executor is executor_before
    assert client._executor._max_workers == 1


# ---------------------------------------------------------------------------
# Finding 3: PreCleanupStage не повторяет заведомо невалидную правку
# на неизменившемся содержимом между global cycles.
# ---------------------------------------------------------------------------

def _make_pipeline_context(tmp_path):
    return PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)


def test_precleanup_skips_known_bad_file_on_second_cycle(tmp_path, monkeypatch):
    """Воспроизводит pygenda: правило детерминированно ломает синтаксис файла.
    Первый прогон должен это обнаружить и НЕ записать (защита уже была).
    Второй прогон (имитация следующего global cycle, то же содержимое) НЕ
    должен повторно прогонять cleanup_pipeline на этом файле."""
    bad_file = tmp_path / "broken.py"
    original_content = "def f():\n    pass\n"
    bad_file.write_text(original_content, encoding="utf-8")

    stage = PreCleanupStage()

    from fixers.rule_based_fixer import RuleBasedFixer
    call_count = {"n": 0}
    invalid_result = "def f(:\n    pass\n"  # синтаксически невалидно

    def _fake_rule(self, error, content):
        call_count["n"] += 1
        from fixers.structured_edit import EditSet, Edit, Anchor
        return EditSet(
            intent="test corruption",
            edits=[Edit(file=error["file"], anchor=Anchor(line=1, match=""),
                        kind="replace_file", new=invalid_result, rationale="test")],
            confidence=0.9, risks=[],
        )

    monkeypatch.setattr(RuleBasedFixer, "_py_remove_llm_noise", _fake_rule)

    ctx = _make_pipeline_context(tmp_path)
    ctx1 = stage.execute(ctx)
    assert call_count["n"] == 1
    # файл не должен быть испорчен на диске
    assert bad_file.read_text(encoding="utf-8") == original_content
    bad_keys = ctx1.metadata.get(MetadataKeys.PRECLEANUP_FAILED_RULES)
    assert bad_keys, "ожидали что неудача записана в metadata"

    # Второй global cycle: то же содержимое, тот же metadata передаётся
    # дальше (как делает PipelineEngine._global_fix_loop) -> правило НЕ
    # должно вызываться повторно для этого файла.
    ctx2 = stage.execute(ctx1)
    assert call_count["n"] == 1, "правило повторно прогналось на неизменившемся файле"
    assert bad_file.read_text(encoding="utf-8") == original_content


def test_precleanup_retries_after_file_content_changes(tmp_path, monkeypatch):
    """Если содержимое файла изменилось (другая стадия его патчила) — это
    НОВОЕ содержимое, заслуживает новой попытки, а не блокируется навечно."""
    target = tmp_path / "broken.py"
    target.write_text("def f():\n    pass\n", encoding="utf-8")

    stage = PreCleanupStage()
    from fixers.rule_based_fixer import RuleBasedFixer
    call_count = {"n": 0}

    def _fake_rule(self, error, content):
        call_count["n"] += 1
        from fixers.structured_edit import EditSet, Edit, Anchor
        return EditSet(
            intent="test corruption",
            edits=[Edit(file=error["file"], anchor=Anchor(line=1, match=""),
                        kind="replace_file", new="def f(:\n    pass\n", rationale="test")],
            confidence=0.9, risks=[],
        )

    monkeypatch.setattr(RuleBasedFixer, "_py_remove_llm_noise", _fake_rule)

    ctx = _make_pipeline_context(tmp_path)
    ctx1 = stage.execute(ctx)
    assert call_count["n"] == 1

    target.write_text("def g():\n    pass\n", encoding="utf-8")  # другое содержимое
    stage.execute(ctx1)
    assert call_count["n"] == 2, "новое содержимое должно получить новую попытку"


# ---------------------------------------------------------------------------
# Finding 4 (всплыла при верификации фикса №1 на cantools/textparser):
# NextErrorStage не сбрасывал segmented_attempts между ошибками.
# ---------------------------------------------------------------------------

def test_next_error_stage_resets_segmented_attempts(tmp_path):
    """Без сброса 4-я РАЗНАЯ ошибка проекта стартовала бы с segmented_attempts=4
    > MAX_SEGMENTED_STRATEGY_ATTEMPTS=3 и немедленно помечалась unfixable без
    единой реальной попытки — счётчик предназначен для одной ошибки, а копился
    по всему проекту."""
    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        MetadataKeys.SEGMENTED_ATTEMPTS: 4,
    }))
    stage = NextErrorStage()
    result = stage.execute(ctx)
    assert MetadataKeys.SEGMENTED_ATTEMPTS not in result.metadata


# ---------------------------------------------------------------------------
# Finding 5 (всплыла при верификации фикса №1 на cantools/textparser):
# core/engine/parallel.py не проверял дедлайн внутри своего per-file цикла.
# ---------------------------------------------------------------------------

def test_parallel_executor_stops_at_deadline(tmp_path, monkeypatch):
    """parallel_workers>1 группирует ошибки по файлам и в каждом потоке гонит
    error_list ДО КОНЦА без единой проверки project_timeout. GeneratePatchStage
    сама уважает дедлайн внутри своих retry-петель, но как только она
    возвращает управление, этот цикл просто берёт следующую ошибку — на
    проекте с ошибками в десятках файлов это держало процесс далеко за
    бюджетом (cantools/textparser, ~23+ минут при бюджете 600s)."""
    import core.engine.parallel as parallel_mod

    call_count = {"n": 0}

    class _FakeGenStage:
        def __init__(self, *a, **k):
            pass

        def execute(self, ctx):
            call_count["n"] += 1
            return ctx  # без patch -> "Нет патча" путь, но дошли до счётчика

    monkeypatch.setattr(parallel_mod, "GeneratePatchStage", _FakeGenStage)

    # 2 файла по 5 ошибок -> error_list по 5 на поток, len(file_to_errors)=2
    # активирует параллельную ветку (она требует > 1 файла).
    errors = []
    for f in ("a.py", "b.py"):
        for line in range(5):
            errors.append({"file": f, "line": line, "code": "E501", "message": "x"})

    ctx = PipelineContext(project_path=tmp_path, language="python")
    ctx = ctx.update(
        current_errors=tuple(errors),
        metadata=dict(ctx.metadata, **{
            MetadataKeys.PROJECT_DEADLINE: time.monotonic() - 1,  # уже прошёл
        }),
    )

    holder = {"ctx": ctx}
    executor = parallel_mod.ParallelExecutor(
        llm_client=MagicMock(), memory=MemoryLearning(), patch_engine=PatchEngine(),
        state_lock=threading.Lock(),
        get_context=lambda: holder["ctx"],
        set_context=lambda c: holder.update(ctx=c),
    )
    executor.execute({"pipeline": {"parallel_workers": 2}}, tmp_path)

    # Дедлайн уже прошёл ДО первого вызова -> GeneratePatchStage.execute
    # не должна вызываться ни разу ни в одном из двух потоков.
    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# Control series 12 анализ потерь конверсии (2026-06-20):
# "import-not-found" не входил в _UNFIXABLE_MYPY_CODES, и даже если бы
# входил — _make_type_ignore_patch хардкодил "# type: ignore[import-untyped]"
# независимо от реального кода ошибки (невалидный ignore-комментарий для
# import-not-found, mypy не подавит).
# ---------------------------------------------------------------------------

def test_import_not_found_in_unfixable_mypy_codes():
    from core.stages.generate_patch_stage import _UNFIXABLE_MYPY_CODES
    assert "import-not-found" in _UNFIXABLE_MYPY_CODES
    assert "import-untyped" in _UNFIXABLE_MYPY_CODES  # не потеряли старый код


def test_make_type_ignore_patch_uses_actual_error_code(tmp_path):
    """Раньше код в ignore-комментарии был хардкодом 'import-untyped' —
    для error['code']='import-not-found' получился бы НЕВАЛИДНЫЙ
    `# type: ignore[import-untyped]` на строке с другой ошибкой."""
    target = tmp_path / "navigation.py"
    target.write_text("import dash_bootstrap_components as dbc\nx = 1\n", encoding="utf-8")

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    error = {
        "file": "navigation.py", "line": 1, "code": "import-not-found",
        "message": 'Cannot find implementation or library stub for module named "dash_bootstrap_components"',
    }

    patch = GeneratePatchStage._make_type_ignore_patch(error, ctx)
    assert patch is not None
    assert "# type: ignore[import-not-found]" in patch
    assert "import-untyped" not in patch


def test_make_type_ignore_patch_still_works_for_import_untyped(tmp_path):
    """Контроль: старый код import-untyped не регрессировал."""
    target = tmp_path / "x.py"
    target.write_text("import yaml\nx = 1\n", encoding="utf-8")

    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    error = {"file": "x.py", "line": 1, "code": "import-untyped", "message": "x"}

    patch = GeneratePatchStage._make_type_ignore_patch(error, ctx)
    assert patch is not None
    assert "# type: ignore[import-untyped]" in patch


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
