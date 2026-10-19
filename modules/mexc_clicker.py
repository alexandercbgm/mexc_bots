import pyautogui
import logging
import time
import re
import os
from pathlib import Path
from datetime import datetime

from typing import List, Dict

from modules.config_base import BaseConfigLoader 
from modules.notifier import Notifier
from modules.decision_maker import DecisionMaker
from modules.mexc_api_client import MexcApiClient, EMPTY_CANDLE_DICT
from modules.utils import TradingUtils


class MexcClicker(BaseConfigLoader, TradingUtils):
    """Основной класс, управляющий действиями в интерфейсе MEXC."""
    
    def __init__(self, logger: logging.Logger, notifier: Notifier, decision_maker: DecisionMaker):
        super().__init__(logger) 
        self.logger = logger.getChild('MexcClicker')
        
        self.notifier = notifier
        self.decision_maker = decision_maker
        
        # Состояние бота - изначально позиций нет
        self.has_position = False 
        
        # Флаг, указывающий, было ли уже выполнено предварительное заполнение стопа
        # Это нужно, чтобы вводить стоп только один раз при первом проходе цикла, когда нет позиции.
        self.initial_stop_filled = False 
        
        # Точность цены торгуемой монеты symbol_price_precision
        self.spp = None
        
        # Параметры из конфига SETTINGS
        settings = self.mexc_clicker_config.get('SETTINGS', {})
        
        self.NUM_CANDLES_TO_FETCH = settings.get('NUM_CANDLES_TO_FETCH', 11) 
        candle_tf_minutes = settings.get('CANDLE_TIMEFRAME_MINUTES', 1)
        # Длительность свечи в секундах, используем для расчета времени до закрытия
        self.CANDLE_PERIOD_SECONDS = candle_tf_minutes * 60 
        
        # Параметры предобработки (для не-ценового OCR)
        self.SCALE_FACTOR = self.mexc_clicker_config.get('IMAGE_PROCESSING', {}).get('SCALE_FACTOR', 2.0)
        self.BLUR_KERNEL_SIZE = tuple(self.mexc_clicker_config.get('IMAGE_PROCESSING', {}).get('BLUR_KERNEL_SIZE', (5, 5)))
        
        # Инициализация API клиента
        self.api_client = MexcApiClient(self.logger, self.mexc_clicker_config)
        
        # Контейнер для свечей
        self.candle_data: List[Dict[str, float]] = [EMPTY_CANDLE_DICT.copy() for _ in range(self.NUM_CANDLES_TO_FETCH)]
        
        self.logger.info(f"Контейнер свечей: {self.NUM_CANDLES_TO_FETCH} свечей (Live API).")
        self.notifier.notify("🚀 <b>Бот-кликер MEXC запущен.</b>")

    # --- 1. ПРИВАТНЫЕ МЕТОДЫ: ДЕЙСТВИЯ ---

    def _click_element(self, coords: list, description: str, delay: float = 0.5):
        """Выполняет клик по заданным координатам с настраиваемой задержкой."""
        if len(coords) != 2:
            self.logger.error(f"КЛИКЕР: Неверный формат координат для {description}: {coords}")
            return
        
        x, y = coords
        self.logger.info(f"КЛИКЕР: Нажатие: '{description}' в ({x}, {y}).")
        pyautogui.click(x, y)
        time.sleep(delay)

    def _type_text(self, coords: list, text: str, description: str, delay: float = 0.5, clean: bool = False):
        """Выполняет клик и ввод текста по заданным координатам с настраиваемой задержкой."""
        if len(coords) != 2:
            self.logger.error(f"КЛИКЕР: Неверный формат координат для {description}: {coords}")
            return
        
        x, y = coords
        self.logger.info(f"КЛИКЕР: Ввод: '{text}' в поле '{description}' в ({x}, {y}). Clean={clean}.")
        
        pyautogui.click(x, y)
        time.sleep(0.5)

        if clean:
            pyautogui.press('end')
            time.sleep(0.2)
            
            for char in str(text):
                pyautogui.press('backspace')
                time.sleep(0.1)
            time.sleep(0.2)
        
        for char in str(text):
            if char == '.':
                pyautogui.press('decimal')
            else:
                pyautogui.press(char)
        time.sleep(delay) 
    
    # --- 2. ПРИВАТНЫЕ МЕТОДЫ: OCR (РАСПОЗНАВАНИЕ) ---
    
    def _preprocess_image(self, region, ocr_config):
        # Импорт библиотек внутри метода, чтобы избежать их импорта, если не используется OCR
        import cv2 
        import numpy as np 
        import pytesseract
        
        OCR_DIR = "ocr"
        Path(OCR_DIR).mkdir(exist_ok=True)
        
        img_filename = f"ocr_image_{int(time.time())}.png"
        img_path = Path(OCR_DIR) / img_filename

        screenshot_pil = pyautogui.screenshot(region=region)
        img_cv = np.array(screenshot_pil)
        img_cv = cv2.cvtColor(img_cv, cv2.COLOR_RGB2BGR) 
        
        h, w, _ = img_cv.shape
        img_upscaled = cv2.resize(img_cv, (int(w * self.SCALE_FACTOR), int(h * self.SCALE_FACTOR)), interpolation=cv2.INTER_LINEAR)
        gray = cv2.cvtColor(img_upscaled, cv2.COLOR_BGR2GRAY)
        gray_blurred = cv2.GaussianBlur(gray, self.BLUR_KERNEL_SIZE, 0)
        
        inverted = cv2.bitwise_not(gray_blurred)
        _, binary = cv2.threshold(inverted, 100, 255, cv2.THRESH_BINARY) 
        
        cv2.imwrite(str(img_path), binary)
        
        raw_text = pytesseract.image_to_string(str(img_path), config=ocr_config).strip()
        os.remove(img_path)
        
        return raw_text

    def _recognize_positions_count(self) -> int:
        """
        (OCR) Проверяет наличие открытой позиции по заголовку Positions(N).
        Возвращает количество позиций (int) или -1, если OCR не смог распознать текст.
        """
        POSITIONS_HEADER_REGION = tuple(self.mexc_clicker_config.get('OCR', {}).get('POSITIONS_HEADER_REGION'))
        POSITIONS_OCR_CONFIG = self.mexc_clicker_config.get('OCR', {}).get('POSITIONS_OCR_CONFIG')
        
        try:
            raw_text = self._preprocess_image(POSITIONS_HEADER_REGION, POSITIONS_OCR_CONFIG)
            
            self.logger.info(f"КЛИКЕР: Raw OCR 'Positions': '{raw_text}'")

            # Если OCR ничего не распознал (пустая строка), возвращаем -1
            if not raw_text:
                self.logger.warning("КЛИКЕР: OCR вернул пустую строку. Невозможно определить количество позиций.")
                return -1 # Специальное значение для "неизвестно"

            match = re.search(r'\((\d+)\)', raw_text)
            if match:
                count = int(match.group(1))
                return count
            
            # Если "positions" найдено, но без числа в скобках, считаем, что 1 позиция
            if "positions" in raw_text.lower():
                return 1 
            
            # Если не найдено ни числа, ни слова "positions", возвращаем 0
            return 0 

        except Exception as e:
            self.logger.error(f"КЛИКЕР: Ошибка OCR 'Positions Count': {e}")
            return -1 # В случае ошибки OCR также возвращаем -1

    def _recognize_suspicious_activity_popup(self) -> bool:
        """
        (ЗАГЛУШКА) Проверка наличия окна об обнаружении подозрительной активности.
        """
        return False
    
    # --- 3. ПРИВАТНЫЕ МЕТОДЫ: ЛОГИКА ДЕЙСТВИЙ ---
    
    def _handle_stop_order_confirmation(self, delay: float = 0.5):
        """
        Обрабатывает pop-up подтверждение о стоп-ордере (нажимает Confirm).
        """
        coords = self.mexc_clicker_config['POPUP_CONFIRMATION']['BUTTON_CONFIRM_COORDS']
        self._click_element(coords, "Pop-up Confirm Button", delay=delay)

    def _page_scroll_up(self, scroll_count: int = 1, delay: float = 0.5):
        """Прокрутка страницы вверх."""
        
        # Позиционируем курсор мыши на область, где работает прокрутка нужной области.
        cancel_form_coords = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['BUTTON_CANCEL']['coords']
        cancel_form_desc = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['BUTTON_CANCEL']['description']
        self._click_element(cancel_form_coords, cancel_form_desc, delay=delay/2)
        
        # Прокручиваем колесико мыши вверх.
        self.logger.info(f"КЛИКЕР: Выполняем прокрутку колесика мыши {scroll_count} раз вверх.")
        pyautogui.scroll(scroll_count)
        time.sleep(delay/2)

    def _execute_open_position(self, lot_size: float, target_stop_price: float):
        """
        Логика открытия позиции с оптимизированными задержками.
        После успешного открытия сразу переставляет стоп на target_stop_price.
        """
        self.logger.warning(f"-> СИГНАЛ НА ОТКРЫТИЕ: Лот: {lot_size}, Целевой расчетный стоп: {target_stop_price:.{self.spp}f}")
        
        # а). Нажимаем кнопку купить (Стоп в % уже был введен на этапе пре-заполнения)
        buy_coords = self.mexc_clicker_config['TRADE_PANEL']['BUTTON_BUY_LONG']['coords']
        buy_desc = self.mexc_clicker_config['TRADE_PANEL']['BUTTON_BUY_LONG']['description']
        self._click_element(buy_coords, buy_desc, delay=0.5)
        
        # б). Нажимаем Confirm для подтверждения ордера.
        self._handle_stop_order_confirmation(delay=0.5) 
        
        # --- КРИТИЧЕСКАЯ ПРОВЕРКА ФАКТИЧЕСКОГО ОТКРЫТИЯ ПОЗИЦИИ ЧЕРЕЗ OCR ---
        # Даем системе время на обработку ордера и обновление интерфейса
        time.sleep(2) 
        positions_count = self._recognize_positions_count()
        
        if positions_count == 1:
            # Успех: позиция открыта
            open_pos_message = f"✅ <b>КЛИКЕР:</b> Открытие позиции успешно подтверждено."
            
            self.logger.warning(open_pos_message.replace('<b>', '').replace('</b>', ''))
            self.notifier.notify(open_pos_message)
            
            self.has_position = True # <--- Флаг установлен в True
            
            # --- Сразу двигаем стоп на расчетный ---
            self._execute_move_stop(target_stop_price)
            
        else:
            # Если позиция не открыта, то отправляем сообщение и продолжаем
            warning_open_pos_message = (
                f"❌ <b>КЛИКЕР:</b> Открытие позиции не подтверждено.\n"
                f"OCR показал Positions({positions_count})."
            )

            self.logger.warning(warning_open_pos_message.replace('<b>', '').replace('</b>', '').replace('\n', ' '))
            self.notifier.notify(warning_open_pos_message)

    def _execute_move_stop(self, new_stop_price: float):
        """Логика движения стопа с оптимизированными задержками."""
        
        # а). Нажимаем значок редактировать стоп
        edit_icon_coords = self.mexc_clicker_config['TP_SL_ADD_FORM_1']['ICON_ADD_STOP']['coords']
        edit_icon_desc = self.mexc_clicker_config['TP_SL_ADD_FORM_1']['ICON_ADD_STOP']['description']
        self._click_element(edit_icon_coords, edit_icon_desc, delay=0.75)
        
        # б). В форме со списком стопов нажимаем на значок редактирования у первого стопа
        edit_stop_coords = self.mexc_clicker_config['TP_SL_ADD_FORM_1']['ICON_EDIT_FIRST_STOP']['coords']
        edit_stop_desc = self.mexc_clicker_config['TP_SL_ADD_FORM_1']['ICON_EDIT_FIRST_STOP']['description']
        self._click_element(edit_stop_coords, edit_stop_desc, delay=0.75)
        
        # в). В форме с данными текущего стопа вводим новый
        new_stop_input_coords = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['STOP_PRICE_INPUT']['coords']
        new_stop_input_desc = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['STOP_PRICE_INPUT']['description']
        self._type_text(new_stop_input_coords, f"{new_stop_price:.{self.spp}f}", new_stop_input_desc, delay=0.5, clean=True)
        
        # г). В этой же форме нажимаем кнопку Confirm
        confirm_form_coords = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['BUTTON_CONFIRM']['coords']
        confirm_form_desc = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['BUTTON_CONFIRM']['description']
        self._click_element(confirm_form_coords, confirm_form_desc, delay=1.00)
        
        # д). Нажимаем Confirm без распознавания
        self._handle_stop_order_confirmation(delay=0.75)
        
        # е). Нажимаем Cancel на случай, если цена уже успела зайти за стоп
        cancel_form_coords = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['BUTTON_CANCEL']['coords']
        cancel_form_desc = self.mexc_clicker_config['TP_SL_ADD_FORM_2']['BUTTON_CANCEL']['description']
        self._click_element(cancel_form_coords, cancel_form_desc, delay=0.75)
        
        # ж). Нажимаем на крестик в форме со списком стопов, чтобы ее закрыть
        close_coords = self.mexc_clicker_config['TP_SL_ADD_FORM_1']['BUTTON_CLOSE']['coords']
        close_desc = self.mexc_clicker_config['TP_SL_ADD_FORM_1']['BUTTON_CLOSE']['description']
        self._click_element(close_coords, close_desc, delay=0.75)
        
        move_stop_message = f"➡️ <b>ДВИЖЕНИЕ СТОПА:</b> Новый SL={new_stop_price:.{self.spp}f}"

        self.logger.warning(move_stop_message.replace('<b>', '').replace('</b>', ''))
        self.notifier.notify(move_stop_message)

    # --- 4. МЕТОДЫ ЦИКЛА (Адаптированы под API) ---
    
    def run_iteration(self):
        """
        Выполняет одну итерацию главного цикла, используя API для данных.
        Оптимизирована проверка наличия позиции.
        """
        # --- ОГРАНИЧЕНИЕ ЗАПРОСОВ К API ---
        now = datetime.now()
        current_second = now.second
        
        # Целевые секунды для запуска
        targets = self.mexc_clicker_config['API']['REFRESH_INTERVALS_SECONDS']
        
        # Находим ближайшую будущую цель
        next_target = None
        for t in targets:
            if current_second < t:
                next_target = t
                break
        
        # Если текущая секунда больше всех целей, ждем 10-й секунды следующей минуты
        if next_target is None:
            sleep_time = (60 - current_second) + 10
        else:
            sleep_time = next_target - current_second
            
        # Логируем ожидание только если оно больше 1 секунды, чтобы не спамить
        if sleep_time > 1:
            self.logger.info(f"Ожидание {sleep_time} сек. до следующего запуска (цель: {next_target if next_target else '10 (след. мин)'} сек).")
            time.sleep(sleep_time)
        # ---------------------------------------------------------------------

        self.logger.info("--- Начинаем новую итерацию ---") # Добавим лог для ясности
        
        if self._recognize_suspicious_activity_popup():
            self.logger.critical("КЛИКЕР: 🛑 Обнаружено окно подозрительной активности! Остановка.")
            self.notifier.notify("🛑 <b>КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ:</b> Обнаружена подозрительная активность. Бот остановлен.")
            raise SystemExit("Подозрительная активность обнаружена.")
        
        # 1. ОБНОВЛЕНИЕ СВЕЧЕЙ (через API)
        self.candle_data = self.api_client.fetch_kline_data(self.NUM_CANDLES_TO_FETCH)
        
        if not self.candle_data:
            self.logger.error("API не вернуло данных. Пропускаем итерацию.")
            return # Прерываем итерацию, если нет данных
        
        # Если точность не расчитана
        if not self.spp:
            self.spp = self._get_max_precision(self.candle_data[0])
        
        current_system_time = time.time() # Текущее системное время в секундах (float)
        
        # Расчет времени до конца текущей свечи с миллисекундами
        time_remaining_in_candle = self.CANDLE_PERIOD_SECONDS - (current_system_time % self.CANDLE_PERIOD_SECONDS)
        
        # До начала каких-либо действий прокручиваем экран наверх до упора для точного позиционирования
        self._page_scroll_up(scroll_count=5, delay=1.0)
        
        # Информативное логирование времени
        self.logger.debug(
            f"Системное время: {datetime.fromtimestamp(current_system_time).strftime('%H:%M:%S.%f')[:-3]} | "
            f"Интервал свечи: {self.CANDLE_PERIOD_SECONDS} сек. | "
            f"Время до закрытия текущей свечи: {time_remaining_in_candle:.2f} сек."
        )

        # Подготовка для нового формата лога
        first_candle_info = "N/A"
        last_candle_info = "N/A"

        if len(self.candle_data) >= 1:
            # Крайняя правая (самая новая, обычно незакрытая) свеча
            last_candle = self.candle_data[0]
            last_candle_info = (
                f"O:{last_candle.get('open', 'N/A'):.{self.spp}f} "
                f"H:{last_candle.get('high', 'N/A'):.{self.spp}f} "
                f"L:{last_candle.get('low', 'N/A'):.{self.spp}f} "
                f"C:{last_candle.get('close', 'N/A'):.{self.spp}f}"
            )

        if len(self.candle_data) >= self.NUM_CANDLES_TO_FETCH:
            # Крайняя левая (самая старая) свеча, которую мы запросили
            first_candle = self.candle_data[self.NUM_CANDLES_TO_FETCH - 1] 
            first_candle_info = (
                f"O:{first_candle.get('open', 'N/A'):.{self.spp}f} "
                f"H:{first_candle.get('high', 'N/A'):.{self.spp}f} "
                f"L:{first_candle.get('low', 'N/A'):.{self.spp}f} "
                f"C:{first_candle.get('close', 'N/A'):.{self.spp}f}"
            )

        # Проверяем количество позиций только если self.has_position = True
        positions_count = -1 # Изначально считаем, что количество позиций неизвестно

        if self.has_position:
            # Если бот считает, что позиция открыта, постоянно мониторим ее наличие
            positions_count = self._recognize_positions_count()
            
            # Если OCR не смог распознать, пропускаем обновление статуса has_position
            if positions_count == -1:
                self.logger.warning("КЛИКЕР: OCR не смог определить количество позиций. Флаг 'has_position' не изменяется.")
            elif positions_count == 0:
                close_pos_message = f"❌ <b>КЛИКЕР:</b> Закрытие позиции успешно подтверждено."
                
                self.logger.warning(close_pos_message.replace('<b>', '').replace('</b>', ''))
                self.notifier.notify(close_pos_message)
                
                self.has_position = False

                # --- ЛОГИКА ЗАПОЛНЕНИЯ ЛОТА ПОСЛЕ ЗАКРЫТИЯ ПОЗИЦИИ ---
                # Это гарантирует, что поле лота будет заполнено до следующего сигнала на открытие
                try:
                    time.sleep(5) # Нужно дать исчезнуть оповещению о закрытии позиции (отображается поверх поля ввода стопа)
                    
                    margin = self.decision_maker.decision_config['RISK_MANAGEMENT']['POSITION_SIZING']['FIXED_MARGIN_USD']
                    leverage = self.decision_maker.decision_config['RISK_MANAGEMENT']['POSITION_SIZING']['LEVERAGE_FACTOR']
                    lot_size = margin * leverage
                    
                    lot_coords = self.mexc_clicker_config['TRADE_PANEL']['LOT_INPUT_USD']['coords']
                    lot_desc = self.mexc_clicker_config['TRADE_PANEL']['LOT_INPUT_USD']['description']
                    self._type_text(lot_coords, str(lot_size), lot_desc, delay=0.5, clean=True)
                    self.logger.info(f"КЛИКЕР: Лот {lot_size} введен после закрытия позиции.")

                    # 2. Заполняем поле с процентом стопа (STOP_INPUT)
                    open_pos_sl_roe = self.decision_maker.decision_config['STOP_MANAGEMENT']['OPEN_POS_SL_ROE']
                    stop_value_for_input = open_pos_sl_roe * leverage
                    
                    stop_input_coords = self.mexc_clicker_config['TRADE_PANEL']['STOP_INPUT']['coords']
                    stop_input_desc = self.mexc_clicker_config['TRADE_PANEL']['STOP_INPUT']['description']
                    self._type_text(stop_input_coords, f"{stop_value_for_input:.2f}", stop_input_desc, delay=0.5, clean=True)
                    self.logger.info(f"КЛИКЕР: Стоп {stop_value_for_input:.2f}% введен при старте.")

                except KeyError as ke:
                    self.logger.error(f"КЛИКЕР: Ошибка чтения конфига DecisionMaker при расчете лота: {ke}. Проверьте структуру конфига.")
                    self.notifier.notify(f"❌ <b>КРИТИЧЕСКАЯ ОШИБКА:</b> Ошибка в конфиге DecisionMaker. Бот остановлен.")
                    
                    raise SystemExit(f"Ошибка конфига DecisionMaker: {ke}")
                # --- КОНЕЦ ЛОГИКИ ЗАПОЛНЕНИЯ ПОСЛЕ ЗАКРЫТИЯ ---
            # else: positions_count > 0, self.has_position остается True, все ОК

        self.logger.info(
            f"API Candles [{first_candle_info}, ..., {last_candle_info}], "
            f"has_position: {self.has_position}, Positions: {positions_count if positions_count != -1 else 'UNKNOWN'}"
        )

        # 2. Вызов Генератора Сигналов
        # DecisionMaker должен всегда получать актуальный статус has_position
        signal_flag, dm_lot_size, dm_stop_value = self.decision_maker.check_signal_and_risk(
            self.has_position,
            self.candle_data,
            time_remaining_in_candle
        )

        # --- ЛОГИКА ЗАПОЛНЕНИЯ ЛОТА И ПРОЦЕНТНОГО СТОПА ПРИ ПЕРВОМ ПРОХОДЕ (если нет позиции) ---
        # Это гарантирует, что поля будут заполнены хотя бы один раз перед первым сигналом на открытие
        if not self.has_position and not self.initial_stop_filled:
            self.logger.warning("КЛИКЕР: Входим в фазу предварительного заполнения лота/процентного стопа.")

            try:
                # 1. Заполняем поле с лотом (Lot Input USD)
                margin = self.decision_maker.decision_config['RISK_MANAGEMENT']['POSITION_SIZING']['FIXED_MARGIN_USD']
                leverage = self.decision_maker.decision_config['RISK_MANAGEMENT']['POSITION_SIZING']['LEVERAGE_FACTOR']
                lot_size = margin * leverage

                lot_coords = self.mexc_clicker_config['TRADE_PANEL']['LOT_INPUT_USD']['coords']
                lot_desc = self.mexc_clicker_config['TRADE_PANEL']['LOT_INPUT_USD']['description']
                self._type_text(lot_coords, str(lot_size), lot_desc, delay=0.5, clean=True)
                self.logger.info(f"КЛИКЕР: Лот {lot_size} введен при старте.")

                # 2. Заполняем поле с процентом стопа (STOP_INPUT)
                open_pos_sl_roe = self.decision_maker.decision_config['STOP_MANAGEMENT']['OPEN_POS_SL_ROE']
                stop_value_for_input = open_pos_sl_roe * leverage
                
                stop_input_coords = self.mexc_clicker_config['TRADE_PANEL']['STOP_INPUT']['coords']
                stop_input_desc = self.mexc_clicker_config['TRADE_PANEL']['STOP_INPUT']['description']
                self._type_text(stop_input_coords, f"{stop_value_for_input:.2f}", stop_input_desc, delay=0.5, clean=True)
                self.logger.info(f"КЛИКЕР: Стоп {stop_value_for_input:.2f}% введен при старте.")
                
                self.initial_stop_filled = True # Устанавливаем флаг, что стоп введен
            except KeyError as ke:
                self.logger.error(f"КЛИКЕР: Ошибка чтения конфига при предварительном заполнении: {ke}. Бот будет остановлен.")
                self.notifier.notify(f"❌ <b>КРИТИЧЕСКАЯ ОШИБКА:</b> Ошибка в конфиге при предзаполнении. Бот остановлен.")
                
                raise SystemExit(f"Ошибка конфига при предзаполнении: {ke}")
        # --- КОНЕЦ ЛОГИКИ ПРЕДВАРИТЕЛЬНОГО ЗАПОЛНЕНИЯ ---
        
        # 3. ЛОГИКА ДЕЙСТВИЙ
        if signal_flag and dm_stop_value is not None and not self.has_position:
            # Если есть сигнал на открытие и внутренний флаг has_position=False, открываем позицию
            # Внимание: dm_stop_value - это ЦЕНА стопа (float), а не процент.
            self._execute_open_position(dm_lot_size, dm_stop_value) 
        
        elif not signal_flag and dm_stop_value is not None and self.has_position:
            # Если нет сигнала на открытие, но есть позиция (по флагу), двигаем стоп
            self._execute_move_stop(dm_stop_value)
        
        else:
            self.logger.info("Нет сигналов и нет необходимости двигать стоп. Бездействие.")

    def run_loop(self, interval_seconds=1):
        """Запускает основной цикл бота."""
        self.logger.info(f"Начало основного цикла с интервалом {interval_seconds} сек.")
        
        try:
            while True:
                start_time = time.time()
                self.run_iteration()
                
                elapsed_time = time.time() - start_time
                sleep_duration = max(0, interval_seconds - elapsed_time)
                
                if sleep_duration > 0:
                    time.sleep(sleep_duration)
                
        except SystemExit as e:
            self.logger.critical(f"Бот остановлен по требованию: {e}")
        except Exception as e:
            error_message = (
                f"❌ <b>КРИТИЧЕСКАЯ ОШИБКА БОТА:</b> <code>{type(e).__name__}: {e}</code>. "
                f"Бот остановлен. Смотрите логи для полного Traceback."
            )
            self.logger.critical(error_message, exc_info=True)
            self.notifier.notify(error_message)
        finally:
            self.notifier.notify("🛑 <b>Бот-кликер MEXC остановлен.</b>")