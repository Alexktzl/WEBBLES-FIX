"""
Построитель контекста для сегментов файла.
Сканирует весь файл, собирает объявления (структуры, impl-блоки, функции, типажи, импорты)
и для заданного сегмента возвращает компактный контекст, который LLM может использовать
для понимания доступных типов, сигнатур и трейтов (Clone, Copy, Debug, PartialEq, Eq).
Добавлен метод build_full_file_context для получения полного семантического контекста файла.
"""

import re
from typing import Dict, List, Optional, Set


class SegmentContextBuilder:
    def __init__(self, full_file_content: str):
        self.lines = full_file_content.splitlines()
        self.structs: Dict[str, str] = {}          # имя -> текст объявления
        self.impls: Dict[str, str] = {}            # "Type::method" -> сигнатура
        self.functions: Dict[str, str] = {}        # имя -> сигнатура
        self.traits: Dict[str, str] = {}           # имя -> объявление trait
        self.imports: Dict[str, str] = {}          # полное имя -> строка импорта
        self.clone_available: Set[str] = set()     # типы, для которых доступен Clone
        self.copy_available: Set[str] = set()      # типы, для которых доступен Copy
        self.debug_available: Set[str] = set()     # типы с Debug
        self.partialeq_available: Set[str] = set() # типы с PartialEq
        self.eq_available: Set[str] = set()        # типы с Eq
        self._parse_file()

    def _parse_file(self) -> None:
        for idx, line in enumerate(self.lines):
            stripped = line.strip()
            # Импорты
            if stripped.startswith("use "):
                parts = stripped[4:].rstrip(";").split("::")
                if parts:
                    name = parts[-1].split()[0]
                    self.imports[name] = line
            # Структуры
            elif stripped.startswith("struct "):
                name = stripped.split()[1].split("{")[0].split("(")[0].split(";")[0]
                self.structs[name] = line
            # Функции
            elif stripped.startswith("fn "):
                match = re.match(r'fn\s+(\w+)\s*\(', stripped)
                if match:
                    name = match.group(1)
                    self.functions[name] = line
            # Реализации трейтов (Clone, Copy, Debug, PartialEq, Eq)
            elif "impl" in stripped:
                trait_match = re.match(r'impl\s+(\w+)\s+for\s+(\w+)', stripped)
                if trait_match:
                    trait, type_name = trait_match.groups()
                    if trait in ("Clone", "Copy", "Debug", "PartialEq", "Eq"):
                        getattr(self, f"{trait.lower()}_available").add(type_name)
            # Атрибутные макросы #[derive(Clone, Copy, Debug, ...)]
            elif stripped.startswith("#[derive"):
                derives = re.findall(r'\b(Clone|Copy|Debug|PartialEq|Eq)\b', stripped)
                # Ищем следующую строку с struct
                if idx + 1 < len(self.lines):
                    next_line = self.lines[idx + 1].strip()
                    if next_line.startswith("struct "):
                        struct_name = next_line.split()[1].split("{")[0].split("(")[0].split(";")[0]
                        for trait in derives:
                            getattr(self, f"{trait.lower()}_available").add(struct_name)

    def build_full_file_context(
        self,
        target_line: Optional[int] = None,
        radius: int = 5
    ) -> str:
        """
        Собирает семантический контекст всего файла:
        - Все объявления структур, функций, трейтов, импортов
        - Информацию о доступных трейтах для используемых типов
        - Сигнатуры функций, которые могут быть связаны с целевой строкой
        
        Если указана target_line, добавляет дополнительные сигнатуры функций,
        вызываемых поблизости от этой строки.
        """
        context_parts = []

        # Все объявления
        if self.structs:
            context_parts.append("// --- STRUCTS ---")
            context_parts.extend(self.structs.values())
        if self.traits:
            context_parts.append("// --- TRAITS ---")
            context_parts.extend(self.traits.values())
        if self.functions:
            context_parts.append("// --- FUNCTIONS ---")
            context_parts.extend(self.functions.values())
        if self.imports:
            context_parts.append("// --- IMPORTS ---")
            context_parts.extend(self.imports.values())

        # Доступные трейты
        trait_hints = []
        all_types = set(self.clone_available) | set(self.copy_available) | set(self.debug_available) | set(self.partialeq_available) | set(self.eq_available)
        for type_name in sorted(all_types):
            hints = []
            if type_name in self.clone_available:
                hints.append("Clone")
            if type_name in self.copy_available:
                hints.append("Copy")
            if type_name in self.debug_available:
                hints.append("Debug")
            if type_name in self.partialeq_available:
                hints.append("PartialEq")
            if type_name in self.eq_available:
                hints.append("Eq")
            if hints:
                trait_hints.append(f"// {type_name}: {', '.join(hints)}")
        if trait_hints:
            context_parts.append("// --- AVAILABLE TRAITS ---")
            context_parts.extend(trait_hints)

        if not context_parts:
            return ""

        return "// --- FULL FILE SEMANTIC CONTEXT ---\n" + "\n".join(context_parts) + "\n// -------------------------------------\n"

    def build_context_for(
        self,
        segment_lines: List[str],
        compiler_help: Optional[str] = None,
        call_graph_info: Optional[Dict[str, List[str]]] = None,
        current_function: Optional[str] = None
    ) -> str:
        """
        Строит контекст для сегмента, включая:
        - объявления используемых идентификаторов
        - информацию о доступных трейтах (Clone, Copy, Debug, PartialEq, Eq)
        - подсказки компилятора (COMPILER SUGGESTS)
        - информацию о том, какие функции вызывают текущую и какие вызываются из неё
        - предупреждения о функциях, забирающих владение
        """
        used_ids = self._extract_identifiers(segment_lines)
        context_parts = []

        # Объявления
        for name in used_ids:
            if name in self.structs:
                context_parts.append(self.structs[name])
        for name in used_ids:
            if name in self.functions:
                context_parts.append(self.functions[name])
        for name in used_ids:
            if name in self.imports:
                context_parts.append(self.imports[name])

        # Доступные трейты для используемых типов
        trait_hints = []
        for name in used_ids:
            hints = []
            if name in self.clone_available:
                hints.append("Clone")
            if name in self.copy_available:
                hints.append("Copy")
            if name in self.debug_available:
                hints.append("Debug")
            if name in self.partialeq_available:
                hints.append("PartialEq")
            if name in self.eq_available:
                hints.append("Eq")
            if hints:
                trait_hints.append(f"{name}: {', '.join(hints)}")
        if trait_hints:
            context_parts.append("// Available traits: " + "; ".join(trait_hints))

        # Подсказка компилятора (COMPILER SUGGESTS)
        if compiler_help:
            context_parts.append(f"// COMPILER SUGGESTS: {compiler_help}")

        # Граф вызовов
        if call_graph_info and current_function:
            call_info = call_graph_info.get(current_function)
            if call_info:
                callers = call_info.get("callers", [])
                callees = call_info.get("callees", [])
                if callers:
                    context_parts.append(f"// Functions that call '{current_function}': {', '.join(callers)}")
                if callees:
                    context_parts.append(f"// Functions called by '{current_function}': {', '.join(callees)}")

        # Предупреждения о владении
        for name in used_ids:
            if name in self.functions:
                sig = self.functions[name]
                # Проверяем, принимает ли функция владение (нет & в типах параметров)
                params_match = re.search(r'fn\s+\w+\s*\(([^)]*)\)', sig)
                if params_match:
                    params = params_match.group(1)
                    if params.strip():
                        # Если нет &, это забирание владения, но только для некопируемых типов
                        has_ownership = False
                        for param in params.split(','):
                            param = param.strip()
                            if not param:
                                continue
                            if ':' in param:
                                type_part = param.split(':')[1].strip()
                                if not type_part.startswith('&') and not type_part.startswith('*'):
                                    base_type = re.split(r'[<\s]', type_part)[0]
                                    if base_type not in self.copy_available:
                                        has_ownership = True
                                        break
                            else:
                                # Параметр без явного типа – считаем, что может забирать владение
                                has_ownership = True
                        if has_ownership:
                            context_parts.append(
                                f"// FUNCTION '{name}' TAKES OWNERSHIP of its argument (need .clone() if used afterwards)"
                            )

        if not context_parts:
            return ""

        return ("// --- SEGMENT CONTEXT (declarations & traits available in this segment) ---\n" +
                "\n".join(context_parts) +
                "\n// ---------------------------------------------------------------\n")

    def _extract_identifiers(self, lines: List[str]) -> Set[str]:
        ids = set()
        for line in lines:
            for token in re.findall(r'\b([a-zA-Z_]\w*)\b', line):
                if token not in {"fn", "let", "mut", "struct", "impl", "use", "pub", "self", "Self",
                                 "true", "false", "if", "else", "match", "while", "for", "in",
                                 "return", "break", "continue", "as", "move", "ref", "where",
                                 "type", "enum", "trait", "mod", "crate", "super", "extern"}:
                    ids.add(token)
        return ids