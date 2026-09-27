import asyncio
import csv
import io
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import requests
from aiohttp import web

# Токен теперь берётся из переменной окружения BOT_TOKEN (задаётся в Render,
# в Environment → Environment Variables). В коде и на GitHub его быть не должно.
TOKEN = os.environ["BOT_TOKEN"]

# Render запускает контейнер в UTC, а не по киевскому времени — если считать
# "сегодня/завтра" через голый datetime.now(), глубокой ночью (0:00–3:00 по
# Киеву) сервер ещё будет думать, что идёт предыдущий день. Поэтому везде
# используем datetime.now(KYIV) вместо datetime.now().
KYIV = ZoneInfo("Europe/Kyiv")

GROUP_NAME = "А-22"

# ID таблицы замен (кусок ссылки между /d/e/ и /pubhtml)
SUBSTITUTIONS_SHEET_ID = "2PACX-1vQlLOazl1JOcO5xS1-Ryan5BF2lve26w7lRG-hQTk3J48S8uwm8UtvWJmteeCyqVUtxqJVN7RHUc1I-"
SUBSTITUTIONS_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/e/{SUBSTITUTIONS_SHEET_ID}/pub?gid=0&single=true&output=csv"
)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# REPLACEMENTS[date_str][pair_number] = (_, subject, zoom_link)
# date_str в формате "%d.%m.%Y", как в send_schedule_for_day
REPLACEMENTS: dict[str, dict[int, tuple]] = {}

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 На сегодня"), KeyboardButton(text="📅 На завтра")],
        [KeyboardButton(text="🟢 Понеділок"), KeyboardButton(text="🟢 Вівторок"), KeyboardButton(text="🟢 Середа")],
        [KeyboardButton(text="🟢 Четвер"), KeyboardButton(text="🟢 П'ятниця")],
        [KeyboardButton(text="🔗 Всі посилання на Zoom"), KeyboardButton(text="🔄 Замены")]
    ],
    resize_keyboard=True
)

def get_week_type(target_date: datetime):
    start_date = datetime(2026, 8, 31, tzinfo=KYIV)
    target_monday = target_date - timedelta(days=target_date.weekday())
    start_monday = start_date - timedelta(days=start_date.weekday())
    days_diff = (target_monday - start_monday).days
    week_index = days_diff // 7
    if week_index % 2 == 0:
        return "Над рискою"
    else:
        return "Під рискою"

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

# Ключ — заметная часть названия предмета в нижнем регистре, значение — ссылка.
# Используется, чтобы подставить Zoom-ссылку к предмету, пришедшему из таблицы замен
# (там нет ссылок, только название предмета и преподаватель).
ZOOM_BY_SUBJECT = {
    "квіт": "https://us05web.zoom.us/j/9856624171?pwd=vdxkCpVL6bpNo514BbcLE7iKNWLsGK.1",  # квітництво / квітникарство
    "фізр": "https://us04web.zoom.us/j/5318097982?pwd=aK3pQZ6y4arwePmfQlUIXpUQWPndkb.1",
    "грунтознав": "https://us02web.zoom.us/j/3188320656?pwd=QWgycFc4S2JjUXk5ZDhoNnhrYjljdz09&omn=82972358550",
    "право": "https://us05web.zoom.us/j/7399873325?pwd=SVFFQUsrK3dpSTZ5NHlOWTJPZ2cxQT09",
    "еколог": "https://us02web.zoom.us/j/8467559257?pwd=emE1NzZuS0RiV0tOODN6OTFtU0twUT09",
    "інформатик": "https://us02web.zoom.us/j/7546161590?pwd=Yk8vNWU2bnpXSFpsTHBPZHBGOWV3dz09",
    "креслен": "https://us04web.zoom.us/j/74812602094?pwd=LtakeMi2lnjEbJZVqbnt2mbyXUhaxJ.1",
    "історі": "https://us02web.zoom.us/j/9790221936?omn=71559763873",
    "ботаніка": "https://us04web.zoom.us/j/75480487895?pwd=REZ04jdCCFGTu8srgqa1vFOXCaaPzo.1",
    "англійськ": "https://us04web.zoom.us/j/4492224328?pwd=Q21OQjBQdUxWejRMczBRczQ1c0ZSdz09",
}

def find_zoom_for_subject(subject: str) -> str | None:
    if not subject:
        return None
    low = subject.lower()
    for key, link in ZOOM_BY_SUBJECT.items():
        if key in low:
            return link
    return None

ZOOM_ALL = (
    "🔗 **Всі посилання на Zoom (А-22):**\n\n"
    "• **Квітівництво** (Жупіньська): [Посилання](https://us05web.zoom.us/j/9856624171?pwd=vdxkCpVL6bpNo514BbcLE7iKNWLsGK.1)\n"
    "• **Фізра** (Дрокина): [Посилання](https://us04web.zoom.us/j/5318097982?pwd=aK3pQZ6y4arwePmfQlUIXpUQWPndkb.1)\n"
    "• **Грунтознавство** (Ковалжи): [Посилання](https://us02web.zoom.us/j/3188320656?pwd=QWgycFc4S2JjUXk5ZDhoNnhrYjljdz09&omn=82972358550)\n"
    "• **Основи права** (Циганенко): [Посилання](https://us05web.zoom.us/j/7399873325?pwd=SVFFQUsrK3dpSTZ5NHlOWTJPZ2cxQT09)\n"
    "• **Екологія** (Батіг): [Посилання](https://us02web.zoom.us/j/8467559257?pwd=emE1NzZuS0RiV0tOODN6OTFtU0twUT09)\n"
    "• **Інформатика** (Бембель): [Посилання](https://us07web.zoom.us/j/7546161590?pwd=Yk8vNWU2bnpXSFpsTHBPZHBGOWV3dz09)\n"
    "• **Креслення** (Переходович): [Посилання](https://us04web.zoom.us/j/74812602094?pwd=LtakeMi2lnjEbJZVqbnt2mbyXUhaxJ.1)\n"
    "• **Історія** (Орел): [Посилання](https://us02web.zoom.us/j/9790221936?omn=71559763873)\n"
    "• **Ботаніка** (Сеніна): [Посилання](https://us04web.zoom.us/j/75480487895?pwd=REZ04jdCCFGTu8srgqa1vFOXCaaPzo.1)\n"
    "• **Англійська мова** (Камишнікова): [Посилання](https://us04web.zoom.us/j/4492224328?pwd=Q21OQjBQdUxWejRMczBRczQ1c0ZSdz09)"
)


def _normalize_group(name: str) -> str:
    """"А-22", "А - 22", "A-22" и т.п. должны считаться одной и той же группой
    (в т.ч. Латинская A и Кириллическая А, они визуально неотличимы)."""
    return name.replace(" ", "").replace("–", "-").upper().replace("A", "А")


def _find_announcement_date(rows: list[list[str]]):
    """Ищет дату вида 28.09.2026 в первых строках таблицы (обычно в заголовке)."""
    for row in rows[:5]:
        for cell in row:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", cell)
            if m:
                day, month, year = map(int, m.groups())
                try:
                    return datetime(year, month, day)
                except ValueError:
                    continue
    return None


def _extract_group_rows(rows: list[list[str]], group_name: str) -> list[dict]:
    """Достаёт строки замен для группы, поддерживая "склеенные" ячейки —
    название группы указано только в первой строке блока, дальше пусто."""
    result = []
    current_group = None
    target = _normalize_group(group_name)

    for row in rows:
        cells = [c.strip() for c in row]
        if not any(cells) or len(cells) < 3:
            continue

        group_cell, pair_cell, subject_cell = cells[0], cells[1], cells[2]
        teacher_cell = cells[4] if len(cells) > 4 else ""

        if group_cell:
            current_group = group_cell

        if not current_group or _normalize_group(current_group) != target or not pair_cell:
            continue

        result.append({"pair": pair_cell, "subject": subject_cell, "teacher": teacher_cell})

    return result


def _all_group_names(rows: list[list[str]]) -> list[str]:
    """Диагностика: собирает все непустые значения из первой колонки таблицы —
    чтобы увидеть, как реально записаны названия групп, если совпадение не нашлось."""
    names = []
    for row in rows:
        if not row:
            continue
        first = row[0].strip()
        if first and first not in names:
            names.append(f"{first!r} (нормализовано: {_normalize_group(first)!r})")
    return names


def _fetch_replacements_sync():
    """Синхронная (блокирующая) часть — запускается в отдельном потоке,
    чтобы не подвешивать бота во время сетевого запроса."""
    response = requests.get(SUBSTITUTIONS_CSV_URL, timeout=15)
    response.raise_for_status()
    text = response.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))

    announced_date = _find_announcement_date(rows)
    if not announced_date:
        return None, [], rows

    return announced_date, _extract_group_rows(rows, GROUP_NAME), rows


async def fetch_replacements():
    """Скачивает таблицу замен и, если объявление на сегодня или завтра,
    кладёт разобранные замены в REPLACEMENTS."""
    try:
        announced_date, group_rows, raw_rows = await asyncio.to_thread(_fetch_replacements_sync)
    except Exception:
        logging.exception("Ошибка при загрузке таблицы замен")
        return

    if not announced_date:
        logging.info("Проверка замен: дата объявления не найдена (или замен пока нет).")
        return

    date_str = announced_date.strftime("%d.%m.%Y")

    if not group_rows:
        logging.info("Проверка замен на %s: для %s замен нет.", date_str, GROUP_NAME)
        logging.info("Диагностика — все названия групп, найденные в таблице: %s", _all_group_names(raw_rows))
        return

    day_map: dict[int, tuple] = {}
    for row in group_rows:
        subject = row["subject"] or "Вільна"
        teacher = row["teacher"]
        full_subject = f"{subject} — {teacher}" if teacher and "Вільн" not in subject else subject
        link = find_zoom_for_subject(subject)

        # "3,4" -> применяем и к 3-й, и к 4-й паре
        for part in row["pair"].split(","):
            part = part.strip()
            if part.isdigit():
                day_map[int(part)] = (None, full_subject, link)

    REPLACEMENTS[date_str] = day_map
    logging.info("Замены на %s обновлены для %s: %s", date_str, GROUP_NAME, day_map)


def send_schedule_for_day(day_name, target_date):
    lessons = SCHEDULE.get(day_name)
    if not lessons:
        return f"📅 На **{day_name}** у групи А-22 занять немає (вихідний) 🎉"
    
    week_type = get_week_type(target_date)
    date_str_formatted = target_date.strftime("%d.%m.%Y")
    day_replacements = REPLACEMENTS.get(date_str_formatted, {})
    
    response = f"📅 **Розклад для групи А-22 — {day_name.upper()}** ({date_str_formatted})\n*(Тиждень: **{week_type}**)*:\n\n"
    
    for index, (time_slot, subject, link) in enumerate(lessons, start=1):
        if index in day_replacements:
            _, subject, link = day_replacements[index]
        else:
            if "Над рискою:" in subject and "Під рискою:" in subject:
                lines = subject.split("\n")
                if week_type == "Над рискою":
                    subject = lines[0].replace("Над рискою: ", "🎯 ")
                else:
                    subject = lines[1].replace("Під рискою: ", "🎯 ")
                
        if link and "Вільно" not in subject and "Вільна" not in subject:
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

@dp.message(F.text.in_(["🟢 Понеділок", "🟢 Вівторок", "🟢 Середа", "🟢 Четвер", "🟢 П'ятниця"]))
async def day_schedule(message: Message):
    day_map_num = {"Понеділок": 0, "Вівторок": 1, "Середа": 2, "Четвер": 3, "П'ятниця": 4}
    day_name = message.text.replace("🟢 ", "")
    
    now = datetime.now(KYIV)
    current_weekday = now.weekday()
    target_weekday = day_map_num[day_name]
    
    # Всегда показываем предстоящий день недели или сегодняшний
    days_ahead = target_weekday - current_weekday
    if days_ahead < 0:
        days_ahead += 7
        
    target_date = now + timedelta(days=days_ahead)
    response = send_schedule_for_day(day_name, target_date)
    await message.answer(response, parse_mode="Markdown", disable_web_page_preview=True)

@dp.message(F.text.in_(["📅 На сегодня", "📅 На завтра", "Расписание на сегодня", "Расписание на завтра"]))
async def today_tomorrow_schedule(message: Message):
    days_map = {0: "Понеділок", 1: "Вівторок", 2: "Середа", 3: "Четвер", 4: "П'ятниця", 5: "Субота", 6: "Неділя"}
    now = datetime.now(KYIV)
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
    await message.answer("🔄 Перевіряю офіційні заміни...")
    await fetch_replacements()

    now = datetime.now(KYIV)
    today_str = now.strftime("%d.%m.%Y")
    tomorrow_str = (now + timedelta(days=1)).strftime("%d.%m.%Y")

    parts = []
    for label, date_str in (("на сьогодні", today_str), ("на завтра", tomorrow_str)):
        day_reps = REPLACEMENTS.get(date_str)
        if day_reps:
            lines = "\n".join(f"  {pair} пара: {info[1]}" for pair, info in sorted(day_reps.items()))
            parts.append(f"**Заміни {label} ({date_str}):**\n{lines}")

    if parts:
        await message.answer("\n\n".join(parts), parse_mode="Markdown")
    else:
        await message.answer("Замін на сьогодні/завтра немає — діє звичайний розклад ✅")

async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    logging.basicConfig(level=logging.INFO)
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(fetch_replacements, 'cron', hour=17, minute=0)
    scheduler.start()
    
    await fetch_replacements()
    
    await web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
