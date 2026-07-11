"""
Кэш состояний для планирования в webles_conveyor.
Обеспечивает детерминированное хеширование состояния проекта и мемоизацию результатов симуляции.
Позволяет использовать DAG вместо дерева поиска.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.pipeline_context import PipelineContext
from core.system_health import SystemHealth

logger = logging.getLogger(__name__)

CACHE_VERSION = "v2"


@dataclass
class CachedState:
    """Сериализуемый результат симулированного состояния."""
    health: SystemHealth
    errors: List[Dict[str, Any]]
    validation_results: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "health": self.health.to_dict(),
            "errors": self.errors,
            "validation_results": self.validation_results,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CachedState":
        health_data = data.get("health", {})
        health = SystemHealth(
            stability=health_data.get("stability", 1.0),
            error_trend_slope=health_data.get("error_trend_slope", 0.0),
            rollback_rate=health_data.get("rollback_rate", 0.0),
            patch_success_rate=health_data.get("patch_success_rate", 1.0),
            memory_hit_rate=health_data.get("memory_hit_rate", 0.0),
            entropy=health_data.get("entropy", 0.0),
        )
        return cls(
            health=health,
            errors=data.get("errors", []),
            validation_results=data.get("validation_results", {}),
            metadata=data.get("metadata", {}),
        )


class StateHasher:
    """
    Вычисляет детерминированный хеш состояния проекта.
    Использует два слоя: хеши содержимого файлов и нормализованные сигнатуры ошибок.
    """

    def __init__(self, project_path: Path, tracked_extensions: Optional[List[str]] = None):
        self.project_path = project_path
        self.tracked_extensions = tracked_extensions or [".rs", ".py", ".js", ".ts"]
        self.version = CACHE_VERSION

    def compute_hash(self, context: PipelineContext) -> str:
        """
        Вычисляет уникальный хеш для текущего состояния на основе:
        - хешей содержимого отслеживаемых файлов
        - нормализованных сигнатур ошибок
        """
        file_hashes = self._compute_file_hashes(context)
        error_signatures = self._compute_error_signatures(context)

        state_dict = {
            "version": self.version,
            "files": file_hashes,
            "errors": error_signatures,
        }

        serialized = json.dumps(state_dict, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _compute_file_hashes(self, context: PipelineContext) -> Dict[str, str]:
        file_hashes = {}
        for ext in self.tracked_extensions:
            for file_path in self.project_path.rglob(f"*{ext}"):
                if self._should_skip(file_path):
                    continue
                try:
                    rel_path = str(file_path.relative_to(self.project_path))
                    content = file_path.read_bytes()
                    file_hashes[rel_path] = hashlib.sha256(content).hexdigest()
                except Exception as e:
                    logger.debug(f"Не удалось хешировать {file_path}: {e}")
        config_files = ["Cargo.toml", "Cargo.lock", "package.json", "package-lock.json",
                        "tsconfig.json", "pyproject.toml", "requirements.txt"]
        for cf in config_files:
            cf_path = self.project_path / cf
            if cf_path.exists():
                try:
                    content = cf_path.read_bytes()
                    file_hashes[cf] = hashlib.sha256(content).hexdigest()
                except Exception:
                    pass
        return file_hashes

    def _should_skip(self, path: Path) -> bool:
        parts = path.parts
        skip_dirs = {".git", "__pycache__", "node_modules", "target", "venv", ".venv", "env", "dist", "build"}
        return any(part in skip_dirs for part in parts)

    def _compute_error_signatures(self, context: PipelineContext) -> List[str]:
        signatures = []
        for err in context.current_errors:
            sig = self._error_signature(err)
            signatures.append(sig)
        return sorted(signatures)

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


class StateCache:
    """
    Кэш в памяти для симулированных состояний. Обеспечивает мемоизацию для планирования.
    """

    def __init__(self, hasher: Optional[StateHasher] = None):
        self._cache: Dict[str, CachedState] = {}
        self.hasher = hasher

    def get(self, state_hash: str) -> Optional[CachedState]:
        return self._cache.get(state_hash)

    def put(self, state_hash: str, state: CachedState) -> None:
        self._cache[state_hash] = state

    def has(self, state_hash: str) -> bool:
        return state_hash in self._cache

    def clear(self) -> None:
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "entries": {h: s.to_dict() for h, s in self._cache.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateCache":
        cache = cls()
        entries = data.get("entries", {})
        for h, s_data in entries.items():
            cache._cache[h] = CachedState.from_dict(s_data)
        return cache

    def save_to_file(self, path: Path) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception as e:
            logger.warning(f"Не удалось сохранить кэш состояний: {e}")

    @classmethod
    def load_from_file(cls, path: Path) -> "StateCache":
        if not path.exists():
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception as e:
            logger.warning(f"Не удалось загрузить кэш состояний: {e}")
            return cls()