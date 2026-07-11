"""
Эталон здоровья проекта для webles_conveyor.
Хранит целевые метрики, вычисляет расстояние до эталона и отслеживает версию модели весов.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from analysis.scoring.weighted_error_model import MODEL_VERSION

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthBaseline:
    """
    Эталонное состояние проекта, к которому стремится система.
    Содержит целевые значения ключевых метрик и версию модели весов.
    """

    target_error_weight: float = 0.0
    target_compile_success: bool = True
    target_security_ok: bool = True
    target_lint_count: int = 0
    model_version: str = MODEL_VERSION
    metadata: Dict[str, Any] = field(default_factory=dict)

    def distance(
        self,
        current_error_weight: float,
        current_compile_success: bool,
        current_security_ok: bool,
        current_lint_count: int,
    ) -> float:
        """
        Вычисляет нормализованное расстояние от текущего состояния до эталона.
        Возвращает значение в диапазоне [0, 1], где 0 — идеальное совпадение.
        """
        dist = 0.0
        max_dist = 0.0

        # Ошибки: чем больше разница, тем дальше
        error_diff = abs(current_error_weight - self.target_error_weight)
        max_error = max(current_error_weight, self.target_error_weight, 1.0)
        dist += error_diff / max_error
        max_dist += 1.0

        # Компиляция: несовпадение добавляет полное расстояние
        if current_compile_success != self.target_compile_success:
            dist += 1.0
        max_dist += 1.0

        # Безопасность
        if current_security_ok != self.target_security_ok:
            dist += 1.0
        max_dist += 1.0

        # Линтер: нормализуем по максимальному количеству предупреждений
        lint_diff = abs(current_lint_count - self.target_lint_count)
        max_lint = max(current_lint_count, self.target_lint_count, 1)
        dist += lint_diff / max_lint
        max_dist += 1.0

        return dist / max_dist if max_dist > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Сериализация для сохранения."""
        return {
            "target_error_weight": self.target_error_weight,
            "target_compile_success": self.target_compile_success,
            "target_security_ok": self.target_security_ok,
            "target_lint_count": self.target_lint_count,
            "model_version": self.model_version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthBaseline":
        """Десериализация."""
        return cls(
            target_error_weight=data.get("target_error_weight", 0.0),
            target_compile_success=data.get("target_compile_success", True),
            target_security_ok=data.get("target_security_ok", True),
            target_lint_count=data.get("target_lint_count", 0),
            model_version=data.get("model_version", "unknown"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def load_or_create(
        cls,
        baseline_file: Path,
        compute_baseline_fn: callable,
    ) -> "HealthBaseline":
        """
        Загружает эталон из файла или создаёт новый с помощью переданной функции.
        Если сохранённая версия модели отличается от текущей, эталон пересоздаётся.
        """
        if baseline_file.exists():
            try:
                with open(baseline_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                saved_version = data.get("model_version", "unknown")
                if saved_version != MODEL_VERSION:
                    logger.info(
                        f"Версия модели изменилась ({saved_version} -> {MODEL_VERSION}), "
                        "эталон будет пересоздан."
                    )
                else:
                    logger.info(f"Загружен эталон здоровья из {baseline_file}")
                    return cls.from_dict(data)
            except Exception as e:
                logger.warning(f"Не удалось загрузить эталон: {e}")

        # Создаём новый эталон
        logger.info("Создание нового эталона здоровья...")
        baseline_data = compute_baseline_fn()
        baseline = cls(
            target_error_weight=baseline_data.get("target_error_weight", 0.0),
            target_compile_success=baseline_data.get("target_compile_success", True),
            target_security_ok=baseline_data.get("target_security_ok", True),
            target_lint_count=baseline_data.get("target_lint_count", 0),
            metadata={"created_at": baseline_data.get("created_at", "")},
        )

        # Сохраняем для будущих запусков
        try:
            with open(baseline_file, "w", encoding="utf-8") as f:
                json.dump(baseline.to_dict(), f, indent=2, ensure_ascii=False)
            logger.info(f"Эталон сохранён в {baseline_file}")
        except Exception as e:
            logger.warning(f"Не удалось сохранить эталон: {e}")

        return baseline