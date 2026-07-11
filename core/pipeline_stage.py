"""
Оркестратор стадий конвейера Webbles Fix.
Все стадии вынесены в модули core.stages.*.
Оставлен базовый класс PipelineStage и импорты.
"""

import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class PipelineStage(ABC):
    """Базовый класс для всех стадий конвейера."""

    @abstractmethod
    def execute(self, context: 'PipelineContext') -> 'PipelineContext':
        pass

    @staticmethod
    def _static_signature(error: Dict[str, Any]) -> str:
        # Единственный источник правды — core.utils.error_signature.
        # Раньше эта функция была продублирована в PipelineContext, pipeline_engine,
        # parallel и тонко расходилась с ними — это ломало AntiLoop/MemoryLearning.
        from core.utils import error_signature
        return error_signature(error)


def _get_source_patterns(language: str) -> List[str]:
    lang = language.lower()
    if lang in ("rust", "rs"):
        return ["*.rs", "Cargo.toml"]
    elif lang in ("python", "py"):
        return ["*.py"]
    elif lang in ("javascript", "typescript", "js", "ts"):
        return ["*.js", "*.ts"]
    else:
        return ["*.*"]