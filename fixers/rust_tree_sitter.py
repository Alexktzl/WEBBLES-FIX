"""
Анализ незакрытых блоков Rust через Tree-sitter.
Возвращает список блоков, требующих закрывающей '}', а также
умеет генерировать сбалансированный код (generate_balanced_code).
"""

from typing import List, Dict, Any

try:
    from tree_sitter import Language, Parser
    import tree_sitter_rust
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False


class RustTreeSitter:
    """Использует Tree-sitter для поиска незакрытых блоков и восстановления структуры."""

    def __init__(self):
        if not HAS_TREE_SITTER:
            raise RuntimeError(
                "tree-sitter и tree-sitter-rust не установлены. "
                "Выполните: pip install tree-sitter tree-sitter-rust"
            )
        lang = Language(tree_sitter_rust.language())
        self.parser = Parser(lang)

    def find_unclosed_blocks(self, source: str) -> List[Dict[str, Any]]:
        """
        Возвращает список блоков, которые не закрыты.
        Каждый элемент: {
            'block_type': 'enum'|'impl'|'fn'|'struct'|'trait'|'mod'|'match',
            'open_line': int (1-based),
            'expected_indent': int (количество пробелов)
        }
        """
        tree = self.parser.parse(bytes(source, "utf-8"))
        missing = []
        self._traverse(tree.root_node, source, missing)
        missing.sort(key=lambda b: b['open_line'], reverse=True)
        return missing

    def _traverse(self, node, source: str, missing: List[Dict[str, Any]]):
        if node.type == 'ERROR' and node.child_count > 0:
            for child in node.children:
                if child.type in (
                    'enum_item', 'impl_item', 'function_item',
                    'struct_item', 'trait_item', 'mod_item',
                    'match_expression'
                ):
                    start_line = child.start_point[0] + 1
                    indent = child.start_point[1]
                    block_name = self._block_type_to_name(child.type)
                    missing.append({
                        'block_type': block_name,
                        'open_line': start_line,
                        'expected_indent': indent,
                    })
                    return
        for child in node.children:
            self._traverse(child, source, missing)

    @staticmethod
    def _block_type_to_name(node_type: str) -> str:
        mapping = {
            'enum_item': 'enum',
            'impl_item': 'impl',
            'function_item': 'fn',
            'struct_item': 'struct',
            'trait_item': 'trait',
            'mod_item': 'mod',
            'match_expression': 'match',
        }
        return mapping.get(node_type, 'block')

    def generate_balanced_code(self, source: str) -> str:
        """
        Строит сбалансированную версию исходного кода, используя
        дерево разбора Tree-sitter. Вставляет недостающие '}' и
        удаляет явно лишние '}'.
        """
        tree = self.parser.parse(bytes(source, "utf-8"))
        lines = source.splitlines(keepends=True)
        # Собираем все "ожидания" закрывающих скобок с их отступами
        # (открывающие скобки с уровнями вложенности, не имеющие пары)
        unclosed = self._collect_unclosed(tree.root_node, source)
        
        # Удаляем лишние закрывающие (те, что не относятся ни к одному блоку)
        # на основе анализа дерева и подсчёта скобок в тексте вне строк/комментариев
        from fixers.brace_utils import count_braces_safe
        safe_open, safe_close = count_braces_safe(source)
        
        # Строим список операций: (строка, действие) – вставка или удаление
        # Удаление лишних закрывающих (разница close - open, но проверяем реальный дефицит открывающих)
        # Проще: если safe_close > safe_open, удаляем лишние закрывающие из конца файла
        # Но надёжнее: удаляем те, что не соответствуют никакому открытию
        result_lines = lines.copy()
        
        # Вставляем недостающие закрывающие для каждого незакрытого блока
        # Сортируем по убыванию позиции, чтобы не сбивать индексы
        unclosed.sort(key=lambda b: b['open_line'], reverse=True)
        for block in unclosed:
            open_line = block['open_line']
            indent = block['expected_indent']
            # Ищем позицию для вставки: перед следующей строкой с таким же или меньшим отступом
            insert_at = open_line  # начинаем искать с открывающей строки
            for i in range(open_line, len(result_lines)):
                stripped = result_lines[i].strip()
                if stripped and not stripped.startswith('//'):
                    current_indent = len(result_lines[i]) - len(result_lines[i].lstrip())
                    if current_indent <= indent:
                        insert_at = i
                        break
            else:
                insert_at = len(result_lines)
            close_line = ' ' * indent + '}\n'
            result_lines.insert(insert_at, close_line)
        
        # Теперь удаляем лишние закрывающие (если safe_close > safe_open)
        # Ищем закрывающие скобки, которые не являются частью известных блоков
        # Упрощённо: удаляем лишние '}' из конца (или начала) файла
        # Но мы можем сделать лучше: удалим те, что помечены как orphan
        if safe_close > safe_open:
            # Удаляем лишние закрывающие с конца (простейшая эвристика)
            excess = safe_close - safe_open
            removed = 0
            # Идём с конца и удаляем строки, содержащие только '}' с минимальным отступом
            for i in range(len(result_lines) - 1, -1, -1):
                if removed >= excess:
                    break
                stripped = result_lines[i].strip()
                if stripped == '}':
                    del result_lines[i]
                    removed += 1
        
        return ''.join(result_lines)

    def _collect_unclosed(self, node, source: str) -> List[Dict[str, Any]]:
        """Собирает незакрытые открывающие блоки из узла ERROR."""
        unclosed = []
        if node.type == 'ERROR':
            for child in node.children:
                if child.type in (
                    'enum_item', 'impl_item', 'function_item',
                    'struct_item', 'trait_item', 'mod_item',
                    'match_expression'
                ):
                    start_line = child.start_point[0] + 1
                    indent = child.start_point[1]
                    unclosed.append({
                        'open_line': start_line,
                        'expected_indent': indent,
                    })
                    # Не обходим глубже, т.к. это уже внутри ошибки
                    return unclosed
        for child in node.children:
            unclosed.extend(self._collect_unclosed(child, source))
        return unclosed