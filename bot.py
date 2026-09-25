import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

TOKEN = "8479473750:AAEq-Sdc5krdwvwIrqxUjeJQ0shOBdU1P3A"

bot = Bot(token=TOKEN)
dp = Dispatcher()

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 На сегодня"), KeyboardButton(text="📅 На завтра")],
        [KeyboardButton(text="🟢 Понеділок"), KeyboardButton(text="🟢 Вівторок"), KeyboardButton(text="🟢 Середа")],
        [KeyboardButton(text="🟢 Четвер"), KeyboardButton(text="🟢 П'ятниця")],
        [KeyboardButton(text="🔗 Всі посилання на Zoom"), KeyboardButton(text="🔄 Замены")]
    ],
    resize_keyboard=True
)

# Функция правильного определения недели (теперь вся текущая неделя имеет одинаковый тип)
def get_week_type(target_date: datetime):
    start_date = datetime(2026, 8, 31) # Начало отсчета (понедельник, Над рискою)
    
    # Сдвигаем целевую дату и дату старта на понедельник их недель, 
    # чтобы вся неделя (пн-вс) считалась одинаково
    target_monday = target_date - timedelta(days=target_date.weekday())
    start_monday = start_date - timedelta(days=start_date.weekday())
    
    days_diff = (target_monday - start_monday).days
    week_index = days_diff // 7
    
    if week_index % 2 == 0:
        return "Над рискою"
    else:
        return "Під рискою"

# База расписания
SCHEDULE = {
    "Понеділок": [
        ("1 пара (8:30-9:50)", "Вільно", None),
        ("2 пара (10:00-11:20)", "Квітівництво — Жупіньська Катерина Юріївна", "https://us05web.zoom.us/j/9856624171?pwd=vdxkCpVL6bpNo514BbcLE7iKNWLsGK.1"),
        ("3 пара (12:00-13:20)", "Фізра — Тетяна Дрокина", "https://us04web.zoom.us/j/5318097982?pwd=aK3pQZ6y4arwePmfQlUIXpUQWPndkb.1"),
        ("4 пара (13:30-13:50)", "Над рискою: Грунтознавство — Ковалжи Наталія Ігорівна\nПід рискою: Основи права — Циганенко Роман Петрович", "https://us02web.zoom.us/j/3188320656?pwd=QWgycFc4S2JjUXk5ZDhoNnhrYjljdz09&omn=82972358550")
    ],
    "Вівторок": [
        ("1 пара (8:30-9:50)", "Вільно", None),
        ("2 пара (10:00-11:20)", "Вільно", None),
        ("3 пара (12:00-13:20)", "Екологія — Батіг Ганна Володимирівна", "https://us02web.zoom.us/j/8467559257?pwd=emE1NzZuS0RiV0tOODN6OTFtU0twUT09"),
        ("4 пара (13:30-13:50)", "Інформатика — Бембель Олександр Дмитрович", "https://us02web.zoom.us/j/7546161590?pwd=Yk8vNWU2bnpXSFpsTHBPZHBGOWV3dz09")
    ],
    "Середа": [
        ("1 пара (8:30-9:50)", "Вільно", None),
        ("2 пара (10:00-11:20)", "Креслення — Переходович Світлана Сергіївна", "https://us04web.zoom.us/j/74812602094?pwd=LtakeMi2lnjEbJZVqbnt2mbyXUhaxJ.1"),
        ("3 пара (12:00-13:20)", "Історія — Орел Олександр Сергійович", "https://us02web.zoom.us/j/9790221936?omn=71559763873"),
        ("4 пара (13:30-13:50)", "Основи права — Циганенко Роман Петрович", "https://us05web.zoom.us/j/7399873325?pwd=SVFFQUsrK3dpSTZ5NHlOWTJPZ2cxQT09")
    ],
    "Четвер": [
        ("1 пара (8:30-9:50)", "Вільно", None),
        ("2 пара (10:00-11:20)", "Грунтознавство — Ковалжи Наталія Ігорівна", "https://us02web.zoom.us/j/3188320656?pwd=QWgycFc4S2JjUXk5ZDhoNnhrYjljdz09&omn=82972358550"),
        ("3 пара (12:00-13:20)", "Ботаніка — Сеніна Ірина Леонідівна", "https://us04web.zoom.us/j/75480487895?pwd=REZ04jdCCFGTu8srgqa1vFOXCaaPzo.1"),
        ("4 пара (13:30-13:50)", "Інформатика — Бембель Олександр Дмитрович", "https://us02web.zoom.us/j/7546161590?pwd=Yk8vNWU2bnpXSFpsTHBPZHBGOWV3dz09")
    ],
    "П'ятниця": [
        ("1 пара (8:30-9:50)", "Ботаніка — Сеніна Ірина Леонідівна", "https://us04web.zoom.us/j/75480487895?pwd=REZ04jdCCFGTu8srgqa1vFOXCaaPzo.1"),
        ("2 пара (10:00-11:20)", "Англійська мова — Камишнікова Анна", "https://us04web.zoom.us/j/4492224328?pwd=Q21OQjBQdUxWejRMczBRczQ1c0ZSdz09"),
        ("3 пара (12:00-13:20)", "Вільно", None),
        ("4 пара (13:30-13:50)", "Вільно", None)
    ]
}

# Временные замены на конкретные даты (например, на 28.09.2026 из вашего скриншота)
REPLACEMENTS = {
    "28.09.2026": {
        2: ("2 пара (10:00-11:20)", "Вільно", None),
        3: ("3 пара (12:00-13:20)", "Квітівництво — Жупіньська Катерина Юріївна", "https://us05web.zoom.us/j/9856624171?pwd=vdxkCpVL6bpNo514BbcLE7iKNWLsGK.1")
    }
}

ZOOM_ALL = (
    "🔗 **Всі посилання на Zoom (А-22):**\n\n"
    "• **Квітівництво** (Жупіньська): [Посилання](https://us05web.zoom.us/j/9856624171?pwd=vdxkCpVL6bpNo514BbcLE7iKNWLsGK.1)\n"
    "• **Фізра** (Дрокина): [Посилання](https://us04web.zoom.us/j/5318097982?pwd=aK3pQZ6y4arwePmfQlUIXpUQWPndkb.1)\n"
    "• **Грунтознавство** (Ковалжи): [Посилання](https://us02web.zoom.us/j/3188320656?pwd=QWgycFc4S2JjUXk5ZDhoNnhrYjljdz09&omn=82972358550)\n"
    "• **Основи права** (Циганенко): [Посилання](https://us05web.zoom.us/j/7399873325?pwd=SVFFQUsrK3dpSTZ5NHlOWTJPZ2cxQT09)\n"
    "• **Екологія** (Батіг): [Посилання](https://us02web.zoom.us/j/8467559257?pwd=emE1NzZuS0RiV0tOODN6OTFtU0twUT09)\n"
    "• **Інформатика** (Бембель): [Посилання](https://us07web.zoom.us/j/7546161590?pwd=Yk8vNWU2bnpXSFpsTHBPZHBGOWV3dz09)\n"
    "• **Креслення** (Переходович): [Посилання](https://us04web.zoom.us/j/74812602094?pwd=LtakeMi2lnjEbJZVqbnt2mbyXUhaxJ.1)\n"
    "• **Історія** (Орел): [Посилання](https://us04web.zoom.us/j/9790221936?omn=71559763873)\n"
    "• **Ботаніка** (Сеніна): [Посилання](https://us04web.zoom.us/j/75480487895?pwd=REZ04jdCCFGTu8srgqa1vFOXCaaPzo.1)\n"
    "• **Англійська мова** (Камишнікова): [Посилання](https://us04web.zoom.us/j/4492224328?pwd=Q21OQjBQdUxWejRMczBRczQ1c0ZSdz09)"
)

def send_schedule_for_day(day_name, target_date):
    lessons = SCHEDULE.get(day_name)
    if not lessons:
        return f"📅 На **{day_name}** у групи А-22 занять немає (вихідний) 🎉"
    
    week_type = get_week_type(target_date)
    date_str = target_date.strftime("%d.%0m.%Y".replace('0m', 'm')) # формат ДД.ММ.ГГГГ
    date_str_formatted = target_date.strftime("%d.%m.%Y")
    
    day_replacements = REPLACEMENTS.get(date_str_formatted, {})
    
    response = f"📅 **Розклад для групи А-22 — {day_name.upper()}** ({date_str_formatted})\n*(Тиждень: **{week_type}**)*:\n\n"
    
    for index, (time_slot, subject, link) in enumerate(lessons, start=1):
        # Проверяем, есть ли замена на эту пару в этот день
        if index in day_replacements:
            _, subject, link = day_replacements[index]
        else:
            # Автоматический выбор предмета в зависимости от недели (Над рискою / Під рискою)
            if "Над рискою:" in subject and "Під рискою:" in subject:
                lines = subject.split("\n")
                if week_type == "Над рискою":
                    subject = lines[0].replace("Над рискою: ", "🎯 ")
                else:
                    subject = lines[1].replace("Під рискою: ", "🎯 ")
                
        if link and "Вільно" not in subject:
            response += f"🔹 **{time_slot}**\n   {subject}\n   🔗 [Підключитися до Zoom]({link})\n\n"
        else:
            response += f"🔹 **{time_slot}**\n   {subject}\n\n"
            
    if day_replacements:
        response += "🔄 *Діють офіційні заміни на цей день!*"
    return response

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! 👋 Це оновлений бот групи **А-22**. Обирай день або потрібну опцію на клавіатурі нижче:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# Обработка дней недели (всегда берем БЛИЖАЙШИЙ будущий или текущий день недели)
@dp.message(F.text.in_(["🟢 Понеділок", "🟢 Вівторок", "🟢 Середа", "🟢 Четвер", "🟢 П'ятниця"]))
async def day_schedule(message: Message):
    day_map_num = {
        "Понеділок": 0,
        "Вівторок": 1,
        "Середа": 2,
        "Четвер": 3,
        "П'ятниця": 4
    }
    day_name = message.text.replace("🟢 ", "")
    
    now = datetime.now()
    current_weekday = now.weekday()
    target_weekday = day_map_num[day_name]
    
    # Считаем разницу дней, чтобы всегда брать ближайший день (если день на этой неделе уже прошел, берем следующий)
    days_ahead = target_weekday - current_weekday
    if days_ahead < 0:
        days_ahead += 7
        
    target_date = now + timedelta(days=days_ahead)
    
    response = send_schedule_for_day(day_name, target_date)
    await message.answer(response, parse_mode="Markdown", disable_web_page_preview=True)

# Обработка "На сегодня" / "На завтра"
@dp.message(F.text.in_(["📅 На сегодня", "📅 На завтра", "Расписание на сегодня", "Расписание на завтра"]))
async def today_tomorrow_schedule(message: Message):
    days_map = {
        0: "Понеділок",
        1: "Вівторок",
        2: "Середа",
        3: "Четвер",
        4: "П'ятниця",
        5: "Субота",
        6: "Неділя"
    }
    
    now = datetime.now()
    target_date = now
    
    if "завтра" in message.text.lower():
        target_date = now + timedelta(days=1)
        
    weekday = target_date.weekday()
    day_name = days_map.get(weekday)
    
    if day_name in SCHEDULE:
        response = send_schedule_for_day(day_name, target_date)
    else:
        response = f"📅 Сьогодні/завтра (**{day_name}**) — вихідний день, пар немає! 🎉"
        
    await message.answer(response, parse_mode="Markdown", disable_web_page_preview=True)

@dp.message(F.text == "🔗 Всі посилання на Zoom")
async def all_zoom(message: Message):
    await message.answer(ZOOM_ALL, parse_mode="Markdown", disable_web_page_preview=True)

@dp.message(F.text.in_(["🔄 Замены", "Замены"]))
async def replacements_info(message: Message):
    await message.answer("🔄 Бот автоматично перевіряє офіційні заміни на поточні дати та коригує розклад.", parse_mode="Markdown")

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())