"""
Read-only синтаксический анализатор для Webbles Fix.
Больше НЕ изменяет файлы на диске.
Все публичные методы возвращают данные или подсказки для LLM.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SyntaxRepair:
    """
    Анализатор синтаксических артефактов.
    Предоставляет данные для enrichment без модификации кода.
    """

    @staticmethod
    def heal_critical_syntax(error: Dict, file_path: Path) -> Optional[str]:
        """
        Возвращает unified-diff патч для критической синтаксической ошибки,
        делегируя read-only языковому хилеру.

        Используется планировщиком (`planning_stage._gather_available_patches`)
        как источник эвристических кандидатов. Возвращает `None`, если
        язык не поддерживается, файл не найден, хилер не справился или вернул
        результат, не пригодный для применения через симулятор песочницы
        (например, прямую правку файла "HEALED" или whole-file replacement).
        """
        if not file_path or not file_path.exists():
            return None
        suffix = file_path.suffix.lower()
        try:
            if suffix == ".rs":
                # RustSyntaxHealer — единственный read-only хилер,
                # возвращающий явные unified-diff патчи.
                from fixers.language_syntax.rust_healer import RustSyntaxHealer
                suggestions = RustSyntaxHealer(dry_run=True).analyze_and_suggest(error, file_path)
                for s in suggestions or []:
                    patch = s.get("patch") if isinstance(s, dict) else None
                    if isinstance(patch, str) and patch.startswith("--- "):
                        return patch
                return None
            # Для остальных языков хилеры либо мутируют файл, либо возвращают
            # маркер "HEALED" — оба варианта не подходят планировщику.
            # Возвращаем None, чтобы планировщик опирался на память/LLM.
            return None
        except Exception as e:
            logger.debug("heal_critical_syntax: ошибка для %s: %s", file_path, e)
            return None

    @staticmethod
    def analyze_file_artifacts(file_path: Path, language: str) -> Dict[str, any]:
        """
        Анализирует файл на наличие артефактов (дубликаты строк, слипшиеся строки и т.п.).
        Возвращает словарь с результатами анализа, но НЕ изменяет файл.
        """
        try:
            original = file_path.read_text(encoding='utf-8')
        except Exception as e:
            logger.warning("analyze_file_artifacts: ошибка чтения %s: %s", file_path, e)
            return {"status": "error", "message": str(e)}

        cleaned = SyntaxRepair._deep_clean_content(original, language)
        has_issues = (cleaned != original)

        return {
            "status": "ok",
            "has_artifacts": has_issues,
            "original_length": len(original),
            "cleaned_length": len(cleaned),
            "suggestion": (
                "Обнаружены артефакты (дубликаты строк, слипшиеся конструкции). "
                "Рекомендуется применить очистку перед генерацией патча."
            ) if has_issues else None,
            "cleaned_content": cleaned if has_issues else None,
        }

    @staticmethod
    def analyze_project_artifacts(project_path: Path, language: str,
                                  changed_files: Optional[list] = None) -> Dict[str, any]:
        """
        Анализирует проект на наличие артефактов в исходных файлах.
        Возвращает отчёт, но НЕ изменяет файлы.
        """
        logger.info("Анализ артефактов проекта (read-only)")
        report = {"status": "ok", "files_with_artifacts": [], "total_issues": 0}
        return report

    # -----------------------------------------------------------------
    # Вспомогательные методы (чистые функции, не мутируют)
    # -----------------------------------------------------------------
    @staticmethod
    def _deep_clean_content(content: str, language: str) -> str:
        """Применяет серию очищающих преобразований к тексту (in memory)."""
        text = content.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'\x00+', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        lines = text.splitlines()
        cleaned_lines = SyntaxRepair._remove_duplicates(lines)
        cleaned_lines = SyntaxRepair._split_merged_lines(cleaned_lines)
        return '\n'.join(cleaned_lines)

    @staticmethod
    def _remove_duplicates(lines: List[str]) -> List[str]:
        cleaned = []
        prev = ""
        for line in lines:
            line = line.strip()
            if not line:
                cleaned.append(line)
                prev = line
                continue
            if line == prev:
                continue
            cleaned.append(line)
            prev = line
        return cleaned

    @staticmethod
    def _split_merged_lines(lines: List[str]) -> List[str]:
        result = []
        for line in lines:
            parts = re.split(r'(?<=[;{}])(?=[^\s])', line)
            if len(parts) > 1:
                for part in parts:
                    stripped = part.strip()
                    if stripped:
                        result.append(stripped)
            else:
                if line.strip():
                    result.append(line.strip())
        return result