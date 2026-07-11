"""
Многоуровневая антипетля для Webbles Fix.
Отслеживает:
- глобальные сигнатуры ошибок и хеши патчей (AntiLoop)
- попытки для отдельных сегментов (SegmentAntiLoop)
- попытки для целых файлов с учётом сигнатур ошибок (FileAntiLoop)
Сброс счётчиков при изменении содержимого файлов или успешном применении патча.
"""

import logging
from collections import defaultdict
from pathlib import PurePath
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


class AntiLoop:
    """
    Глобальная антипетля (уровень сигнатур ошибок и патчей).
    Пороги ослаблены для больших проектов и поддержки золотых патчей.
    Счётчики сигнатур ошибок сбрасываются при успешном применении патча.
    """

    def __init__(self, max_iterations: int = 50, max_repeated_patches: int = 60, max_error_occurrence: int = 15):
        self.max_iterations = max_iterations
        self.max_repeated_patches = max_repeated_patches
        self.max_error_occurrence = max_error_occurrence
        self.history: List[Tuple[str, str, str]] = []
        self.error_occurrence: Dict[str, int] = defaultdict(int)
        self.patch_occurrence: Dict[str, int] = defaultdict(int)

    def record_attempt(self, error_signature: str, patch_hash: str, file_path: str) -> None:
        self.history.append((error_signature, patch_hash, file_path))
        self.error_occurrence[error_signature] += 1
        self.patch_occurrence[patch_hash] += 1
        logger.debug("AntiLoop: сигнатура=%s хеш=%s файл=%s",
                     error_signature, patch_hash, file_path)

    def record_success(self, error_signature: str) -> None:
        """Сбрасывает счётчик для сигнатуры ошибки, т.к. патч был успешно применён."""
        if error_signature in self.error_occurrence:
            logger.info("AntiLoop: сброс счётчика сигнатуры %s (успешное исправление)", error_signature)
            self.error_occurrence[error_signature] = 0

    def should_break(self) -> bool:
        for count in self.patch_occurrence.values():
            if count >= self.max_repeated_patches:
                logger.warning("AntiLoop: патч применился %d раз – разрыв", count)
                return True
        for count in self.error_occurrence.values():
            if count >= self.max_error_occurrence:
                logger.warning("AntiLoop: сигнатура ошибки встречена %d раз – разрыв", count)
                return True
        return False

    def reset(self) -> None:
        self.history.clear()
        self.error_occurrence.clear()
        self.patch_occurrence.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "history": self.history,
            "error_occurrence": dict(self.error_occurrence),
            "patch_occurrence": dict(self.patch_occurrence),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AntiLoop":
        obj = cls()
        obj.history = data.get("history", [])
        obj.error_occurrence = defaultdict(int, data.get("error_occurrence", {}))
        obj.patch_occurrence = defaultdict(int, data.get("patch_occurrence", {}))
        return obj


class SegmentAntiLoop:
    """
    Антипетля для сегментов (ханков).
    Отслеживает попытки для пары (file_path, segment_start_line).
    Лимит: 20 попыток.
    """

    def __init__(self, max_attempts_per_segment: int = 20):
        self.max_attempts = max_attempts_per_segment
        self._attempts: Dict[Tuple[str, int], int] = {}
        self._file_hashes: Dict[str, int] = {}

    def record_attempt(self, file_path: str, segment_start_line: int,
                       current_file_hash: int) -> bool:
        """
        Возвращает True, если сегмент ещё не заблокирован,
        и False, если лимит исчерпан.
        """
        # Сброс при изменении файла
        old_hash = self._file_hashes.get(file_path)
        if old_hash is not None and old_hash != current_file_hash:
            logger.info("SegmentAntiLoop: файл %s изменился – сброс счётчиков сегментов", file_path)
            keys_to_remove = [k for k in self._attempts if k[0] == file_path]
            for k in keys_to_remove:
                del self._attempts[k]
        self._file_hashes[file_path] = current_file_hash

        key = (file_path, segment_start_line)
        count = self._attempts.get(key, 0) + 1
        self._attempts[key] = count

        if count > self.max_attempts:
            logger.warning("SegmentAntiLoop: сегмент %s:%d заблокирован (%d/%d)",
                           file_path, segment_start_line, count, self.max_attempts)
            return False
        logger.debug("SegmentAntiLoop: сегмент %s:%d попытка %d/%d",
                     file_path, segment_start_line, count, self.max_attempts)
        return True

    def is_stuck(self, file_path: str, segment_start_line: int) -> bool:
        key = (file_path, segment_start_line)
        return self._attempts.get(key, 0) >= self.max_attempts

    def reset_segment(self, file_path: str, segment_start_line: int) -> None:
        key = (file_path, segment_start_line)
        self._attempts.pop(key, None)
        logger.debug("SegmentAntiLoop: сброшен счётчик для %s:%d", file_path, segment_start_line)

    def reset_all(self) -> None:
        self._attempts.clear()
        self._file_hashes.clear()
        logger.info("SegmentAntiLoop: полный сброс")


class FileAntiLoop:
    """
    Антипетля для целых файлов с учётом сигнатур ошибок.
    Хранит общий счётчик попыток на файл и счётчик для каждой сигнатуры.
    Смена сигнатуры сбрасывает счётчик сигнатуры, но общий лимит сохраняется.

    2026-06-25 (контрольная серия на 10 проектах, jsonschema/validators.py):
    при старых порогах (12/20) один файл с анкор-несовпадением (anchor не
    найден — каждая попытка реально вызывает LLM на full-file regeneration)
    дошёл до 21 попытки, прежде чем антипетля сработала — это десятки секунд
    реальных LLM-вызовов, съевших большую часть PROJECT_TIMEOUT бюджета
    ДО того, как очередь дошла до большинства других ошибок проекта.
    record_attempt не различал "продуктивные" повторы (причина неудачи
    меняется — есть прогресс) от "застрявших" (та же механическая ошибка
    повторяется без изменений) — теперь принимает failure_reason и обрывает
    застрявший файл значительно раньше (_MAX_CONSECUTIVE_SAME_FAILURE),
    не дожидаясь общего лимита попыток. Сами max_attempts_per_file/
    max_total_attempts тоже снижены (12/20 → 6/10) по эмпирике этой серии:
    продуктивные файлы (gunicorn, anyio) укладывались в гораздо меньшее
    число попыток на файл.
    """

    _MAX_CONSECUTIVE_SAME_FAILURE = 3

    # Perf-2 (2026-07-07, цель ≤600s/проект): 6/10 → 4/6. По learning_cases
    # июля продуктивные файлы укладываются в 1-4 попытки; хвост 5-10 занимали
    # только обречённые файлы, которые теперь режут circuit-breaker perf-1
    # (3 безрезультатных LLM-REJECT) и stuck-стрик 3 (стабильные
    # failure_reason на всех rollback-выходах Apply с e1f431a/сегодня).
    # Карантин каталога (2026-07-07, Delgan/loguru): tests/exceptions/source/
    # — корпус из 99 НАМЕРЕННО сломанных файлов-фикстур (loguru тестирует на
    # них форматирование исключений: PEP 750 t-строки, exception groups,
    # специальные F821/B018/E999). Прогон сжёг 1871s и 160 LLM-вызовов при
    # cycles_run=0: per-file защиты не сработали — 76 откатов размазались по
    # ~1-2 на файл, порог unanchorable (3 на файл) не набирался. Агрегация по
    # КАТАЛОГУ ловит корпус целиком: 3 заблокированных файла одной директории
    # → остальные её файлы (и подкаталоги) в NR без LLM.
    DIR_QUARANTINE_THRESHOLD = 3

    def __init__(self, max_attempts_per_file: int = 4, max_total_attempts: int = 6):
        self.max_attempts_per_file = max_attempts_per_file
        self.max_total_attempts = max_total_attempts
        self._attempts: Dict[str, int] = {}           # общий счётчик попыток на файл
        self._sig_attempts: Dict[Tuple[str, str], int] = {}  # счётчик для пары (файл, сигнатура)
        self._file_hashes: Dict[str, int] = {}
        self._last_signature: Dict[str, str] = {}
        self._last_failure_reason: Dict[str, str] = {}
        self._consecutive_same_failure: Dict[str, int] = {}
        # dir-карантин: каталог → множество заблокированных файлов (файл
        # считается один раз, каким бы путём его ни заблокировали).
        self._blocked_files_by_dir: Dict[str, Set[str]] = defaultdict(set)

    def record_attempt(self, file_path: str, current_file_hash: int,
                       error_signature: str = "", failure_reason: str = "") -> bool:
        """
        Возвращает True, если файл можно обрабатывать.
        Смена сигнатуры ошибки сбрасывает счётчик для пары (файл, сигнатура).
        Общий лимит max_total_attempts сохраняется. failure_reason (если
        передан) сравнивается с предыдущим — повтор ОДНОЙ И ТОЙ ЖЕ причины
        _MAX_CONSECUTIVE_SAME_FAILURE раз подряд обрывает файл досрочно,
        не дожидаясь max_total_attempts.
        """
        # Сброс при изменении файла
        old_hash = self._file_hashes.get(file_path)
        if old_hash is not None and old_hash != current_file_hash:
            logger.info("FileAntiLoop: файл %s изменился – сброс всех счётчиков", file_path)
            self._attempts[file_path] = 0
            self._sig_attempts = {k: v for k, v in self._sig_attempts.items() if k[0] != file_path}
            self._last_signature.pop(file_path, None)
            self._last_failure_reason.pop(file_path, None)
            self._consecutive_same_failure.pop(file_path, None)
        self._file_hashes[file_path] = current_file_hash

        # Общий счётчик — не растём за лимит во избежание бесконечного роста (BUG-5).
        total = self._attempts.get(file_path, 0) + 1
        self._attempts[file_path] = min(total, self.max_total_attempts)

        if error_signature:
            prev_sig = self._last_signature.get(file_path)
            if prev_sig is not None and prev_sig != error_signature:
                # сброс счётчика сигнатуры при смене ошибки
                old_key = (file_path, prev_sig)
                self._sig_attempts.pop(old_key, None)
                logger.debug("FileAntiLoop: сигнатура изменилась %s -> %s, сброс счётчика сигнатуры", prev_sig, error_signature)
            self._last_signature[file_path] = error_signature

            key = (file_path, error_signature)
            sig_count = self._sig_attempts.get(key, 0) + 1
            self._sig_attempts[key] = min(sig_count, self.max_attempts_per_file)

            if sig_count > self.max_attempts_per_file:
                logger.warning("FileAntiLoop: сигнатура %s в файле %s превысила лимит (%d/%d)",
                               error_signature, file_path, sig_count, self.max_attempts_per_file)
                self._note_block(file_path)
                return False

        if failure_reason:
            prev_reason = self._last_failure_reason.get(file_path)
            if prev_reason is not None and prev_reason == failure_reason:
                streak = self._consecutive_same_failure.get(file_path, 0) + 1
            else:
                streak = 1
            self._consecutive_same_failure[file_path] = streak
            self._last_failure_reason[file_path] = failure_reason

            if streak >= self._MAX_CONSECUTIVE_SAME_FAILURE:
                logger.warning(
                    "FileAntiLoop: файл %s застрял на одной причине %d раз подряд (%r) — "
                    "обрываем без ожидания общего лимита попыток",
                    file_path, streak, failure_reason[:120],
                )
                self._note_block(file_path)
                return False

        if total > self.max_total_attempts:
            logger.warning("FileAntiLoop: файл %s превысил общий лимит попыток (%d/%d)",
                           file_path, total, self.max_total_attempts)
            self._note_block(file_path)
            return False

        logger.debug("FileAntiLoop: файл %s попытка %d/%d (сигнатура %s)",
                     file_path, total, self.max_total_attempts, error_signature)
        return True

    def _note_block(self, file_path: str) -> None:
        """Регистрирует блокировку файла для dir-карантина у ВСЕХ каталогов-
        предков (кроме корня проекта — карантин всего проекта недопустим).

        2026-07-08 (верификация на loguru, series5_loguru2): первая версия
        писала только прямого родителя — корпус tests/exceptions/source
        разбит на подкаталоги (backtrace/diagnose/modern/...), блокировки
        размазались по ним (2+1+...) и порог 3 не набрался НИ ОДНОМУ
        каталогу; карантин сработал только на плоском .github/workflows.
        Агрегация по предкам суммирует подкаталоги на каждом уровне."""
        norm = str(file_path).replace("\\", "/")
        parent = PurePath(norm).parent
        while str(parent) not in ("", "."):
            self._blocked_files_by_dir[str(parent).replace("\\", "/")].add(norm)
            parent = parent.parent

    def quarantined_dirs(self, threshold: int = None) -> Set[str]:
        """Каталоги, в которых заблокировано >= порога файлов (включая
        подкаталоги — см. _note_block и инцидент loguru в докстринге класса).

        Порог зависит от глубины: для каталогов ВЕРХНЕГО уровня (depth 1,
        например `loguru/` или `tests/`) планка вдвое выше — агрегация по
        предкам иначе могла бы закарантинить корневой пакет проекта после
        3 неудачных файлов в разных его подкаталогах и убить конверсию на
        живом коде. Fixture-корпусы практически всегда вложены
        (tests/exceptions/source, tests/data, ...) — им хватает базового
        порога на глубине >= 2."""
        base = threshold if threshold is not None else self.DIR_QUARANTINE_THRESHOLD
        out = set()
        for d, files in self._blocked_files_by_dir.items():
            depth = d.count("/") + 1
            th = base if depth >= 2 else base * 2
            if len(files) >= th:
                out.add(d)
        return out

    def is_stuck(self, file_path: str) -> bool:
        return self._attempts.get(file_path, 0) >= self.max_total_attempts

    def reset_file(self, file_path: str) -> None:
        self._attempts.pop(file_path, None)
        self._sig_attempts = {k: v for k, v in self._sig_attempts.items() if k[0] != file_path}
        self._last_signature.pop(file_path, None)
        self._last_failure_reason.pop(file_path, None)
        self._consecutive_same_failure.pop(file_path, None)
        logger.debug("FileAntiLoop: сброшены счётчики для %s", file_path)

    def reset_all(self) -> None:
        self._attempts.clear()
        self._sig_attempts.clear()
        self._file_hashes.clear()
        self._last_signature.clear()
        self._last_failure_reason.clear()
        self._consecutive_same_failure.clear()
        logger.info("FileAntiLoop: полный сброс")