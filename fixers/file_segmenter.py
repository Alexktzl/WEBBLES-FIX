"""
Сегментатор файлов для эшелонированной стратегии Webbles Fix.
Разбивает большие файлы на логические блоки с точным сохранением глобального контекста.
Использует tree-sitter для Rust (опционально) или улучшенную эвристику.
Добавлен метод проверки принадлежности строк одному блоку.
Все ключевые операции покрыты DEBUG-логами.
"""

import logging
import re
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

# Попытка загрузить tree-sitter для точной сегментации
try:
    import tree_sitter_rust as ts_rust
    from tree_sitter import Language, Parser
    RUST_LANGUAGE = Language(ts_rust.language())
    TREE_SITTER_AVAILABLE = True
except ImportError:
    RUST_LANGUAGE = None
    TREE_SITTER_AVAILABLE = False


class FileSegmenter:
    """Отвечает за разбиение исходного кода на сегменты и сборку обратно."""

    def __init__(self, language: str = "rust"):
        self.language = language.lower()
        self._parser = None
        if self.language == "rust" and TREE_SITTER_AVAILABLE:
            # Исправление для tree-sitter >= 0.21: передаём язык в конструктор
            self._parser = Parser(RUST_LANGUAGE)
            logger.debug("FileSegmenter: tree-sitter активирован для Rust")
        else:
            logger.debug("FileSegmenter: язык=%s, tree-sitter=%s", self.language, "доступен" if TREE_SITTER_AVAILABLE else "недоступен")

    def extract_global_context(self, content: str) -> str:
        logger.debug("extract_global_context: язык=%s, длина файла=%d", self.language, len(content))
        if self.language == "rust" and self._parser:
            logger.debug("Используется tree-sitter для извлечения контекста")
            return self._extract_rust_context_tree_sitter(content)
        logger.debug("Используется эвристический метод извлечения контекста")
        return self._extract_context_heuristic(content)

    def segment_file(self, content: str, max_segment_lines: int = 400) -> List[Dict[str, Any]]:
        logger.debug("segment_file: язык=%s, длина файла=%d, макс. строк в сегменте=%d",
                     self.language, len(content), max_segment_lines)
        if self.language == "rust" and self._parser:
            logger.debug("Используется tree-sitter сегментация")
            return self._segment_rust_tree_sitter(content, max_segment_lines)
        logger.debug("Используется эвристическая сегментация")
        return self._segment_heuristic(content, max_segment_lines)

    # ------------------------------------------------------------------
    # Tree-sitter реализация для Rust
    # ------------------------------------------------------------------
    def _extract_rust_context_tree_sitter(self, content: str) -> str:
        logger.debug("_extract_rust_context_tree_sitter: парсинг файла")
        # tree-sitter оперирует БАЙТОВЫМИ смещениями, поэтому срезы делаем по байтам,
        # иначе при наличии не-ASCII символов границы узлов поедут.
        content_bytes = bytes(content, "utf-8")
        tree = self._parser.parse(content_bytes)
        root = tree.root_node
        context_lines = []
        for child in root.children:
            if child.type in ("function_item", "impl_item", "trait_item", "mod_item",
                              "struct_item", "enum_item", "union_item", "type_item",
                              "macro_definition", "use_declaration", "extern_crate_declaration",
                              "static_item", "const_item"):
                if child.type == "function_item":
                    body = child.child_by_field_name("body")
                    if body:
                        end_byte = body.start_byte
                        sig_bytes = content_bytes[child.start_byte:end_byte].decode("utf-8", errors="replace").rstrip()
                        context_lines.append(sig_bytes + "\n")
                    else:
                        context_lines.append(content_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace") + "\n")
                else:
                    context_lines.append(content_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace") + "\n")
        result = ''.join(context_lines).strip()
        logger.debug("_extract_rust_context_tree_sitter: контекст извлечён, длина=%d", len(result))
        return result

    def _segment_rust_tree_sitter(self, content: str, max_lines: int) -> List[Dict[str, Any]]:
        logger.debug("_segment_rust_tree_sitter: парсинг файла")
        tree = self._parser.parse(bytes(content, "utf-8"))
        root = tree.root_node
        segments = []
        current_segment_lines = []
        current_start_line = 1
        lines = content.splitlines(keepends=True)

        def flush_segment():
            nonlocal current_segment_lines, current_start_line
            if current_segment_lines:
                seg_text = ''.join(current_segment_lines)
                segments.append({
                    "start_line": current_start_line,
                    "end_line": current_start_line + seg_text.count('\n') - 1,
                    "text": seg_text
                })
                logger.debug("Сегмент сохранён: строки %d-%d", current_start_line,
                             current_start_line + seg_text.count('\n') - 1)
                current_segment_lines = []
                current_start_line = None

        for child in root.children:
            if child.type in ("function_item", "impl_item", "trait_item", "mod_item"):
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                element_lines = lines[start_line-1:end_line]
                if current_segment_lines and (len(current_segment_lines) + len(element_lines) > max_lines):
                    flush_segment()
                if not current_segment_lines:
                    current_start_line = start_line
                current_segment_lines.extend(element_lines)
            else:
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                element_lines = lines[start_line-1:end_line]
                if not current_segment_lines:
                    current_start_line = start_line
                current_segment_lines.extend(element_lines)
                if len(current_segment_lines) >= max_lines:
                    flush_segment()

        flush_segment()

        if not segments:
            logger.debug("Сегменты не созданы, добавлен весь файл как один сегмент")
            segments.append({
                "start_line": 1,
                "end_line": len(lines),
                "text": content
            })
        logger.debug("_segment_rust_tree_sitter: всего сегментов=%d", len(segments))
        return segments

    # ------------------------------------------------------------------
    # Эвристический fallback (без tree-sitter)
    # ------------------------------------------------------------------
    def _extract_context_heuristic(self, content: str) -> str:
        logger.debug("_extract_context_heuristic: извлечение контекста")
        lines = content.splitlines(keepends=True)
        context_lines = []
        brace_depth = 0
        for line in lines:
            stripped = line.strip()
            if re.match(r'^(pub\s+)?(fn|impl|trait|mod|struct|enum|union|type|use|extern crate|macro_rules!)\s', stripped):
                context_lines.append(line)
                brace_delta = stripped.count('{') - stripped.count('}')
                brace_depth += brace_delta
                if stripped.startswith(('fn ', 'pub fn ')) and brace_depth == 0:
                    pass
            elif brace_depth > 0:
                brace_depth += stripped.count('{') - stripped.count('}')
        result = ''.join(context_lines).strip()
        logger.debug("_extract_context_heuristic: контекст извлечён, длина=%d", len(result))
        return result

    def _segment_heuristic(self, content: str, max_lines: int) -> List[Dict[str, Any]]:
        logger.debug("_segment_heuristic: разбиение файла, макс. строк=%d", max_lines)
        lines = content.splitlines(keepends=True)
        segments = []
        current_segment = []
        current_start = 1
        brace_depth = 0
        in_fn_or_impl = False

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if re.match(r'^(pub\s+)?(fn|impl|trait|mod)\s', stripped) and brace_depth == 0:
                if current_segment:
                    segments.append({
                        "start_line": current_start,
                        "end_line": i - 1,
                        "text": ''.join(current_segment)
                    })
                    logger.debug("Сегмент сохранён: строки %d-%d", current_start, i - 1)
                current_segment = [line]
                current_start = i
                brace_depth += stripped.count('{') - stripped.count('}')
                in_fn_or_impl = True
                continue

            current_segment.append(line)
            brace_depth += stripped.count('{') - stripped.count('}')

            if in_fn_or_impl and brace_depth == 0:
                if len(current_segment) >= max_lines or i == len(lines):
                    segments.append({
                        "start_line": current_start,
                        "end_line": i,
                        "text": ''.join(current_segment)
                    })
                    logger.debug("Сегмент сохранён: строки %d-%d", current_start, i)
                    current_segment = []
                    current_start = i + 1
                    in_fn_or_impl = False
                else:
                    in_fn_or_impl = False

        if current_segment:
            segments.append({
                "start_line": current_start,
                "end_line": len(lines),
                "text": ''.join(current_segment)
            })
            logger.debug("Последний сегмент сохранён: строки %d-%d", current_start, len(lines))

        if not segments:
            logger.debug("Сегменты не созданы, добавлен весь файл как один сегмент")
            segments.append({
                "start_line": 1,
                "end_line": len(lines),
                "text": content
            })
        logger.debug("_segment_heuristic: всего сегментов=%d", len(segments))
        return segments

    # ------------------------------------------------------------------
    # Проверка принадлежности строк одному блоку
    # ------------------------------------------------------------------
    @staticmethod
    def is_same_block(segments: List[Dict[str, Any]], line_a: int, line_b: int) -> bool:
        logger.debug("is_same_block: line_a=%d, line_b=%d, всего сегментов=%d", line_a, line_b, len(segments))
        for seg in segments:
            if seg["start_line"] <= line_a <= seg["end_line"] and seg["start_line"] <= line_b <= seg["end_line"]:
                logger.debug("is_same_block: строки в одном сегменте %d-%d", seg["start_line"], seg["end_line"])
                return True
        logger.debug("is_same_block: строки в разных сегментах")
        return False

    # ------------------------------------------------------------------
    # Построение промпта для сегмента
    # ------------------------------------------------------------------
    def build_segment_prompt(self, segment: Dict[str, Any], global_ctx: str,
                            errors: List[Dict[str, Any]],
                            target_error: Optional[Dict[str, Any]] = None) -> str:
        """`target_error` — ошибка, которую модель ДОЛЖНА исправить (уже описана
        в "🔴 ERROR TO FIX" заголовке промпта выше этого блока, см. prompts/fix_prompt.txt).
        Без неё `errors` сваливаются в один безразличный список — на плотных
        участках кода (10+ соседних flake8-находок) модель путает целевую
        ошибку с соседними и чинит более простую/заметную вместо нужной,
        патч уходит как нерелевантный (см. control series 13, pygenda).
        С target_error — целевая помечена явно, остальные — как контекст,
        который НЕ нужно трогать.
        """
        logger.debug("build_segment_prompt: сегмент %d-%d, ошибок всего=%d",
                     segment["start_line"], segment["end_line"], len(errors))

        def _is_target(err: Dict[str, Any]) -> bool:
            if not target_error:
                return False
            return (
                err.get("line") == target_error.get("line")
                and err.get("code") == target_error.get("code")
            )

        target_lines = []
        other_lines = []
        for err in errors:
            line = err.get("line", 0)
            if not (segment["start_line"] <= line <= segment["end_line"]):
                continue
            text = f"Line {err.get('line', '?')}: [{err.get('code', '')}] {err.get('message', '')}"
            if _is_target(err):
                target_lines.append(text)
            else:
                other_lines.append(text)

        if target_lines:
            error_section = (
                "👉 TARGET ERROR — fix exactly this one (already detailed above):\n"
                + "\n".join(target_lines)
            )
            if other_lines:
                error_section += (
                    "\n\nOther errors nearby (context only — do NOT fix these, "
                    "fixing them is out of scope and will get your patch rejected):\n"
                    + "\n".join(other_lines)
                )
        elif other_lines:
            # target_error не передан (старые/несегментные вызовы) или не нашёлся
            # в этом сегменте — сохраняем старое поведение без разметки.
            error_section = "\n".join(other_lines)
        else:
            error_section = "No specific errors in this segment."
        logger.debug("build_segment_prompt: найдено ошибок в сегменте=%d (target=%d, other=%d)",
                     len(target_lines) + len(other_lines), len(target_lines), len(other_lines))

        prompt = f"""You are a code fixing assistant. You will receive the global context of the file and one SEGMENT of the file to fix.
Global context (signatures, imports) is provided so you understand the structure. You MUST fix only this segment and return a unified diff.

--- GLOBAL CONTEXT (DO NOT EDIT, FOR REFERENCE ONLY) ---
{global_ctx}

--- SEGMENT TO FIX (lines {segment['start_line']}-{segment['end_line']}) ---
{segment['text']}

--- ERRORS RELEVANT TO THIS SEGMENT ---
{error_section}

Output ONLY a unified diff that modifies the segment above. If no changes needed, output an empty diff.
"""
        logger.debug("build_segment_prompt: промпт создан, длина=%d", len(prompt))
        return prompt