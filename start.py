import requests
import time
import threading
import signal
import sys
import os
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from calendar import monthrange
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

# === НАСТРОЙКИ ===
API_URL = "https://belarusborder.by/info/monitoring-new?token=test&checkpointId=53d94097-2b34-11ec-8467-ac1f6bf889c0"
BOT_TOKEN = "8022336559:AAGY2jsvuPl0iXlNIbhbzKOnFOFdd4_g5BE"
CHECK_INTERVAL = 30
STAT_FILE = "statistic.txt"

monitored_cars = {}
last_seen = set()
passed_counter = Counter()
date_selection = {}
current_hour = None
current_queue_count = None  # None - данные не получены, 0+ - актуальное количество
running = True

# ================== ОБРАБОТКА ПРЕРЫВАНИЙ ==================
def signal_handler(sig, frame):
    global running
    print("\n🛑 Завершение работы...")
    running = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ================== ЛОГИРОВАНИЕ ==================
def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

# ================== ФУНКЦИИ МОНИТОРИНГА ==================
def get_queue_data():
    try:
        response = requests.get(
            API_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
                "Accept": "application/json"
            },
            timeout=20
        )
        
        if response.status_code != 200:
            log(f"[API ERROR] HTTP {response.status_code}: {response.text[:200]}")
            return None
        
        data = response.json()
        
        # Проверка структуры ответа
        if 'carLiveQueue' not in data or not isinstance(data['carLiveQueue'], list):
            log("[API WARN] Некорректный формат carLiveQueue")
            return None
            
        return data['carLiveQueue']
        
    except requests.exceptions.RequestException as e:
        log(f"[API ERROR] Ошибка соединения: {str(e)}")
        return None
    except Exception as e:
        log(f"[API ERROR] Необработанная ошибка: {str(e)}")
        return None

def process_passed_cars(current_seen):
    global last_seen
    disappeared = last_seen - current_seen
    if disappeared:
        hour_key = datetime.now().strftime("%Y-%m-%d %H")
        passed_counter[hour_key] += len(disappeared)
    last_seen = current_seen

def save_statistics():
    global current_hour
    now = datetime.now()
    new_hour = now.strftime("%Y-%m-%d %H")
    
    if new_hour != current_hour and current_hour is not None:
        hour_start = datetime.strptime(current_hour, "%Y-%m-%d %H")
        next_hour = hour_start + timedelta(hours=1)
        line = f"{hour_start.strftime('%d.%m.%Y')} {hour_start.strftime('%H')}-{next_hour.strftime('%H')} {passed_counter[current_hour]}\n"
        try:
            with open(STAT_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            log(f"[STAT] {line.strip()}")
        except Exception as e:
            log(f"[STAT ERROR] {str(e)}")
        
        passed_counter[current_hour] = 0
        current_hour = new_hour

def monitor_loop():
    global current_hour, running, last_seen, current_queue_count
    current_hour = datetime.now().strftime("%Y-%m-%d %H")
    
    while running:
        try:
            queue = get_queue_data()
            if queue is not None:
                current_queue_count = len(queue)
                log(f"[QUEUE] Обновлено: {current_queue_count} авто")
                current_seen = {item.get("regnum", "").upper() for item in queue}
            else:
                current_queue_count = None
                current_seen = set()
                log("[QUEUE] Данные не получены")
            
            # Автоматическое удаление авто
            disappeared = last_seen - current_seen
            for regnum in disappeared:
                if regnum in monitored_cars and monitored_cars[regnum].get('last_position') == 1:
                    send_telegram_message(
                        monitored_cars[regnum]['chat_id'], 
                        f"✅ {regnum} проехал КПП (последняя позиция: 1)."
                    )
                    del monitored_cars[regnum]
                    log(f"[AUTO-REMOVE] {regnum} удален")
            
            process_passed_cars(current_seen)
            
            # Обновление позиций
            for regnum, info in list(monitored_cars.items()):
                match = next((item for item in (queue or []) if item.get("regnum", "").upper() == regnum.upper()), None)
                if match and (position := match.get("order_id")) is not None:
                    monitored_cars[regnum]['last_position'] = position
                    if position <= info['threshold'] and info.get('last_reported_pos') != position:
                        send_telegram_message(info['chat_id'], f"ℹ️ {regnum}: позиция {position}.")
                        info['last_reported_pos'] = position
                        if position == 1:
                            send_telegram_message(info['chat_id'], f"🔔 {regnum} в позиции 1!")
            
            save_statistics()
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            log(f"[MONITOR ERROR] {str(e)}")
            time.sleep(10)

# ================== ФУНКЦИИ КАЛЕНДАРЯ ==================
def generate_calendar(year, month):
    keyboard = []
    month_name = datetime(year, month, 1).strftime('%B %Y')
    keyboard.append([InlineKeyboardButton(month_name, callback_data='ignore')])
    keyboard.append([InlineKeyboardButton(day, callback_data='ignore') for day in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']])
    
    _, days_in_month = monthrange(year, month)
    days = [InlineKeyboardButton(str(d), callback_data=f'day_{year}_{month}_{d}') for d in range(1, days_in_month + 1)]
    for i in range(0, len(days), 7):
        keyboard.append(days[i:i+7])
    
    prev_year, prev_month = (year, month-1) if month > 1 else (year-1, 12)
    next_year, next_month = (year, month+1) if month < 12 else (year+1, 1)
    keyboard.append([
        InlineKeyboardButton("←", callback_data=f'nav_{prev_year}_{prev_month}'),
        InlineKeyboardButton("→", callback_data=f'nav_{next_year}_{next_month}')
    ])
    return InlineKeyboardMarkup(keyboard)

def handle_calendar_callback(chat_id, data):
    try:
        if data.startswith('nav'):
            _, year, month = data.split('_')
            return generate_calendar(int(year), int(month))
        
        elif data.startswith('day'):
            _, year, month, day = data.split('_')
            selected_date = datetime(int(year), int(month), int(day)).strftime('%d.%m.%Y')
            user_data = date_selection.get(chat_id, {"type": "start"})
            
            if user_data["type"] == "start":
                user_data.update({"start": selected_date, "type": "end"})
                date_selection[chat_id] = user_data
                return ("Выберите конечную дату:", generate_calendar(int(year), int(month)))
            else:
                user_data["end"] = selected_date
                date_selection.pop(chat_id, None)
                process_stat_period(chat_id, user_data["start"], selected_date)
        
        return None
    except Exception as e:
        log(f"[CALENDAR ERROR] {e}")
        return None

# ================== ОСНОВНЫЕ ФУНКЦИИ ==================
def send_telegram_message(chat_id, text, reply_markup=None):
    try:
        payload = {
            "chat_id": chat_id,
            "text": text
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup.to_json()
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15
        )
        if response.status_code != 200:
            log(f"[SEND ERROR] HTTP {response.status_code}: {response.text}")
    except Exception as e:
        log(f"[SEND ERROR] {str(e)}")

def send_main_menu(chat_id):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [{"text": "Добавить авто"}, {"text": "Удалить авто"}],
            [{"text": "Статистика"}, {"text": "Всего авто"}]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    send_telegram_message(chat_id, "Выберите действие:", reply_markup=keyboard)

def send_calendar(chat_id):
    send_telegram_message(
        chat_id,
        "Выберите начальную дату:",
        generate_calendar(datetime.now().year, datetime.now().month)
    )

def process_stat_period(chat_id, start_date, end_date):
    try:
        start = datetime.strptime(start_date, "%d.%m.%Y")
        end = datetime.strptime(end_date, "%d.%m.%Y") + timedelta(days=1)
        
        stats = defaultdict(list)
        if os.path.exists(STAT_FILE):
            with open(STAT_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) < 3: continue
                    date_str, hour_range, count = parts[0], parts[1], parts[2]
                    try:
                        line_date = datetime.strptime(date_str, "%d.%m.%Y")
                        if start <= line_date < end:
                            stats[date_str].append(f"{hour_range} - {count} машин")
                    except:
                        continue
        
        response = "📊 Статистика:\n"
        for date in sorted(stats):
            response += f"\n{date}:\n" + "\n".join(stats[date]) + "\n"
        
        max_length = 4096
        if len(response) > max_length:
            for i in range(0, len(response), max_length):
                send_telegram_message(chat_id, response[i:i+max_length])
                time.sleep(1)
        else:
            send_telegram_message(chat_id, response)
    
    except Exception as e:
        log(f"[STAT ERROR] {e}")
        send_telegram_message(chat_id, "❌ Ошибка обработки")

# ================== ОБРАБОТКА СООБЩЕНИЙ ==================
def process_update(update):
    if "callback_query" in update:
        query = update["callback_query"]
        chat_id = query["message"]["chat"]["id"]
        data = query["data"]
        result = handle_calendar_callback(chat_id, data)
        
        if isinstance(result, tuple):
            send_telegram_message(chat_id, result[0], result[1])
        elif result:
            send_telegram_message(chat_id, "Выберите дату:", result)
        return
    
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip().lower()
    
    if not text:
        return
    
    log(f"[MSG] {chat_id}: {text}")
    
    if text == "/start":
        if chat_id in date_selection:
            del date_selection[chat_id]
        send_main_menu(chat_id)
    elif text == "статистика":
        date_selection[chat_id] = {"type": "start"}
        send_calendar(chat_id)
    elif text == "всего авто":
        if current_queue_count is not None:
            msg = (
                f"🚙 Авто в очереди: {current_queue_count}\n"
                f"⌚ Обновлено: {datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            msg = "⚠️ Данные не получены. Проверьте подключение к API."
        send_telegram_message(chat_id, msg)
    elif text == "добавить авто":
        send_telegram_message(chat_id, "Введите номер авто и порог (пример: ABC123 10)")
    elif text == "удалить авто":
        send_telegram_message(chat_id, "Введите STOP и номер (пример: STOP ABC123)")
    elif text.startswith("stop"):
        parts = text.split()
        if len(parts) == 2:
            regnum = parts[1].upper()
            if regnum in monitored_cars:
                del monitored_cars[regnum]
                send_telegram_message(chat_id, f"🛑 {regnum} удален")
            else:
                send_telegram_message(chat_id, "❌ Авто не найдено")
    elif len(text.split()) == 2:
        regnum, threshold = text.split()
        if threshold.isdigit():
            monitored_cars[regnum.upper()] = {
                "chat_id": chat_id,
                "threshold": int(threshold),
                "last_position": None,
                "last_reported_pos": None
            }
            send_telegram_message(chat_id, f"🟢 {regnum} добавлен (порог: {threshold})")
        else:
            send_telegram_message(chat_id, "❌ Неверный формат")

def process_updates():
    last_update_id = 0
    log("[BOT] Запущен...")
    while running:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 20},
                timeout=25
            )
            
            if response.status_code != 200:
                log(f"[API ERROR] HTTP {response.status_code}")
                time.sleep(5)
                continue
                
            updates = response.json().get("result", [])
            if not updates:
                continue
                
            for update in updates:
                last_update_id = update["update_id"]
                process_update(update)
                
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log(f"[UPDATE ERROR] {str(e)}")
            time.sleep(5)

if __name__ == "__main__":
    try:
        monitor_thread = threading.Thread(target=monitor_loop)
        monitor_thread.daemon = True
        monitor_thread.start()
        process_updates()
    except KeyboardInterrupt:
        signal_handler(None, None)