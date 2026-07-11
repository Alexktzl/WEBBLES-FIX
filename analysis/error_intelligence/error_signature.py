"""
Стабильные сигнатуры ошибок для webles_conveyor.
Обеспечивает детерминированные нормализованные идентификаторы ошибок.
Теперь это ЕДИНЫЙ класс ErrorSignature для всей системы (frozen, hashable).
"""

import hashlib
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class SignatureMode(Enum):
    """Режим построения сигнатуры."""
    LOGICAL = "logical"   # ошибка как тип (без учёта строки)
    INSTANCE = "instance" # конкретный экземпляр (с учётом строки)


# Схема и версия сигнатуры
SCHEMA_ID = "f2:code+msg+type+line(opt)"
SCHEMA_HASH = hashlib.sha256(SCHEMA_ID.encode()).hexdigest()[:12]
SIGNATURE_VERSION = f"v6:{SCHEMA_HASH}"
NORMALIZATION_PROFILE = "minimal"


@dataclass(frozen=True)
class ErrorSignature:
    """
    Единый класс сигнатуры ошибки.
    Неизменяемый (frozen), hashable, с богатым набором полей.
    Используется во всей системе: аудит, кластеризация, сравнение.
    """
    file: str
    code: Optional[str] = None
    message: Optional[str] = None
    line: Optional[int] = None
    error_type: Optional[str] = None
    severity: Optional[str] = None

    def identify(self, mode: SignatureMode = SignatureMode.LOGICAL, project_root: Optional[str] = None) -> str:
        """
        Возвращает стабильный идентификатор ошибки (хэш) для заданного режима.
        Если указан project_root, путь к файлу делается относительным для воспроизводимости.
        """
        # Нормализация пути
        norm_file = os.path.normpath(self.file).replace("\\", "/")
        if project_root:
            root = os.path.normpath(project_root).replace("\\", "/")
            if norm_file.startswith(root):
                norm_file = norm_file[len(root):].lstrip("/")
        else:
            norm_file = os.path.basename(norm_file)

        normalized_msg = self._normalize_message(self.message or "")

        components = [
            SIGNATURE_VERSION,
            NORMALIZATION_PROFILE,
            mode.value,
            norm_file,
        ]

        if self.code:
            components.append(f"PRIMARY:{self.code}")
        components.append(f"SECONDARY:{normalized_msg}")

        if self.error_type:
            components.append(f"type={self.error_type}")

        if mode == SignatureMode.INSTANCE:
            line_repr = str(self.line) if self.line is not None else "none"
            components.append(f"line={line_repr}")

        combined = "::".join(components)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def to_hash(self) -> str:
        """Возвращает стабильный хэш для сравнения (логический режим без project_root)."""
        return self.identify(mode=SignatureMode.LOGICAL)

    def to_key(self, mode: SignatureMode = SignatureMode.LOGICAL, project_root: Optional[str] = None) -> str:
        """Возвращает человекочитаемый ключ для отладки и группировки."""
        norm_file = os.path.normpath(self.file).replace("\\", "/")
        if project_root:
            root = os.path.normpath(project_root).replace("\\", "/")
            if norm_file.startswith(root):
                norm_file = norm_file[len(root):].lstrip("/")
        else:
            norm_file = os.path.basename(norm_file)

        base = norm_file
        if mode == SignatureMode.INSTANCE and self.line:
            base += f":{self.line}"
        if self.error_type:
            base = f"{self.error_type}:{base}"
        if self.code:
            return f"{base}::{self.code}"
        normalized = self._normalize_message(self.message or "")
        return f"{base}::{normalized[:100]}"

    def to_tuple(self) -> tuple:
        return (self.file, self.code or "", self._normalize_message(self.message or ""))

    @staticmethod
    def _normalize_message(msg: str) -> str:
        """Минимальная нормализация: замена чисел (целых слов) и схлопывание пробелов."""
        normalized = re.sub(r'\b\d+\b', '#', msg)
        normalized = ' '.join(normalized.split())
        return normalized

    @classmethod
    def from_error(cls, error: Dict[str, Any]) -> "ErrorSignature":
        """Создаёт ErrorSignature из словаря с данными ошибки (для совместимости с audit.py и др.)."""
        return cls(
            file=error.get("file", ""),
            code=error.get("code") or error.get("error_code"),
            message=error.get("message", ""),
            line=error.get("line"),
            error_type=error.get("error_type"),
            severity=error.get("severity"),
        )

    @classmethod
    def from_error_dict(cls, error: Dict[str, Any]) -> "ErrorSignature":
        """Алиас для from_error."""
        return cls.from_error(error)

    @classmethod
    def from_record(cls, record: "ErrorRecord") -> "ErrorSignature":
        """Создаёт сигнатуру из ErrorRecord (для совместимости со старым кодом)."""
        return cls(
            file=record.file,
            code=record.code,
            message=record.message,
            line=None,  # сигнатура без строки
            error_type=record.error_class,
            severity=None,
        )

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ErrorSignature):
            return self.to_tuple() == other.to_tuple()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.to_tuple())

    def __repr__(self) -> str:
        return f"ErrorSignature({self.to_key()})"