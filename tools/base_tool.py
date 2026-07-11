import subprocess
import sys
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

class BaseTool(ABC):
    """Базовый класс для всех внешних инструментов."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        self._available_cache: Optional[bool] = None

    @abstractmethod
    def is_available(self) -> bool:
        """Проверяет, установлен ли инструмент в системе."""
        ...

    @abstractmethod
    def run(self, **kwargs) -> Any:
        """Запускает инструмент с переданными параметрами."""
        ...

    def safe_run(self, **kwargs) -> Any:
        if not self.enabled:
            return None
        # Доступность инструмента (наличие бинаря в PATH) не меняется в течение
        # одного прогона движка, а is_available() у внешних тулов (semgrep,
        # bandit и т.п.) запускает отдельный subprocess. AnalyzeStage держит
        # один и тот же экземпляр тула на все global_cycle прогона и раньше
        # платил этот subprocess-вызов на каждом цикле — кэшируем результат.
        if self._available_cache is None:
            self._available_cache = self.is_available()
        if not self._available_cache:
            return None
        try:
            return self.run(**kwargs)
        except Exception:
            return None

    # === НОВЫЙ МЕТОД ===
    def _prompt_install(self, install_cmd: str, tool_name: str) -> bool:
        """
        Спрашивает пользователя, хочет ли он установить tool_name,
        и выполняет install_cmd. Возвращает True, если установка прошла успешно.
        """
        print(f"\n⚠️  {tool_name} не найден.")
        print(f"    Команда для установки: {install_cmd}")
        answer = input("    Установить сейчас? (y/N): ").strip().lower()
        if answer != 'y':
            print("    Установка отменена. Инструмент будет отключён.")
            return False

        print(f"    Устанавливаю {tool_name}...")
        try:
            # Запускаем установку с правами текущего пользователя
            subprocess.run(install_cmd, shell=True, check=True)
            print(f"    ✅ {tool_name} успешно установлен.")
            return True
        except subprocess.CalledProcessError as e:
            print(f"    ❌ Ошибка установки: {e}")
            return False