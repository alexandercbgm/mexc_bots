from typing import Dict, Optional

class TradingUtils:
    """
    Класс, содержащий вспомогательные функции для работы с данными.
    """
    
    def _get_decimal_precision(self, price: float) -> int:
        """
        Определяет количество знаков после запятой в цене, чтобы избежать округления биржей (тик).
        Используется строковое представление float.
        """
        if price is None or price == 0.0:
            # Безопасное значение по умолчанию, если цена не получена
            return 5 

        s = str(price)
        if '.' not in s:
            return 0
        
        # Получаем количество знаков после запятой
        precision = len(s.split('.')[-1])
        
        return min(precision, 10) # Максимум 10 знаков

    def _get_max_precision(self, candle: Dict[str, float]) -> int:
        """
        Определяет максимальную точность среди цен OHLC переданной свечи.
        Необходимо для корректного расчета размера тика.
        """
        if not candle:
            return 0
            
        prices = [
            candle.get('open', 0.0),
            candle.get('high', 0.0),
            candle.get('low', 0.0),
            candle.get('close', 0.0)
        ]
        
        return max(self._get_decimal_precision(p) for p in prices)