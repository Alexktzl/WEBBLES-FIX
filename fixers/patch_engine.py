"""
Движок применения патчей (собственная реализация unified diff).
Расширен методами слияния, проверки пустоты и релевантности патчей.
Все ключевые методы покрыты DEBUG-логами.
Добавлен метод apply_patches_and_diff для последовательного применения
списка патчей и получения чистого объединённого диффа.
Ханки сортируются по убыванию позиции, чтобы избежать сдвига строк.
"""

import difflib
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fixers.patch_lines import (
    HUNK_HEADER_RE,
    body_kind,
    header_path,
    is_file_header,
    is_llm_placeholder,
    strip_body_prefix,
)


def _path_matches(section_file: Optional[str], target_file: Optional[str]) -> bool:
    if target_file is None or section_file is None:
        return True
    sect = section_file.replace("\\", "/").lstrip("./")
    tgt = str(target_file).replace("\\", "/")
    if tgt.endswith(sect) or sect.endswith(tgt):
        return True
    return sect.rsplit("/", 1)[-1] == tgt.rsplit("/", 1)[-1]

logger = logging.getLogger(__name__)


class PatchEngine:
    """Применяет unified diff к файлам, а также предоставляет статические утилиты для работы с патчами."""

    def __init__(self) -> None:
        self.last_error_code: str = ""

    def apply_patch(self, file_path: Path, patch: str) -> bool:
        """Применяет unified-diff к файлу.

        Робастный путь:
          1) Из тела ханка отдельно собираем «старые» строки (` ` + `-`) и
             «новые» (` ` + `+`). Это И ЕСТЬ истинные счётчики: заголовок
             `@@ -N,M +K,L @@` от LLM часто врёт (несовпадение M/L с body),
             если ему верить — удалим лишнюю строку и впихнём дубль.
          2) Перед заменой проверяем, что старые строки реально стоят на
             `old_start` в файле. Если нет — fuzzy: пробуем offset ±5
             (через difflib.SequenceMatcher по самим строкам). Если и так
             не нашли — отказ (return False), patch_stage откатится.
        Это закрывает класс багов «движок ввалил вторую копию строки и съел
        конец функции» при некорректном @@-хедере от LLM.
        """
        logger.debug("apply_patch: файл=%s, размер патча=%d символов", file_path, len(patch))
        if not file_path.exists():
            logger.error("Файл %s не существует", file_path)
            return False
        try:
            original_lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            logger.debug("Файл прочитан, строк: %d", len(original_lines))
        except Exception as e:
            logger.error("Не удалось прочитать файл %s: %s", file_path, e)
            return False
        hunks = self._parse_patch(patch, target_file=str(file_path))
        if not hunks:
            logger.warning("Патч не содержит ханков для %s (или неверный формат)", file_path)
            return False
        logger.debug("Найдено ханков: %d", len(hunks))
        for old_start, old_count, new_lines in sorted(hunks, key=lambda h: h[0], reverse=True):
            logger.debug("Применение ханка: old_start=%d, заявлено old_count=%d, body-строк=%d",
                         old_start, old_count, len(new_lines))
            # --- Сегрегация body на «старые» (то что должно быть в файле) и
            # «новые» (что туда положить) ---
            old_body: List[str] = []
            new_body: List[str] = []
            # Пары (индекс в old_body, индекс в new_body) для контекстных
            # (' ') строк — после поиска анкера подставим туда РЕАЛЬНОЕ
            # содержимое файла, а не то, как LLM его «вспомнила» (другие
            # пробелы/перевод строки в контексте иначе тихо просочились бы
            # в файл даже при успешном совпадении).
            context_link: List[Tuple[int, int]] = []
            for line in new_lines:
                kind = body_kind(line)
                if kind is None:
                    continue
                content = strip_body_prefix(line)
                if kind == ' ':
                    context_link.append((len(old_body), len(new_body)))
                    old_body.append(content)
                    new_body.append(content)
                elif kind == '-':
                    old_body.append(content)
                elif kind == '+':
                    if is_llm_placeholder(content):
                        logger.debug("apply_patch: пропускаем LLM-плейсхолдер %r", content.strip())
                        continue
                    new_body.append(content)

            real_old_count = len(old_body)
            if real_old_count != old_count:
                logger.info("apply_patch: @@-хедер врёт (старых строк в body=%d, заявлено=%d) — берём из body",
                            real_old_count, old_count)

            # --- O.15: детектор слипшейся строки ---
            # Кейс из лога 2026-06-02 (database.py:5): LLM объединил две
            # исходные строки (комментарий + код) в одну `+`, удалив `\n`
            # между ними. Файл при этом синтаксически валиден (комментарий
            # «съел» хвост), DecideStage ACCEPT-нул патч, а в проекте
            # пропал вызов `sqlite3.connect(...)`.
            merge_warning = self._detect_line_merge(old_body, new_body)
            if merge_warning is not None:
                joined_pieces, merged_line = merge_warning
                # O.15-recovery: пробуем разбить слипшуюся строку обратно
                # по позициям piece'ов из old_body (только для пары 2 строк).
                recovered = self._try_recover_line_merge(old_body, new_body, joined_pieces, merged_line)
                if recovered is not None:
                    logger.info(
                        "apply_patch: O.15-recovery — разбита слипшаяся строка '%s' → %d строк",
                        merged_line[:80], len(recovered) - (len(new_body) - 1),
                    )
                    new_body = recovered
                else:
                    logger.error(
                        "apply_patch: O.15 — патч слипает в одну строку %d "
                        "исходных строки (%s) в '%s' — отказ",
                        len(joined_pieces), joined_pieces, merged_line[:120],
                    )
                    self.last_error_code = "O.15"
                    return False

            # --- O.16: детектор pure-insert в непустой файл ---
            # Кейс 2026-06-04: LLM-путь _handle_critical_syntax для E999
            # выдал «полный новый файл» как один большой `+`-ханк БЕЗ `-`-строк.
            # `old_body` оказался пустой, и текущая логика делала ВСТАВКУ в
            # начало (`original_lines[0:0] = new_body`), оставляя старый
            # битый код рядом. Получился файл = двойной конкатенат, который
            # DecideStage случайно принял (E999 ушла из новой части).
            #
            # Защита: если ханк не имеет ни ` `, ни `-` строк (т.е. чистая
            # вставка), а new-блок выглядит как «целый файл» (≥5 строк), и
            # оригинал-файл непустой — это почти наверняка ошибка LLM-разметки.
            # Применять такое нельзя.
            if len(old_body) == 0 and len(new_body) >= 5 and len(original_lines) > 0:
                logger.error(
                    "apply_patch: O.16 — pure-insert ханка (%d новых строк, "
                    "0 контекста/-) в непустой файл (%d строк) выглядит как "
                    "full-file replace без `-`-строк — отказ",
                    len(new_body), len(original_lines),
                )
                return False

            # --- Поиск правильной точки применения ---
            anchor = self._locate_hunk(original_lines, old_start, old_body)
            if anchor is None:
                logger.error("apply_patch: контекст ханка не найден в файле (старт=%d) — отказ",
                             old_start)
                return False
            if anchor != old_start - 1:
                logger.info("apply_patch: смещение ханка skorректировано %d → %d (fuzzy)",
                            old_start - 1, anchor)

            for old_idx, new_idx in context_link:
                new_body[new_idx] = original_lines[anchor + old_idx]

            original_lines[anchor : anchor + real_old_count] = new_body
        try:
            file_path.write_text(''.join(original_lines), encoding="utf-8")
            logger.debug("Файл %s успешно записан", file_path)
            return True
        except Exception as e:
            logger.error("Ошибка записи файла %s: %s", file_path, e)
            return False

    # ------------------------------------------------------------------
    # O.15 — детектор слипшейся строки
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_line_merge(
        old_body: List[str],
        new_body: List[str],
    ) -> Optional[tuple]:
        """Возвращает `(pieces, merged_line)` если одна строка из `new_body`
        содержит подстроками ≥2 разные строки из `old_body`, и None иначе.

        Это сигнал, что LLM слил несколько исходных строк в одну (удалил
        `\\n` между ними). Кейс из 2026-06-02: `database.py:5` получил
        `# комментарий    conn = sqlite3.connect(...)` без перевода строки;
        `ast.parse` пропустил (комментарий валиден), DecideStage ACCEPT-нул.

        Эвристика консервативна:
          * сравниваем по содержимому БЕЗ префикса `+`/`-`/' ' (то, что
            кладёт `strip_body_prefix`) и без CR/LF;
          * учитываем только строки длиннее 4 символов — иначе случайные
            совпадения типа `):`/`}` ловят false positives;
          * требуем ≥2 РАЗНЫХ pieces в одной строке `new_body` — одиночное
            совпадение (типичный refactor) не считаем слипанием;
          * скользящие префиксы исключаем: если piece-A полностью входит в
            piece-B, считаем как одну.
        """
        if not new_body or not old_body:
            return None

        pieces: List[str] = []
        for raw in old_body:
            t = raw.rstrip("\r\n").strip()
            if len(t) >= 4 and t not in pieces:
                pieces.append(t)
        # фильтр «префиксов»: оставляем только строки, не входящие подстрокой
        # в более длинные. Без этого `import os` ⊂ `import os, sys` даёт пару.
        pieces.sort(key=len, reverse=True)
        independent: List[str] = []
        for p in pieces:
            if not any(p in q for q in independent):
                independent.append(p)
        if len(independent) < 2:
            return None

        for raw_new in new_body:
            nl = raw_new.rstrip("\r\n")
            if len(nl) < 8:
                continue
            contained: List[str] = []
            for p in independent:
                if p in nl and p not in contained:
                    contained.append(p)
                    if len(contained) >= 2:
                        return (contained, nl)
        return None

    # ------------------------------------------------------------------
    # O.15-recovery: разбиваем слипшуюся строку обратно
    # ------------------------------------------------------------------
    @staticmethod
    def _try_recover_line_merge(
        old_body: List[str],
        new_body: List[str],
        pieces: List[str],
        merged_line: str,
    ) -> Optional[List[str]]:
        """Восстанавливает new_body когда одна строка содержит 2 слипших piece.

        Алгоритм (только для пары из 2 piece'ов, чтобы не угадывать порядок):
          1. Ищем piece_a и piece_b в merged_line.
          2. Определяем индент piece_b из оригинального old_body.
          3. Разбиваем merged_line на [line_a, line_b] с сохранением индентов.
          4. Проверяем что O.15 больше не появляется.

        Возвращает исправленный new_body или None (если разбить безопасно нельзя).
        """
        if len(pieces) != 2:
            return None

        ml = merged_line.rstrip("\r\n")

        # Находим реальный порядок piece'ов в merged_line
        pos0 = ml.find(pieces[0])
        pos1 = ml.find(pieces[1])
        if pos0 == -1 or pos1 == -1:
            return None

        # first/second — порядок появления в строке
        if pos0 <= pos1:
            first, second = pieces[0], pieces[1]
            end_first = pos0 + len(first)
            pos_second = ml.find(second, end_first)
        else:
            first, second = pieces[1], pieces[0]
            end_first = pos1 + len(first)
            pos_second = ml.find(second, end_first)

        if pos_second == -1:
            return None

        # Индентация second — берём из оригинального raw old_body
        raw_second_indent = ""
        for raw in old_body:
            stripped = raw.rstrip("\r\n").strip()
            if stripped == second:
                raw_content = raw.rstrip("\r\n")
                raw_second_indent = raw_content[: len(raw_content) - len(raw_content.lstrip())]
                break

        line_first = ml[:end_first] + "\n"
        line_second = raw_second_indent + second + "\n"

        # Строим новый new_body с заменой merged на два
        recovered: List[str] = []
        replaced = False
        for rn in new_body:
            if not replaced and rn.rstrip("\r\n") == ml:
                recovered.append(line_first)
                recovered.append(line_second)
                replaced = True
            else:
                recovered.append(rn)

        if not replaced:
            return None

        # Проверка: O.15 не должен срабатывать на recovered
        if PatchEngine._detect_line_merge(old_body, recovered) is not None:
            return None

        return recovered

    # ------------------------------------------------------------------
    # Локатор позиции ханка (fuzzy)
    # ------------------------------------------------------------------
    @staticmethod
    def _locate_hunk(file_lines: List[str], hunk_old_start_1based: int,
                     old_body: List[str], *, max_drift: int = 5,
                     fuzzy_threshold: float = 0.85,
                     fuzzy_window: int = 200) -> Optional[int]:
        """Возвращает 0-based индекс в `file_lines`, с которого нужно делать
        замену. Сначала проверяет ровно `old_start-1`, потом дрейфует ±max_drift,
        затем уникальную подстроку по всему файлу, и наконец — настоящий fuzzy-матч
        через difflib.SequenceMatcher (раньше был только заявлен в докстринге
        apply_patch, но не реализован — отсюда основная масса «anchor не найден»).

        Если `old_body` пустой (чистая вставка без контекста) — точка-вставки
        принимается как есть (`old_start - 1`), если в пределах файла.
        """
        ideal = max(0, hunk_old_start_1based - 1)
        n = len(file_lines)

        if not old_body:
            return ideal if 0 <= ideal <= n else None

        def _normalize(s: str) -> str:
            # Сравнение без учёта обрамляющих пробелов и финальных переводов
            # строки. strip() (не только rstrip) — потому что чаще всего LLM
            # промахивается именно с отступом контекстных строк (другой уровень
            # вложенности «в голове» модели), а не с содержимым строки.
            return s.rstrip("\r\n").strip()

        old_norm = [_normalize(x) for x in old_body]
        block_len = len(old_norm)

        def _matches_at(idx: int) -> bool:
            end = idx + block_len
            if idx < 0 or end > n:
                return False
            file_slice = [_normalize(file_lines[i]) for i in range(idx, end)]
            return file_slice == old_norm

        # 1) Ровно по заявленному смещению.
        if _matches_at(ideal):
            return ideal

        # 2) Дрейф ±max_drift (сначала ближайшие).
        for delta in range(1, max_drift + 1):
            for cand in (ideal - delta, ideal + delta):
                if _matches_at(cand):
                    return cand

        # 3) Глобальный fallback: точная уникальная подстрока old_body в файле.
        first = old_norm[0]
        candidates: List[int] = [i for i in range(n) if _normalize(file_lines[i]) == first]
        full_matches: List[int] = [i for i in candidates if _matches_at(i)]
        if len(full_matches) == 1:
            return full_matches[0]

        # 4) Настоящий fuzzy-fallback (difflib.SequenceMatcher), ограниченный
        # окном вокруг заявленного смещения — чтобы не сканировать весь файл
        # построчным SequenceMatcher на каждый ханк (дорого на больших файлах).
        if block_len > 0 and block_len <= 200:
            lo = max(0, ideal - fuzzy_window)
            hi = min(n - block_len, ideal + fuzzy_window)
            if hi >= lo:
                old_joined = "\n".join(old_norm)
                scored: List[Tuple[float, int]] = []
                for idx in range(lo, hi + 1):
                    window = "\n".join(_normalize(file_lines[i]) for i in range(idx, idx + block_len))
                    ratio = difflib.SequenceMatcher(None, old_joined, window).ratio()
                    scored.append((ratio, idx))
                scored.sort(key=lambda t: t[0], reverse=True)
                if scored:
                    best_ratio, best_idx = scored[0]
                    second_ratio = scored[1][0] if len(scored) > 1 else 0.0
                    if best_ratio >= fuzzy_threshold and (best_ratio - second_ratio) >= 0.05:
                        logger.info(
                            "apply_patch: fuzzy-match анкера (ratio=%.2f, заявлен старт=%d -> %d)",
                            best_ratio, hunk_old_start_1based, best_idx,
                        )
                        return best_idx

        # Совпадений нет или они неоднозначны — отказ.
        return None

    def revert_last_patch(self):
        logger.debug("revert_last_patch вызван (заглушка)")
        pass

    def _parse_patch(self, patch: str, target_file: Optional[str] = None) -> List[tuple]:
        logger.debug("_parse_patch: разбор патча длиной %d символов", len(patch))
        hunks = []
        lines = patch.splitlines(keepends=True)
        i = 0
        current_file = None
        while i < len(lines):
            line = lines[i]
            if is_file_header(line):
                if line[:3] == '+++':
                    current_file = header_path(line)
                i += 1
                continue
            if line.startswith('@@'):
                match = HUNK_HEADER_RE.match(line.strip())
                if match:
                    old_start = int(match.group(1))
                    old_count = int(match.group(2)) if match.group(2) else 1
                    new_start = int(match.group(3))
                    new_count = int(match.group(4)) if match.group(4) else 1
                    hunk_file = current_file
                    logger.debug("Найден ханк: old_start=%d, old_count=%d, file=%s", old_start, old_count, hunk_file)
                    i += 1
                    new_lines = []
                    while i < len(lines) and (lines[i].startswith((' ', '+', '-')) or lines[i].strip() == ''):
                        if is_file_header(lines[i]):
                            if lines[i][:3] == '+++':
                                current_file = header_path(lines[i])
                            i += 1
                            continue
                        new_lines.append(lines[i])
                        i += 1
                    if _path_matches(hunk_file, target_file):
                        hunks.append((old_start, old_count, new_lines))
                    else:
                        logger.debug("  ханк секции %s пропущен (цель %s)", hunk_file, target_file)
                else:
                    i += 1
            else:
                i += 1
        logger.debug("_parse_patch: всего ханков=%d (target=%s)", len(hunks), target_file)
        return hunks

    @staticmethod
    def files_in_patch(patch: str) -> List[str]:
        files: List[str] = []
        for line in (patch or "").splitlines():
            if line[:3] == '+++':
                p = header_path(line)
                if p and p != "/dev/null" and p not in files:
                    files.append(p)
        return files

    @staticmethod
    def _parse_patch_static(patch: str) -> List[tuple]:
        hunks = []
        lines = patch.splitlines(keepends=True)
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('@@'):
                match = HUNK_HEADER_RE.match(line.strip())
                if match:
                    old_start = int(match.group(1))
                    old_count = int(match.group(2)) if match.group(2) else 1
                    i += 1
                    new_lines = []
                    while i < len(lines) and (lines[i].startswith((' ', '+', '-')) or lines[i].strip() == ''):
                        if is_file_header(lines[i]):
                            i += 1
                            continue
                        new_lines.append(lines[i])
                        i += 1
                    hunks.append((old_start, old_count, new_lines))
                else:
                    i += 1
            else:
                i += 1
        return hunks

    @staticmethod
    def apply_patches_and_diff(original_file_content: str, patches: List[str], file_name: str = "unknown") -> Optional[str]:
        if not patches:
            return None
        try:
            all_hunks = []
            for patch in patches:
                hunks = PatchEngine._parse_patch_static(patch)
                if not hunks:
                    logger.warning("apply_patches_and_diff: патч не содержит ханков")
                    return None
                all_hunks.extend(hunks)
            all_hunks.sort(key=lambda h: h[0], reverse=True)
            result_lines = original_file_content.splitlines(keepends=True)
            for old_start, _old_count, new_lines in all_hunks:
                # Раньше здесь был слепой сплайс по `old_start` из @@-хедера —
                # отдельный, третий путь применения патча, не использующий
                # anchor/fuzzy-логику _locate_hunk (см. apply_patch). Если LLM
                # на сегменте промахивалась с номером строки (видя только
                # кусок файла), правка попадала не туда и весь segmented-merge
                # проваливался ниже по pipeline. Приводим к единому движку.
                old_body: List[str] = []
                new_body: List[str] = []
                context_link: List[Tuple[int, int]] = []
                for line in new_lines:
                    kind = body_kind(line)
                    if kind is None:
                        continue
                    content = strip_body_prefix(line)
                    if kind == ' ':
                        context_link.append((len(old_body), len(new_body)))
                        old_body.append(content)
                        new_body.append(content)
                    elif kind == '-':
                        old_body.append(content)
                    elif kind == '+':
                        if is_llm_placeholder(content):
                            continue
                        new_body.append(content)

                anchor = PatchEngine._locate_hunk(result_lines, old_start, old_body)
                if anchor is None:
                    logger.warning(
                        "apply_patches_and_diff: anchor не найден (заявлен старт=%d) — отказ",
                        old_start,
                    )
                    return None
                for old_idx, new_idx in context_link:
                    new_body[new_idx] = result_lines[anchor + old_idx]
                result_lines[anchor : anchor + len(old_body)] = new_body
            final_content = ''.join(result_lines)
            diff = difflib.unified_diff(
                original_file_content.splitlines(True),
                final_content.splitlines(True),
                fromfile=f"a/{file_name}",
                tofile=f"b/{file_name}",
            )
            unified = ''.join(diff)
            if not unified or not re.search(r'^[-+]', unified, re.MULTILINE):
                logger.debug("apply_patches_and_diff: итоговый патч пуст")
                return None
            return unified
        except Exception as e:
            logger.error("apply_patches_and_diff: ошибка %s", e)
            return None

    @staticmethod
    def is_patch_anchorable(patch: str, file_content: str) -> bool:
        """Проверяет (без применения), что каждый ханк патча находит анкер в
        file_content через _locate_hunk. Используется, чтобы отбраковывать
        релевантные-но-непривязываемые кандидаты ДО того, как retry-бюджет
        будет потрачен впустую на единственную финальную попытку merge."""
        hunks = PatchEngine._parse_patch_static(patch)
        if not hunks:
            return False
        file_lines = file_content.splitlines(keepends=True)
        for old_start, _old_count, new_lines in hunks:
            old_body: List[str] = []
            for line in new_lines:
                kind = body_kind(line)
                if kind in (' ', '-'):
                    old_body.append(strip_body_prefix(line))
            if PatchEngine._locate_hunk(file_lines, old_start, old_body) is None:
                return False
        return True

    @staticmethod
    def merge_patches(patches: List[str]) -> Optional[str]:
        logger.debug("merge_patches: входных патчей=%d", len(patches))
        old_paths = set()
        new_paths = set()
        for patch in patches:
            for line in patch.splitlines():
                if line.startswith('--- '):
                    old_paths.add(line[4:].split('\t')[0].strip())
                elif line.startswith('+++ '):
                    new_paths.add(line[4:].split('\t')[0].strip())
        if len(old_paths) > 1 or len(new_paths) > 1:
            logger.warning("Патчи относятся к разным файлам — объединение невозможно")
            return None
        header = []
        if patches:
            first_lines = patches[0].splitlines()
            if len(first_lines) >= 2 and first_lines[0].startswith('---') and first_lines[1].startswith('+++'):
                header = [first_lines[0] + '\n', first_lines[1] + '\n']
            else:
                header = ["--- a/unknown\n", "+++ b/unknown\n"]
        all_hunks = []
        for patch in patches:
            lines = patch.splitlines(True)
            i = 0
            while i < len(lines):
                line = lines[i]
                if line.startswith('@@'):
                    match = HUNK_HEADER_RE.match(line.strip())
                    if match:
                        old_start = int(match.group(1))
                        old_count = int(match.group(2)) if match.group(2) else 1
                        body_lines = [line]
                        i += 1
                        while i < len(lines) and (lines[i].startswith((' ', '+', '-')) or lines[i].strip() == ''):
                            if is_file_header(lines[i]):
                                i += 1
                                continue
                            body_lines.append(lines[i])
                            i += 1
                        all_hunks.append(((old_start, old_count), body_lines))
                    else:
                        i += 1
                else:
                    i += 1
        if not all_hunks:
            logger.debug("merge_patches: не найдено ни одного ханка")
            return None
        all_hunks.sort(key=lambda x: x[0][0])
        logger.debug("merge_patches: всего ханков после сортировки=%d", len(all_hunks))
        merged_ranges = []
        final_hunks = []
        for (start, count), body in all_hunks:
            new_range = (start, start + count - 1)
            conflict = False
            for existing_start, existing_end in merged_ranges:
                if new_range[0] <= existing_end and existing_start <= new_range[1]:
                    conflict = True
                    logger.info("Конфликт ханков: существующий %d-%d, новый %d-%d. Пропускаем новый.", existing_start, existing_end, new_range[0], new_range[1])
                    break
            if not conflict:
                final_hunks.append(((start, count), body))
                merged_ranges.append(new_range)
        if not final_hunks:
            logger.debug("merge_patches: все ханки конфликтовали, результат=None")
            return None
        result = ''.join(header)
        for _, body in final_hunks:
            result += ''.join(body)
        logger.debug("merge_patches: итоговый патч длиной %d символов", len(result))
        return result

    @staticmethod
    def is_empty_patch(patch: str) -> bool:
        changes = [line for line in patch.splitlines()
                   if (line.startswith('+') or line.startswith('-')) and not line.startswith(('+++', '---'))]
        logger.debug("is_empty_patch: строк с изменениями = %d", len(changes))
        if not changes:
            logger.debug("is_empty_patch: патч пустой (нет изменённых строк)")
            return True
        return False

    @staticmethod
    def is_patch_relevant(patch: str, error: Dict[str, Any], file_content: str, segments: List[Dict[str, Any]]) -> bool:
        error_line = error.get("line", 0)
        error_code = error.get("code", "")
        if not error_line or error_code in ("E0601", "E0425", "E0432", "E0433"):
            logger.info("is_patch_relevant: строка ошибки неинформативна (error_line=%s, code=%s) – пропускаем проверку", error_line, error_code)
            return True
        structural = error.get("error_type", "") in (
            "type_mismatch", "ownership_error", "borrow_error", "mutability_error", "type_mismatch_argument"
        ) or error_code in ("E0308", "E0382", "E0502", "E0599")
        logger.debug("is_patch_relevant: error_line=%d, code=%s, structural=%s", error_line, error_code, structural)
        for match in re.finditer(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', patch):
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) else 1
            old_end = old_start + old_count - 1
            if old_start <= error_line <= old_end:
                logger.info("is_patch_relevant: прямое попадание в ханк %d-%d", old_start, old_end)
                return True
            if error_code == "E0765":
                if old_start <= error_line and old_end >= error_line - 30:
                    logger.info("is_patch_relevant: допустимое смещение для E0765 (ханк %d-%d закрывает строку не дальше %d)", old_start, old_end, error_line)
                    return True
            if structural and segments:
                error_seg = None
                for seg in segments:
                    if seg["start_line"] <= error_line <= seg["end_line"]:
                        error_seg = seg
                        break
                for seg in segments:
                    if seg["start_line"] <= old_start and old_end <= seg["end_line"]:
                        if error_seg is not None and seg == error_seg:
                            logger.info("is_patch_relevant: ханк %d-%d находится в том же сегменте, что и ошибка", old_start, old_end)
                            return True
                        break
        logger.info("is_patch_relevant: ханки не перекрывают ошибку (error_line=%s)", error_line)
        return False
