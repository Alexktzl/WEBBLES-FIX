"""
Фабрика планировщика для webles_conveyor.
Отвечает за создание и инициализацию компонентов планирования,
отделяя эту логику от PipelineEngine.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PlanningFactory:
    """Создаёт компоненты планирования по требованию."""

    def __init__(
        self,
        project_path: Path,
        language: str,
        analyzer,
        compiler,
        linter,
        security,
        memory,
        health_evaluator,
    ):
        self.project_path = project_path
        self.language = language
        self.analyzer = analyzer
        self.compiler = compiler
        self.linter = linter
        self.security = security
        self.memory = memory
        self.health_evaluator = health_evaluator

    def create(self, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Создаёт и возвращает словарь с компонентами планирования:
        {
            'sandbox': IncrementalSandbox,
            'state_cache': StateCache,
            'state_hasher': StateHasher,
            'patch_scorer': PatchScorer,
            'simulator': VirtualPatchSimulator,
            'planner': RepairPlanner,
        }
        Возвращает None, если модули планирования недоступны.
        """
        try:
            from planning.incremental_sandbox import IncrementalSandbox
            from planning.patch_scorer import PatchScorer
            from planning.repair_horizon import RepairPlanner, VirtualPatchSimulator
            from planning.state_cache import StateCache, StateHasher
        except ImportError as e:
            logger.warning(f"Модули планирования недоступны: {e}")
            return None

        sandbox = IncrementalSandbox(
            base_path=self.project_path / ".webles_sandbox",
            source_project=self.project_path,
        )
        state_cache = StateCache()
        state_hasher = StateHasher(self.project_path)
        patch_scorer = PatchScorer(memory=self.memory)
        simulator = VirtualPatchSimulator(
            project_path=self.project_path,
            language=self.language,
            analyzer=self.analyzer,
            compiler=self.compiler,
            linter=self.linter,
            security=self.security,
            sandbox=sandbox,
            state_cache=state_cache,
            health_evaluator=self.health_evaluator,
        )
        planner = RepairPlanner(
            simulator=simulator,
            patch_scorer=patch_scorer,
            lookahead_depth=config.get("planning_depth", 2),
            beam_width=config.get("beam_width", 3),
        )

        return {
            "sandbox": sandbox,
            "state_cache": state_cache,
            "state_hasher": state_hasher,
            "patch_scorer": patch_scorer,
            "simulator": simulator,
            "planner": planner,
        }