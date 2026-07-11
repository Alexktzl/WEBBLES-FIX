"""
Модуль сохранения и восстановления состояния конвейера.
Вынесен из pipeline_engine.py для модульности.
Содержит подробные DEBUG-логи.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from core.anti_loop import AntiLoop
from core.pipeline_context import PipelineContext

logger = logging.getLogger(__name__)


class StateManager:
    """Управляет сериализацией и восстановлением состояния конвейера."""

    def __init__(self, project_path: Path, memory, state_lock,
                 runtime_dir: Optional[Path] = None):
        self.project_path = project_path
        self.memory = memory
        self._state_lock = state_lock
        self._runtime_dir = runtime_dir
        self.STATE_FILE = "state.json"

    @property
    def state_file(self) -> Path:
        if self._runtime_dir:
            return self._runtime_dir / self.STATE_FILE
        return self.project_path / ".webbles_fix" / self.STATE_FILE

    # H6 (аудит 2026-07-01): file_error_signatures_before — dict из set-ов
    # объектов ErrorSignature. json.dump(default=str) превращал каждый set в
    # ОДНУ строку "{ErrorSignature(...)}" — на resume audit_run получал
    # строку вместо set и терял весь baseline (все ошибки «новые» / fallback
    # без символьных проверок). Сериализуем в списки кортежей и
    # восстанавливаем в set-ы на load. invariant_guard — живой объект,
    # в persist ему не место (движок переинжектит его после load).
    _SIGS_KEY = "file_error_signatures_before"

    @classmethod
    def _serialize_metadata(cls, metadata: dict) -> dict:
        meta = dict(metadata or {})
        meta.pop("invariant_guard", None)
        sigs = meta.get(cls._SIGS_KEY)
        if isinstance(sigs, dict):
            out = {}
            for fname, items in sigs.items():
                triples = []
                for item in (items or []):
                    if hasattr(item, "to_tuple"):
                        triples.append(list(item.to_tuple()))
                    elif isinstance(item, (list, tuple)) and len(item) >= 3:
                        triples.append([str(item[0]), str(item[1]), str(item[2])])
                out[fname] = triples
            meta[cls._SIGS_KEY] = out
        return meta

    @classmethod
    def _deserialize_metadata(cls, metadata: dict) -> dict:
        meta = dict(metadata or {})
        sigs = meta.get(cls._SIGS_KEY)
        if isinstance(sigs, dict):
            meta[cls._SIGS_KEY] = {
                fname: {
                    tuple(str(x) for x in item[:3])
                    for item in (items or [])
                    if isinstance(item, (list, tuple)) and len(item) >= 3
                }
                for fname, items in sigs.items()
                if isinstance(items, (list, tuple, set))
            }
        return meta

    @staticmethod
    def _sanitize_for_persist(obj):
        """Рекурсивно удаляет потенциальные секреты ИЗ снимка перед записью
        на диск. Удаляем любые ключи, содержащие подстроки secret/token/
        api_key/password/credential/auth (case-insensitive). Также
        зануляем сам ключ `config` целиком — он живёт в `.env` и
        `webles_config.json`, в state ему не место.
        """
        _SENSITIVE = ("secret", "token", "api_key", "apikey",
                      "password", "credential", "authorization", "auth_")
        if isinstance(obj, dict):
            cleaned = {}
            for k, v in obj.items():
                ks = str(k).lower()
                if ks == "config":
                    cleaned[k] = "<redacted: config not persisted in state>"
                    continue
                if any(s in ks for s in _SENSITIVE):
                    cleaned[k] = "<redacted>"
                    continue
                cleaned[k] = StateManager._sanitize_for_persist(v)
            return cleaned
        if isinstance(obj, list):
            return [StateManager._sanitize_for_persist(x) for x in obj]
        return obj

    def save_state(self, context: PipelineContext, anti_loop: AntiLoop,
                   circuit_breaker, dynamic_params: dict, step_times: dict,
                   start_time: float) -> None:
        """Сохраняет текущее состояние конвейера в файл."""
        with self._state_lock:
            _ctx_dict = context.to_dict()
            _ctx_dict["metadata"] = self._serialize_metadata(_ctx_dict.get("metadata") or {})
            snapshot = {
                "context": _ctx_dict,
                "anti_loop": anti_loop.to_dict(),
                "circuit_breaker": {"failures": circuit_breaker.failure_count},
                "dynamic_params": dynamic_params,
                "step_times": step_times,
                "total_runtime": __import__('time').time() - start_time,
                "timestamp": __import__('datetime').datetime.utcnow().isoformat(),
            }
            try:
                # P0.2: создаём .webbles_fix/ при необходимости и
                # ВЫЧИЩАЕМ потенциальные секреты из снимка перед записью.
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                safe_snapshot = self._sanitize_for_persist(snapshot)
                with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8',
                    dir=self.state_file.parent,
                    prefix='.webbles_tmp_',
                    delete=False
                ) as tf:
                    json.dump(safe_snapshot, tf, indent=2, default=str)
                    tmp_name = tf.name
                os.replace(tmp_name, self.state_file)
                logger.debug("Состояние сохранено в %s (sanitized)", self.state_file)
            except Exception as e:
                logger.warning("Не удалось сохранить состояние: %s", e)

    def load_state(self) -> PipelineContext:
        """Загружает состояние из файла, если он существует."""
        if not self.state_file.exists():
            logger.debug("Файл состояния не найден, старт с чистого листа")
            return None

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            ctx_data = data.get("context", {})
            # H6: восстанавливаем типы сигнатур (списки кортежей → set-ы).
            ctx_data["metadata"] = self._deserialize_metadata(ctx_data.get("metadata") or {})
            # H6 (попутная находка): _sanitize_for_persist заменяет config
            # СТРОКОЙ-плейсхолдером — PipelineContext.__init__ зовёт
            # self.config.get(...) и падал → load_state возвращал None →
            # resume ТИХО стартовал с чистого листа с момента появления
            # санитизации. Живой config переинжектит PipelineEngine после
            # load (см. комментарий в PipelineEngine.__init__).
            if not isinstance(ctx_data.get("config"), dict):
                ctx_data["config"] = {}
            context = PipelineContext.from_dict(ctx_data, memory=self.memory)
            logger.info("Состояние загружено из %s", self.state_file)
            logger.debug("Загруженный контекст: %d ошибок, состояние %s",
                         len(context.current_errors), context.current_state.name)
            return context
        except Exception as e:
            logger.error("Ошибка загрузки состояния: %s; начинаем заново", e)
            return None