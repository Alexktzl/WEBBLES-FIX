from typing import Tuple

def count_braces_safe(content: str) -> Tuple[int, int]:
    """
    Безопасный подсчёт фигурных скобок в Rust-коде.
    Игнорирует строковые и char-литералы, комментарии,
    а также повреждённые строки.
    Возвращает кортеж (открывающие, закрывающие).
    """
    open_braces = 0
    close_braces = 0
    in_line_comment = False
    in_block_comment = False
    in_string = False
    in_char = False
    escape = False
    i = 0
    n = len(content)

    while i < n:
        c = content[i]
        nxt = content[i + 1] if i + 1 < n else ''

        # Обработка комментариев
        if in_line_comment:
            if c == '\n':
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if c == '*' and nxt == '/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        # Обработка строк и символов
        if in_string:
            if not escape and c == '"':
                in_string = False
            escape = (c == '\\' and not escape)
            i += 1
            continue

        if in_char:
            if not escape and c == "'":
                in_char = False
            escape = (c == '\\' and not escape)
            i += 1
            continue

        # Обнаружение начала строки / комментария / символа
        if c == '/' and nxt == '/':
            in_line_comment = True
            i += 2
            continue

        if c == '/' and nxt == '*':
            in_block_comment = True
            i += 2
            continue

        if c == '"':
            in_string = True
            i += 1
            continue

        if c == "'":
            in_char = True
            i += 1
            continue

        # Собственно подсчёт скобок
        if c == '{':
            open_braces += 1
        elif c == '}':
            close_braces += 1

        i += 1

    return open_braces, close_braces