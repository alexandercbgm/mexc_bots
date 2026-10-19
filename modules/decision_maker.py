import logging
import time
import numpy as np

from typing import List, Dict, Tuple, Optional
from modules.config_base import BaseConfigLoader
from modules.notifier import Notifier
from modules.utils import TradingUtils


# Тип данных для свечей
CandleOHLC = Dict[str, float]


class DecisionMaker(BaseConfigLoader, TradingUtils):
    """
    Класс принятия решений.
    Собран на основе ТЗ: структура нового кода, логика сигналов старого кода,
    сложная математика стопов по ТЗ.
    """

    def __init__(self, logger: logging.Logger, notifier: Notifier):
        super().__init__(logger)
        self.logger = logger.getChild('SIGNAL')

        self.notifier = notifier
        
        # --- Состояние Позиции и Риск-Менеджмента ---
        self.is_paused = False
        self.pause_until = 0
        
        # Счетчики для риск-менеджмента
        self.consecutive_loss_deals = 0          # Кол-во подряд идущих убыточных сделок (> sensitive level)
        self.current_margin_loss_accumulated = 0.0 # Накопленный убыток в USD (для паузы 5%)
        self.total_margin_loss_accumulated = 0.0   # Общий накопленный убыток в USD (для остановки 30%)
        
        self.last_entry_price = 0.0 
        self.last_stop_price = 0.0 
        self.initial_stop_price = 0.0 
        self.is_long_position = False 
        
        # Переменная для отслеживания изменения состояния позиции (для детекции закрытия)
        self._prev_has_position = False

        # --- Флаги для Логики Стопа ---
        self.signal_candle_ohlc: Optional[CandleOHLC] = None 
        self.entry_candle_time: Optional[int] = None      
        self.candles_since_entry = 0 
        self.last_stop_update_price = 0.0

    # ----------------------------------------------------------------------
    # --- 1. ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ---
    # ----------------------------------------------------------------------

    def _get_current_balance(self) -> float:
        """Получает текущий баланс из конфига (заглушка, если нет API баланса)."""
        return self.decision_config['RISK_MANAGEMENT']['CAPITAL']['INITIAL_CAPITAL_USD']

    def _update_candle_count_by_time(self, current_candle_time: int, candle_timeframe_seconds: int, has_position: bool):
        """
        Обновляет счетчик закрытых свечей с момента входа на основе времени.
        """
        if not has_position:
            self.entry_candle_time = None
            self.candles_since_entry = 0
            return

        if self.entry_candle_time is None:
            self.entry_candle_time = current_candle_time
            return

        time_diff = current_candle_time - self.entry_candle_time
        if time_diff < 0: return

        # Количество полностью прошедших таймфреймов
        new_count = int(time_diff / candle_timeframe_seconds)
        
        if new_count != self.candles_since_entry:
            self.logger.debug(f"Счетчик свечей обновлен: {self.candles_since_entry} -> {new_count}")
            self.candles_since_entry = new_count

    def _get_candle_body(self, candle: CandleOHLC) -> float:
        precision = self._get_max_precision(candle)
        candle_body = abs(candle['open'] - candle['close']) if candle else 0.0
        return round(candle_body, precision)

    def _get_candle_color(self, candle: CandleOHLC) -> str:
        return 'Green' if candle['close'] >= candle['open'] else 'Red'

    def _calculate_percentile(self, data: List[float], percentile: int) -> float:
        return np.percentile(data, percentile) if data and len(data) >= 2 else 0.0

    def _get_history_bodies(self, candle_data: List[CandleOHLC], current_idx: int) -> List[float]:
        """Получает список тел свечей за период N ДО указанного индекса."""
        period = self.decision_config['FILTERS']['BODY_VOLATILITY']['BODY_PERCENTILE_PERIOD']
        start_idx = current_idx + 1
        end_idx = start_idx + period
        
        if len(candle_data) < end_idx:
            return []
            
        return [self._get_candle_body(c) for c in candle_data[start_idx:end_idx]]

    def _is_body_in_percentile_range(self, candle_data: List[CandleOHLC], idx: int) -> Tuple[bool, bool, List[float], bool]:
        """
        Проверяет тело свечи на вхождение в диапазон > 5% и < 95%, 
        а также на соответствие BODY_MIN_POINTS.
        """
        bodies = self._get_history_bodies(candle_data, idx)
        if not bodies:
            return False, False, [], False
        
        cfg = self.decision_config['FILTERS']['BODY_VOLATILITY']
        low_pct = cfg['BODY_PERCENTILE_LOWER']   # 5
        high_pct = cfg['BODY_PERCENTILE_UPPER']  # 95
        min_points = cfg['BODY_MIN_POINTS']      # Минимальное тело в пунктах
        
        val_5 = self._calculate_percentile(bodies, low_pct)
        val_95 = self._calculate_percentile(bodies, high_pct)
        
        precision = self._get_max_precision(candle_data[0])
        
        current_body = self._get_candle_body(candle_data[idx])
        min_body = round(min_points * 1 / (10 ** precision), precision)
        
        # 1. Проверка на минимальное значение в пунктах
        is_min_points_valid = current_body >= min_body
        
        # 2. Проверка ТЗ: "больше 5 и меньше 95" для сигнала
        is_percentile_range_valid = (current_body > val_5) and (current_body < val_95)
        
        # Итоговая валидность для сигнала
        is_valid_signal_range = is_min_points_valid and is_percentile_range_valid
        
        # Проверка на аномально большую (для стопов)
        is_large = current_body > val_95
        
        return is_valid_signal_range, is_large, bodies, is_min_points_valid

    # ----------------------------------------------------------------------
    # --- 2. ФИЛЬТРЫ ---
    # ----------------------------------------------------------------------

    def _check_max_body_exception(self, current_body: float, candle_data: List[CandleOHLC]) -> bool:
        """
        Проверяет, попадает ли аномально большая свеча в допустимый диапазон MAX_BODY_RATIO.
        Динамически исключает исторические выбросы, подгружая новые свечи, если они есть в candle_data.
        """
        cfg = self.decision_config['FILTERS']['BODY_VOLATILITY']

        ratio = cfg['MAX_BODY_RATIO']
        period = cfg['BODY_PERCENTILE_PERIOD']
        high_pct = cfg['BODY_PERCENTILE_UPPER']

        precision = self._get_max_precision(candle_data[0])

        # 1. Базовая проверка: нужно минимум period исторических свечей + 1 текущая [0]
        # История лежит в индексах [1]...[period]
        if len(candle_data) < period + 1:
            self.logger.warning("Фильтр MAX BODY: Недостаточно истории для расчета.")
            return False

        # Формируем начальную выборку тел (ровно period штук)
        # range(1, period + 1) берет индексы 1, 2, ..., period
        history_bodies = [self._get_candle_body(candle_data[i]) for i in range(1, period + 1)]
        
        # Указатель на следующую свечу в истории, которую можно взять на замену
        # Сейчас мы взяли до index = period. Следующая доступная = period + 1.
        next_candidate_idx = period + 1
        
        max_history_body = 0.0
        while True:
            if not history_bodies:
                break
            
            local_max = max(history_bodies)
            
            # Если осталась 1 свеча или меньше, сравнивать не с чем, считаем ее базой
            if len(history_bodies) <= 1:
                max_history_body = local_max
                break

            # Формируем выборку БЕЗ текущего максимума (удаляем только ОДНО вхождение)
            # Раньше удалялись все дубликаты максимума, из-за чего он ложно считался выбросом.
            remaining_bodies = history_bodies.copy()
            remaining_bodies.remove(local_max)

            val_95 = self._calculate_percentile(remaining_bodies, high_pct)

            # Проверка: является ли текущий максимум выбросом?
            # Если local_max значительно больше, чем 95% ОСТАЛЬНЫХ свечей
            if local_max > val_95:
                # Проверяем, есть ли свечи на замену
                if next_candidate_idx < len(candle_data):
                    # Удаляем текущий выброс из рабочей выборки
                    history_bodies.remove(local_max)
                    
                    # Добавляем новую свечу из глубины
                    new_body = self._get_candle_body(candle_data[next_candidate_idx])
                    history_bodies.append(new_body)
                    
                    self.logger.info(f"MAX BODY: Исключен выброс {local_max:.{precision}f} (> {val_95:.{precision}f}), взята доп. свеча [{next_candidate_idx}].")
                    
                    # Сдвигаем указатель на следующую возможную замену
                    next_candidate_idx += 1
                    continue
                else:
                    # Истории больше нет, брать неоткуда. Приходится использовать этот максимум.
                    self.logger.info(f"MAX BODY: Обнаружен выброс {local_max:.{precision}f}, но история исчерпана.")
                    max_history_body = local_max
                    break
            else:
                # Максимум вписывается в нормальное распределение
                max_history_body = local_max
                break

        # Расчет итогового лимита
        max_allowed_body = round(max_history_body * ratio, precision)

        if current_body <= max_allowed_body:
            self.logger.info(f"Фильтр MAX BODY: ПРОЙДЕН. Тело {current_body:.{precision}f} <= Max {max_allowed_body:.{precision}f} (Base: {max_history_body:.{precision}f}).")
            return True
        
        self.logger.info(f"Фильтр MAX BODY: ОТКЛОНЕН. Тело {current_body:.{precision}f} > Max {max_allowed_body:.{precision}f} (Base: {max_history_body:.{precision}f}).")
        return False

    def _check_volatility_filter(self, candle_data: List[CandleOHLC], precision: int) -> bool:
        """
        Проверяет волатильность тел свечей.
        idx 0 (Signal): Полная проверка (Min < Body < Max).
        idx 1 (Prev): Только проверка на аномально большой размер (фильтр "тормозного пути").
        """
        cfg = self.decision_config['FILTERS']['BODY_VOLATILITY']
        enable_exception = cfg['ENABLE_MAX_BODY_EXCEPTION']

        # Проходим по очереди: сначала текущая, потом предыдущая
        for idx, label in [(0, "Current"), (1, "Previous")]:
            is_in_range, is_large_anomaly, bodies, is_min_points_valid = self._is_body_in_percentile_range(candle_data, idx)
            curr_body = self._get_candle_body(candle_data[idx])

            if idx == 1:
                # Только если аномально большая (импульс, после которого опасно входить).
                if is_large_anomaly:
                    # Пробуем применить исключение (например, поглощение)
                    if enable_exception and self._check_max_body_exception(curr_body, candle_data):
                         continue
                    else:
                        val_95 = self._calculate_percentile(bodies, cfg['BODY_PERCENTILE_UPPER'])
                        self.logger.info(f"Фильтр Volatility ({label}) ОТКЛОНЕН: Пред. свеча аномально большая ({curr_body:.{precision}f} > {val_95:.{precision}f}).")
                        return False
                continue

            # 1. Проверка минимального размера в пунктах
            if not is_min_points_valid:
                min_body_val = cfg['BODY_MIN_POINTS'] * 1 / (10 ** precision)
                self.logger.info(f"Фильтр Volatility ({label}) ОТКЛОНЕН: Тело {curr_body:.{precision}f} МЕНЬШЕ min {min_body_val:.{precision}f}.")
                return False

            # 2. Проверка на аномально большой размер
            if is_large_anomaly:
                if enable_exception and self._check_max_body_exception(curr_body, candle_data):
                    self.logger.info(f"[{label}] Исключение MAX BODY: Сигнал разрешен.")
                else:
                    val_95 = self._calculate_percentile(bodies, cfg['BODY_PERCENTILE_UPPER'])
                    self.logger.info(f"Фильтр Volatility ({label}) ОТКЛОНЕН: Тело {curr_body:.{precision}f} СЛИШКОМ ВЕЛИКО (> 95%: {val_95:.{precision}f}).")
                    return False
            
            # 3. Проверка на слишком маленький размер (по процентилю)
            # elif not is_in_range:
            #     low_pct = cfg['BODY_PERCENTILE_LOWER']
            #     val_low = self._calculate_percentile(bodies, low_pct)
            #     self.logger.info(f"Фильтр Volatility ({label}) ОТКЛОНЕН: Тело {curr_body:.{precision}f} СЛИШКОМ МАЛО (< {low_pct}%: {val_low:.{precision}f}).")
            #     return False

        return True

    def _check_zz_filter(self, candle_data: List[CandleOHLC], is_long_signal: bool) -> bool:
        """ЗЗ Фильтр."""
        if len(candle_data) < 3: return True
        
        prev_color = self._get_candle_color(candle_data[1])
        prev_prev_color = self._get_candle_color(candle_data[2])
        
        if is_long_signal:
            if prev_color == 'Red':
                self.logger.info("Фильтр Z-Z: ПРОЙДЕН. Пред. свеча [1] Red.")
                return True

            if prev_color == 'Green':
                is_valid_range, is_large_anomaly, _, _ = self._is_body_in_percentile_range(candle_data, 1)
                is_small_anomaly = (not is_valid_range) and (not is_large_anomaly)
                
                if is_small_anomaly and prev_prev_color == 'Red':
                    self.logger.info("Фильтр Z-Z: ПРОЙДЕН. Паттерн Red[2] -> Small Green[1].")
                    return True
                
                reason = "не аномально маленькая" if (is_valid_range or is_large_anomaly) else f"аномально маленькая, но [2] {prev_prev_color}"
                self.logger.info(f"Фильтр Z-Z: ОТКЛОНЕН. Пред. свеча [1] Green {reason}.")
                return False

        return True

    def _check_trend_filter(self, candle_data: List[CandleOHLC]) -> bool:
        """
        Фильтр тренда по углу наклона WMA (Weighted Moving Average).
        Рассчитывает WMA за WMA_PERIOD для последних MA_ANGLE_PERIOD + 1 свечей.
        Строит линию тренда и проверяет, превышает ли угол наклона WMA и положение цены.
        """
        cfg = self.decision_config['FILTERS']['TREND']
        
        wma_period = cfg['WMA_PERIOD']
        angle_period = cfg['WMA_ANGLE_PERIOD']
        min_angle = cfg['WMA_ANGLE_LIMIT']

        # Проверка наличия истории
        required_len = wma_period + angle_period + 1
        if len(candle_data) < required_len:
            self.logger.warning(f"Фильтр TREND: Недостаточно истории. Есть {len(candle_data)}, надо {required_len}.")
            return False

        # Используем numpy массив для удобства вычислений с весами
        closes = np.array([c['close'] for c in candle_data])
        ma_values = []

        # --- ПОДГОТОВКА WMA ---
        # Веса для WMA: от wma_period до 1.
        # closes[0] (новая) имеет вес N, closes[N] (старая) - вес 1.
        weights = np.arange(wma_period, 0, -1)
        sum_weights = weights.sum()

        # Рассчитываем WMA для закрытых свечей
        for i in range(0, angle_period + 1):
            subset = closes[i : i + wma_period]
            
            # Формула WMA: Сумма(Цена * Вес) / Сумма(Весов)
            wma = np.sum(subset * weights) / sum_weights
            ma_values.append(wma)

        # Для корректного расчета угла развернем массив (от старой к новой)
        y_raw = ma_values[::-1]
        x = np.arange(len(y_raw))

        # Превращаем цены в проценты относительно первой точки
        base_price = y_raw[0]
        y_norm = [((val - base_price) / base_price) * 100 for val in y_raw]

        # Считаем slope по нормализованным данным
        slope = np.polyfit(x, y_norm, 1)[0]
        scaled_slope = slope * 20
        
        # Конвертация в градусы
        angle = np.degrees(np.arctan(scaled_slope))
        
        # 1. Проверка угла
        if angle < min_angle:
            self.logger.info(
                f"Фильтр TREND (WMA): ОТКЛОНЕН (Угол). "
                f"Угол {angle:.2f}° < {min_angle:.2f}° "
                f"(Slope: {slope:.4f}%)."
            )
            return False

        # 2. Проверка положения цены (Close > WMA)
        current_wma = ma_values[0]
        current_close = closes[0]

        # if current_close <= current_wma:
        #     self.logger.info(
        #         f"Фильтр TREND (WMA): ОТКЛОНЕН (Позиция цены). "
        #         f"Цена {current_close:.2f} <= WMA {current_wma:.2f}. "
        #         f"(Угол был OK: {angle:.2f}°)."
        #     )
        #     return False
        
        # Если прошли обе проверки
        self.logger.info(
            f"Фильтр TREND (WMA): ПРОЙДЕН. "
            f"Угол {angle:.2f}° >= {min_angle:.2f}°, "
            f"Цена {current_close:.2f} > WMA {current_wma:.2f}."
        )
        return True

    def _check_flat_filter(self, candle_data: List[CandleOHLC]) -> bool:
        """
        Распознавание флэта через анализ распределения средних цен.
        """
        
        cfg = self.decision_config['FILTERS']['FLAT']
        
        N = cfg['FLAT_PERIOD_N']
        K = cfg['FLAT_BALANCE_K']
        USE_SHADOW = cfg['FLAT_USE_SHADOW']
        P = cfg['FLAT_PERCENTILE_P']
        B = cfg['FLAT_BREAKOUT_B']
        
        # 1. Проверка минимальной истории
        # Нам нужно N ЗАКРЫТЫХ свечей. Если N=8, нужны индексы [1]...[8]
        required_len = N + 1 
        if len(candle_data) < required_len:
            self.logger.warning(f"Фильтр FLAT: Недостаточно истории. Есть {len(candle_data)-1}, нужно {N} закрытых свечей.")
            # Возвращаем False и "inside" по умолчанию
            return False, "inside", None, None, None, None, None, None

        # 2. Извлечение данных для анализа N ЗАКРЫТЫХ свечей (индексы [1] по [N] включительно)
        # Срезы numpy берут данные с конца, но наш candle_data отсортирован от новой к старой.
        # Поэтому берем срез [1 : N + 1]
        history = candle_data[1 : N + 1] 
        
        # Превращаем данные в numpy массивы, чтобы быстро считать процентили
        closes = np.array([c['close'] for c in history])
        highs = np.array([c['high'] for c in history])
        lows = np.array([c['low'] for c in history])
        opens = np.array([c['open'] for c in history])
        
        last_close = candle_data[0]['close'] # Цена закрытия (текущая цена)

        # 3. Расчет Средних Цен (Mean Prices) для истории
        mean_prices = []
        for i in range(N):
            if USE_SHADOW:
                # Используем (High + Low) / 2
                mean_prices.append((highs[i] + lows[i]) / 2)
            else:
                # Используем (Open + Close) / 2
                mean_prices.append((opens[i] + closes[i]) / 2)

        median_price = np.median(mean_prices)

        # 4. Границы Боковика (Flat Range)
        # Используем N-1 свечу (история без самой новой закрытой свечи, т.е. без closes[0] в нашем history)
        # В нашем случае, т.к. мы уже взяли срез [1:N+1], то N свечей.
        # Оригинальный код использовал slice [-n:-1] (без последней свечи в срезе).
        # Для нашего массива history (N свечей), это означает, что нам нужно использовать N-1 свечу.
        
        history_minus_one = history[1:] # Свечи [2] по [N]
        
        # Снова формируем массивы для N-1 свечи
        closes_m1 = np.array([c['close'] for c in history_minus_one])
        highs_m1 = np.array([c['high'] for c in history_minus_one])
        lows_m1 = np.array([c['low'] for c in history_minus_one])
        opens_m1 = np.array([c['open'] for c in history_minus_one])
        
        if len(history_minus_one) < 1:
            # Невозможно рассчитать процентили по N-1 свече
            return False, "inside", median_price, None, None, None, None, None

        if USE_SHADOW:
            flat_min = np.percentile(lows_m1, P)
            flat_max = np.percentile(highs_m1, 100 - P)
        else:
            flat_min = np.percentile(np.minimum(opens_m1, closes_m1), P)
            flat_max = np.percentile(np.maximum(opens_m1, closes_m1), 100 - P)


        # 5. Анализ распределения средних цен (на баланс)
        # Делим период N на две части: Старая история (N/2) и Новая история (N/2)
        # В нашем массиве mean_prices (N свечей), индексы 0 до N/2-1 - самая новая часть.
        # Индексы N/2 до N-1 - самая старая часть.
        
        # Counts[0]: Новая часть (N/2), Цена ниже медианы
        # Counts[1]: Новая часть (N/2), Цена выше медианы
        # Counts[2]: Старая часть (N/2), Цена выше медианы
        # Counts[3]: Старая часть (N/2), Цена ниже медианы
        counts = [0, 0, 0, 0]
        half_N = N // 2
        
        for i, mp in enumerate(mean_prices):
            if mp == median_price:
                continue
            
            # Новая история (i < N/2):
            if i < half_N:
                if mp > median_price:
                    counts[1] += 1
                else: # mp < median_price
                    counts[0] += 1
            # Старая история (i >= N/2):
            else:
                if mp > median_price:
                    counts[2] += 1
                else: # mp < median_price
                    counts[3] += 1

        # Коэффициент баланса для Старой истории (k_34 - ключевой для флэта)
        max_34 = max(counts[2], counts[3])
        k_34 = min(counts[2], counts[3]) / max_34 if max_34 > 0 else 0
        
        # k_12 не используется в финальном результате, но оставлен для полноты:
        # max_12 = max(counts[0], counts[1])
        # k_12 = min(counts[0], counts[1]) / max_12 if max_12 > 0 else 0
        # k_n = k_12 * k_34 # (Не используется)


        # 6. Проверка на Breakout в истории (без текущей свечи)
        
        # Используем весь history (N свечей)
        if USE_SHADOW:
            lows_check = lows < flat_min
            highs_check = highs > flat_max
        else:
            lows_bodies = np.minimum(opens, closes)
            highs_bodies = np.maximum(opens, closes)
            lows_check = lows_bodies < flat_min
            highs_check = highs_bodies > flat_max

        # Считаем, сколько свечей вышло за границы
        breakout_count = (lows_check | highs_check).sum()
        breakout = breakout_count > B
        

        # 7. Направление выхода текущей свечи (на основе candle_data[0]['close'])
        if last_close > flat_max:
            direction = "up"
        elif last_close < flat_min:
            direction = "down"
        else:
            direction = "inside"

        # 8. Финальный результат
        
        # Флэт: K_34 > K (старая часть истории сбалансирована) 
        #       AND direction == "inside" (текущая цена не вышла)
        #       AND not breakout (нет сильного пробоя в истории)
        
        is_flat = (k_34 > K) and (direction == "inside") and (not breakout)

        if is_flat:
            self.logger.info(f"Фильтр FLAT: ОТКЛОНЕН. K_34={k_34:.2f} > {K} | Dir={direction} | Breakout={breakout_count} < {B}.")
        else:
            self.logger.info(f"Фильтр FLAT: ПРОЙДЕН. K_34={k_34:.2f}, Dir={direction}, Breakout={breakout_count}. Требуется K_34 > {K}, Dir='inside', Breakout < {B}.")

        return not is_flat

    # ----------------------------------------------------------------------
    # --- 3. ЛОГИКА СТОПОВ (По ТЗ) ---
    # ----------------------------------------------------------------------

    def _calculate_initial_stop(self, is_long: bool, signal_candle: CandleOHLC, precision: int) -> float:
        """Начальный стоп на 1 пункт ниже минимума."""
        tick = 1 / (10 ** precision)
        if is_long:
            return round(signal_candle['low'] - tick, precision)
        else:
            return round(signal_candle['high'] + tick, precision)

    def _apply_stop_logic_long(self, candle_data: List[CandleOHLC], current_stop: float, current_price: float, precision: int) -> Tuple[Optional[float], str]:
        """Реализация логики ТЗ для Лонга."""
        self.logger.info(f"--- STOP LOSS: n_candles={self.candles_since_entry}, SL={current_stop:.{precision}f}, CP={current_price:.{precision}f} ---")
        
        cfg_stop = self.decision_config['STOP_MANAGEMENT']
        
        tick = 1 / (10 ** precision)
        min_move_points = cfg_stop['MIN_STOP_MOVE_POINTS'] * tick
        multiplier = cfg_stop['ADJACENT_CANDLE_BODY_MULTIPLIER']
        
        # Параметры Trailing Stop
        ts_acc_dist_points = cfg_stop['TS_ACC_DIST_POINTS']
        ts_move_multiplier = cfg_stop['TS_MOVE_MULTIPLIER']
        ts_acc_dist_currency = ts_acc_dist_points * tick
        
        new_stop = current_stop
        reason = ""
        
        current_candle = candle_data[0] 
        current_body = self._get_candle_body(current_candle)
        current_color = self._get_candle_color(current_candle)
        signal_body = self._get_candle_body(self.signal_candle_ohlc)
        
        _, is_current_large_anomaly, hist_bodies, is_min_points_valid = self._is_body_in_percentile_range(candle_data, 0)
        
        # --- 1. СЛЕДУЮЩАЯ ЗА СИГНАЛЬНОЙ (Свеча +1) ---
        if self.candles_since_entry == 1:
            if current_color == 'Green':
                if current_body >= signal_body:
                    if current_candle['open'] > new_stop:
                        new_stop = current_candle['open']
                        reason = "+1 Green > Signal"
                    else:
                        self.logger.info(f"STOP LOGIC [+1 Green]: Open ({current_candle['open']:.{precision}f}) <= Current Stop ({new_stop:.{precision}f}). Skip.")
                else:
                    self.logger.info(f"STOP LOGIC [+1 Green]: Current Body ({current_body:.{precision}f}) <= Signal Body ({signal_body:.{precision}f}). Skip.")

                if is_current_large_anomaly:
                    calc = round(current_candle['open'] + (current_body * multiplier), precision)
                    if calc > new_stop:
                        new_stop = calc
                        reason = "+1 Green > 95%"
                    else:
                        self.logger.info(f"STOP LOGIC [+1 Green Anomaly]: Calc ({calc:.{precision}f}) <= Current Stop ({new_stop:.{precision}f}). Skip.")
            
            elif current_color == 'Red':
                # Если текущая цена ниже цены входа, используем текущую цену как ориентир для "минимума"
                base_low = current_candle['low']
                if current_price < self.last_entry_price:
                    base_low = round(min(base_low, current_price), precision)
                    self.logger.info(f"STOP LOGIC [+1 Red]: Dynamic Low logic applied. Base Low={base_low:.{precision}f}")

                calc = round((current_stop + base_low) / 2, precision)
                if calc > new_stop:
                    new_stop = calc
                    reason = "+1 Red Mid"
                else:
                    self.logger.info(f"STOP LOGIC [+1 Red]: Calc Mid ({calc:.{precision}f}) <= Current Stop ({new_stop:.{precision}f}). Skip.")
                    
        # --- 3 & 4. СВЕЧИ ИДУЩИЕ ДАЛЕЕ (Свеча +2 и более) ---
        elif self.candles_since_entry >= 2:
            prev_candle = candle_data[1]
            prev_body = self._get_candle_body(prev_candle)
            
            if current_color == 'Green':
                cfg_filter = self.decision_config['FILTERS']['BODY_VOLATILITY']
                pct_5 = self._calculate_percentile(hist_bodies, cfg_filter['BODY_PERCENTILE_LOWER'])
                
                if not is_min_points_valid or current_body < pct_5:
                    self.logger.info(f"STOP LOGIC [+2+ Green]: Skip (Small candle). MinPtsValid={is_min_points_valid}, Body={current_body:.{precision}f} < 5%({pct_5}).")
                else:
                    if current_body > prev_body and self._get_candle_color(prev_candle) == 'Green':
                        if prev_candle['open'] > new_stop:
                            new_stop = prev_candle['open']
                            reason = "+2+ > Prev Green"
                        else:
                            self.logger.info(f"STOP LOGIC [+2+ Green > Prev]: Prev Open ({prev_candle['open']:.{precision}f}) <= SL ({new_stop:.{precision}f}).")
                    
                    pct_95_hist = self._calculate_percentile(hist_bodies, cfg_filter['BODY_PERCENTILE_UPPER'])
                    if (current_price - current_stop) > pct_95_hist:
                        calc = round(min(prev_candle['open'], prev_candle['close']), precision)
                        if calc > new_stop:
                            new_stop = calc
                            reason = "+2+ Dist > 95%"
                        else:
                            self.logger.info(f"STOP LOGIC [+2+ Dist 95%]: Calc Min ({calc:.{precision}f}) <= SL ({new_stop:.{precision}f}).")
                    
                    if is_current_large_anomaly:
                        calc = round(current_candle['open'] + (current_body * multiplier), precision)
                        if calc > new_stop:
                            new_stop = calc
                            reason = "+2+ Green > 95%"
                        else:
                            self.logger.info(f"STOP LOGIC [+2+ Anomaly]: Calc ({calc:.{precision}f}) <= SL ({new_stop:.{precision}f}).")

            elif current_color == 'Red':
                decline = round(current_candle['open'] - current_price, precision)
                if decline > 0:
                    calc = current_stop + decline
                    limit = current_price - min_move_points
                    final = round(min(calc, limit), precision)
                    
                    if final > new_stop:
                        new_stop = final
                        reason = f"+2+ Red Correction (+{decline:.{precision}f})"
                    else:
                        self.logger.info(f"STOP LOGIC [+2+ Red]: Final ({final:.{precision}f}) <= SL ({new_stop:.{precision}f}). Decline={decline:.{precision}f}.")
                else:
                    self.logger.info(f"STOP LOGIC [+2+ Red]: No decline (Open {current_candle['open']:.{precision}f} <= Price {current_price:.{precision}f}).")
        
        # ОБЩИЕ ПРАВИЛА (Применяем, если результат лучше)
        final_stop = new_stop
        final_reason = reason
        
        distance_from_stop = current_price - current_stop
        
        # Накопленное расстояние превышает порог
        if distance_from_stop >= ts_acc_dist_currency:
            ts_move_amount = round(ts_acc_dist_currency * ts_move_multiplier, precision)
            calc_ts_stop = current_stop + ts_move_amount
            
            if calc_ts_stop > final_stop:
                self.logger.info(f"STOP LOGIC [TS]: Move {ts_move_amount:.{precision}f} (Dist {distance_from_stop:.{precision}f} >= Thr {ts_acc_dist_currency:.{precision}f}).")
                final_stop = calc_ts_stop
                final_reason = "Gen: Trailing Stop"

        profit = round(current_price - self.last_entry_price, precision)
        req_profit = round(signal_body * cfg_stop['BREAKEVEN_PROFIT_RATIO'], precision)
        
        if profit >= req_profit:
            if self.last_entry_price > final_stop:
                final_stop = self.last_entry_price
                final_reason = "Gen: Breakeven"
        else:
             if final_stop == new_stop: # Логируем только если не сработали другие правила, чтобы не спамить
                 self.logger.info(f"STOP LOGIC [Gen BE]: Profit {profit:.{precision}f} < Req {req_profit:.{precision}f}.")

        if self.candles_since_entry >= cfg_stop['BREAKEVEN_IDLE_CANDLES']:
            if current_price > self.last_entry_price:
                 calc = self.last_entry_price 
                 suf = "Breakeven"
            else:
                 calc = round((current_stop + current_price) / 2, precision)
                 suf = "Mid(Stop, Price)"
            
            if calc > final_stop:
                final_stop = calc
                final_reason = f"Gen: Timeout {cfg_stop['BREAKEVEN_IDLE_CANDLES']} ({suf})"
            else:
                self.logger.info(f"STOP LOGIC [Gen Timeout]: Calc ({calc:.{precision}f}) <= Final ({final_stop:.{precision}f}).")

        # --- SAFETY GAP ---
        final_stop = round(final_stop, precision)

        if final_stop > current_stop:
            safety_gap = self.decision_config['FILTERS']['BODY_VOLATILITY']['BODY_MIN_POINTS'] * tick
            
            if safety_gap < min_move_points:
                safety_gap = min_move_points

            max_safe_stop = current_price - safety_gap
            
            if final_stop > max_safe_stop:
                final_stop_capped = round(max_safe_stop, precision)
                
                if final_stop_capped > current_stop:
                    self.logger.warning(f"⚠️ Стоп ограничен безопасным зазором: {final_stop:.{precision}f} -> {final_stop_capped:.{precision}f}")
                    return final_stop_capped, final_reason + " (Ограничен)"
                else:
                    self.logger.info(f"⚠️ Новый стоп ПРОПУЩЕН, он меньше безопасного зазора: Новый {final_stop:.{precision}f} vs Безопасный {final_stop_capped:.{precision}f}")
                    return None, ""

            return final_stop, final_reason

        elif final_stop and final_stop < current_stop:
            self.logger.warning(f"⚠️ Новый стоп ({final_stop:.{precision}f}) хуже текущего ({current_stop:.{precision}f}) — ПРОПУЩЕН.")

        return None, ""

    # ----------------------------------------------------------------------
    # --- 4. РИСК-МЕНЕДЖМЕНТ ---
    # ----------------------------------------------------------------------

    def _check_risk_pause(self) -> bool:
        """Проверяет, находится ли бот на паузе, и управляет выходом из нее."""
        if self.is_paused:
            if time.time() < self.pause_until:
                # Пауза еще действует
                return True
            else:
                # Пауза закончилась
                self.is_paused = False
                self.pause_until = 0
                
                # Сбрасываем счетчики серии и накопленного убытка серии
                self.consecutive_loss_deals = 0
                self.current_margin_loss_accumulated = 0.0
                
                msg = f"🟢 <b>РИСК-МЕНЕДЖМЕНТ:</b>\nПауза завершена. Торговля возобновлена."
                self.logger.info(msg.replace("<b>", "").replace("</b>", ""))
                self.notifier.notify(msg)
                return False
        return False

    def _register_loss(self, exit_price: float):
        """Регистрирует убыток и проверяет лимиты."""
        cfg_risk = self.decision_config['RISK_MANAGEMENT']
        loss_limits = cfg_risk['LOSS_LIMITS']
        capital = cfg_risk['CAPITAL']
        
        # 1. Расчет убытка от начального стопа
        entry = self.last_entry_price
        initial_stop = self.initial_stop_price
        
        # Полный риск в сделке (дистанция до стопа)
        risk_distance = abs(entry - initial_stop)
        
        # Фактический убыток в пунктах (положительное число при убытке)
        actual_loss_points = 0.0
        if self.is_long_position:
            if exit_price < entry:
                actual_loss_points = entry - exit_price
        else:
            # Short (заглушка, но логика зеркальная)
            if exit_price > entry:
                actual_loss_points = exit_price - entry
                
        if actual_loss_points <= 0:
            # Это прибыль или безубыток, сбрасываем серию
            if self.consecutive_loss_deals > 0:
                self.logger.info(f"Сделка прибыльная/БУ. Сброс серии убытков (было {self.consecutive_loss_deals}).")
            self.consecutive_loss_deals = 0
            self.current_margin_loss_accumulated = 0.0
            return

        # 2. Проверка чувствительности (доля от стопа)
        sensitive_threshold = risk_distance * loss_limits['INITIAL_STOP_SENSITIVE_LEVEL']
        
        if actual_loss_points > sensitive_threshold:
            # Убыток значительный, учитываем его
            self.consecutive_loss_deals += 1
            
            # Расчет денежного убытка
            lot_usd = self._calculate_lot_size()
            loss_usd = (actual_loss_points / entry) * lot_usd
            
            self.current_margin_loss_accumulated += loss_usd
            self.total_margin_loss_accumulated += loss_usd
            
            self.logger.warning(
                f"🔴 УБЫТОК #{self.consecutive_loss_deals}: -{loss_usd:.2f}$ ({actual_loss_points} pts). "
                f"Накоплено (серия): {self.current_margin_loss_accumulated:.2f}$. "
                f"Всего (HALT): {self.total_margin_loss_accumulated:.2f}$"
            )
            
            # 3. Проверка условий для ПАУЗЫ
            # А. По количеству сделок
            is_limit_count = self.consecutive_loss_deals >= loss_limits['MAX_CONSECUTIVE_LOSS_DEALS']
            
            # Б. По сумме убытка
            fixed_margin = self.decision_config['RISK_MANAGEMENT']['POSITION_SIZING']['FIXED_MARGIN_USD']
            pause_loss_limit = capital['MARGIN_LOSS_LIMIT_PAUSE'] * fixed_margin
            is_limit_sum = self.current_margin_loss_accumulated >= pause_loss_limit
            
            if is_limit_count or is_limit_sum:
                reason = "Макс. кол-во убытков" if is_limit_count else "Лимит убытка серии (5%)"
                pause_min = loss_limits['BREAK_DURATION_SHORT_MINUTES']
                self.pause_until = time.time() + (pause_min * 60)
                self.is_paused = True
                
                msg = (f"🛑 <b>РИСК-ПАУЗА ({pause_min} мин):</b>\n"
                       f"Причина: {reason}.\n"
                       f"Серия: {self.consecutive_loss_deals} сд., Убыток: {self.current_margin_loss_accumulated:.2f}$")
                self.logger.warning(msg.replace("<b>", "").replace("</b>", ""))
                self.notifier.notify(msg)
                
            # 4. Проверка условий для ОСТАНОВКИ (HALT)
            halt_loss_limit = capital['MARGIN_LOSS_LIMIT_HALT'] * fixed_margin
            
            if self.total_margin_loss_accumulated >= halt_loss_limit:
                msg = (f"⛔ <b>КРИТИЧЕСКАЯ ОСТАНОВКА БОТА:</b>\n"
                       f"Достигнут лимит общего убытка {halt_loss_limit}%.\n"
                       f"Всего убыток: {self.total_margin_loss_accumulated:.2f}$ >= {halt_loss_limit:.2f}$")
                self.logger.critical(msg.replace("<b>", "").replace("</b>", ""))
                self.notifier.notify(msg)
                raise SystemExit("Risk Manager: MARGIN_LOSS_LIMIT_HALT reached.")
        else:
            self.logger.info(f"Убыток незначительный (< {loss_limits['INITIAL_STOP_SENSITIVE_LEVEL']*100}% от стопа). Не учитываем в серии.")

    # ----------------------------------------------------------------------
    # --- 5. ОСНОВНОЙ МЕТОД ---
    # ----------------------------------------------------------------------

    def check_signal_and_risk(self, has_position: bool, candle_data: List[CandleOHLC], time_to_close: float) -> Tuple[bool, float, Optional[float]]:
        """
        Главный метод для кликера.
        """
        # 0. Проверка паузы риск-менеджмента
        if self._check_risk_pause():
            return False, 0.0, None

        current_price = candle_data[0]['close']

        # Вычисляем таймфрейм
        t0 = candle_data[0]['time']
        t1 = candle_data[1]['time']
        tf_sec = int(abs(t0 - t1)) if t0 != t1 else 60 

        precision = self._get_max_precision(candle_data[0])
        
        # Обновляем счетчик свечей
        self._update_candle_count_by_time(int(t0), tf_sec, has_position)

        # Мы используем последнюю известную цену стопа для расчета убытка,
        # так как это наиболее точное представление цены выхода по SL.
        if self._prev_has_position and not has_position:
            # Позиция закрылась
            exit_price = self.last_stop_price if self.last_stop_price > 0 else current_price
            self.logger.info(f"Позиция закрыта. Цена выхода (Last SL) ~{exit_price}. Расчет рисков...")
            
            self._register_loss(exit_price)
            
            # Сброс состояния сделки
            self.last_entry_price = 0.0
            self.last_stop_price = 0.0
            self.initial_stop_price = 0.0
            self.is_long_position = False
            self.signal_candle_ohlc = None
            self.entry_candle_time = None
            self.candles_since_entry = 0
            
        self._prev_has_position = has_position

        # ==========================================
        # А. ЛОГИКА СТОПОВ (Если позиция есть)
        # ==========================================
        if has_position:
            stop_offset = self.decision_config['STOP_MANAGEMENT']['TRAILING_UPDATE_TIME_OFFSET_SEC']
            
            # Движение стопа навстречу цене, если цена ушла ниже входа (для лонга)
            is_urgent_move = (
                self.is_long_position and 
                current_price < self.last_entry_price and
                self.last_stop_price < self.last_entry_price # Стоп еще не в безубытке
            )
            
            if not is_urgent_move and time_to_close > stop_offset:
                return False, 0.0, None
            
            new_stop = None
            reason = ""
            
            if self.is_long_position:
                new_stop, reason = self._apply_stop_logic_long(candle_data, self.last_stop_price, current_price, precision)
            else:
                pass # Short пока нет
            
            if new_stop:
                self.logger.info(f"♻️ UPDATE STOP: {self.last_stop_price:.{precision}f} -> {new_stop:.{precision}f} | {reason}")
                self.last_stop_price = new_stop
                return False, 0.0, new_stop
            
            return False, 0.0, None

        # ==========================================
        # Б. ЛОГИКА СИГНАЛОВ (Если позиции нет)
        # ==========================================
        
        # 1. Фильтр Времени
        entry_offset = self.decision_config['SIGNAL_MODULE']['ENTRY_TIME_OFFSET_SEC']
        if time_to_close > entry_offset:
            return False, 0.0, None

        # 2. Базовая логика сигнала (Лонг)
        signal_candle = candle_data[0]
        is_green = self._get_candle_color(signal_candle) == 'Green'
        
        if not is_green:
            return False, 0.0, None

        # 3. Проверка процентилей
        if not self._check_volatility_filter(candle_data, precision):
            return False, 0.0, None

        # 4. ZZ Фильтр
        if not self._check_zz_filter(candle_data, is_green):
            return False, 0.0, None

        # 5. Trend Filter (MA Angle)
        if not self._check_trend_filter(candle_data):
            return False, 0.0, None

        # 6. Flat Filter (Distribution)
        if not self._check_flat_filter(candle_data):
            return False, 0.0, None

        # СИГНАЛ ПОДТВЕРЖДЕН
        lot = self._calculate_lot_size()
        sl = self._calculate_initial_stop(True, signal_candle, precision)
        
        self.is_long_position = True
        self.last_entry_price = current_price
        self.last_stop_price = sl
        self.initial_stop_price = sl
        self.signal_candle_ohlc = signal_candle
        self.entry_candle_time = int(t0)
        self.candles_since_entry = 0
        
        signal_message = f"🚀 <b>СИГНАЛ MARKET LONG:</b>\nCP {current_price:.{precision}f}, SL {sl:.{precision}f}"
        
        self.logger.info(signal_message.replace('<b>', '').replace('</b>', '').replace('\n', ' '))
        self.notifier.notify(signal_message)
        
        return True, lot, sl

    def _calculate_lot_size(self) -> float:
        margin = self.decision_config['RISK_MANAGEMENT']['POSITION_SIZING']['FIXED_MARGIN_USD']
        leverage = self.decision_config['RISK_MANAGEMENT']['POSITION_SIZING']['LEVERAGE_FACTOR']
        return margin * leverage