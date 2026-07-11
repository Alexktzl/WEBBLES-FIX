"""
Telegram репортер для Webbles Fix.
Отправляет уведомления о ходе выполнения в Telegram на русском языке.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TelegramReporter:
    """
    Отправляет сообщения в Telegram бот.
    """

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

    def send(self, message: str) -> None:
        """Отправляет сообщение, если репортер настроен."""
        if not self.enabled:
            return

        try:
            import requests
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message}
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code != 200:
                logger.debug(f"Ошибка отправки Telegram: {response.text}")
        except Exception as e:
            logger.debug(f"Ошибка Telegram: {e}")