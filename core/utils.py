"""
Общие утилиты для Webbles Fix.

Сейчас содержит каноническую реализацию `error_signature` — раньше она
дублировалась в 4 местах (PipelineStage, PipelineContext, pipeline_engine,
parallel) с тонкими расхождениями: один вариант давал `{file}::{code}`,
другой `{file}::{code}::{normalized[:80]}`. Из-за этого один и тот же
error попадал в `processed_errors`/`unfixable` под разными ключами,
ломая AntiLoop и MemoryLearning.

Все вызовы теперь делегируют сюда.
"""

import re
from typing import Any, Dict

_DIGITS_RE = re.compile(r'\d+')
_NON_WORD_RE = re.compile(r'[^\w\s]')


def normalize_message(msg: str) -> str:
    """Нормализует текст сообщения ошибки для сравнения сигнатур.

    Заменяет числа на `#`, удаляет пунктуацию, схлопывает пробелы,
    приводит к нижнему регистру.
    Это даёт устойчивую к мелким перестановкам сигнатуру.
    """
    if not msg:
        return ""
    normalized = msg.lower()
    normalized = _DIGITS_RE.sub('#', normalized)
    normalized = _NON_WORD_RE.sub('', normalized)
    normalized = ' '.join(normalized.split())
    # "SyntaxError invalid syntax" и "invalid syntax" — одна и та же E999.
    if normalized.startswith('syntaxerror '):
        normalized = normalized[len('syntaxerror '):]
    return normalized


def error_signature(error: Dict[str, Any]) -> str:
    """Канонический ключ для memory / unfixable / successful_fixes.

    Формат:
      - если есть code:        `{file}::{code}::{normalized_message[:80]}`
      - если нет:              `{file}::{normalized_message[:100]}`

    Включаем нормализованное сообщение, потому что один error_code
    (например E0382) может означать разные ошибки в разных местах,
    и MemoryLearning должен это различать.
    """
    if not isinstance(error, dict):
        return ""
    file = error.get("file") or ""
    code = error.get("code") or error.get("error_code") or ""
    msg = error.get("message") or ""
    normalized = normalize_message(msg)
    if code:
        return f"{file}::{code}::{normalized[:80]}"
    return f"{file}::{normalized[:100]}"


def process_key(error: Dict[str, Any]) -> str:
    """Ключ для processed_errors — включает номер строки.

    В отличие от error_signature, каждый экземпляр одного и того же кода
    на разных строках получает независимый счётчик попыток. Это позволяет
    pipeline обрабатывать несколько E128/E302/E231 в одном файле без
    преждевременного исчерпания лимита попыток.
    """
    if not isinstance(error, dict):
        return ""
    base = error_signature(error)
    line = error.get("line") or 0
    return f"{base}::L{line}"
