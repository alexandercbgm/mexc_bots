import logging
import requests

from modules.config_base import BaseConfigLoader 


# --- КОНСТАНТЫ И ФУНКЦИИ ОБРАБОТКИ ---
MAX_MESSAGE_LENGTH = 4000


def _sanitize_message(message: str) -> str:
    """Обрезает сообщение до максимальной длины (4000 символов)."""
    
    if len(message) > MAX_MESSAGE_LENGTH:
        # Обрезаем и добавляем уведомление об обрезке
        return message[:MAX_MESSAGE_LENGTH - 20] + "\n... (обрезано)"
    
    # При использовании parse_mode='HTML' нет необходимости в сложном экранировании
    return message


class Notifier:
    """Базовый интерфейс для оповещений."""
    def notify(self, message: str):
        raise NotImplementedError

class TelegramNotifier(BaseConfigLoader, Notifier):
    """Класс для отправки уведомлений в Telegram (синхронный)."""
    
    def __init__(self, logger: logging.Logger):
        # BaseConfigLoader должен быть импортирован корректно (см. строку 6)
        super().__init__(logger) 
        self.logger = logger.getChild('Notifier')
        
        # Получение кредов из загруженных конфигов
        self.token = self.telegram_config.get('TELEGRAM', {}).get('BOT_TOKEN')
        self.chat_id = self.telegram_config.get('TELEGRAM', {}).get('CHAT_ID')
        self.url = f'https://api.telegram.org/bot{self.token}/sendMessage' if self.token else None
        
        if self.token and self.chat_id:
             self.logger.info("Telegram Notifier инициализирован.")
        else:
             self.logger.warning("Telegram-креды не найдены. Оповещения будут пропускаться.")

    def notify(self, message: str):
        """Отправляет сообщение в Telegram синхронно с HTML-форматированием."""
        if not self.url or not self.chat_id:
            return

        # 1. Санитизация (обрезка) сообщения
        safe_message = _sanitize_message(message)

        data_dict = {
            'chat_id': self.chat_id,
            'text': safe_message,
            # ИСПРАВЛЕНО: parse_mode установлен на 'HTML' для корректного форматирования
            'parse_mode': 'HTML', 
            'disable_notification': True
        }
        
        headers = {'Content-Type': 'application/json'}

        try:
            response = requests.post(self.url, json=data_dict, headers=headers, timeout=5)
            
            if response.status_code != 200:
                self.logger.error(f"❌ Ошибка отправки Telegram ({response.status_code}): {response.text}")
            else:
                self.logger.debug(f"Telegram: Успешно отправлено сообщение: '{safe_message[:70]}...'")
        except requests.exceptions.RequestException as e:
            self.logger.error(f"❌ Ошибка запроса Telegram: {e}")