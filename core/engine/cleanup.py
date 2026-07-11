"""
Модуль финальной очистки.
Вынесен из pipeline_engine.py для модульности.

L6 (аудит 2026-07-01): методы `cleanup_pass`, `final_resolve_unfixable` и
`deep_clean_before_audit` УДАЛЕНЫ. Они были мёртвым кодом (ни одного вызова
в проекте — подтверждено grep-ом), но при этом писали напрямую в
`self.project_path` — В ОРИГИНАЛЬНЫЙ проект, мимо sandbox, мимо всех
guard-ов и финального аудита. Подключение любого из них в будущем стало бы
готовым P0 (немодерируемая мутация пользовательского кода). Эквивалентная
sandbox-безопасная функциональность живёт в FinalResolveStage
(core/stages/final_resolve_stage.py, работает с working_path и только для
Rust). Класс-оболочка сохранён: PipelineEngine его инстанцирует.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class CleanupManager:
    """Оболочка финальной очистки (см. docstring модуля)."""

    def __init__(self, project_path: Path, language: str, patch_engine, config: dict):
        self.project_path = project_path
        self.language = language
        self.patch_engine = patch_engine
        self.config = config
