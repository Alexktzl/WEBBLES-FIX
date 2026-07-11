"""
Горизонт планирования исправлений для webles_conveyor.
Выполняет опережающий поиск по возможным последовательностям исправлений с использованием:
- инкрементальной песочницы для быстрой copy-on-write симуляции
- кэша состояний для мемоизации посещённых состояний
- скорера патчей для предварительной фильтрации и упорядочивания
"""

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.pipeline_context import PipelineContext
from core.system_health import SystemHealth, SystemHealthEvaluator
from core.timeout_utils import run_with_timeout
from fixers.patch_repair import apply_patch_with_repair
from planning.incremental_sandbox import IncrementalSandbox
from planning.patch_scorer import PatchScorer
from planning.state_cache import CachedState, StateCache

logger = logging.getLogger(__name__)


@dataclass
class RepairAction:
    """Кандидат на исправление (патч)."""
    error: Dict[str, Any]
    patch: str
    source: str  # "memory", "llm", "template"
    confidence: float = 0.5
    pre_score: float = 0.5


@dataclass
class RepairTrajectory:
    """Последовательность действий и предсказанные исходы."""
    actions: List[RepairAction] = field(default_factory=list)
    predicted_health: List[SystemHealth] = field(default_factory=list)
    cumulative_score: float = 0.0
    state_hashes: List[str] = field(default_factory=list)


class VirtualPatchSimulator:
    """
    Симулирует применение патча с использованием инкрементальной песочницы.
    Работает напрямую с хешами состояний, не зависит от PipelineContext.
    """

    def __init__(
        self,
        project_path: Path,
        language: str,
        analyzer: Any,
        compiler: Any,
        linter: Any,
        security: Any,
        sandbox: IncrementalSandbox,
        state_cache: StateCache,
        health_evaluator: Optional[SystemHealthEvaluator] = None,
    ):
        self.project_path = project_path
        self.language = language
        self.analyzer = analyzer
        self.compiler = compiler
        self.linter = linter
        self.security = security
        self.sandbox = sandbox
        self.state_cache = state_cache
        self.health_evaluator = health_evaluator or SystemHealthEvaluator(window_size=5)

    def simulate_action(
        self,
        action: RepairAction,
        parent_state_hash: str,
    ) -> Tuple[Optional[str], SystemHealth, List[Dict[str, Any]], Dict[str, Any]]:
        """
        Применяет действие в песочнице и возвращает:
            - new_state_hash (или None, если патч не применился)
            - предсказанное SystemHealth
            - список ошибок после патча
            - словарь с результатами валидации
        """
        error = action.error
        file_rel_path = error.get("file", "")
        patch_content = action.patch

        # +++ Защита: patch_content должен быть строкой +++
        if not isinstance(patch_content, str):
            logger.error(
                f"simulate_action: patch_content имеет тип {type(patch_content).__name__}, ожидалась str. "
                f"Действие для ошибки в {file_rel_path} будет пропущено."
            )
            return None, self._failed_health(), [], {}

        parent_dir = self.sandbox.get_project_path(parent_state_hash)
        if parent_dir is None:
            logger.error(f"Родительское состояние {parent_state_hash} не найдено")
            return None, self._failed_health(), [], {}

        original_file = parent_dir / file_rel_path
        if not original_file.exists():
            logger.warning(f"Файл {file_rel_path} отсутствует в состоянии {parent_state_hash}")
            return None, self._failed_health(), [], {}

        try:
            original_content = original_file.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"Не удалось прочитать {original_file}: {e}")
            return None, self._failed_health(), [], {}

        patched_content = apply_patch_with_repair(original_content, patch_content)
        if patched_content is None:
            logger.debug("Патч не применился (даже после восстановления)")
            return None, self._failed_health(), [], {}

        try:
            new_state_hash, new_state_dir = self.sandbox.apply_patch(
                parent_state_hash, file_rel_path, patched_content
            )
        except Exception as e:
            logger.error(f"Ошибка apply_patch в песочнице: {e}")
            return None, self._failed_health(), [], {}

        cached = self.state_cache.get(new_state_hash)
        if cached:
            logger.debug(f"Попадание в кэш для состояния {new_state_hash[:8]}")
            return new_state_hash, cached.health, cached.errors, cached.validation_results

        validation_results = self._run_validation(new_state_dir)

        try:
            new_errors = run_with_timeout(self.analyzer.analyze, 300, new_state_dir)
        except Exception as e:
            logger.warning(f"Анализатор не сработал в песочнице: {e}")
            new_errors = []

        sim_context = PipelineContext(
            project_path=new_state_dir,
            language=self.language,
            current_errors=tuple(new_errors),
            initial_errors=tuple(new_errors),
        )
        self.health_evaluator.update(sim_context)
        health = self.health_evaluator.evaluate()

        cached_state = CachedState(
            health=health,
            errors=new_errors,
            validation_results=validation_results,
            metadata={"state_hash": new_state_hash},
        )
        self.state_cache.put(new_state_hash, cached_state)

        return new_state_hash, health, new_errors, validation_results

    def _run_validation(self, sandbox_dir: Path) -> Dict[str, Any]:
        results = {}
        try:
            compile_ok, compile_out = run_with_timeout(
                self.compiler.run, 300, sandbox_dir, self.language
            )
            results["compile"] = {"success": compile_ok, "output": compile_out}
        except Exception as e:
            results["compile"] = {"success": False, "output": [str(e)]}

        try:
            lint_ok, lint_out = run_with_timeout(
                self.linter.run, 120, sandbox_dir, self.language
            )
            results["lint"] = {"success": lint_ok, "output": lint_out}
        except Exception as e:
            results["lint"] = {"success": False, "output": [str(e)]}

        try:
            sec_ok, sec_out = run_with_timeout(
                self.security.run, 120, sandbox_dir, self.language
            )
            results["security"] = {"success": sec_ok, "output": sec_out}
        except Exception as e:
            results["security"] = {"success": False, "output": [str(e)]}

        return results

    def _failed_health(self) -> SystemHealth:
        return SystemHealth(
            stability=0.0,
            error_trend_slope=0.0,
            rollback_rate=1.0,
            patch_success_rate=0.0,
            memory_hit_rate=0.0,
            entropy=1.0,
        )


class RepairPlanner:
    """
    Планирует последовательность исправлений с использованием beam search и мемоизации.
    """

    def __init__(
        self,
        simulator: VirtualPatchSimulator,
        patch_scorer: PatchScorer,
        lookahead_depth: int = 2,
        beam_width: int = 3,
        max_patches_per_error: int = 2,
    ):
        self.simulator = simulator
        self.patch_scorer = patch_scorer
        self.lookahead_depth = lookahead_depth
        self.beam_width = beam_width
        self.max_patches_per_error = max_patches_per_error

    def plan(
        self,
        initial_state_hash: str,
        current_errors: List[Dict[str, Any]],
        available_patches: Dict[str, List[str]],
        language: str,
    ) -> List[RepairAction]:
        """
        Возвращает оптимальную последовательность действий, используя beam search.
        """
        if not current_errors:
            return []

        scored_actions: List[RepairAction] = []
        for error in current_errors[:10]:
            sig = self._error_signature(error)
            patches = available_patches.get(sig, [])
            for patch in patches[:self.max_patches_per_error]:
                # +++ Защита: патч должен быть строкой +++
                if not isinstance(patch, str):
                    logger.warning(
                        f"Планировщик: патч для сигнатуры {sig} имеет тип {type(patch).__name__}, "
                        f"ожидалась str. Пропускаем кандидата."
                    )
                    continue
                pre_score = self.patch_scorer.score_patch(
                    error=error, patch=patch, file_context="", language=language
                )
                scored_actions.append(RepairAction(
                    error=error, patch=patch, source="candidate",
                    confidence=pre_score, pre_score=pre_score
                ))

        if not scored_actions:
            return []

        scored_actions.sort(key=lambda a: a.pre_score, reverse=True)
        initial_actions = scored_actions[:self.beam_width * 2]

        trajectories: List[RepairTrajectory] = []
        for action in initial_actions:
            traj = RepairTrajectory(actions=[action])
            trajectories.append(traj)

        for depth in range(self.lookahead_depth):
            new_trajectories = []
            for traj in trajectories:
                if not traj.actions:
                    continue

                state_hash = initial_state_hash
                valid = True
                for i, action in enumerate(traj.actions):
                    new_hash, health, errors, _ = self.simulator.simulate_action(action, state_hash)
                    if new_hash is None:
                        valid = False
                        break
                    state_hash = new_hash
                    # Здоровье первого действия учитываем только на depth==0,
                    # когда траектория впервые создаётся. На depth>=1 траектория
                    # уже была полностью оценена при постролении в прошлой итерации,
                    # повторный re-walk нужен лишь для продвижения state_hash.
                    if i == 0 and depth == 0:
                        traj.predicted_health.append(health)
                        traj.cumulative_score += health.overall()
                        traj.state_hashes.append(state_hash)

                if not valid:
                    continue

                if not errors:
                    new_trajectories.append(traj)
                    continue

                next_actions: List[RepairAction] = []
                for error in errors[:5]:
                    sig = self._error_signature(error)
                    patches = available_patches.get(sig, [])
                    for patch in patches[:1]:
                        # Защита на следующих итерациях
                        if not isinstance(patch, str):
                            logger.warning(
                                f"Планировщик (глубина {depth+1}): патч для {sig} "
                                f"имеет тип {type(patch).__name__}, пропускаем."
                            )
                            continue
                        pre_score = self.patch_scorer.score_patch(
                            error=error, patch=patch, file_context="", language=language
                        )
                        next_actions.append(RepairAction(
                            error=error, patch=patch, source="candidate",
                            confidence=pre_score, pre_score=pre_score
                        ))

                for next_action in next_actions:
                    new_traj = RepairTrajectory(
                        actions=traj.actions + [next_action],
                        predicted_health=traj.predicted_health.copy(),
                        cumulative_score=traj.cumulative_score,
                        state_hashes=traj.state_hashes.copy(),
                    )
                    new_hash, health, _, _ = self.simulator.simulate_action(next_action, state_hash)
                    if new_hash is not None:
                        new_traj.predicted_health.append(health)
                        new_traj.cumulative_score += health.overall()
                        new_traj.state_hashes.append(new_hash)
                        new_trajectories.append(new_traj)

            if new_trajectories:
                trajectories = sorted(new_trajectories, key=lambda t: t.cumulative_score, reverse=True)[:self.beam_width]
            else:
                break

        if trajectories:
            best = max(trajectories, key=lambda t: t.cumulative_score)
            logger.info(f"Лучшая траектория, счёт: {best.cumulative_score:.3f}")
            return best.actions
        return []

    @staticmethod
    def _error_signature(error: Dict[str, Any]) -> str:
        import re
        file = error.get("file", "")
        code = error.get("code") or error.get("error_code")
        if code:
            return f"{file}::{code}"
        msg = error.get("message", "")
        normalized = re.sub(r'\d+', '#', msg)
        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = ' '.join(normalized.split())
        return f"{file}::{normalized[:100]}"