"""
Regression-тест: PipelineEngine(resume=True) не должен оставлять
context.config зачищенным плейсхолдером StateManager'а.

StateManager.save_state() намеренно зануляет весь config перед записью в
state.json — секреты живут в .env/webles_config.json, а не в state (см.
StateManager._sanitize_for_persist). Но PipelineEngine.__init__ должен
восстанавливать живой config на резюмированном контексте — иначе любой код
вида context.config.get(...) (например NextErrorStage) падает с
AttributeError, потому что на диске вместо dict лежит строка-плейсхолдер
"<redacted: config not persisted in state>".

Важно: debug_mode=True в сохранённом контексте — это не случайная деталь, а
точное воспроизведение прод-сценария, который реально уронил резюмированный
прогон demo_project. PipelineContext.__init__ делает
`self.debug_mode = debug_mode or self.config.get("verbose", False)` — если
debug_mode уже True (сохранён с прошлого verbose-запуска), `or` коротко
замыкается и НЕ обращается к config, поэтому PipelineContext.from_dict()
строится без ошибок, а строка-плейсхолдер тихо просачивается в живой
context.config. Падение происходит позже, когда какая-то стадия (например
NextErrorStage) реально вызывает context.config.get(...) — то есть отложенно,
а не сразу при resume. Если debug_mode=False, StateManager.load_state() сам
поймает AttributeError внутри своего try/except и тихо вернёт None, и баг
не воспроизведётся — собственно поэтому в тесте важно явно выставить
debug_mode=True перед сохранением.
"""

import shutil
from pathlib import Path

from core.pipeline_engine import PipelineEngine


class _FakeLLMClient:
    """Подсовываем готовый объект, чтобы конструктор PipelineEngine не пытался
    собрать настоящий LLMClient (там не нужны сетевые вызовы, но и реальные
    провайдеры/ключи в тесте ни к чему)."""
    language_provider = None


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x = 1\n", encoding="utf-8")
    return project


def test_resume_restores_live_config_not_redacted_placeholder(tmp_path):
    project = _make_project(tmp_path)
    live_config = {"pipeline": {"use_planning": False}, "llm": {"timeout": 5}}
    runtime_dir = Path(__file__).resolve().parents[1] / "runtime" / project.name
    try:
        engine1 = PipelineEngine(
            project_path=project, language="python", llm_client=_FakeLLMClient(),
            config=live_config, resume=False,
        )
        # Воспроизводим прод-условие: debug_mode уже True с прошлого запуска,
        # см. объяснение в docstring модуля.
        engine1.context = engine1.context.update(debug_mode=True)
        engine1.state_manager.save_state(
            engine1.context, engine1.anti_loop, engine1.circuit_breaker,
            engine1.dynamic_params, engine1.step_times, engine1.start_time,
        )

        engine2 = PipelineEngine(
            project_path=project, language="python", llm_client=_FakeLLMClient(),
            config=live_config, resume=True,
        )

        assert isinstance(engine2.context.config, dict), (
            f"context.config остался нерасшифрованным плейсхолдером StateManager'а: "
            f"{engine2.context.config!r}"
        )
        assert engine2.context.config == live_config

        # Точное воспроизведение прод-крэша: NextErrorStage.execute() делает
        # ровно этот вызов на резюмированном context.
        engine2.context.config.get("pipeline", {}).get("use_planning")
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)
