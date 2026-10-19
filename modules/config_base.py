import yaml
import logging


class BaseConfigLoader:
    """Базовый класс для загрузки и хранения конфигураций (general, clicker, telegram, decision)."""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        # Файлы, которые будут загружены
        self.config_files = {
            'general': 'config/general_config.yaml',            # Общие настройки (может быть добавлено позже)
            'mexc_clicker': 'config/mexc_clicker_config.yaml',  # Координаты, OCR
            'telegram': 'config/telegram_config.yaml',          # Креды Telegram
            'decision': 'config/decision_config.yaml'           # Конфиг модуля принятия решений
        }
        
        # Словари для хранения данных
        self.general_config = {}
        self.mexc_clicker_config = {}
        self.telegram_config = {}
        self.decision_config = {}
        
        self._load_all_configs()

    def _load_yaml(self, file_path: str):
        """Загружает один YAML-файл."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            self.logger.critical(f"Файл конфигурации '{file_path}' не найден. Пропуск/Ошибка.")
            return None
        except Exception as e:
            self.logger.critical(f"Ошибка чтения YAML файла '{file_path}': {e}.")
            return None

    def _load_all_configs(self):
        """Загружает все необходимые конфигурационные файлы."""
        self.logger.info("Загрузка конфигурационных файлов...")
        
        # 1. Загрузка основного конфига кликера
        self.mexc_clicker_config = self._load_yaml(self.config_files['mexc_clicker']) or {}
        
        # 2. Загрузка кредов Telegram
        self.telegram_config = self._load_yaml(self.config_files['telegram']) or {}
        
        # 3. Загрузка конфига модуля принятия решений
        self.decision_config = self._load_yaml(self.config_files['decision']) or {}
        
        # 4. (Перспектива) Загрузка общего конфига
        # self.general_config = self._load_yaml(self.config_files['general']) or {}
        
        # Проверка и установка Tesseract
        tess_path = self.mexc_clicker_config.get('OCR', {}).get('TESSERACT_PATH')
        if tess_path:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = tess_path
            self.logger.info("Tesseract путь установлен.")
        else:
            self.logger.warning("Tesseract путь не найден в конфиге.")