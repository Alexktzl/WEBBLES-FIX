"""
Контрактная модель данных для всей системы Webbles Fix.

Фиксирует:
- ErrorRecord — полную запись об ошибке
- ErrorSignature — единый класс, реэкспортированный из analysis.error_intelligence.error_signature
- InvariantDecision — результат проверки инварианта
- MetadataKeys — именованные ключи для context.metadata
- AuditVerdict — строгие критерии успеха/провала аудита (включая инварианты)
- ClassificationRules — правила и веса классов ошибок (запасной вариант)
- вспомогательные функции валидации и нормализации

Все стадии и модули (анализаторы, классификатор, аудит, генерация патчей)
должны импортировать эти определения для исключения расхождений в форматах данных.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# =============================================================================
# ЕДИНЫЙ КЛАСС СИГНАТУРЫ ОШИБКИ (из analysis.error_intelligence.error_signature)
# =============================================================================
from analysis.error_intelligence.error_signature import ErrorSignature  # теперь это единый frozen класс

# Для обратной совместимости оставляем алиас, если кто-то вдруг использует from core.contract import ErrorSignature как раньше
# (сам класс теперь живёт в analysis.error_intelligence.error_signature)


# =============================================================================
# ЗАПИСЬ ОБ ОШИБКЕ
# =============================================================================
@dataclass
class ErrorRecord:
    """
    Полная запись об ошибке, проходящая через конвейер.
    Содержит все поля, необходимые для классификации, приоритизации и аудита.
    """
    file: str
    line: int
    code: str
    message: str
    language: str = "rust"
    level: str = "error"           # error, warning, note, help
    source: str = ""               # rustc, clippy, ...
    error_class: str = ""          # CRITICAL_SYNTAX, BLOCKING, CLEANUP, ...
    allowed_actions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Экспорт в словарь для передачи между стадиями."""
        return {
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "message": self.message,
            "language": self.language,
            "level": self.level,
            "source": self.source,
            "error_class": self.error_class,
            "allowed_actions": self.allowed_actions,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ErrorRecord":
        """Импорт из словаря."""
        return cls(
            file=d.get("file", ""),
            line=int(d.get("line", 0)),
            code=d.get("code", ""),
            message=d.get("message", ""),
            language=d.get("language", "rust"),
            level=d.get("level", "error"),
            source=d.get("source", ""),
            error_class=d.get("error_class", ""),
            allowed_actions=d.get("allowed_actions", []),
            metadata=d.get("metadata", {}),
        )

    @property
    def signature(self) -> ErrorSignature:
        """Сигнатура ошибки, не зависящая от номера строки."""
        return ErrorSignature.from_record(self)

    def is_valid(self) -> bool:
        """Минимальная валидность записи."""
        return bool(self.file) and bool(self.message)

    def __str__(self) -> str:
        return f"{self.file}:{self.line} [{self.code}] {self.message}"


# =============================================================================
# РЕЗУЛЬТАТ ИНВАРИАНТНОЙ ПРОВЕРКИ
# =============================================================================
@dataclass(frozen=True)
class InvariantDecision:
    """Результат проверки инварианта патча."""
    accepted: bool
    reason: Optional[str] = None

    @classmethod
    def accepted(cls) -> "InvariantDecision":
        return cls(accepted=True)

    @classmethod
    def rejected(cls, reason: str) -> "InvariantDecision":
        return cls(accepted=False, reason=reason)

    def to_dict(self) -> Dict[str, Any]:
        d = {"accepted": self.accepted}
        if self.reason:
            d["reason"] = self.reason
        return d


# =============================================================================
# КЛЮЧИ МЕТАДАННЫХ КОНТЕКСТА
# =============================================================================
class MetadataKeys:
    """
    Именованные ключи, используемые в context.metadata.
    Исключают опечатки и гарантируют единые названия во всех стадиях.
    """
    FILE_ERROR_SIGNATURES_BEFORE = "file_error_signatures_before"
    SEGMENTED_ATTEMPTS = "segmented_attempts"
    LAST_PATCH_FAILURE = "last_patch_failure"
    COMPILER_FEEDBACK = "compiler_feedback"
    LAST_DECISION = "last_decision"
    PATCH_SOURCE = "patch_source"
    EMPTY_RETRIES = "empty_retries"
    PATCH_ATTEMPT = "patch_attempt"
    DYNAMIC_PARAMS = "dynamic_params"
    FINAL_RESOLVED = "final_resolved"
    SEGMENTED_PATCHES = "segmented_patches"
    FULL_FILE_REPLACEMENT = "full_file_replacement"
    PATCH_SNAPSHOTS = "patch_snapshots"                 # снимки сегментов до/после
    INVARIANT_VIOLATIONS = "invariant_violations"       # список нарушений инвариантов
    CONFIDENCE = "confidence"                           # уверенность в патче (D.2)
    FILE_CONTENT_HASH = "file_content_hash"             # sha256 снапшота, ушедшего в LLM (stale detection)
    ATTEMPT_HISTORY = "_attempt_history"                # list[{attempt, intent, failure, result, net_delta, new_errors}] per error_sig
    LAST_PATCH_INTENT = "last_patch_intent"             # intent последнего сгенерированного патча
    LAST_ATTEMPT_DATA = "_last_attempt_data"            # {result, reason, net_delta, new_errors} — структурированные данные последней попытки
    PROJECT_DEADLINE = "_project_deadline"              # time.monotonic() дедлайн project_timeout; читают внутренние retry-петли стадий
    PRECLEANUP_FAILED_RULES = "_precleanup_failed_rules"  # list["file::rule_code"] — детерминированно проваленные правки, не повторять между циклами


# =============================================================================
# СTAGE D.2 — DEFAULT CONFIDENCE BY PATCH SOURCE
# =============================================================================
# Используется DecideStage (safety net) и ReviewStage (D.3, ±0.2 от default).
# structured_llm здесь специально нет: на этом пути confidence пишется из
# EditSet.confidence ещё в B.4. Если оно почему-то отсутствует — фолбэк ниже
# трактует структурный путь как умеренный (0.7).

_DEFAULT_CONFIDENCE_BY_SOURCE: Dict[str, float] = {
    # rule-based детерминированные правила — самые надёжные.
    "quote_heuristic": 0.95,
    "brace_heuristic": 0.95,
    "splice_recovery": 0.95,
    # уже проверенные и подтверждённые когда-то патчи.
    "golden": 0.95,
    "memory": 0.9,
    # tool-assisted фиксы — высокое доверие, но контекстный.
    "correctr": 0.85,
    "semantic_repair": 0.85,
    # Конверсия-1 (2026-07-02): детерминированный `# type: ignore[...]` для
    # _UNFIXABLE_MYPY_CODES. Раньше источника не было в карте → 0.5 →
    # гарантированно ниже и accept-порога (0.85), и даже reject-порога на
    # ветке count-unchanged — вместе с flake8-слепотой after-скана это
    # давало 35% accept у ПОЛНОСТЬЮ детерминированного фикса. Целевой
    # mypy-re-check (ValidateStage._recheck_mypy_target) теперь независимо
    # подтверждает исчезновение ошибки перед принятием.
    "type_ignore": 0.9,
    # структурный путь — если confidence из EditSet почему-то потерян.
    "structured_llm": 0.7,
    "structured_llm_blocking": 0.7,
    # подсказки rustc — надёжнее свободного LLM.
    "rustc_suggestion": 0.7,
    # legacy LLM — не имеют ни ревью, ни валидации EditSet.
    "llm": 0.6,
    "llm_critical": 0.6,
    "llm_segmented": 0.6,
    # llm_blocking_<phase> — обрабатывается префиксом ниже.
}

# Нейтральное значение, если источник вообще не известен.
DEFAULT_CONFIDENCE_UNKNOWN: float = 0.5


def default_confidence_for(patch_source: str) -> float:
    """Возвращает default confidence для данного `patch_source`.

    Поддерживает префиксное сопоставление для `llm_blocking_<phase>`.
    Никаких исключений не бросает: на пустой строке вернёт нейтральное
    `DEFAULT_CONFIDENCE_UNKNOWN`. Это сознательно — функция используется
    в DecideStage как safety net, и должна работать молча даже если в
    metadata оказался unexpected source.
    """
    src = (patch_source or "").strip()
    if not src:
        return DEFAULT_CONFIDENCE_UNKNOWN
    if src in _DEFAULT_CONFIDENCE_BY_SOURCE:
        return _DEFAULT_CONFIDENCE_BY_SOURCE[src]
    # llm_blocking_single / llm_blocking_multi / llm_blocking_segment — все
    # «обычный LLM с дополнительной итерацией».
    if src.startswith("llm_blocking_"):
        return 0.6
    return DEFAULT_CONFIDENCE_UNKNOWN


# =============================================================================
# ВЕРДИКТ АУДИТА
# =============================================================================
class AuditVerdict(Enum):
    """Строго определённые исходы финального аудита."""
    IMPROVED_NO_NEW_ERRORS = auto()
    NO_DEGRADATION = auto()
    NEW_ERRORS_DETECTED = auto()
    INVARIANT_VIOLATION = auto()          # патч сломал бизнес-логику
    FALLBACK_INCONCLUSIVE = auto()

    def is_success(self) -> bool:
        """Успешным считается улучшение или отсутствие деградации (но не нарушение инвариантов)."""
        return self in (AuditVerdict.IMPROVED_NO_NEW_ERRORS, AuditVerdict.NO_DEGRADATION)


# =============================================================================
# ПРАВИЛА КЛАССИФИКАЦИИ ОШИБОК (запасной статический набор)
# =============================================================================
class ClassificationRules:
    """
    Инкапсулирует логику классификации ошибок на основе их атрибутов.
    Не зависит от языка программирования.
    Используется ТОЛЬКО как fallback, если нет языкового провайдера.
    """
    CRITICAL_SYNTAX = "CRITICAL_SYNTAX"
    BLOCKING = "BLOCKING"
    STRUCTURAL = "STRUCTURAL"
    CLEANUP = "CLEANUP"
    WARNING = "WARNING"
    UNKNOWN = "UNKNOWN"

    CLASS_WEIGHTS: Dict[str, float] = {
        CRITICAL_SYNTAX: 200.0,
        BLOCKING: 100.0,
        STRUCTURAL: 70.0,
        CLEANUP: 30.0,
        WARNING: 10.0,
        UNKNOWN: 50.0,
    }

    CRITICAL_KEYWORDS: FrozenSet[str] = frozenset({
        "unclosed", "unterminated", "double quote",
        "expected ';'", "expected token",
        "expected one of", "expected '('", "expected ')'", "expected '{'",
        "expected '}'", "expected expression", "could not parse",
        "unexpected token", "unexpected end of file",
        "unknown prefix",
    })

    @staticmethod
    def classify_by_message(message: str) -> str:
        """Классифицирует только по тексту сообщения (без привязки к коду или языку)."""
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in ClassificationRules.CRITICAL_KEYWORDS):
            return ClassificationRules.CRITICAL_SYNTAX
        return ClassificationRules.UNKNOWN

    @staticmethod
    def get_weight(error_class: str) -> float:
        """Возвращает вес класса ошибки для вычисления здоровья проекта."""
        return ClassificationRules.CLASS_WEIGHTS.get(
            error_class, ClassificationRules.CLASS_WEIGHTS[ClassificationRules.UNKNOWN]
        )


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================
def extract_signatures(errors: List[Dict[str, Any]]) -> set:
    """Извлекает множество ErrorSignature из списка словарей ошибок."""
    return {ErrorSignature.from_error(e) for e in errors}

def normalize_and_compare(
    before: set, after: set
) -> Tuple[set, set, set]:
    """
    Сравнивает множества сигнатур до и после.
    Возвращает (новые, исчезнувшие, общие).
    """
    new_sigs = after - before
    lost_sigs = before - after
    common = before & after
    return new_sigs, lost_sigs, common

def audit_verdict_from_sets(
    before: set, after: set
) -> AuditVerdict:
    """
    Принимает два множества сигнатур (до и после) и возвращает вердикт аудита.
    Для инвариантных нарушений используйте AuditVerdict.INVARIANT_VIOLATION напрямую.
    """
    new_sigs, lost_sigs, _ = normalize_and_compare(before, after)
    if not new_sigs and lost_sigs:
        return AuditVerdict.IMPROVED_NO_NEW_ERRORS
    elif not new_sigs and not lost_sigs:
        return AuditVerdict.NO_DEGRADATION
    else:
        return AuditVerdict.NEW_ERRORS_DETECTED