"""
Построитель промптов для LLM.
Формирует строгий инженерный брифинг с явными требованиями и проверками.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class PromptBuilder:
    """Собирает итоговый промпт для LLM в формате инженерного брифинга."""

    def __init__(self, template_path: Optional[Path] = None):
        if template_path is None:
            template_path = Path(__file__).parent.parent / "prompts" / "fix_prompt.txt"
        self.template_path = template_path
        # Шаблон больше не используется, весь промпт строится в build()

    def build(
        self,
        error: Dict[str, Any],
        context: str,
        language: str,
        failure_reason: str = "",
        attempt: int = 1,
        compiler_feedback: str = "",
        example: str = "",
        enrichment_context: str = "",
        structural_anchors: str = "",
        attempt_history: Optional[List[Dict[str, Any]]] = None,
        tool_contract: str = "",
        dependency_context: str = "",
    ) -> str:
        """
        Формирует строгий инженерный брифинг для LLM.
        """
        file_path = error.get("file", "unknown")
        line = error.get("line", 0)
        error_code = error.get("code", "")
        message = error.get("message", "unknown error")
        error_type = error.get("error_type", "unknown")
        error_class = error.get("error_class", "")

        prompt = f"""You are an EXPERT {language} developer performing SURGICAL code repair.

================================================================================
GOAL
================================================================================
Fix error [{error_code}] in {file_path}:{line}.

MESSAGE: {message}
TYPE: {error_type}
CLASS: {error_class}

================================================================================
SYSTEM CONTRACT (how this patch will be evaluated)
================================================================================
{tool_contract if tool_contract else "Patch is accepted if total errors decrease and no new errors appear in modified lines."}

================================================================================
STRUCTURAL ANCHORS (MUST BE PRESERVED)
================================================================================
{structural_anchors if structural_anchors else "No specific anchors provided."}
"""

        if dependency_context:
            prompt += f"""
================================================================================
DEPENDENCY CONTEXT (symbols in the error zone are used elsewhere — do NOT break these contracts)
================================================================================
{dependency_context}
"""

        if attempt_history:
            prompt += "\n================================================================================\nPATCH CONTRACT — PREVIOUS FAILED ATTEMPTS (do NOT repeat these approaches)\n================================================================================\n"
            for h in attempt_history:
                result = h.get("result", "REJECT")
                extras = []
                nd = h.get("net_delta")
                new_errs = h.get("new_errors") or []
                if new_errs:
                    extras.append(f"introduced new errors: {', '.join(str(e) for e in new_errs[:5])}")
                if nd is not None:
                    extras.append(f"net_delta={'+' if nd > 0 else ''}{nd}")
                extra_str = f" ({'; '.join(extras)})" if extras else ""
                prompt += (
                    f"  Attempt {h.get('attempt', '?')} [{result}]: "
                    f"intent=\"{h.get('intent', '?')}\"\n"
                    f"    FAILED: {h.get('failure', '?')[:200]}{extra_str}\n"
                )
            prompt += "  --> Generate a COMPLETELY different fix that avoids all the above.\n"

        prompt += f"""
================================================================================
CONSTRAINTS (HARD RULES – VIOLATION MEANS INSTANT REJECTION)
================================================================================
1. Return ONLY a unified diff. No explanations, no markdown.
2. Change ONLY the minimal code necessary to fix the error (≤5 lines).
3. NEVER change any function signature, impl block, or struct definition.
4. NEVER add comments (FIXME, TODO, etc.).
5. NEVER add duplicate code or modify unrelated lines.
6. The resulting code MUST compile and preserve all existing logic.
7. The diff must apply cleanly to the original file.

================================================================================
PLAN (THINK BEFORE YOU WRITE)
================================================================================
1. Analyze the error message and the provided code context.
2. Identify the exact line(s) causing the error.
3. Formulate a minimal change that resolves the error without breaking anything.
4. Verify mentally that the change does NOT alter any structural anchor.
5. Write the unified diff.

================================================================================
VERIFY (AFTER YOUR FIX)
================================================================================
- The error [{error_code}] is no longer present.
- No new compilation errors, warnings, or structural damage.
- All structural anchors are intact.
- The code compiles and behaves identically except for the fixed error.

================================================================================
CODE CONTEXT
================================================================================
{context}
"""
        if failure_reason:
            prompt += f"\n\n!!! PREVIOUS ATTEMPT FAILED: {failure_reason} !!!\n"
        if compiler_feedback:
            prompt += f"\nCOMPILER FEEDBACK:\n{compiler_feedback[:500]}\n"
        if enrichment_context:
            prompt += f"\nADDITIONAL CONTEXT:\n{enrichment_context}\n"

        prompt += "\n================================================================================\nOUTPUT (UNIFIED DIFF ONLY)\n================================================================================\n"
        return prompt

    # =================================================================
    # CaseFileBuilder (Stage C.4)
    # -----------------------------------------------------------------
    # Возвращает СТРОКУ-досье, которая передаётся как `extra_hint` в
    # LLMClient.generate_structured_fix(...). Не заменяет основной
    # промпт `build()`, а обогащает его конкретной информацией об
    # ошибке: что сломано, какие определения рядом, что точно нельзя
    # делать, как чинили похожее раньше, где anchor.
    # =================================================================

    # Сколько строк до/после error.line попадает в SPAN-сниппет.
    _CASE_FILE_CONTEXT_LINES = 6

    # Максимум строк, который мы реально показываем из related_defs / fixes
    # — иначе LLM-окно раздуется на ровном месте.
    _CASE_FILE_RELATED_MAX_LINES = 80
    _CASE_FILE_SIMILAR_MAX = 2
    _CASE_FILE_SIMILAR_PATCH_LINES = 30

    # EXTERNAL EXAMPLES (Stage J.6): сколько примеров показываем и насколько
    # длинным может быть сниппет одного примера.
    _CASE_FILE_EXTERNAL_MAX = 3
    _CASE_FILE_EXTERNAL_SNIPPET_LINES = 25

    def build_case_file(
        self,
        error: Dict[str, Any],
        file_content: str = "",
        related_defs: str = "",
        constraints: Optional[Tuple[List[str], List[str]]] = None,
        similar_fixes: Optional[List[Dict[str, Any]]] = None,
        external_examples: Optional[List[Dict[str, Any]]] = None,
        cross_file: str = "",
        security_example: str = "",
        fix_recipe: str = "",
    ) -> str:
        """Собирает структурированное «досье» по ошибке.

        Возвращает готовую строку. Если ни одной секции наполнить не удалось
        (нет ни сообщения, ни related, ни constraints, ни similar) — вернёт
        пустую строку: вызывающий код увидит, что enrichment'а нет.
        """
        do_list, dont_list = constraints if constraints else ([], [])
        similar = similar_fixes or []

        sections: List[str] = []

        # 1. SUMMARY
        summary = self._case_file_summary(error)
        if summary:
            sections.append("## SUMMARY\n" + summary)

        # 2. SPAN
        span = self._case_file_span(error, file_content)
        if span:
            sections.append("## SPAN\n" + span)

        # 3. RELATED DEFINITIONS
        if related_defs and related_defs.strip():
            trimmed = self._truncate_lines(related_defs.rstrip(),
                                           self._CASE_FILE_RELATED_MAX_LINES)
            sections.append("## RELATED DEFINITIONS\n" + trimmed)

        # 3b. PROJECT USAGE (Stage I) — где символы определены/используются по
        #     всему проекту (cross-file), дополняет одно-файловый RELATED выше.
        if cross_file and cross_file.strip():
            trimmed_xf = self._truncate_lines(cross_file.rstrip(),
                                              self._CASE_FILE_RELATED_MAX_LINES)
            sections.append("## PROJECT USAGE\n" + trimmed_xf)

        # 4. CONSTRAINTS
        constraints_block = self._case_file_constraints(do_list, dont_list)
        if constraints_block:
            sections.append("## CONSTRAINTS\n" + constraints_block)

        # 4b. SECURITY FIX EXAMPLE (идея Алекса 2026-07-09) — курируемый
        #     before→after для контекст-зависимого security-кода. Ставим
        #     сразу после CONSTRAINTS: LLM видит правило И конкретный образец.
        if security_example and security_example.strip():
            sections.append("## SECURITY FIX EXAMPLE (follow this pattern)\n"
                            + security_example.strip())

        # 4c. FIX RECIPE (идея Алекса 2026-07-10) — курируемый образец
        #     было→стало для КАНОНИЧЕСКОГО баг-кода (dispose/null/printf/...).
        #     «Разжёвываем» мастеру готовый паттерн — слабая модель копирует,
        #     а не думает с нуля (обобщение security-примеров на обычные баги).
        if fix_recipe and fix_recipe.strip():
            sections.append("## HOW TO FIX (follow this before→after pattern)\n"
                            + fix_recipe.strip())

        # 5. SIMILAR FIXES (память)
        similar_block = self._case_file_similar(similar)
        if similar_block:
            sections.append("## SIMILAR FIXES (from memory)\n" + similar_block)

        # 6. EXTERNAL EXAMPLES (Stage J) — прецеденты из открытых источников.
        external_block = self._case_file_external(external_examples or [])
        if external_block:
            sections.append("## EXTERNAL EXAMPLES\n" + external_block)

        # 7. ANCHOR HINTS
        anchor_block = self._case_file_anchor_hints(error, file_content)
        if anchor_block:
            sections.append("## ANCHOR HINTS\n" + anchor_block)

        if not sections:
            return ""

        # Маркер для логирования и для лёгкого визуального различия от
        # основного промпта в логах.
        header = "# CASE FILE"
        return "\n\n".join([header] + sections) + "\n"

    # ---- helpers ----------------------------------------------------

    @staticmethod
    def _case_file_summary(error: Dict[str, Any]) -> str:
        """1–2 строки. Никакого сырого rustc multi-line dump'а."""
        code_raw = (error.get("code") or "").strip()
        cls = (error.get("error_class") or "").strip()
        msg = (error.get("message") or "").strip()
        if not code_raw and not msg:
            return ""
        code = code_raw or "<no-code>"
        first_line = next((ln.strip() for ln in msg.splitlines() if ln.strip()), "")
        if len(first_line) > 200:
            first_line = first_line[:197] + "..."
        line1 = f"[{code}] {first_line}" if first_line else f"[{code}]"
        line2 = f"class={cls}" if cls else ""
        return "\n".join(p for p in (line1, line2) if p)

    @classmethod
    def _case_file_span(cls, error: Dict[str, Any], file_content: str) -> str:
        """`file:line[:col]` + нумерованный сниппет окна."""
        file_path = (error.get("file") or "").strip()
        line = int(error.get("line") or 0)
        if not file_path and line <= 0:
            return ""
        file_path = file_path or "<unknown>"
        col = error.get("column") or error.get("col")
        header = f"{file_path}:{line}" + (f":{col}" if col else "")
        if not file_content or line <= 0:
            return header
        snippet = cls._numbered_window(file_content, line, cls._CASE_FILE_CONTEXT_LINES)
        if not snippet:
            return header
        return f"{header}\n```\n{snippet}\n```"

    @staticmethod
    def _case_file_constraints(do_list: List[str], dont_list: List[str]) -> str:
        if not do_list and not dont_list:
            return ""
        parts: List[str] = []
        if do_list:
            parts.append("DO:")
            parts.extend(f"  - {item}" for item in do_list)
        if dont_list:
            if parts:
                parts.append("")
            parts.append("DON'T:")
            parts.extend(f"  - {item}" for item in dont_list)
        return "\n".join(parts)

    @classmethod
    def _case_file_similar(cls, similar: List[Dict[str, Any]]) -> str:
        if not similar:
            return ""
        out: List[str] = []
        for idx, fx in enumerate(similar[:cls._CASE_FILE_SIMILAR_MAX], start=1):
            intent = (fx.get("intent") or "").strip()
            confidence = fx.get("confidence")
            patch = (fx.get("patch") or fx.get("diff") or "").rstrip()
            header_bits = [f"### precedent #{idx}"]
            if intent:
                header_bits.append(f"intent={intent!r}")
            if isinstance(confidence, (int, float)):
                header_bits.append(f"confidence={confidence:.2f}")
            out.append(" ".join(header_bits))
            if patch:
                trimmed = cls._truncate_lines(patch, cls._CASE_FILE_SIMILAR_PATCH_LINES)
                out.append("```diff\n" + trimmed + "\n```")
        return "\n".join(out)

    @classmethod
    def _case_file_external(cls, external: List[Dict[str, Any]]) -> str:
        """Прецеденты из открытых источников (rustc --explain, GitHub, SO).

        Принимает list of dict `{source, title, snippet, url}` — НЕ
        `Example`-объекты: prompt_builder намеренно не зависит от пакета
        `analysis.external_examples` (декаплинг, см. J.6).
        """
        if not external:
            return ""
        out: List[str] = []
        for idx, ex in enumerate(external[:cls._CASE_FILE_EXTERNAL_MAX], start=1):
            if not isinstance(ex, dict):
                continue
            source = (ex.get("source") or "?").strip()
            title = (ex.get("title") or "").strip()
            url = (ex.get("url") or "").strip()
            snippet = (ex.get("snippet") or "").strip()
            if not title and not snippet:
                continue
            header = f"### [{source}] {title}".rstrip()
            out.append(header)
            if url:
                out.append(f"<{url}>")
            if snippet:
                trimmed = cls._truncate_lines(snippet,
                                              cls._CASE_FILE_EXTERNAL_SNIPPET_LINES)
                out.append("```\n" + trimmed + "\n```")
        return "\n".join(out)

    @classmethod
    def _case_file_anchor_hints(cls, error: Dict[str, Any], file_content: str) -> str:
        """Подсказка для structured EditSet: какую `anchor.line` и
        `anchor.match` подставить, если LLM решит править эту ошибку.
        """
        line = int(error.get("line") or 0)
        if line <= 0:
            return ""
        if not file_content:
            return f"anchor.line: {line}\nanchor.match: <use the exact text of that line>"
        target = cls._line_text(file_content, line)
        if not target:
            return f"anchor.line: {line}\nanchor.match: <line {line} not found in file_content; recheck>"
        if len(target) > 200:
            target = target[:197] + "..."
        return (
            f"anchor.line: {line}\n"
            f"anchor.match: {target!r}\n"
            "(anchor.match must be a substring of the line in the file. "
            "Keep it short and unique.)"
        )

    # ---- low-level text helpers -------------------------------------

    @staticmethod
    def _numbered_window(text: str, line: int, radius: int) -> str:
        """Вернёт `radius` строк до/после `line`, пронумерованных."""
        lines = text.splitlines()
        if not lines or line <= 0:
            return ""
        start = max(1, line - radius)
        end = min(len(lines), line + radius)
        width = len(str(end))
        out: List[str] = []
        for n in range(start, end + 1):
            marker = ">>" if n == line else "  "
            out.append(f"{marker} {n:>{width}} | {lines[n - 1]}")
        return "\n".join(out)

    @staticmethod
    def _line_text(text: str, line: int) -> str:
        lines = text.splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].rstrip()
        return ""

    @staticmethod
    def _truncate_lines(text: str, max_lines: int) -> str:
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        kept = lines[:max_lines]
        kept.append(f"... [{len(lines) - max_lines} more lines truncated]")
        return "\n".join(kept)
