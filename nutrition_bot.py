import os
import json
import asyncio
import datetime
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ==============================================================================
# CONFIG MATRIX (Все твои ключи на месте)
# ==============================================================================
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY_HERE"
GOOGLE_SHEETS_KEY = "YOUR_GOOGLE_SHEET_ID_HERE"

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
ai_client = OpenAI(api_key=OPENAI_API_KEY)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("google_creds.json", scope)
sheets_client = gspread.authorize(creds)
doc = sheets_client.open_by_key(GOOGLE_SHEETS_KEY)

sheet_dash = doc.worksheet("Dashboard")
sheet_agent = doc.worksheet("Agent")
sheet_hist = doc.worksheet("История")

PRODUCT_DIRECTORY = {}
BLUDO_GRAMS_CACHE = {}

MEAL_RANGES = {
    "ЗАВТРАК": (6, 7),
    "ОБЕД": (9, 10),
    "УЖИН": (12, 13),
    "ПЕРЕКУС": (14, 17)
}

# ==============================================================================
# FUNDAMENTAL UTILITIES, SAFEGUARDS & MUTATION ENGINE
# ==============================================================================

def safe_num(val):
    if val is None: return 0
    s = str(val).strip().replace(" ", "").replace(",", ".")
    if not s or s in ["0", "0.00", "0,00", "#REF!", "#VALUE!"]: return 0
    try:
        if "." in s: return float(s)
        return int(s)
    except ValueError: return val

def col_to_name(n):
    name = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        name = chr(65 + remainder) + name
    return name

def apply_failsafe_mapping(items, raw_text):
    raw_lower = str(raw_text).lower()
    for food in items:
        if safe_num(food.get('weight', 0)) == 0 and food.get('category') != "Блюдо":
            for bludo_name in BLUDO_GRAMS_CACHE.keys():
                if bludo_name.lower() in raw_lower:
                    food['item'] = bludo_name
                    food['category'] = "Блюдо"
                    break
    return items

def parse_modification_intent(raw_text: str, directory: dict) -> dict:
    directory_context = ""
    for cat, items in directory.items():
        if items: directory_context += f"- {cat}: {', '.join(items)}\n"

    prompt = f'''
    Ты — управляющий модуль редактирования записей Дениса. Твоя задача — понять, какой продукт он хочет удалить или заменить, и на какой именно.
    Вот матрица продуктов Дениса:
    {directory_context}

    ИНСТРУКЦИЯ:
    1. Определи действие ("action"): "replace" (если просит заменить) или "delete" (если просит удалить/убрать).
    2. Выдели старый продукт ("old_item_search") — это базовое имя продукта для поиска в таблицах (например: "арахис", "рис", "сметана").
    3. ОПРЕДЕЛИ ПРИЕМ ПИЩИ ("target_meal"): Если в тексте есть маркер приема пищи ("в обеде", "из завтрака", "в ужине", "перекус"), строго верни КАПСОМ: "ЗАВТРАК", "ОБЕД", "УЖИН" или "ПЕРЕКУС". Если контекста нет — верни пустую строку "".
    4. Если это "replace", сопоставь новый продукт ("new_item") с матрицей Дениса. Если вес изменен, запиши его в "weight", иначе верни 0.
    5. Если это "delete", поле "new_item" должно быть {{}}.

    Выдай СТРОГО JSON object формата:
    {{
       "action": "replace" или "delete",
       "old_item_search": "имя старого продукта для поиска",
       "target_meal": "ЗАВТРАК или ОБЕД или УЖИН или ПЕРЕКУС или пустая строка",
       "new_item": {{
          "item": "Точное название нового продукта из матрицы",
          "category": "Категория нового продукта",
          "termo": "Варка/Жарка/Сырое/",
          "weight": число_или_0
       }}
    }}
    '''
    response = ai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"}, 
        messages=[
            {"role": "system", "content": "You are a data modifier that outputs ONLY raw valid JSON."},
            {"role": "user", "content": prompt + f'\n\nЗапрос на изменение: "{raw_text}"'}
        ],
        temperature=0.0
    )
    return json.loads(response.choices[0].message.content.strip())


async def try_process_modification_command(raw_text: str, message: types.Message) -> bool:
    txt_lower = raw_text.lower()
    keywords = ["замени", "измени", "удали", "убери", "выкинь", "поменяй"]
    if not any(kw in txt_lower for kw in keywords): return False
    
    status_msg = await message.answer("🔄 Запущена транзакция модификации...")
    try:
        mod = parse_modification_intent(raw_text, PRODUCT_DIRECTORY)
        action = mod.get("action")
        search_key = str(mod.get("old_item_search", "")).lower().strip()
        target_meal = mod.get("target_meal", "").upper().strip()
        
        if not search_key:
            await status_msg.edit_text("❌ Не удалось распознать целевой продукт правки."); return True
            
        has_dash_keyword = "дашборд" in txt_lower or "dashboard" in txt_lower
        
        found_in_dash = None
        current_meal_key = ""
        found_in_agent = None

        if target_meal and target_meal in MEAL_RANGES:
            s_row, e_row = MEAL_RANGES[target_meal]
            rows_data = sheet_dash.get(f"B{s_row}:F{e_row}")
            if rows_data:
                for idx, r in enumerate(rows_data):
                    if len(r) >= 3 and search_key in str(r[2]).lower():
                        found_in_dash = s_row + idx
                        current_meal_key = target_meal
                        break
        elif has_dash_keyword:
            for meal, (s_row, e_row) in MEAL_RANGES.items():
                rows_data = sheet_dash.get(f"B{s_row}:F{e_row}")
                if rows_data:
                    for idx, r in enumerate(rows_data):
                        if len(r) >= 3 and search_key in str(r[2]).lower():
                            found_in_dash = s_row + idx
                            current_meal_key = meal
                            break
                if found_in_dash: break
        else:
            agent_items = sheet_agent.col_values(3)
            for idx, name in enumerate(agent_items):
                if idx >= 4 and search_key in name.lower():
                    found_in_agent = idx + 1; break
            
            if not found_in_agent:
                for meal, (s_row, e_row) in MEAL_RANGES.items():
                    rows_data = sheet_dash.get(f"B{s_row}:F{e_row}")
                    if rows_data:
                        for idx, r in enumerate(rows_data):
                            if len(r) >= 3 and search_key in str(r[2]).lower():
                                found_in_dash = s_row + idx
                                current_meal_key = meal
                                break
                    if found_in_dash: break

        if not found_in_dash and not found_in_agent:
            await status_msg.edit_text(f"❌ Продукт '{search_key}' не найден в выбранном контексте."); return True

        if action == "delete":
            if found_in_dash:
                empty_payload = ["", "", "", "", ""]
                sheet_dash.update(range_name=f"B{found_in_dash}:F{found_in_dash}", values=[empty_payload])
                await status_msg.edit_text(f"🗑️ Из слота [{current_meal_key}] удалена строка {found_in_dash}.")
            else:
                empty_payload = ["", "", "", ""]
                sheet_agent.update(range_name=f"B{found_in_agent}:E{found_in_agent}", values=[empty_payload])
                await status_msg.edit_text(f"🗑️ Из накопителя Agent удалена строка {found_in_agent}.")
            return True

        if action == "replace":
            new_food = mod.get("new_item", {})
            n_cat = new_food.get("category")
            n_item = new_food.get("item")
            n_termo = new_food.get("termo", "")
            n_weight = safe_num(new_food.get("weight", 0))
            
            if n_cat not in PRODUCT_DIRECTORY or n_item not in PRODUCT_DIRECTORY[n_cat]:
                found_failsafe = False
                if n_cat in PRODUCT_DIRECTORY:
                    for real_i in PRODUCT_DIRECTORY[n_cat]:
                        if real_i.lower() == n_item.lower():
                            n_item = real_i; found_failsafe = True; break
                if not found_failsafe:
                    await status_msg.edit_text(f"❌ Продукта '{n_item}' нет в справочнике '{n_cat}'! Операция отменена."); return True

            if found_in_dash:
                if n_weight == 0:
                    n_weight = safe_num(sheet_dash.acell(f"F{found_in_dash}").value)
                
                payload = [current_meal_key, n_cat, n_item, n_termo, n_weight]
                sheet_dash.update(range_name=f"B{found_in_dash}:F{found_in_dash}", values=[payload])
                
                await status_msg.edit_text("⚡ Изменения внесены. Калибрую КБЖУ...")
                await asyncio.sleep(1.0)
                
                tk, tp, tf, tc = 0, 0, 0, 0
                try:
                    r_vals = sheet_dash.get(f"G{found_in_dash}:J{found_in_dash}")
                    if r_vals and r_vals[0]:
                        r = r_vals[0]
                        while len(r) < 4: r.append("0")
                        tk, tp, tf, tc = safe_num(r[0]), safe_num(r[1]), safe_num(r[2]), safe_num(r[3])
                except Exception as e: print(e)
                
                report = f"🔄 Замена успешна в слоте [{current_meal_key}] (строка {found_in_dash})!\nЗалито: {n_item} ({n_weight}г)"
                kbju_text = f"🔥 Новое КБЖУ строки:\n🔋 {int(tk)} ккал  |  🧬 Б: {int(tp)}г  |  💧 Ж: {int(tf)}г  |  🍞 У: {int(tc)}г"
                await status_msg.edit_text(f"{report}\n\n{kbju_text}")
                
            else:
                if n_weight == 0:
                    n_weight = safe_num(sheet_agent.acell(f"E{found_in_agent}").value)
                    
                payload = [n_cat, n_item, n_termo, n_weight]
                sheet_agent.update(range_name=f"B{found_in_agent}:E{found_in_agent}", values=[payload])
                await status_msg.edit_text(f"🔄 В накопителе Agent (строка {found_in_agent}) позиция изменена на {n_item} ({n_weight}г).")
            return True
            
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка модификации транзакции: {e}")
        return True
    return False


async def try_process_weight_command(raw_text: str, message: types.Message) -> bool:
    txt_upper = raw_text.strip().upper()
    if "ВЕС" in txt_upper or "WEIGHT" in txt_upper:
        match = re.search(r'([\d.,]+)', txt_upper)
        if match:
            weight_val = safe_num(match.group(1))
            if isinstance(weight_val, (int, float)) and weight_val > 0:
                try:
                    sheet_dash.update(range_name="C18", values=[[weight_val]])
                    await message.answer(f"⚖️ Живой вес зафиксирован!\nЗалит в Dashboard!C18 -> {weight_val} кг.")
                    return True
                except Exception as e: 
                    await message.answer(f"❌ Ошибка записи веса: {e}"); return True
    return False


async def send_help_manual(message: types.Message):
    help_text = (
        "📋 ИНСТРУКЦИЯ ПО УПРАВЛЕНИЮ KIRA AGENT\n\n"
        "📥 1. ВВОД ДАННЫХ И ЕДЫ\n"
        "• В накопитель (Agent): Просто диктуй продукты и граммы ('Банан 80г', 'Гречка 150г').\n"
        "• Прямая инъекция на Дашборд: Добавляй в запросе привязку к слоту ('В обед гейнер на воде', 'Добавь к завтраку 2 яйца').\n\n"
        "📦 2. СХЛОПЫВАНИЕ БУФЕРА\n"
        "• Отправь команду ЗАВТРАК, ОБЕД или УЖИН (текстом или через Меню). Бот соберет все продукты из накопителя, упакует в одну монолитную строку Дашборда и обнулит буфер.\n\n"
        "🔄 3. ИЗМЕНЕНИЕ И УДАЛЕНИЕ\n"
        "• Замена: 'Замени рис на гречку' (по умолчанию ищет в Агенте) или 'Замени в завтраке рис на овсянку' (точечно на Дашборде).\n"
        "• Удаление: 'Удали сметану' или 'Убери банан из обеда' — строка полностью очистится.\n\n"
        "📊 4. МОНИТОРЫ И ВЕС\n"
        "• Команда СТАТУС — краткий остаток КБЖУ и скользящие средние веса с трендом.\n"
        "• Команда СЕГОДНЯ (или /today) — развернутый дашборд тарелок с детализацией.\n"
        "• Команда ТРЕНДЫ (или /stats) — скользящие средние тренды КБЖУ и веса за 7 и 30 дней.\n"
        "• Запись веса: 'Вес 59.5' или 'Weight 59.5' — автоматом залетает в Dashboard!C18.\n\n"
        "🔒 5. ЖЕСТКИЕ ЛИМИТЫ СТРОК\n"
        "• Завтрак/Обед/Ужин: лимит 2 строки прямого докидывания.\n"
        "• Перекусы: лимит 4 строки. При превышении бот заблокирует транзакцию."
    )
    await message.answer(help_text)

def load_all_products_from_sheets():
    global PRODUCT_DIRECTORY, BLUDO_GRAMS_CACHE
    categories = ["Мясо", "Каша", "Рыба", "Овощи", "Фрукты", "Молочка", "Хлебобулочные", "Напитки", "Прочее", "Блюдо"]
    temp_dir = {}
    temp_grams = {}
    
    print("⏳ Индексация справочников...")
    for cat in categories:
        try:
            ws = doc.worksheet(cat)
            all_rows = ws.get_all_values()
            if len(all_rows) > 1:
                rows = all_rows[1:]
                temp_dir[cat] = [r[0].strip() for r in rows if r and r[0].strip()]
                if cat == "Блюдо":
                    for r in rows:
                        if r and r[0].strip():
                            temp_grams[r[0].strip()] = r[1].strip() if len(r) > 1 else ""
        except Exception as e:
            print(f"⚠️ Ошибка чтения листа '{cat}': {e}")
            
    PRODUCT_DIRECTORY = temp_dir
    BLUDO_GRAMS_CACHE = temp_grams
    print("Base synchronized. Меню и Гайд зашиты.")

# ==============================================================================
# CORE AGENT INTENT PARSER
# ==============================================================================

def parse_food_text_via_llm(raw_text: str, directory: dict) -> dict:
    directory_context = ""
    for cat, items in directory.items():
        if items: directory_context += f"- {cat}: {', '.join(items)}\n"

    prompt = f'''
    Ты — аналитический модуль учета питания Дениса. Твоя задача — распарсить сырой текст и вытащить продукты, сопоставляя их с матрицей.
    Вот актуальная матрица продуктов Дениса:
    {directory_context}

    ИНСТРУКЦИЯ:
    1. КРИТИЧЕСКИЙ ПРИОРИТЕТ ДЛЯ КАТЕГОРИИ "Блюдо": Если в запросе звучит название из категории "Блюдо" или слово "блюдо", мапь СТРОГО на вкладку "Блюдо".
    2. Для остальных продуктов найди НАИБОЛЕЕ БЛИЗКОЕ по смыслу название из матрицы.
    3. Вытащи вес в граммах (число). Если вес НЕ НАЗВАН в тексте — строго верни 0.
    4. Определи тип обработки ("termo"): "Варка", "Жарка", "Сырое" или "".
    5. ТРИГГЕР ПРЯМОГО ДОКИДЫВАНИЯ ("target_meal"): Если есть слова "в завтрак/обед/ужин/перекус" — верни КАПСОМ. Иначе пустая строка "".

    Выдай СТРОГО JSON object следующего формата:
    {{
        "target_meal": "ЗАВТРАК или ОБЕД или УЖИН или ПЕРЕКУС или пустая строка",
        "items": [
            {{
                "item": "Точное название продукта из матрицы",
                "weight": число_или_0,
                "category": "Категория",
                "termo": "Варка/Жарка/Сырое/"
            }}
        ]
    }}
    '''
    response = ai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"}, 
        messages=[
            {"role": "system", "content": "You are a precise data extractor that outputs ONLY raw valid JSON objects."},
            {"role": "user", "content": prompt + f'\n\nТекст для анализа: "{raw_text}"'}
        ],
        temperature=0.0
    )
    return json.loads(response.choices[0].message.content.strip())


# ==============================================================================
# ENGINE: СУХОЙ ИНЖЕНЕРНЫЙ СТАТУС ВСЕГО ДНЯ И ТРЕНДОВ ВЕСА
# ==============================================================================

def pull_weight_history_context() -> str:
    today = datetime.date.today()
    m = today.month  
    start_col = 1 + (m - 1) * 6  
    day_col_letter = col_to_name(start_col)       
    weight_col_letter = col_to_name(start_col + 5) 
    try:
        # Выкачиваем всю историю за текущий месяц
        res_data = sheet_hist.get(f"{day_col_letter}4:{weight_col_letter}35")
        weight_list = []
        if res_data:
            for row in res_data:
                if len(row) >= 6:
                    w = safe_num(row[5])
                    if w > 0:
                        weight_list.append(w)
        
        if not weight_list:
            return "Данные веса еще не накоплены."
        
        # Скользящее среднее за неделю (последние 7 непустых логов)
        sub_7 = weight_list[-7:] if len(weight_list) >= 7 else weight_list
        avg_7 = round(sum(sub_7) / len(sub_7), 2) if sub_7 else 0
        
        # Скользящее среднее за весь месяц
        avg_month = round(sum(weight_list) / len(weight_list), 2) if weight_list else 0
        
        # Дебаг и калибровка вектора прогресса
        delta = avg_7 - avg_month
        if delta > 0.1:
            trend = "набор массы 📈"
        elif delta < -0.1:
            trend = "похудение 📉"
        else:
            trend = "удержание веса ⚖️"
            
        return f"\n🔹 За неделю твой средний вес — {avg_7} кг.\n🔹 А за весь месяц твой средний вес — {avg_month} кг.\n🎯 Направление: {trend}"
    except Exception as e: 
        return f"Ошибка расчета аналитики веса ({e})"


async def get_clean_kbju_status_text() -> str:
    batch_data = sheet_dash.batch_get(["G18:J18", "AF18:AI18", "AC17:AE18"])
    
    fact_row = batch_data[0][0] if (batch_data[0] and batch_data[0][0]) else [0,0,0,0]
    consumed = {"kcal": safe_num(fact_row[0]), "p": safe_num(fact_row[1]), "f": safe_num(fact_row[2]), "c": safe_num(fact_row[3])}
    
    plan_row = batch_data[1][0] if (batch_data[1] and batch_data[1][0]) else [0,0,0,0]
    targets = {"kcal": safe_num(plan_row[0]), "p": safe_num(plan_row[1]), "f": safe_num(plan_row[2]), "c": safe_num(plan_row[3])}
    
    goal_rows = batch_data[2] if len(batch_data) > 2 else []
    goal_context = "Настройка баланса"
    if goal_rows: goal_context = " | ".join([" ".join([str(cell) for cell in r]) for r in goal_rows])
    
    weight_trend_report = pull_weight_history_context()

    def calc_delta_str(fact, target, unit="г"):
        diff = target - fact
        if diff >= 0: return f"осталось: {int(diff)}{unit}"
        else: return f"🚨 ПЕРЕБОР: +{int(abs(diff))}{unit}"

    kcal_str = f"осталось: {int(targets['kcal'] - consumed['kcal'])} ккал" if (targets['kcal'] - consumed['kcal']) >= 0 else f"🚨 ПЕРЕБОР: +{int(abs(targets['kcal'] - consumed['kcal']))} ккал"

    report = (
        f"📊 АКТУАЛЬНЫЙ СТАТУС ДНЯ:\n"
        f"🎯 Стратегия: {goal_context.strip()}\n"
        f"───────────────────\n"
        f"⚖️ АНАЛИТИКА ВЕСА:{weight_trend_report}\n"
        f"───────────────────\n"
        f"🔋 Калории: {int(consumed['kcal'])} / {int(targets['kcal'])} ккал\n"
        f"   └ {kcal_str}\n\n"
        f"🧬 Белки: {int(consumed['p'])}г / {int(targets['p'])}г\n"
        f"   └ {calc_delta_str(consumed['p'], targets['p'])}\n\n"
        f"💧 Жиры: {int(consumed['f'])}г / {int(targets['f'])}г\n"
        f"   └ {calc_delta_str(consumed['f'], targets['f'])}\n\n"
        f"🍞 Углеводы: {int(consumed['c'])}г / {int(targets['c'])}г\n"
        f"   └ {calc_delta_str(consumed['c'], targets['c'])}"
    )
    return report

# ==============================================================================
# PIPELINE ROUTING FUNCTIONS
# ==============================================================================

def inject_into_agent_sheet(items):
    if not items: return False, "❌ Нейросеть не смогла распознать продукты."
    col_c_values = sheet_agent.col_values(3)
    while len(col_c_values) < 35: col_c_values.append("")
    log_report = []
    
    for food in items:
        cat = food.get('category')
        item = food.get('item')
        final_weight = food['weight']
        
        if cat not in PRODUCT_DIRECTORY or item not in PRODUCT_DIRECTORY[cat]:
            found = False
            if cat in PRODUCT_DIRECTORY:
                for real_item in PRODUCT_DIRECTORY[cat]:
                    if real_item.lower() == item.lower():
                        food['item'] = real_item; item = real_item; found = True; break
            if not found:
                log_report.append(f"❌ Продукта '{item}' нет в справочнике '{cat}'! Сначала добавь его в таблицу.")
                continue
                
        if final_weight == 0:
            if cat == "Блюдо" and item in BLUDO_GRAMS_CACHE:
                final_weight = BLUDO_GRAMS_CACHE[item]
            else: log_report.append(f"❌ {item} — пропущен (укажи граммы!)"); continue
                
        target_row = 5
        while target_row <= 35 and col_c_values[target_row - 1].strip() != "": target_row += 1
        if target_row > 35: return False, "❌ На листе Agent кончилось место!"
            
        numeric_weight = safe_num(final_weight)
        payload = [cat, item, food.get('termo', ''), numeric_weight]
        sheet_agent.update(range_name=f"B{target_row}:E{target_row}", values=[payload])
        col_c_values[target_row - 1] = item
        log_report.append(f"🔹 {item} ({numeric_weight}г)")
        
    if not log_report: return False, "❌ Операция отменена: продуктов нет в твоей базе."
    return True, "\n".join(log_report)


async def inject_direct_into_dashboard(meal_type, items):
    if not items: return "❌ Нейросеть вернула пустой пак продуктов.", []
    meal_key = meal_type.upper()
    if meal_key not in MEAL_RANGES: return f"❌ Неизвестный прием пищи: {meal_type}", []
    
    start_row, end_row = MEAL_RANGES[meal_key]
    expected_len = end_row - start_row + 1
    
    current_rows = sheet_dash.get(f"D{start_row}:D{end_row}")
    names = ["" for _ in range(expected_len)]
    if current_rows:
        for idx, r in enumerate(current_rows):
            if r: names[idx] = str(r[0]).strip()
            
    log_report = []
    updated_rows = []
    
    for food in items:
        cat = food.get('category')
        item = food.get('item')
        final_weight = food['weight']
        
        if cat not in PRODUCT_DIRECTORY or item not in PRODUCT_DIRECTORY[cat]:
            found = False
            if cat in PRODUCT_DIRECTORY:
                for real_item in PRODUCT_DIRECTORY[cat]:
                    if real_item.lower() == item.lower():
                        food['item'] = real_item; item = real_item; found = True; break
            if not found:
                log_report.append(f"❌ Продукта '{item}' нет в справочнике '{cat}'! Сначала добавь его в таблицу.")
                continue
                
        if final_weight == 0:
            if cat == "Блюдо" and item in BLUDO_GRAMS_CACHE:
                final_weight = BLUDO_GRAMS_CACHE[item]
            else: log_report.append(f"❌ {item} — пропущен (укажи граммы!)"); continue
                
        target_row = None
        for i in range(expected_len):
            if names[i] == "": target_row = start_row + i; break
                
        if target_row is None:
            error_msg = f"🖕 Денис, или хватит жрать, или собери нормальную сборку! В слоте [{meal_key}] больше нет места."
            if log_report: return "\n".join(log_report) + f"\n\n{error_msg}", updated_rows
            return error_msg, updated_rows
            
        numeric_weight = safe_num(final_weight)
        payload = [meal_key, cat, item, food.get('termo', ''), numeric_weight]
        sheet_dash.update(range_name=f"B{target_row}:F{target_row}", values=[payload])
        
        names[target_row - start_row] = item 
        log_report.append(f"⚡ {item} ({numeric_weight}г) -> [{meal_key}]")
        updated_rows.append(target_row)
        
    return "\n".join(log_report), updated_rows


async def execute_buffer_export(meal_type: str, message: types.Message):
    MEAL_ROWS = {"ЗАВТРАК": 5, "ОБЕД": 8, "УЖИН": 11}
    target_row = MEAL_ROWS.get(meal_type)
    if not target_row: return

    status = await message.answer(f"📦 Схлопываю буфер для [{meal_type}]...")
    try:
        check_cell_content = sheet_dash.acell(f"D{target_row}").value
        if check_cell_content and check_cell_content.strip() != "":
            await status.edit_text(f"🖕 Денис, слот {meal_type} уже занят в строке {target_row}!")
            return

        await asyncio.sleep(0.3)
        res = sheet_agent.get("E36:I36")
        if not res or not res[0]: return
        totals = res[0]
        while len(totals) < 5: totals.append("0")
            
        payload = [
            "Блюдо", f"Сборный {meal_type.lower()}", "", 
            safe_num(totals[0]), safe_num(totals[1]), safe_num(totals[2]), safe_num(totals[3]), safe_num(totals[4])
        ]
        sheet_dash.update(range_name=f"C{target_row}:J{target_row}", values=[payload])
        
        # --- ⚡ МИДЛВАРЬ: АГРЕГАЦИЯ И СОХРАНЕНИЕ ДЕТАЛИЗАЦИИ В L5:S35 ---
        try:
            agent_raw = sheet_agent.get("B5:I35")  # Категория, Продукт, Термо, Вес, Ккал, Б, Ж, У
            if agent_raw:
                current_daily = sheet_agent.get("L5:S35")
                daily_dict = {}
                
                # Кэшируем то, что уже записано в правом интрадей-блоке за сегодня
                if current_daily:
                    for row in current_daily:
                        if len(row) >= 8 and row[1].strip():
                            key = (row[1].strip().lower(), row[2].strip().lower())  # Ключ: (Продукт, Термо)
                            daily_dict[key] = [
                                row[0], row[1], row[2], 
                                safe_num(row[3]), safe_num(row[4]), safe_num(row[5]), safe_num(row[6]), safe_num(row[7])
                            ]

                # Сверяем новые продукты из левого блока ввода (B5:I35)
                for row in agent_raw:
                    if len(row) >= 2 and row[1].strip():
                        while len(row) < 8: row.append("0")
                        cat, item, termo = row[0], row[1], row[2]
                        w, kcal, p, f, c = safe_num(row[3]), safe_num(row[4]), safe_num(row[5]), safe_num(row[6]), safe_num(row[7])
                        if w == 0: continue
                        
                        key = (item.lower(), termo.lower())
                        if key in daily_dict:
                            # Продукт найден — суммируем граммы и пересчитываем макросы
                            daily_dict[key][3] += w
                            daily_dict[key][4] += kcal
                            daily_dict[key][5] += p
                            daily_dict[key][6] += f
                            daily_dict[key][7] += c
                        else:
                            # Новый продукт за день — создаем уникальную строку
                            daily_dict[key] = [cat, item, termo, w, kcal, p, f, c]

                # Формируем плоскую матрицу строго на 31 строку (размер L5:S35)
                new_daily_rows = list(daily_dict.values())
                final_matrix = []
                for i in range(31):
                    if i < len(new_daily_rows):
                        final_matrix.append(new_daily_rows[i])
                    else:
                        final_matrix.append(["", "", "", "", "", "", "", ""])
                
                sheet_agent.update(range_name="L5:S35", values=final_matrix)
        except Exception as ex:
            print(f"Ошибка логирования интрадей-буфера в L5:S35: {ex}")
        # --- КОНЕЦ МИДЛВАРЯ ---

        empty_block = [["", "", "", ""] for _ in range(31)]
        sheet_agent.update(range_name="B5:E35", values=empty_block)
        
        await asyncio.sleep(1.0) 
        
        total_kcal, total_p, total_f, total_c = 0, 0, 0, 0
        try:
            row_vals = sheet_dash.get(f"G{target_row}:J{target_row}")
            if row_vals and row_vals[0]:
                r = row_vals[0]
                while len(r) < 4: r.append("0")
                total_kcal = safe_num(r[0])
                total_p = safe_num(r[1])
                total_f = safe_num(r[2])
                total_c = safe_num(r[3])
        except Exception as e: print(e)
            
        kbju_added_text = (
            "🔥 КБЖУ сборки:\n"
            f"🔋 {int(total_kcal)} ккал  |  🧬 Б: {int(total_p)}г  |  💧 Ж: {int(total_f)}г  |  🍞 У: {int(total_c)}г"
        )
        await status.edit_text(f"🚀 {meal_type} монолитно залит в строку {target_row}!\n\n{kbju_added_text}")
        
    except Exception as e: await status.edit_text(f"❌ Ошибка выгрузки: {str(e)}")


# ==============================================================================
# TELEGRAM HANDLERS
# ==============================================================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Шеф, Кира V4.1 в строю. Модуль аналитики веса интегрирован в команду статус.")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    await send_help_manual(message)

@dp.message(Command("status"))
async def status_cmd_menu(message: types.Message):
    status_msg = await message.answer("📊 Считываю Дашборд и рассчитываю тренды...")
    clean_status = await get_clean_kbju_status_text()
    await status_msg.edit_text(clean_status)

@dp.message(Command("today"))
async def today_cmd(message: types.Message):
    status_msg = await message.answer("📊 Собираю дашборд за сегодня...")
    try:
        # 1. Считываем интрадей-детализацию из диапазона L5:S35
        daily_rows = sheet_agent.get("L5:S35")
        products_text = ""
        
        if daily_rows:
            for row in daily_rows:
                if len(row) >= 8 and row[1].strip():
                    name, termo = row[1].strip(), f" ({row[2].strip()})" if row[2].strip() else ""
                    w = int(safe_num(row[3]))
                    kcal = int(safe_num(row[4]))
                    p, f, c = round(safe_num(row[5]), 1), round(safe_num(row[6]), 1), round(safe_num(row[7]), 1)
                    if w > 0:
                        products_text += f"• {name}{termo}: {w}г — {kcal} ккал (Б: {p}г, Ж: {f}г, У: {c}г)\n"
        if not products_text:
            products_text = "_Пока пусто. Сборки приемов пищи не выполнялись._\n"

        # 2. Считываем прямые докидывания в Dashboard мимо сборок
        dash_ranges = ["B6:J7", "B9:J10", "B12:J13", "B14:J17"]
        batch_dash = sheet_dash.batch_get(dash_ranges)
        dash_text = ""
        
        for block in batch_dash:
            if block:
                for row in block:
                    if len(row) >= 6 and row[2].strip():  # Проверяем колонку D (Наименование)
                        meal, item = row[0].strip(), row[2].strip()
                        w, kcal = int(safe_num(row[4])), int(safe_num(row[5]))
                        dash_text += f"• [{meal}] {item}: {w}г — {kcal} ккал\n"
        if not dash_text:
            dash_text = "_Прямых докидываний мимо сборок не было._\n"

        # 3. Подтягиваем текущие агрегированные лимиты КБЖУ дня
        clean_status = await get_clean_kbju_status_text()
        
        report = (
            f"📅 *ДЕТАЛИЗИРОВАННЫЙ ЛОГ ЗА СЕГОДНЯ*\n\n"
            f"🥦 *В твоих сборках (детализация из L5:S35):*\n{products_text}\n"
            f"🍫 *В твоем дашборде (дополнительно):*\n{dash_text}\n"
            f"───────────────────\n"
            f"{clean_status}"
        )
        await status_msg.edit_text(report, parse_mode="Markdown")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка генерации дашборда: {e}")

@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    status_msg = await message.answer("📉 Высчитываю скользящие средние тренды...")
    try:
        today = datetime.date.today()
        m = today.month
        start_col = 1 + (m - 1) * 6
        day_col_letter = col_to_name(start_col)
        weight_col_letter = col_to_name(start_col + 5)
        
        res_data = sheet_hist.get(f"{day_col_letter}4:{weight_col_letter}35")
        
        kcal_list, b_list, j_list, u_list, weight_list = [], [], [], [], []
        
        if res_data:
            for row in res_data:
                if len(row) >= 6:
                    k, b, j, u, w = safe_num(row[1]), safe_num(row[2]), safe_num(row[3]), safe_num(row[4]), safe_num(row[5])
                    if k > 0:
                        kcal_list.append(k); b_list.append(b); j_list.append(j); u_list.append(u)
                    if w > 0:
                        weight_list.append(w)
        
        def calc_avg(lst, days):
            sub = lst[-days:] if len(lst) >= days else lst
            return int(sum(sub) / len(sub)) if sub else 0

        def calc_avg_w(lst, days):
            sub = lst[-days:] if len(lst) >= days else lst
            return round(sum(sub) / len(sub), 2) if sub else 0

        report = (
            f"📉 *АНАЛИТИКА ТРЕНДОВ (Скользящие средние)*\n\n"
            f"📅 *За последние 7 дней:*\n"
            f"🔋 Калории: {calc_avg(kcal_list, 7)} ккал\n"
            f"🧬 Б: {calc_avg(b_list, 7)}г  |  💧 Ж: {calc_avg(j_list, 7)}г  |  🍞 У: {calc_avg(u_list, 7)}г\n"
            f"⚖️ Средний вес: {calc_avg_w(weight_list, 7)} кг\n\n"
            f"📅 *За последние 30 дней (или текущий месяц):*\n"
            f"🔋 Калории: {calc_avg(kcal_list, 30)} ккал\n"
            f"🧬 Б: {calc_avg(b_list, 30)}г  |  💧 Ж: {calc_avg(j_list, 30)}г  |  🍞 У: {calc_avg(u_list, 30)}г\n"
            f"⚖️ Средний вес: {calc_avg_w(weight_list, 30)} кг\n\n"
            f"💡 _Шум убран. Если средний вес за 7 дней плавно растет — профицит настроен верно!_"
        )
        await status_msg.edit_text(report, parse_mode="Markdown")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка расчета трендов: {e}")

@dp.message(Command("breakfast"))
async def breakfast_cmd_menu(message: types.Message):
    await execute_buffer_export("ЗАВТРАК", message)

@dp.message(Command("lunch"))
async def lunch_cmd_menu(message: types.Message):
    await execute_buffer_export("ОБЕД", message)

@dp.message(Command("dinner"))
async def dinner_cmd_menu(message: types.Message):
    await execute_buffer_export("УЖИН", message)

@dp.message(F.text.invert_filter(Command))
async def handle_text_commands(message: types.Message):
    text_cmd = message.text.strip().upper()
    if text_cmd in ["СТАТУС", "АНАЛИТИКА", "БЖУ", "STATUS", "ANALYTICS", "ЧТО ПО БЖУ"]:
        await status_cmd_menu(message); return

    if text_cmd in ["TODAY", "СЕГОДНЯ", "ЧЕКНИ ДЕНЬ", "ЛОГ ЗА СЕГОДНЯ", "ЛОГ"]:
        await today_cmd(message); return

    if text_cmd in ["ТРЕНДЫ", "STATS", "СТАТИСТИКА ЗА НЕДЕЛЮ", "СТАТИСТИКА", "СТАТС"]:
        await stats_cmd(message); return
        
    if text_cmd in ["ГАЙД", "HELP", "СПРАВКА", "КОМАНДЫ", "ЧТО ТЫ УМЕЕШЬ"]:
        await send_help_manual(message); return
        
    if await try_process_modification_command(message.text, message): return
    if await try_process_weight_command(message.text, message): return
    
    if text_cmd in ["ЗАВТРАК", "ОБЕД", "УЖИН", "ПЕРЕКУС"]:
        await execute_buffer_export(text_cmd, message); return

@dp.message(F.voice)
async def handle_voice_flow(message: types.Message):
    status_msg = await message.answer("🎙️ Слушаю...")
    voice_file_path = f"{message.voice.file_id}.ogg"
    try:
        file_info = await bot.get_file(message.voice.file_id)
        await bot.download_file(file_info.file_path, voice_file_path)
        with open(voice_file_path, "rb") as audio_file:
            transcript = ai_client.audio.transcriptions.create(model="whisper-1", file=audio_file, language="ru")
        
        raw_text = transcript.text
        clean_text = re.sub(r'[.,!?]', '', raw_text.strip().upper()).strip()
        
        if await try_process_modification_command(raw_text, message):
            await status_msg.delete()
            if os.path.exists(voice_file_path): os.remove(voice_file_path)
            return

        if await try_process_weight_command(raw_text, message):
            await status_msg.delete()
            if os.path.exists(voice_file_path): os.remove(voice_file_path)
            return
            
        if clean_text in ["СТАТУС", "АНАЛИТИКА", "БЖУ", "STATUS", "ANALYTICS", "ПОКАЖИ СТАТУС", "ЧТО ПО БЖУ"]:
            await status_msg.delete(); await status_cmd_menu(message)
            if os.path.exists(voice_file_path): os.remove(voice_file_path)
            return

        if clean_text in ["TODAY", "СЕГОДНЯ", "ЧЕКНИ ДЕНЬ", "ЛОГ ЗА СЕГОДНЯ", "ЛОГ"]:
            await status_msg.delete(); await today_cmd(message)
            if os.path.exists(voice_file_path): os.remove(voice_file_path)
            return
            
        if clean_text in ["ТРЕНДЫ", "STATS", "СТАТИСТИКА ЗА НЕДЕЛЮ", "СТАТИСТИКА", "СТАТС"]:
            await status_msg.delete(); await stats_cmd(message)
            if os.path.exists(voice_file_path): os.remove(voice_file_path)
            return
            
        if clean_text in ["ГАЙД", "HELP", "СПРАВКА", "КОМАНДЫ", "ЧТО ТЫ УМЕЕШЬ"]:
            await status_msg.delete(); await send_help_manual(message)
            if os.path.exists(voice_file_path): os.remove(voice_file_path)
            return
            
        if clean_text in ["ЗАВТРАК", "ОБЕД", "УЖИН", "ПЕРЕКУС"]:
            await status_msg.delete(); await execute_buffer_export(clean_text, message)
            if os.path.exists(voice_file_path): os.remove(voice_file_path)
            return

        await status_msg.edit_text(f"📝 «{raw_text}»\n🤖 Сверяю маршруты...")
        parsed_data = parse_food_text_via_llm(raw_text, PRODUCT_DIRECTORY)
        items = parsed_data.get("items", [])
        
        items = apply_failsafe_mapping(items, raw_text)
        target_meal = parsed_data.get("target_meal", "")
        
        if target_meal != "":
            meal_mapping_roots = {"ЗАВТРАК": ["завтрак", "завтр"], "ОБЕД": ["обед"], "УЖИН": ["ужин"], "ПЕРЕКУС": ["перекус"]}
            allowed_roots = meal_mapping_roots.get(target_meal.upper(), [])
            if not any(root in raw_text.lower() for root in allowed_roots): target_meal = "" 

        if target_meal != "":
            report, updated_rows = await inject_direct_into_dashboard(target_meal, items)
            if "🖕" in report or "❌" in report:
                await status_msg.answer(report)
            else:
                await status_msg.edit_text(f"{report}\n\n⏳ Считаю КБЖУ добавленного...")
                await asyncio.sleep(1.0) 
                
                total_kcal, total_p, total_f, total_c = 0, 0, 0, 0
                for r_num in updated_rows:
                    try:
                        row_vals = sheet_dash.get(f"G{r_num}:J{r_num}")
                        if row_vals and row_vals[0]:
                            r = row_vals[0]
                            while len(r) < 4: r.append("0")
                            total_kcal += safe_num(r[0])
                            total_p += safe_num(r[1])
                            total_f += safe_num(r[2])
                            total_c += safe_num(r[3])
                    except Exception as e: print(e)
                        
                kbju_added_text = (
                    "🔥 Добавлено КБЖУ:\n"
                    f"🔋 {int(total_kcal)} ккал  |  🧬 Б: {int(total_p)}г  |  💧 Ж: {int(total_f)}г  |  🍞 У: {int(total_c)}г"
                )
                await status_msg.edit_text(f"{report}\n\n{kbju_added_text}")
        else:
            success, report = inject_into_agent_sheet(items)
            await status_msg.answer(report)

        if os.path.exists(voice_file_path): os.remove(voice_file_path)
    except Exception as e:
        await status_msg.answer(f"❌ Сбой пайплайна: {str(e)}")
        if os.path.exists(voice_file_path): os.remove(voice_file_path)

# ==============================================================================
# ИНИЦИАЛИЗАЦИЯ КНОПКИ МЕНЮ КОМАНД В ТЕЛЕГРАМЕ (Откалибровано под правила ТГ)
# ==============================================================================
async def set_main_menu(bot: Bot):
    main_menu_commands = [
        types.BotCommand(command="start", description="Перезапустить бота"),
        types.BotCommand(command="help", description="Показать краткий инженерный гайд"),
        types.BotCommand(command="status", description="Вывести сухой отчет КБЖУ дня"),
        types.BotCommand(command="today", description="Детализированный лог тарелок за сегодня"),
        types.BotCommand(command="stats", description="Скользящие средние тренды за неделю/месяц"),
        types.BotCommand(command="breakfast", description="Схлопнуть буфер завтрака в Дашборд"),
        types.BotCommand(command="lunch", description="Схлопнуть буфер обеда в Дашборд"),
        types.BotCommand(command="dinner", description="Схлопнуть буфер ужина в Дашборд")
    ]
    await bot.set_my_commands(main_menu_commands)

async def main():
    load_all_products_from_sheets()
    await set_main_menu(bot) 
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())