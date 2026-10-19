import logging
import time
import json

from typing import List, Dict, Any, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

# Определяем структуру пустой свечи для возврата в случае ошибки
EMPTY_CANDLE_DICT = {
    'open': 0.0,
    'high': 0.0,
    'low': 0.0,
    'close': 0.0,
    'time': 0
}

class MexcApiClient:
    """
    Класс для выполнения запросов к публичному API MEXC (K-line data).
    Использует персистентную сессию Selenium для ускорения запросов.
    """
    
    BASE_URL = "https://futures.mexc.com/api/v1/contract/kline/"
    BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7890.101 Safari/537.36"
    
    def __init__(self, logger: logging.Logger, config: dict):
        self.logger = logger.getChild('ApiClient')
        self.config = config
        
        settings = config.get('API', {})
        self.symbol = settings.get('API_SYMBOL', '')
        self.interval = settings.get('API_INTERVAL', '')
        
        # Хранилище для драйвера
        self.driver = None

    def _init_driver(self):
        """Инициализирует и настраивает драйвер Chrome."""
        self.logger.info("Инициализация браузера Chrome...")
        chrome_options = Options()
        chrome_options.add_argument(f"user-agent={self.BROWSER_USER_AGENT}")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--log-level=3")
        # Рекомендую добавить headless, если не нужно видеть окно (ускорит еще сильнее)
        # chrome_options.add_argument("--headless=new") 
        
        driver = webdriver.Chrome(options=chrome_options)
        # Устанавливаем тайм-аут загрузки страницы (например, 5 сек)
        driver.set_page_load_timeout(10)
        return driver

    def _get_driver(self):
        """Возвращает активный драйвер или создает новый, если старый умер."""
        if self.driver is None:
            self.driver = self._init_driver()
        else:
            # Проверка жив ли драйвер (опционально, можно просто ловить исключения в запросе)
            try:
                # Легкая проверка связи
                _ = self.driver.window_handles
            except WebDriverException:
                self.logger.warning("Браузер был закрыт или потерян. Перезапуск...")
                self.driver = self._init_driver()
        return self.driver

    def _close_driver(self):
        """Безопасное закрытие драйвера."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def fetch_kline_data(self, limit: int) -> List[Dict[str, float]]:
        """
        Использует открытый браузер для получения JSON.
        """
        url = (
            f"{self.BASE_URL}{self.symbol}"
            f"?interval={self.interval}&limit={limit}"
        )
        self.logger.debug(f"Браузер: навигация на {url}")

        try: 
            driver = self._get_driver()
            driver.get(url)
            
            # Получаем body и парсим
            page_content = driver.find_element(By.TAG_NAME, "body").text
            data = json.loads(page_content)

            if data.get('success') and data.get('code') == 0:
                self.logger.debug("Браузер: данные успешно получены и распарсены.")
                return self._parse_kline_response(data, limit)
            else:
                self.logger.warning(f"Браузер: API вернуло ошибку: {data.get('msg', 'Unknown Error')}")
                return [EMPTY_CANDLE_DICT.copy()] * limit

        except json.JSONDecodeError:
            self.logger.error("Браузер: Не удалось декодировать JSON.")
            return [EMPTY_CANDLE_DICT.copy()] * limit
            
        except WebDriverException as e:
            # Если ошибка связана с драйвером (например, окно закрыли руками),
            # сбрасываем драйвер, чтобы в следующий раз он пересоздался.
            self.logger.error(f"Ошибка драйвера Selenium: {e}")
            self._close_driver() 
            return [EMPTY_CANDLE_DICT.copy()] * limit
            
        except Exception as e:
            self.logger.error(f"Общая ошибка: {e}")
            return [EMPTY_CANDLE_DICT.copy()] * limit

    def _parse_kline_response(self, data: Dict[str, Any], limit: int) -> List[Dict[str, float]]:
        """
        Преобразует колоночный ответ API MEXC в список словарей OHLC и разворачивает его.
        """
        try:
            ohlc_data = data['data']
            
            # Определяем количество полученных свечей
            count = len(ohlc_data['time'])
            
            if count == 0:
                self.logger.warning("API вернуло пустой список свечей.")
                return [] # Возвращаем пустой список, чтобы retry_attempts его поймал

            # Создаем список словарей свечей
            candles: List[Dict[str, float]] = []
            
            # Проходим по индексам, чтобы собрать каждую свечу
            for i in range(count):
                candle = {
                    'open': float(ohlc_data['open'][i]),
                    'high': float(ohlc_data['high'][i]),
                    'low': float(ohlc_data['low'][i]),
                    'close': float(ohlc_data['close'][i]),
                    'time': int(ohlc_data['time'][i])
                }
                candles.append(candle)
            
            # Теперь candle_data[0] будет самой новой свечой, а candle_data[limit-1] - самой старой.
            candles.reverse()

            return candles
            
        except KeyError as e:
            self.logger.error(f"Ошибка парсинга ответа API: отсутствует ключ {e}")
            return [] # Возвращаем пустой список
        except Exception as e:
            self.logger.error(f"Неизвестная ошибка при обработке данных: {e}")
            return [] # Возвращаем пустой список

    def __del__(self):
        """Гарантирует закрытие браузера при удалении объекта класса."""
        self._close_driver()