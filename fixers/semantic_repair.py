"""
Семантические эвристики для детерминированного исправления ошибок,
требующих понимания типов, владения и контекста.
Запускаются перед LLM-генерацией для ошибок BLOCKING.
Добавлен шаблон для E0277 (trait not satisfied).
"""

import logging
import re
from pathlib import Path
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class SemanticRepair:
    """
    Набор правил для быстрого исправления типовых семантических ошибок
    без привлечения LLM.
    """
    
    @staticmethod
    def try_fix(error: Dict, file_path: Path) -> Optional[str]:
        """
        Попытаться исправить ошибку детерминированно.
        Возвращает unified diff патч или None, если правило не применимо.
        """
        code = error.get("code", "")
        message = error.get("message", "")
        line_num = error.get("line", 0)

        if code == "E0308" and "mismatched types" in message:
            return SemanticRepair._fix_mismatched_types(error, file_path, line_num)
        elif code == "E0277":
            return SemanticRepair._fix_trait_not_satisfied(error, file_path, line_num)
        elif code == "E0599" and "method not found" in message:
            return SemanticRepair._fix_method_not_found(error, file_path, line_num)
        elif code == "E0596" or code == "E0594":
            # cannot borrow as mutable / immutable
            return SemanticRepair._fix_mut_borrow(error, file_path, line_num)
        elif code == "E0425":
            return SemanticRepair._fix_unresolved_function(error, file_path, line_num)
        elif "cannot borrow as mutable" in message or "does not live long enough" in message:
            return None  # слишком сложно для эвристики
        return None

    @staticmethod
    def _fix_trait_not_satisfied(error: Dict, file_path: Path, line_num: int) -> Optional[str]:
        """
        Исправляет ошибки вида:
        - `main` возвращает `Result<(), String>` (String не реализует Termination)
        - Ожидается тип, реализующий определённый трейт
        """
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if line_num <= 0 or line_num > len(lines):
                return None
            
            message = error.get("message", "")
            # Ищем имя трейта, который не реализован
            trait_match = re.search(r'trait `([^`]+)` is not implemented for `([^`]+)`', message)
            if not trait_match:
                # Альтернативный паттерн: the trait `X` is not implemented for `Y`
                trait_match = re.search(r'the trait `([^`]+)` is not implemented for `([^`]+)`', message)
            if not trait_match:
                return None
            
            trait_name = trait_match.group(1)
            type_name = trait_match.group(2)
            
            # Особый случай: Termination для main
            if trait_name == "Termination" and type_name == "String":
                file_name = file_path.name
                # Ищем строку с объявлением main
                for i in range(line_num - 1, -1, -1):
                    if "fn main()" in lines[i]:
                        line = lines[i]
                        # Заменяем Result<(), String> на Result<(), Box<dyn std::error::Error>>
                        new_line = line.replace("Result<(), String>", "Result<(), Box<dyn std::error::Error>>")
                        if new_line != line:
                            return (
                                f"--- a/{file_name}\n"
                                f"+++ b/{file_name}\n"
                                f"@@ -{i+1},1 +{i+1},1 @@\n"
                                f"-{line}"
                                f"+{new_line}"
                            )
                return None
            
            # Общий случай: добавить derive или impl для трейта
            logger.debug("SemanticRepair._fix_trait_not_satisfied: trait=%s, type=%s", trait_name, type_name)
            return None  # пока не реализовано для общего случая
            
        except Exception as e:
            logger.debug("SemanticRepair._fix_trait_not_satisfied: %s", e)
            return None

    @staticmethod
    def _fix_unresolved_function(error: Dict, file_path: Path, line_num: int) -> Optional[str]:
        """
        Пытается исправить вызов несуществующей функции.
        Если имя похоже на известную функцию, предлагает исправление.
        """
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if line_num <= 0 or line_num > len(lines):
                return None
            line = lines[line_num - 1]
            
            # Извлекаем имя неразрешённой функции из сообщения
            message = error.get("message", "")
            func_match = re.search(r"cannot find (?:function|value) `([^`]+)`", message)
            if not func_match:
                return None
            func_name = func_match.group(1)
            
            # Распространённые опечатки
            corrections = {
                "pritnln": "println",
                "pritn": "print",
                "epritnln": "eprintln",
                "formt": "format",
                "assrt": "assert",
                "dbg": "dbg",
                "write": "write",
                "writeln": "writeln",
            }
            
            if func_name in corrections:
                correct_name = corrections[func_name]
                new_line = line.replace(func_name, correct_name, 1)
                if new_line != line:
                    file_name = file_path.name
                    return (
                        f"--- a/{file_name}\n"
                        f"+++ b/{file_name}\n"
                        f"@@ -{line_num},1 +{line_num},1 @@\n"
                        f"-{line}"
                        f"+{new_line}"
                    )
            return None
            
        except Exception as e:
            logger.debug("SemanticRepair._fix_unresolved_function: %s", e)
            return None

    @staticmethod
    def _fix_mismatched_types(error: Dict, file_path: Path, line_num: int) -> Optional[str]:
        """
        Пытается исправить несовпадение типов вставкой `as` приведения.
        Пример: let z = x + y; где x: i32, y: u32 → let z = x + y as i32;
        """
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if line_num <= 0 or line_num > len(lines):
                return None
            line = lines[line_num - 1]
            
            # Ищем выражение с оператором и двумя операндами разных типов
            # Ожидаем сообщение: expected i32, found u32 и т.п.
            expected_match = re.search(r"expected\s+(\w+)", message := error.get("message", ""))
            found_match = re.search(r"found\s+(\w+)", message)
            if not expected_match or not found_match:
                return None
            expected_type = expected_match.group(1)
            found_type = found_match.group(1)
            
            # Найдём переменную слева от оператора, тип которой не совпадает с expected
            # Упрощённо: ищем первое вхождение found_type в строке и заменяем на `as expected_type`
            if found_type not in line and expected_type not in line:
                return None
            # Ищем бинарное выражение с участием переменной с неправильным типом
            # Очень грубо, для демонстрации
            new_line = line.replace(found_type, f"{found_type} as {expected_type}", 1)
            if new_line == line:
                # Альтернатива: если нашлось expected_type слева от знака =, то исправляем слева
                if f" {expected_type} " in line:
                    new_line = line.replace(f" {expected_type} ", f" {expected_type} as {found_type} ", 1)
            if new_line == line:
                return None
            
            file_name = file_path.name
            return (
                f"--- a/{file_name}\n"
                f"+++ b/{file_name}\n"
                f"@@ -{line_num},1 +{line_num},1 @@\n"
                f"-{line}"
                f"+{new_line}"
            )
        except Exception as e:
            logger.debug("SemanticRepair._fix_mismatched_types: %s", e)
            return None

    @staticmethod
    def _fix_method_not_found(error: Dict, file_path: Path, line_num: int) -> Optional[str]:
        """
        Если метод не найден, пробует заменить его на похожий из контекста.
        Пока не реализовано — возвращает None.
        """
        return None

    @staticmethod
    def _fix_mut_borrow(error: Dict, file_path: Path, line_num: int) -> Optional[str]:
        """
        Добавляет `mut` к переменной, если она пытается изменяться без mut.
        """
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if line_num <= 0 or line_num > len(lines):
                return None
            line = lines[line_num - 1]
            
            # Ищем let без mut, где переменная используется с изменением в следующей строке
            match = re.match(r'\s*let\s+(\w+)\s*=', line)
            if not match:
                return None
            var_name = match.group(1)
            # Проверяем следующую строку на наличие вызова метода, который требует mut
            if line_num < len(lines):
                next_line = lines[line_num]
                if f"{var_name}." in next_line and any(m in next_line for m in [".push(", ".insert(", ".remove(", "= "]):
                    new_line = line.replace("let", "let mut", 1)
                    file_name = file_path.name
                    return (
                        f"--- a/{file_name}\n"
                        f"+++ b/{file_name}\n"
                        f"@@ -{line_num},1 +{line_num},1 @@\n"
                        f"-{line}"
                        f"+{new_line}"
                    )
            # Также проверяем всю строку на наличие изменяющего вызова
            if re.search(rf'{var_name}\.\s*(push|insert|remove|pop|sort|clear)', line):
                new_line = line.replace("let", "let mut", 1)
                file_name = file_path.name
                return (
                    f"--- a/{file_name}\n"
                    f"+++ b/{file_name}\n"
                    f"@@ -{line_num},1 +{line_num},1 @@\n"
                    f"-{line}"
                    f"+{new_line}"
                )
            return None
        except Exception as e:
            logger.debug("SemanticRepair._fix_mut_borrow: %s", e)
            return None