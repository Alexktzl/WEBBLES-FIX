"""
Структурный формат правок (EditSet) — основа «гибрида diff+JSON».

Зачем
-----
LLM, отдающий unified diff текстом, ломается на десятке мелочей:
`+++ b/file` попадает внутрь хунка и пишется в код как `++ b/file`,
плейсхолдер `// existing code` оседает в исходнике, line numbers
в `@@`-заголовке плывут, контекст «не совпал», и т.д. Мы это всё
наблюдали в логах и латали уже на трёх уровнях (apply_patch,
patch_normalizer, patch_validator).

Гибрид: LLM возвращает JSON-объект EditSet, мы **сами** строим
unified-diff. Anchor.match верифицируется ДО сборки diff'а —
если LLM выдумал расположение, мы ловим это сразу и можем
сделать retry/fallback вместо мусора в файле.

JSON-форма ответа от LLM
------------------------
{
  "intent": "rename Player.player_name access to Player.name",
  "confidence": 0.92,
  "edits": [
    {
      "file": "src/player.rs",
      "anchor": {"line": 9, "match": "player_name: name,"},
      "kind": "replace",
      "new": "            name,",
      "rationale": "field is called 'name' in struct"
    }
  ],
  "risks": ["no other usages of player_name in project"]
}

`kind` ∈ {"replace", "insert_before", "insert_after", "delete"}.
"""

from __future__ import annotations

import difflib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Окно поиска anchor.match вокруг заявленного anchor.line.
# Увеличено до 15: предыдущий патч в том же файле мог сдвинуть строки
# на 3-12 позиций. Если anchor.match сам изменён предыдущим патчем —
# _find_anchor вернёт None и устаревший патч будет корректно пропущен.
ANCHOR_SEARCH_WINDOW = 15

# Допустимые виды правок.
EDIT_KINDS = ("replace", "insert_before", "insert_after", "delete", "replace_file")


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

@dataclass
class Anchor:
    """Точка привязки правки к файлу.

    `line` — 1-based номер строки, как считает LLM.
    `match` — текст, который ДОЛЖЕН встретиться в этой строке (или в окне
    ±ANCHOR_SEARCH_WINDOW вокруг). Если не совпало — правка отбрасывается.
    Хранится как substring-проверка (LLM редко попадает в полное равенство).
    """

    line: int
    match: str

    def to_dict(self) -> Dict[str, Any]:
        return {"line": int(self.line), "match": self.match}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Anchor":
        return cls(
            line=int(data.get("line", 0)),
            match=str(data.get("match", "")),
        )


@dataclass
class Edit:
    """Одна правка одного файла."""

    file: str
    anchor: Anchor
    kind: str
    new: str = ""
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "anchor": self.anchor.to_dict(),
            "kind": self.kind,
            "new": self.new,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Edit":
        anchor_data = data.get("anchor") or {}
        if not isinstance(anchor_data, dict):
            anchor_data = {}
        return cls(
            file=str(data.get("file", "")),
            anchor=Anchor.from_dict(anchor_data),
            kind=str(data.get("kind", "replace")),
            new=str(data.get("new", "")),
            rationale=str(data.get("rationale", "")),
        )


@dataclass
class EditSet:
    """Набор правок как единая транзакция."""

    intent: str = ""
    edits: List[Edit] = field(default_factory=list)
    confidence: float = 0.5
    risks: List[str] = field(default_factory=list)

    # -----------------------------------------------------------------------
    # Сериализация
    # -----------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": float(self.confidence),
            "edits": [e.to_dict() for e in self.edits],
            "risks": list(self.risks),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], diag: Optional[Dict[str, Any]] = None,
    ) -> Optional["EditSet"]:
        """`diag`, если передан, заполняется при возврате None:
        {"reason": "parser_fail", "detail": "..."} — JSON был валиден, но
        не прошёл схему EditSet (см. empty_response диагностика, 2026-06-22)."""
        if not isinstance(data, dict):
            if diag is not None:
                diag["reason"] = "parser_fail"
                diag["detail"] = f"top-level JSON не объект: {type(data).__name__}"
            return None
        edits_raw = data.get("edits", [])
        if not isinstance(edits_raw, list):
            if diag is not None:
                diag["reason"] = "parser_fail"
                diag["detail"] = f"'edits' не список: {type(edits_raw).__name__}"
            return None
        edits: List[Edit] = []
        skipped = 0
        for raw in edits_raw:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            edit = Edit.from_dict(raw)
            if not edit.file or not edit.anchor.match or edit.kind not in EDIT_KINDS:
                logger.debug("EditSet.from_dict: пропущена невалидная правка %r", raw)
                skipped += 1
                continue
            edits.append(edit)
        if not edits:
            if diag is not None:
                diag["reason"] = "parser_fail"
                diag["detail"] = f"0 валидных правок из {len(edits_raw)} (пропущено {skipped})"
            return None
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return cls(
            intent=str(data.get("intent", "")),
            edits=edits,
            confidence=max(0.0, min(1.0, confidence)),
            risks=[str(r) for r in (data.get("risks") or []) if r],
        )

    @classmethod
    def from_json(
        cls, raw: str, diag: Optional[Dict[str, Any]] = None,
    ) -> Optional["EditSet"]:
        """Парсит JSON-строку (с защитой от ```json fences).

        `diag`, если передан, заполняется при возврате None — различает
        "bad_format" (текст не парсится как JSON вовсе) от "parser_fail"
        (валидный JSON, но не EditSet-схема) — см. empty_response
        диагностика, 2026-06-22 (расследование REJECT-аномалии empty_response
        для локальной LLM)."""
        if not raw:
            if diag is not None:
                diag["reason"] = "empty"
            return None
        text = raw.strip()
        if text.startswith("```"):
            # LLM любит обернуть в ```json … ```
            lines = text.splitlines()
            if lines and lines[0].lstrip("`").lower().startswith("json"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning("EditSet.from_json: не-JSON ответ от LLM: %s", e)
            if diag is not None:
                diag["reason"] = "bad_format"
                diag["detail"] = str(e)
            return None
        return cls.from_dict(data, diag=diag)

    # -----------------------------------------------------------------------
    # Сборка unified diff с верификацией anchor'ов
    # -----------------------------------------------------------------------

    def to_unified_diff(
        self,
        file_contents: Dict[str, str],
        diag: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Строит unified diff из правок.

        `file_contents` — словарь {relative_path: original_text}.

        Для каждого `Edit`:
          1. находит anchor (Anchor.line ± ANCHOR_SEARCH_WINDOW, по substring);
          2. применяет правку в локальной копии текста;
          3. в итоге выдаёт `difflib.unified_diff` поверх ОРИГИНАЛА.

        Возвращает None, если:
          - какая-то правка не сматчилась с anchor'ом,
          - в `file_contents` нет нужного файла,
          - kind не из EDIT_KINDS.

        `diag`, если передан, заполняется при возврате None — различает
        "anchor_empty" (anchor.match пуст после strip — структурный баг,
        см. W291-расследование 2026-06-22), "anchor_mismatch" (match непуст,
        но не найден в файле — вероятная галлюцинация LLM по содержимому) и
        "diff_fail" (anchor сошёлся, но итоговый текст не изменился, или нет
        контента нужного файла)."""
        if not self.edits:
            if diag is not None:
                diag["reason"] = "parser_fail"
                diag["detail"] = "EditSet.edits пуст"
            return None

        # Группируем правки по файлам.
        by_file: Dict[str, List[Edit]] = {}
        for edit in self.edits:
            by_file.setdefault(edit.file, []).append(edit)

        # На Windows ключи file_contents могут быть с '\', а edit.file от LLM —
        # с '/'. Нормализуем разделители, чтобы поиск не промахивался.
        norm_contents = {str(k).replace("\\", "/"): v for k, v in (file_contents or {}).items()}
        # Индекс по basename для fallback: LLM иногда возвращает только имя файла
        # без директории, а fc содержит полный относительный путь (BUG-7).
        basename_contents: Dict[str, str] = {}
        for k, v in norm_contents.items():
            bn = k.rsplit("/", 1)[-1]
            if bn not in basename_contents:  # первое совпадение побеждает
                basename_contents[bn] = v
        # 2026-07-08 (loguru, 108 CRITICAL_SYNTAX-откатов): Windows-путь в
        # промпте ("loguru\_recattrs.py") LLM читала как markdown-escape `\_`
        # и возвращала "loguru_recattrs.py" — ни norm-, ни basename-fallback
        # не матчились (подчёркивание съело разделитель). Корень закрыт
        # (в промпт идёт forward-slash), это страховка: индекс по
        # «md-mangled» варианту известных путей — разделитель перед '_'
        # схлопнут ("loguru/_recattrs.py" → "loguru_recattrs.py").
        # Маппинг mangled → КАНОНИЧЕСКИЙ путь (не контент): найденный через
        # mangled правки должны получить канонический путь и в diff-заголовки
        # (a/... b/...), иначе применение промахнётся на следующем шаге.
        mangled_to_canon: Dict[str, str] = {}
        for k in norm_contents:
            if "/_" in k:
                mk = k.replace("/_", "_")
                if mk not in mangled_to_canon and mk not in norm_contents:
                    mangled_to_canon[mk] = k

        diff_parts: List[str] = []
        for file_path, edits in by_file.items():
            original = file_contents.get(file_path)
            if original is None:
                original = norm_contents.get(str(file_path).replace("\\", "/"))
            if original is None:
                # Fallback: попробуем по basename (LLM мог вернуть путь без директории).
                original = basename_contents.get(str(file_path).replace("\\", "/").rsplit("/", 1)[-1])
            if original is None:
                # Fallback 2026-07-08: md-mangled путь (см. mangled_to_canon
                # выше) — контент берём по каноническому пути и ЗАМЕНЯЕМ
                # file_path на канонический, чтобы diff-заголовки были верными.
                _canon = mangled_to_canon.get(str(file_path).replace("\\", "/"))
                if _canon is not None:
                    original = norm_contents.get(_canon)
                    if original is not None:
                        logger.info(
                            "EditSet.to_unified_diff: md-mangled путь %s → %s "
                            "(восстановлен разделитель перед '_')",
                            file_path, _canon,
                        )
                        file_path = _canon
            if original is None:
                # Нет контента для этого файла — пропускаем ТОЛЬКО его, не топим
                # весь EditSet (в кросс-файловом наборе остальные файлы валидны).
                logger.warning("EditSet.to_unified_diff: нет контента для %s — пропускаем файл", file_path)
                if diag is not None:
                    diag.setdefault("reason", "diff_fail")
                    diag.setdefault("detail", f"missing_content:{file_path}")
                continue

            patched_lines = original.splitlines(keepends=True)
            # Чтобы более поздние правки не сдвигали ранние — применяем
            # правки в порядке убывания anchor.line.
            edits_sorted = sorted(edits, key=lambda e: e.anchor.line, reverse=True)

            # Пофайловая устойчивость: если хоть одна правка файла не легла
            # (битый anchor — частый случай галлюцинации LLM по Cargo.toml),
            # пропускаем ВЕСЬ этот файл (его атомарность), но НЕ выбрасываем
            # уже собранные правки других файлов. Инвариант B.2 («не выдумываем
            # diff на битый anchor») сохраняется на уровне файла; одно-файловый
            # EditSet с битым anchor по-прежнему даёт None (diff_parts пуст).
            file_ok = True
            for edit in edits_sorted:
                if edit.kind == "replace_file":
                    patched_lines[:] = _split_to_lines(edit.new)
                    continue
                resolved_line = _find_anchor(patched_lines, edit.anchor)
                if resolved_line is None:
                    logger.warning(
                        "EditSet.to_unified_diff: anchor не найден для %s:%d match=%r — пропускаем файл",
                        edit.file, edit.anchor.line, edit.anchor.match[:60],
                    )
                    if diag is not None:
                        diag["reason"] = "anchor_empty" if not edit.anchor.match.strip() else "anchor_mismatch"
                        diag["detail"] = f"{edit.file}:{edit.anchor.line} match={edit.anchor.match[:80]!r}"
                    file_ok = False
                    break
                if not _apply_edit(patched_lines, resolved_line, edit):
                    if diag is not None:
                        diag.setdefault("reason", "diff_fail")
                        diag.setdefault("detail", f"_apply_edit failed: {edit.file}:{edit.anchor.line}")
                    file_ok = False
                    break
            if not file_ok:
                continue

            new_text = "".join(patched_lines)
            if new_text == original:
                logger.debug("EditSet.to_unified_diff: правки нулевые для %s", file_path)
                if diag is not None:
                    diag.setdefault("reason", "diff_fail")
                    diag.setdefault("detail", f"no_op:{file_path}")
                continue

            diff_iter = difflib.unified_diff(
                original.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                n=3,
            )
            diff_parts.append("".join(diff_iter))

        if not diff_parts:
            if diag is not None:
                diag.setdefault("reason", "diff_fail")
            return None
        return "".join(diff_parts)

    # -----------------------------------------------------------------------
    # Focused-fix режим (2026-06-22) — anti-hallucination guard
    # -----------------------------------------------------------------------

    def filter_to_target_window(
        self,
        target_file: str,
        target_line: int,
        error_code: str = "",
        window_radius: int = 3,
    ) -> Tuple["EditSet", List[Dict[str, Any]]]:
        """Оставляет только правки в пределах `target_line ± window_radius`
        для `target_file` — остальные ОТБРАСЫВАЕТ с диагностикой.

        Найдено живьём (control series 2026-06-22, geopython/pygeofilter):
        запрос на ОДНУ конкретную ошибку E501 на конкретной строке — модель
        возвращает EditSet с 6-9 правками, "доисправляя" ВСЕ похожие длинные
        строки в файле, и придумывает несуществующий "# noqa: E501" в
        anchor.match для каждой такой лишней правки. Из-за файловой
        атомарности to_unified_diff (одна несовпавшая правка топит ВЕСЬ
        файл) даже ВАЛИДНЫЙ фикс целевой строки терялся.

        Для error_code=="E501" дополнительно отбрасывает любые правки, где
        anchor.match содержит "noqa" НЕ на target_line — модель не должна
        массово добавлять noqa-suppression к соседним похожим строкам,
        которые не являются целевой ошибкой.

        Правки для ДРУГИХ файлов (не target_file) не трогает — кросс-
        файловые правки (манифесты и т.п.) вне области этой защиты."""
        norm_target = str(target_file).replace("\\", "/")
        target_basename = norm_target.rsplit("/", 1)[-1]
        # 2026-07-08 (loguru): LLM теряла '\' перед '_' в Windows-пути
        # ("loguru\_recattrs.py" → "loguru_recattrs.py") — целевой файл не
        # распознавался и валидные правки отбрасывались. Допускаем md-mangled
        # вариант цели (разделитель перед '_' схлопнут).
        mangled_target = norm_target.replace("/_", "_")
        kept: List[Edit] = []
        dropped: List[Dict[str, Any]] = []
        for edit in self.edits:
            norm_edit_file = str(edit.file).replace("\\", "/")
            is_target_file = (
                norm_edit_file == norm_target
                or norm_edit_file.rsplit("/", 1)[-1] == target_basename
                or norm_edit_file == mangled_target
            )
            if not is_target_file:
                kept.append(edit)
                continue
            is_noqa_hallucination = (
                error_code == "E501"
                and "noqa" in edit.anchor.match.lower()
                and edit.anchor.line != target_line
            )
            if is_noqa_hallucination:
                dropped.append({
                    "file": edit.file, "line": edit.anchor.line,
                    "match": edit.anchor.match[:80], "reason": "noqa_outside_target",
                })
                continue
            in_window = abs(edit.anchor.line - target_line) <= window_radius
            if not in_window:
                dropped.append({
                    "file": edit.file, "line": edit.anchor.line,
                    "match": edit.anchor.match[:80], "reason": "outside_target_window",
                })
                continue
            kept.append(edit)
        new_set = EditSet(
            intent=self.intent, edits=kept,
            confidence=self.confidence, risks=list(self.risks),
        )
        return new_set, dropped

    def apply(
        self,
        file_contents: Dict[str, str],
    ) -> Optional[Dict[str, str]]:
        """Применяет правки и возвращает `{file: new_text}`.

        В отличие от `to_unified_diff`, отдаёт уже изменённый текст —
        удобно для rule-based fixer (Stage E) и тестов, которые сверяют
        результат с эталоном `after/`. Возвращает None, если хоть один
        anchor не сошёлся или нет нужного файла (та же строгость, что у
        to_unified_diff)."""
        if not self.edits:
            return None
        by_file: Dict[str, List[Edit]] = {}
        for edit in self.edits:
            by_file.setdefault(edit.file, []).append(edit)
        norm_contents = {str(k).replace("\\", "/"): v for k, v in (file_contents or {}).items()}
        out: Dict[str, str] = {}
        for file_path, edits in by_file.items():
            original = file_contents.get(file_path)
            if original is None:
                original = norm_contents.get(str(file_path).replace("\\", "/"))
            if original is None:
                # Пропускаем только этот файл (см. to_unified_diff) — не топим
                # остальные валидные правки набора.
                logger.warning("EditSet.apply: нет контента для %s — пропускаем файл", file_path)
                continue
            patched_lines = original.splitlines(keepends=True)
            file_ok = True
            for edit in sorted(edits, key=lambda e: e.anchor.line, reverse=True):
                if edit.kind == "replace_file":
                    patched_lines[:] = _split_to_lines(edit.new)
                    continue
                resolved_line = _find_anchor(patched_lines, edit.anchor)
                if resolved_line is None:
                    logger.warning(
                        "EditSet.apply: anchor не найден для %s:%d match=%r — пропускаем файл",
                        edit.file, edit.anchor.line, edit.anchor.match[:60],
                    )
                    file_ok = False
                    break
                if not _apply_edit(patched_lines, resolved_line, edit):
                    file_ok = False
                    break
            if not file_ok:
                continue
            out[file_path] = "".join(patched_lines)
        return out or None


# ---------------------------------------------------------------------------
# Внутренние помощники
# ---------------------------------------------------------------------------

def _find_anchor(lines: List[str], anchor: Anchor) -> Optional[int]:
    """Находит 0-based индекс строки, на которой anchor.match реально встречается.

    Стратегия:
      * сначала ищем в окне ±ANCHOR_SEARCH_WINDOW вокруг заявленного anchor.line;
      * среди кандидатов берём ближайший к anchor.line;
      * если в окне нет — пробуем по всему файлу (один уникальный match);
      * иначе возвращаем None.
    """
    needle = anchor.match.strip()
    if not needle:
        return None
    target = max(0, anchor.line - 1)  # 0-based

    # Локальное окно
    lo = max(0, target - ANCHOR_SEARCH_WINDOW)
    hi = min(len(lines), target + ANCHOR_SEARCH_WINDOW + 1)
    local_matches = [i for i in range(lo, hi) if needle in lines[i]]
    if local_matches:
        # Ближайший к target
        return min(local_matches, key=lambda i: abs(i - target))

    # Global fallback — допускается ТОЛЬКО единственный однозначный match.
    global_matches = [i for i in range(len(lines)) if needle in lines[i]]
    if len(global_matches) == 1:
        logger.debug(
            "anchor: line %d не подошла, но точно нашли по match в строке %d",
            anchor.line, global_matches[0] + 1,
        )
        return global_matches[0]
    return None


def _apply_edit(lines: List[str], index: int, edit: Edit) -> bool:
    """Применяет одну правку in-place. Возвращает True при успехе."""
    if edit.kind == "replace":
        new_block = _split_to_lines(edit.new)
        lines[index:index + 1] = new_block
        return True
    if edit.kind == "delete":
        del lines[index:index + 1]
        return True
    if edit.kind == "insert_before":
        new_block = _split_to_lines(edit.new)
        lines[index:index] = new_block
        return True
    if edit.kind == "insert_after":
        new_block = _split_to_lines(edit.new)
        lines[index + 1:index + 1] = new_block
        return True
    logger.warning("Edit.kind=%r не поддерживается", edit.kind)
    return False


def _split_to_lines(text: str) -> List[str]:
    """Разрезает строку на список line-with-keepends. Гарантирует, что
    каждая строка (кроме, возможно, последней без \n) заканчивается \n.
    """
    if text == "":
        return []
    out = text.splitlines(keepends=True)
    # Если последняя строка без trailing \n — добавляем, чтобы diff был чистым.
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] = out[-1] + "\n"
    return out


# ---------------------------------------------------------------------------
# JSON-схема — подсовываем LLM в system message
# ---------------------------------------------------------------------------

JSON_SCHEMA_HINT = """\
Return ONLY a JSON object with this shape — no markdown, no commentary:

{
  "intent": "<one-sentence summary of what this fix does>",
  "confidence": <float between 0.0 and 1.0>,
  "edits": [
    {
      "file": "<relative path>",
      "anchor": {"line": <int, 1-based>, "match": "<substring that uniquely identifies the line>"},
      "kind": "replace" | "insert_before" | "insert_after" | "delete",
      "new": "<replacement text; empty for delete; line should end with \\n>",
      "rationale": "<why this specific change>"
    }
  ],
  "risks": ["<short risk note>", "..."]
}

Rules:
- DO NOT include file headers ("+++", "---") or hunk markers ("@@") — we build the diff ourselves.
- DO NOT use placeholders like "// existing code" or "..." — write the actual replacement text.
- `anchor.match` must be an EXACT substring copied character-for-character from the existing line in the file. Do NOT paraphrase, abbreviate, or add extra characters. Copy the first 30-60 characters of the target line exactly as they appear.
- If you are not sure about the exact line number, still give your best guess — we search ±5 lines.
- Keep changes minimal. Touch only what the error requires.
- NEVER split a string literal across lines without proper closing/opening quotes. If a line inside a string is too long, shorten the string content or use a longer limit — do NOT insert a raw newline inside a quoted string.
- For E501 (line too long): if the long line is inside a string literal (e.g. a docstring body, a help= argument, a log message), DO NOT split the string content. Instead, either: (a) wrap at the Python expression level (break the outer function call, list, or parenthesized expression), or (b) add `  # noqa: E501` at the end of that line.
- For E501 in a module-level docstring line or a test docstring line where wrapping would change the text, add `  # noqa: E501` at the end of that specific line.
"""
