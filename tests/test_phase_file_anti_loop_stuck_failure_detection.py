"""
2026-06-25 (контрольная серия на 10 проектах, jsonschema/validators.py):
старые пороги FileAntiLoop (max_attempts_per_file=12, max_total_attempts=20)
позволяли файлу с anchor-несовпадением дойти до 21 попытки — каждая попытка
реально вызывает LLM на full-file regeneration, прежде чем антипетля
сработала. Это съело большую часть PROJECT_TIMEOUT бюджета ДО того, как
очередь дошла до большинства других ошибок проекта.

Фикс: (1) пороги снижены (12/20 -> 6/10) по эмпирике этой серии — продуктивные
файлы (gunicorn, anyio) укладывались в гораздо меньшее число попыток;
(2) record_attempt теперь принимает failure_reason и обрывает файл
ДОСРОЧНО, если одна и та же механическая причина неудачи повторяется
_MAX_CONSECUTIVE_SAME_FAILURE (3) раза подряд, не дожидаясь общего лимита.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.anti_loop import FileAntiLoop


def test_default_thresholds_lowered_to_4_6():
    # 2026-07-07 (perf-2, цель ≤600s/проект): 6/10 → 4/6 — тест кодировал
    # прежние пороги; по learning_cases июля продуктивные файлы укладываются
    # в 1-4 попытки, хвост обслуживал только обречённые файлы (их теперь
    # режут circuit-breaker perf-1 и stuck-стрик 3).
    fal = FileAntiLoop()
    assert fal.max_attempts_per_file == 4
    assert fal.max_total_attempts == 6


def test_same_failure_reason_three_times_blocks_file_before_total_limit():
    """Файл с лимитом total=10 не должен доходить до 10 попыток, если одна
    и та же ошибка ('anchor не найден') повторяется 3 раза подряд."""
    fal = FileAntiLoop(max_attempts_per_file=6, max_total_attempts=10)
    same_reason = "structured direct apply: anchor не найден"

    ok1 = fal.record_attempt("a.py", current_file_hash=1, failure_reason=same_reason)
    ok2 = fal.record_attempt("a.py", current_file_hash=1, failure_reason=same_reason)
    ok3 = fal.record_attempt("a.py", current_file_hash=1, failure_reason=same_reason)

    assert ok1 is True
    assert ok2 is True
    assert ok3 is False, (
        "третья попытка с ТОЙ ЖЕ причиной неудачи подряд должна заблокировать "
        "файл, не дожидаясь общего лимита (10)"
    )


def test_changing_failure_reason_does_not_trigger_stuck_detection():
    """Если причина неудачи МЕНЯЕТСЯ от попытки к попытке (признак прогресса,
    не застревания) — досрочная блокировка не должна сработать; файл
    отрабатывает до обычного общего лимита."""
    fal = FileAntiLoop(max_attempts_per_file=6, max_total_attempts=10)
    reasons = [
        "NET_DELTA regression: +1 новых ошибок",
        "Патч не затрагивает указанную ошибку",
        "Пустой объединённый патч",
        "NET_DELTA regression: +2 новых ошибок",
        "Патч не затрагивает целевой файл",
    ]
    results = [
        fal.record_attempt("a.py", current_file_hash=1, failure_reason=r)
        for r in reasons
    ]
    assert all(results), (
        "разные причины неудачи подряд не должны триггерить досрочную "
        "блокировку — только повтор ОДНОЙ И ТОЙ ЖЕ причины"
    )


def test_stuck_streak_resets_after_different_reason_interrupts_it():
    """2 подряд одинаковых, затем другая причина, затем снова 2 подряд той
    же первой — не должно засчитаться как 4 подряд (streak должен
    сброситься на прерывании)."""
    fal = FileAntiLoop(max_attempts_per_file=6, max_total_attempts=10)
    same = "anchor не найден"
    other = "NET_DELTA regression"

    assert fal.record_attempt("a.py", 1, failure_reason=same) is True
    assert fal.record_attempt("a.py", 1, failure_reason=same) is True
    assert fal.record_attempt("a.py", 1, failure_reason=other) is True  # прерывание -> сброс streak
    assert fal.record_attempt("a.py", 1, failure_reason=same) is True
    assert fal.record_attempt("a.py", 1, failure_reason=same) is True
    # это 5-я попытка, total=5 < 10, и streak для 'same' после сброса = 2 < 3
    assert fal.record_attempt("a.py", 1, failure_reason=same) is False, (
        "третий ПОДРЯД повтор 'same' ПОСЛЕ сброса должен снова заблокировать"
    )


def test_no_failure_reason_does_not_engage_stuck_detection():
    """Старое поведение (вызов без failure_reason) не должно ломаться —
    пустая строка не считается 'повторяющейся причиной'."""
    fal = FileAntiLoop(max_attempts_per_file=6, max_total_attempts=10)
    for _ in range(9):
        assert fal.record_attempt("a.py", current_file_hash=1) is True
    # 10-я попытка превышает total=10? total идёт 1..10, 10-я попытка ==
    # max_total_attempts, не больше -> True; 11-я была бы False. Проверим
    # ровно границу.
    assert fal.record_attempt("a.py", current_file_hash=1) is True   # 10-я, total==10, не > 10
    assert fal.record_attempt("a.py", current_file_hash=1) is False  # 11-я, total>10


def test_file_hash_change_resets_failure_reason_tracking():
    """Изменение содержимого файла (новый hash) должно сбрасывать и
    streak/last_failure_reason — это новая попытка с чистого листа."""
    fal = FileAntiLoop(max_attempts_per_file=6, max_total_attempts=10)
    same = "anchor не найден"
    assert fal.record_attempt("a.py", current_file_hash=1, failure_reason=same) is True
    assert fal.record_attempt("a.py", current_file_hash=1, failure_reason=same) is True
    # файл изменился (новый hash) -> сброс
    assert fal.record_attempt("a.py", current_file_hash=2, failure_reason=same) is True
    assert fal.record_attempt("a.py", current_file_hash=2, failure_reason=same) is True
    # это всего 2 подряд после сброса, streak=2 < 3 -> не блокируется
    assert fal.record_attempt("a.py", current_file_hash=2, failure_reason="other") is True


def test_reset_file_clears_failure_reason_state():
    fal = FileAntiLoop(max_attempts_per_file=6, max_total_attempts=10)
    same = "anchor не найден"
    fal.record_attempt("a.py", 1, failure_reason=same)
    fal.record_attempt("a.py", 1, failure_reason=same)
    fal.reset_file("a.py")
    assert fal._last_failure_reason.get("a.py") is None
    assert fal._consecutive_same_failure.get("a.py") is None
    # после reset_file должно начинаться с чистого счётчика streak
    assert fal.record_attempt("a.py", 1, failure_reason=same) is True
    assert fal.record_attempt("a.py", 1, failure_reason=same) is True
    assert fal.record_attempt("a.py", 1, failure_reason=same) is False


def test_generate_patch_stage_passes_failure_reason_to_file_anti_loop(tmp_path):
    """Интеграционная проверка: GeneratePatchStage.execute() реально
    передаёт context.metadata[LAST_PATCH_FAILURE] в file_anti_loop.record_attempt
    как failure_reason (а не молча игнорирует) — иначе детект 'застрял на
    одной причине' выше никогда не сработает в реальном пайплайне."""
    from unittest.mock import MagicMock
    from core.contract import MetadataKeys
    from core.pipeline_context import PipelineContext
    from core.stages.generate_patch_stage import GeneratePatchStage
    from fixers.patch_engine import PatchEngine

    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    llm_client = MagicMock()
    llm_client.generate_fix.return_value = (None, "empty_response")
    llm_client.generate_structured_fix.return_value = (None, "empty_response")

    stage = GeneratePatchStage(
        llm_client=llm_client, memory=MagicMock(), patch_engine=PatchEngine(),
    )
    captured = {}
    real_record = stage.file_anti_loop.record_attempt

    def spy_record(*args, **kwargs):
        captured["kwargs"] = kwargs
        captured["args"] = args
        return real_record(*args, **kwargs)

    stage.file_anti_loop.record_attempt = spy_record

    error = {"file": "mod.py", "line": 1, "code": "E1", "message": "x"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error)
    ctx = ctx.update(metadata=dict(ctx.metadata, **{
        MetadataKeys.LAST_PATCH_FAILURE: "structured direct apply: anchor не найден",
    }))

    stage.execute(ctx)

    assert "kwargs" in captured, "ожидался вызов file_anti_loop.record_attempt"
    assert captured["kwargs"].get("failure_reason") == "structured direct apply: anchor не найден"


# ---------------------------------------------------------------------------
# 2026-07-04 (экзамен-проба Performance, MatthewFlamm/pytest-homeassistant-
# custom-component): 3 сложных .github/workflows/*.yml, которые structured-diff
# не мог заанкорить, откатывались в ApplyPatchStage по «новым CRITICAL_SYNTAX»
# БЕЗ установки LAST_PATCH_FAILURE. GeneratePatchStage читает именно этот ключ
# как failure_reason для FileAntiLoop, поэтому early-abort «3× одна причина»
# не срабатывал → каждый файл жёг весь лимит попыток (11/10) × ~35s LLM-
# генерации ≈ 19 мин из 31-мин бюджета, cycles_run=0. Фикс: CRITICAL_SYNTAX-
# откат кладёт стабильную (без номеров строк) причину в LAST_PATCH_FAILURE.
# ---------------------------------------------------------------------------

class _StubPatchEngine:
    """Пишет заведомо ломающее содержимое и рапортует успех применения —
    имитирует apply_patch fallback, дающий синтаксически валидный по AST, но
    семантически битый (для инъектированного анализатора) файл."""
    last_error_code = ""

    def __init__(self, broken_text: str):
        self._broken = broken_text

    def files_in_patch(self, patch):
        return ["ci.yml"]

    def apply_patch(self, file_path, patch):
        file_path.write_text(self._broken, encoding="utf-8")
        return True


class _StubCriticalAnalyzer:
    """После патча возвращает одну новую CRITICAL_SYNTAX ошибку по .yml."""
    def analyze(self, project_dir, clean_before_each=False, files=None):
        return [{
            "file": "ci.yml", "line": 1, "code": "yaml-syntax",
            "message": "broken yaml", "error_class": "CRITICAL_SYNTAX",
        }]


def _run_apply_reaching_critical_syntax(tmp_path):
    from core.contract import MetadataKeys
    from core.pipeline_context import PipelineContext
    from core.stages.apply_patch_stage import ApplyPatchStage

    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "ci.yml"
    target.write_text("name: ci\non: [push]\n", encoding="utf-8")

    stage = ApplyPatchStage(
        patch_engine=_StubPatchEngine("name: ci\non: [push\n  broken: ]\n"),
        analyzer=_StubCriticalAnalyzer(),
    )
    # error_class у самой ошибки НЕ CRITICAL_SYNTAX — чтобы дойти до
    # project-wide проверки шага 3, а не сработать на балансе скобок.
    error = {"file": "ci.yml", "line": 1, "code": "yaml.sec", "message": "x",
             "error_class": "SECURITY"}
    ctx = PipelineContext(project_path=tmp_path, language="python", working_path=tmp_path)
    ctx = ctx.set_selected_error(error).set_patch("--- a/ci.yml\n+++ b/ci.yml\n")
    ctx = ctx.set_errors([])  # before_critical = 0
    return stage.execute(ctx), MetadataKeys


def test_critical_syntax_rollback_sets_stable_failure_reason(tmp_path):
    result, MetadataKeys = _run_apply_reaching_critical_syntax(tmp_path)
    reason = result.metadata.get(MetadataKeys.LAST_PATCH_FAILURE, "")
    assert reason, (
        "CRITICAL_SYNTAX-откат обязан положить failure_reason в LAST_PATCH_FAILURE — "
        "иначе FileAntiLoop не увидит 'застрял на одной причине'"
    )
    assert "CRITICAL_SYNTAX" in reason
    # Стабильность: строка не содержит номеров строк/имён файлов, которые
    # ломали бы накопление streak между разными ошибками одного файла.
    assert "ci.yml" not in reason
    assert not any(ch.isdigit() for ch in reason)


def test_critical_syntax_reason_is_identical_across_files_so_streak_builds(tmp_path):
    """Два независимых CRITICAL_SYNTAX-отката должны дать ПОБАЙТОВО одинаковую
    причину — только тогда FileAntiLoop.record_attempt накапливает streak."""
    r1, MK = _run_apply_reaching_critical_syntax(tmp_path / "a")
    r2, _ = _run_apply_reaching_critical_syntax(tmp_path / "b")
    reason1 = r1.metadata.get(MK.LAST_PATCH_FAILURE, "")
    reason2 = r2.metadata.get(MK.LAST_PATCH_FAILURE, "")
    assert reason1 and reason1 == reason2

    # И этот стабильный reason реально блокирует файл на 3-й попытке подряд.
    fal = FileAntiLoop(max_attempts_per_file=6, max_total_attempts=10)
    assert fal.record_attempt("wf.yml", 1, failure_reason=reason1) is True
    assert fal.record_attempt("wf.yml", 1, failure_reason=reason1) is True
    assert fal.record_attempt("wf.yml", 1, failure_reason=reason1) is False
