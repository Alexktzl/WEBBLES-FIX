"""
Мультиязычный модуль восстановления после критических синтаксических разрушений.
Добавлен многошаговый подход: удаление мусорных строк перед отправкой LLM.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.language_support import LanguageSupport
from fixers.file_segmenter import FileSegmenter
from fixers.syntax_repair import SyntaxRepair

logger = logging.getLogger(__name__)


class SyntaxDisasterRecovery:
    """Восстанавливает файл после синтаксической катастрофы."""

    def __init__(self, llm_client, segmenter: FileSegmenter,
                 language_provider: Optional[LanguageSupport] = None):
        self.llm_client = llm_client
        self.segmenter = segmenter
        self.language_provider = language_provider

    def recover(self, file_path: Path, error: Dict[str, Any],
                context: Any) -> Optional[str]:
        if not self._is_cascade(error, context):
            return None
        logger.info("Обнаружена каскадная катастрофа, запускаем восстановление")
        try:
            file_content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Не удалось прочитать файл {file_path}: {e}")
            return None

        lang = context.language if hasattr(context, 'language') else 'rust'

        # ШАГ 1: Быстрая очистка файла (удаление дубликатов, склеенных строк)
        cleaned = SyntaxRepair._deep_clean_content(file_content, lang)
        if cleaned != file_content:
            file_path.write_text(cleaned, encoding="utf-8")
            file_content = cleaned
            logger.info("DisasterRecovery: файл очищен от артефактов")

        disaster_start, disaster_end = self._find_disaster_bounds(
            file_content, error.get("line", 0), lang
        )
        if disaster_start is None:
            return None
        
        if disaster_start > disaster_end:
            disaster_start, disaster_end = disaster_end, disaster_start
        if disaster_end - disaster_start < 3:
            disaster_start, disaster_end = self._expand_to_logical_block(
                file_content, disaster_start, disaster_end, lang
            )

        logger.info(f"Границы восстановления: строки {disaster_start}-{disaster_end}")

        # ШАГ 2: Удаление строк, не содержащих ключевых слов языка (вероятный мусор)
        keywords = self._get_code_keywords(lang)
        lines = file_content.splitlines(keepends=True)
        cleaned_lines = []
        removed_count = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Сохраняем строку, если она пустая, содержит ключевое слово, '{', '}' или является частью проблемной зоны
            if (disaster_start <= i + 1 <= disaster_end or
                not stripped or
                any(kw in stripped for kw in keywords) or
                stripped in ('{', '}', '(', ')', '[', ']')):
                cleaned_lines.append(line)
            else:
                removed_count += 1
        if removed_count > 0:
            file_content = "".join(cleaned_lines)
            file_path.write_text(file_content, encoding="utf-8")
            logger.info("DisasterRecovery: удалено %d мусорных строк", removed_count)

        prompt = self._build_recovery_prompt(
            error, file_content, disaster_start, disaster_end, lang
        )
        recovered = self._call_llm_for_recovery(prompt, lang)
        if recovered and isinstance(recovered, str) and len(recovered.strip()) > 10:
            if not self._braces_balanced(file_content, recovered):
                logger.warning("DisasterRecovery discarded: braces not balanced")
                return None
            logger.info(f"LLM вернул восстановленное содержимое ({len(recovered)} символов)")
            return recovered
        else:
            logger.warning("LLM не вернул пригодное содержимое")
            return None

    def _is_cascade(self, error: Dict[str, Any], context: Any) -> bool:
        error_class = error.get("error_class", "")
        if error_class != "CRITICAL_SYNTAX":
            return False
        try:
            current_errors = context.current_errors if hasattr(context, 'current_errors') else []
            error_file = error.get("file", "")
            same_file_errors = [e for e in current_errors if e.get("file") == error_file]
            if len(same_file_errors) >= 2:
                logger.info(f"Каскад обнаружен: {len(same_file_errors)} ошибок в файле {error_file}, запускаем DisasterRecovery")
                return True
            else:
                logger.info(f"Ошибка CRITICAL_SYNTAX, но всего {len(same_file_errors)} ошибок в файле – недостаточно для каскада")
                return False
        except Exception:
            return True

    def _find_disaster_bounds(self, file_content: str, error_line: int,
                              language: str) -> Tuple[Optional[int], Optional[int]]:
        lines = file_content.splitlines(keepends=True)
        total = len(lines)
        if error_line >= total:
            return None, None
        disaster_start = error_line + 1
        if disaster_start > total:
            return None, None
        keywords = self._get_code_keywords(language)
        for i in range(disaster_start - 1, total):
            stripped = lines[i].strip()
            if self._is_valid_code_start(stripped, keywords):
                return disaster_start, i
        return disaster_start, total

    def _expand_to_logical_block(self, content: str, start: int, end: int, lang: str) -> Tuple[int, int]:
        lines = content.splitlines(keepends=True)
        for i in range(start - 1, -1, -1):
            stripped = lines[i].strip()
            if any(stripped.startswith(prefix) for prefix in ('fn ', 'pub fn ', 'impl ', 'pub impl ', 'struct ', 'pub struct ', 'trait ', 'mod ', 'pub trait ')):
                start = i + 1
                break
        balance = 0
        for i in range(start - 1, len(lines)):
            balance += lines[i].count('{') - lines[i].count('}')
            if balance == 0 and i >= end:
                end = i + 1
                break
        return max(1, start), min(end, len(lines))

    def _is_valid_code_start(self, line: str, keywords: set) -> bool:
        if not line:
            return False
        for kw in keywords:
            if line.startswith(kw):
                return True
        if line.startswith(('{', '}')):
            return True
        return False

    def _get_code_keywords(self, language: str) -> set:
        if self.language_provider and hasattr(self.language_provider, 'get_code_keywords'):
            return self.language_provider.get_code_keywords()
        if language in ('rust', 'rs'):
            return {'fn ', 'let ', 'impl ', 'struct ', 'enum ', 'trait ', 'mod ', 'use ', 'pub ',
                    'macro_rules!', 'async ', 'unsafe ', 'extern ', 'if ', 'match ', 'loop ',
                    'while ', 'for ', 'return ', 'break ', 'continue ', 'println', 'print',
                    'format', 'assert', 'vec!', 'const ', 'static '}
        elif language in ('python', 'py'):
            return {'def ', 'class ', 'import ', 'from ', 'if ', 'elif ', 'else:', 'for ',
                    'while ', 'with ', 'try:', 'except ', 'finally:', 'return ', 'print('}
        elif language in ('javascript', 'typescript', 'js', 'ts'):
            return {'function ', 'const ', 'let ', 'var ', 'if ', 'else ', 'for ', 'while ',
                    'do ', 'switch ', 'case ', 'break ', 'continue ', 'return ', 'import ',
                    'export ', 'class ', 'new ', 'console.', 'document.'}
        return set()

    def _build_recovery_prompt(self, error: Dict[str, Any], file_content: str,
                               disaster_start: int, disaster_end: int,
                               language: str) -> str:
        lines = file_content.splitlines(keepends=True)
        before_disaster = lines[:disaster_start]
        after_disaster = lines[disaster_end:] if disaster_end < len(lines) else []
        disaster_lines = lines[disaster_start:disaster_end]

        extra_hints = ""
        if self.language_provider:
            extra_hints = self.language_provider.get_prompt_enhancements(error) or ""

        func_before = self._find_last_function_before(lines, disaster_start)

        prompt = f"""You are an expert {language} developer performing SURGICAL code repair.
The file below has been corrupted by a syntax disaster near line {error.get('line')}.
The corrupted lines are between {disaster_start} and {disaster_end}. We have already removed them.
Your job is to RESTORE the removed code so that the file compiles and the program logic remains intact.

CRITICAL RULES – VIOLATION MEANS INSTANT REJECTION:
1. You will see the code BEFORE the corruption zone and the code AFTER it.
2. Your task is to write the MISSING code that goes BETWEEN them.
3. The braces must be balanced: the total number of '{{' in the final file MUST EQUAL the total number of '}}'.
4. Preserve ALL function signatures, types, and business logic exactly as they were.
5. If there was a malicious or incomplete line, replace it with a correct, minimal implementation.
6. DO NOT modify ANY code outside the missing block. Not a single character.
7. Output ONLY the complete corrected file content. NO markdown, NO diffs, NO explanations.
{extra_hints}

--- CODE BEFORE CORRUPTION (DO NOT MODIFY) ---
{''.join(before_disaster)}

--- CORRUPTED CODE THAT WAS REMOVED (FOR CONTEXT ONLY, DO NOT COPY BLINDLY) ---
{''.join(disaster_lines) if disaster_lines else "// (entire block was corrupted)"}

--- CODE AFTER CORRUPTION (DO NOT MODIFY) ---
{''.join(after_disaster)}

--- RESTORED FILE (INSERT YOUR CODE BETWEEN THE SECTIONS ABOVE) ---
{''.join(before_disaster)}
"""
        if func_before:
            prompt += f"\n// Note: the last valid function before corruption was:\n// {func_before.strip()}\n"
        prompt += "\n--- END ---\n"
        return prompt

    def _find_last_function_before(self, lines: List[str], line_num: int) -> Optional[str]:
        for i in range(line_num - 2, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith('fn ') or stripped.startswith('pub fn '):
                return stripped
        return None

    def _call_llm_for_recovery(self, prompt: str, language: str) -> Optional[str]:
        fake_error = {"file": "recovery_target", "code": "SYNTAX_DISASTER", "message": "syntax disaster recovery"}
        try:
            result = self.llm_client.generate_full_file(
                error=fake_error,
                full_file_content=prompt,
                language=language
            )
            return result
        except Exception as e:
            logger.error(f"Ошибка вызова LLM для recovery: {e}")
            return None

    @staticmethod
    def _braces_balanced(original: str, new: str) -> bool:
        if new.count('{') != new.count('}'):
            logger.warning("В новом файле скобки не сбалансированы: %d '{' и %d '}'",
                           new.count('{'), new.count('}'))
            return False
        return True

    @staticmethod
    def _braces_count_exact(original: str, new: str) -> bool:
        return SyntaxDisasterRecovery._braces_balanced(original, new)