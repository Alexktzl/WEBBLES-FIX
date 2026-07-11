"""
Безопасная обёртка для запуска команд с таймаутом.
Полностью убирает multiprocessing, чтобы избежать OSError на Windows.
Использует subprocess.run с timeout.
"""

import subprocess
import traceback
from typing import Any, Callable, Union


class SubprocessError(Exception):
    """Обёртка для исключений, возникших при выполнении команды."""
    def __init__(self, exc_type_name: str, exc_message: str, tb_text: str):
        self.exc_type_name = exc_type_name
        self.exc_message = exc_message
        self.tb_text = tb_text
        super().__init__(f"{exc_type_name}: {exc_message}\n{tb_text}")


def run_with_timeout(func_or_cmd, timeout: float, *args, **kwargs) -> Any:
    """
    Выполнить команду или функцию с таймаутом.

    - Если func_or_cmd — строка или список (команда), используется subprocess.run.
    - Если func_or_cmd — callable, он вызывается напрямую БЕЗ таймаута.
      (Таймаут должен обеспечиваться самой функцией, например, через requests/urllib3 timeout).
    """
    if isinstance(func_or_cmd, (str, list)):
        try:
            result = subprocess.run(
                func_or_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                **kwargs
            )
            return result
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"Команда '{' '.join(func_or_cmd) if isinstance(func_or_cmd, list) else func_or_cmd}' превысила таймаут {timeout}с")
        except Exception as e:
            raise SubprocessError(
                exc_type_name=type(e).__name__,
                exc_message=str(e),
                tb_text=traceback.format_exc()
            )
    else:
        # Для обычных функций таймаут не применяем, 
        # ответственность за таймаут лежит на самой функции
        return func_or_cmd(*args, **kwargs)