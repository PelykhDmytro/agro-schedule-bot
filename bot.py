import asyncio
import csv
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
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

# Telegram id старости (и любых других админов через запятую) — узнать у
# @userinfobot. Только эти люди смогут добавлять домашние задания.
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

# Хранилище домашних заданий — JSONBin.io (бесплатный внешний JSON-стор).
# Нужно, потому что на бесплатном Render локальные файлы/память не переживают
# сон и редеплой сервиса — а домашка должна сохраняться надолго.
JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY")
JSONBIN_BIN_ID = os.environ.get("JSONBIN_BIN_ID")
JSONBIN_BASE = "https://api.jsonbin.io/v3/b"

# Google Classroom — автоматическая подтяжка домашних заданий
CLASSROOM_CLIENT_ID = os.environ.get("CLASSROOM_CLIENT_ID")
CLASSROOM_CLIENT_SECRET = os.environ.get("CLASSROOM_CLIENT_SECRET")
CLASSROOM_REFRESH_TOKEN = os.environ.get("CLASSROOM_REFRESH_TOKEN")
CLASSROOM_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
]

# HOMEWORK[date_str][subject_lower] = текст завдання
HOMEWORK: dict[str, dict[str, str]] = {}

# USERS[user_id_str] = {"name", "username", "first_seen", "last_seen", "count"}
USERS: dict[str, dict] = {}

# Кто подписан на напоминания за 5 минут до пары (user_id как строка)
SUBSCRIBERS: set[str] = set()

# Общая обёртка над содержимым бина в JSONBin — храним домашку, статистику
# пользователей и подписки на напоминания вместе, чтобы не заводить много бинов.
BIN_DATA: dict = {"homework": {}, "users": {}, "subscribers": []}

DAY_NAMES = {0: "Понеділок", 1: "Вівторок", 2: "Середа", 3: "Четвер", 4: "П'ятниця", 5: "Субота", 6: "Неділя"}

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
        [KeyboardButton(text="🔗 Всі посилання на Zoom"), KeyboardButton(text="🔄 Замены")],
        [KeyboardButton(text="🔔 Нагадування")],
        [KeyboardButton(text="🎓 Classroom"), KeyboardButton(text="⚠️ Прострочені")]
    ],
    resize_keyboard=True,
    is_persistent=True
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
        ("3 пара (12:00-13:20)", "Історія — Орел Олександр Сергійович", "https://us04web.zoom.us/j/9790221936?pwd=1234567&omn=71559763873"),
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
    "прав": "https://us05web.zoom.us/j/7399873325?pwd=SVFFQUsrK3dpSTZ5NHlOWTJPZ2cxQT09",
    "еколог": "https://us02web.zoom.us/j/8467559257?pwd=emE1NzZuS0RiV0tOODN6OTFtU0twUT09",
    "інформатик": "https://us02web.zoom.us/j/7546161590?pwd=Yk8vNWU2bnpXSFpsTHBPZHBGOWV3dz09",
    "креслен": "https://us04web.zoom.us/j/74812602094?pwd=LtakeMi2lnjEbJZVqbnt2mbyXUhaxJ.1",
    "історі": "https://us04web.zoom.us/j/9790221936?pwd=1234567&omn=71559763873",
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

# Тематическая иконка под конкретный предмет — ключи те же "корни", что и в
# ZOOM_BY_SUBJECT, чтобы не дублировать логику сопоставления.
SUBJECT_ICONS = {
    "квіт": "🌸",           # квітництво
    "фізр": "⚽",           # фізра
    "грунтознав": "🌍",      # ґрунтознавство
    "прав": "⚖️",          # основи права
    "еколог": "🌿",          # екологія
    "інформатик": "💻",      # інформатика
    "креслен": "📐",         # креслення
    "історі": "📜",          # історія
    "ботаніка": "🌱",        # ботаніка
    "англійськ": "🇬🇧",       # англійська мова
}

def subject_icon(subject: str) -> str:
    if not subject or "Вільн" in subject:
        return "🆓"
    low = subject.lower()
    for key, icon in SUBJECT_ICONS.items():
        if key in low:
            return icon
    return "📘"


def canonical_subject_key(text: str) -> str:
    """Приводит вольное написание предмета к одному из "корневых" ключей из
    ZOOM_BY_SUBJECT (та же логика, что уже надёжно работает для Zoom-ссылок,
    и учитывает варианты написания вроде "Квітництво"/"Квітівництво")."""
    low = text.lower()
    for key in ZOOM_BY_SUBJECT:
        if key in low:
            return key
    return low


def _classroom_configured() -> bool:
    return bool(CLASSROOM_CLIENT_ID and CLASSROOM_CLIENT_SECRET and CLASSROOM_REFRESH_TOKEN)


def _fetch_classroom_sync() -> list[dict]:
    """Синхронная (блокирующая) часть — идёт в отдельном потоке. Собирает
    свежие задания (coursework) и оголошення (announcements) по всем активным
    курсам Google Classroom, к которым привязан авторизованный аккаунт."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=CLASSROOM_REFRESH_TOKEN,
        client_id=CLASSROOM_CLIENT_ID,
        client_secret=CLASSROOM_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=CLASSROOM_SCOPES,
    )
    service = build("classroom", "v1", credentials=creds, cache_discovery=False)

    courses = service.courses().list(courseStates=["ACTIVE"]).execute().get("courses", [])

    items = []
    for course in courses:
        course_id = course["id"]
        course_name = course.get("name", "Без назви")

        cw_resp = service.courses().courseWork().list(
            courseId=course_id, orderBy="dueDate desc", pageSize=15
        ).execute()
        for cw in cw_resp.get("courseWork", []):
            due = cw.get("dueDate")
            due_str = f"{due['day']:02d}.{due['month']:02d}.{due['year']}" if due else None
            items.append({
                "course": course_name,
                "title": cw.get("title", ""),
                "description": (cw.get("description") or "").strip(),
                "due": due_str,
                "due_date_obj": (due["year"], due["month"], due["day"]) if due else None,
                "update_time": cw.get("updateTime"),
                "link": cw.get("alternateLink"),
                "type": "завдання",
            })

        ann_resp = service.courses().announcements().list(
            courseId=course_id, orderBy="updateTime desc", pageSize=5
        ).execute()
        for ann in ann_resp.get("announcements", []):
            text = (ann.get("text") or "").strip()
            if not text:
                continue
            items.append({
                "course": course_name,
                "title": text[:80],
                "description": text,
                "due": None,
                "due_date_obj": None,
                "update_time": ann.get("updateTime"),
                "link": ann.get("alternateLink"),
                "type": "оголошення",
            })

    return items


async def fetch_classroom() -> list[dict]:
    """Тянет сырые данные из Classroom (без фильтрации по срокам — это делают
    отдельно _group_classroom_actual и _filter_classroom_overdue)."""
    if not _classroom_configured():
        return []
    try:
        return await asyncio.to_thread(_fetch_classroom_sync)
    except Exception:
        logging.exception("Не вдалося отримати дані з Google Classroom")
        return []


def _group_classroom_actual(items: list[dict]) -> dict[str, list[dict]]:
    """Делит ещё не просроченные задания на корзины по оставшемуся сроку —
    как на твоей схеме: до 3 днів / до тижня / до місяця / пізніше, плюс
    отдельно свіжі оголошення без дедлайну."""
    today = datetime.now(KYIV).date()
    recent_cutoff = datetime.now(KYIV).astimezone(timezone.utc) - timedelta(days=5)

    buckets: dict[str, list[dict]] = {"soon": [], "week": [], "month": [], "later": [], "nodate": []}

    for item in items:
        if item["due_date_obj"]:
            y, m, d = item["due_date_obj"]
            due_date = datetime(y, m, d).date()
            if due_date < today:
                continue  # просрочено — не сюда, это для отдельной кнопки
            days_left = (due_date - today).days
            if days_left <= 3:
                buckets["soon"].append(item)
            elif days_left <= 7:
                buckets["week"].append(item)
            elif days_left <= 30:
                buckets["month"].append(item)
            else:
                buckets["later"].append(item)
        else:
            ut = item.get("update_time")
            if not ut:
                continue
            try:
                updated = datetime.fromisoformat(ut.replace("Z", "+00:00"))
            except ValueError:
                continue
            if updated < recent_cutoff:
                continue  # старое оголошення без дедлайна — неактуально
            buckets["nodate"].append(item)

    for key in ("soon", "week", "month", "later"):
        buckets[key].sort(key=lambda it: it["due_date_obj"])

    return buckets


def _filter_classroom_overdue(items: list[dict]) -> list[dict]:
    """Все задания с дедлайном в прошлом, свіжі прострочені — зверху."""
    today = datetime.now(KYIV).date()
    overdue = []
    for item in items:
        if not item["due_date_obj"]:
            continue
        y, m, d = item["due_date_obj"]
        due_date = datetime(y, m, d).date()
        if due_date < today:
            overdue.append(item)
    overdue.sort(key=lambda it: it["due_date_obj"], reverse=True)
    return overdue


def _format_classroom_item(item: dict) -> str:
    emoji = "📌" if item["type"] == "завдання" else "📣"
    due = f" (до {item['due']})" if item["due"] else ""
    line = f"{emoji} **{item['course']}**{due}\n{item['title']}"
    if item["link"]:
        line += f"\n🔗 [Відкрити]({item['link']})"
    return line


async def _send_long_text(message: Message, text: str):
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], parse_mode="Markdown", disable_web_page_preview=True)


def _jsonbin_headers():
    return {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}


def _load_bin_sync() -> dict:
    resp = requests.get(f"{JSONBIN_BASE}/{JSONBIN_BIN_ID}/latest", headers=_jsonbin_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get("record") or {}


def _save_bin_sync(data: dict) -> None:
    resp = requests.put(f"{JSONBIN_BASE}/{JSONBIN_BIN_ID}", json=data, headers=_jsonbin_headers(), timeout=15)
    resp.raise_for_status()


async def load_bin():
    """Подтягивает домашку и статистику пользователей из JSONBin при старте
    бота (после сна/редеплоя память бота пустая, а в JSONBin данные остались)."""
    global BIN_DATA, HOMEWORK, USERS, SUBSCRIBERS
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        logging.info("JSONBin не настроен — домашка, статистика и напоминания работать не будут (см. README).")
        return
    try:
        BIN_DATA = await asyncio.to_thread(_load_bin_sync)
        HOMEWORK = BIN_DATA.setdefault("homework", {})
        USERS = BIN_DATA.setdefault("users", {})
        SUBSCRIBERS = set(BIN_DATA.setdefault("subscribers", []))
        logging.info(
            "Дані з JSONBin завантажені: %d дат д/з, %d користувачів, %d підписок на нагадування",
            len(HOMEWORK), len(USERS), len(SUBSCRIBERS)
        )
    except Exception:
        logging.exception("Не вдалося завантажити дані з JSONBin")


async def save_bin():
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        return
    BIN_DATA["homework"] = HOMEWORK
    BIN_DATA["users"] = USERS
    BIN_DATA["subscribers"] = list(SUBSCRIBERS)
    try:
        await asyncio.to_thread(_save_bin_sync, BIN_DATA)
    except Exception:
        logging.exception("Не вдалося зберегти дані в JSONBin")


def track_user(user) -> None:
    """Записывает факт обращения пользователя к боту (в памяти; на диск
    сохраняется периодически по расписанию, см. main())."""
    now_iso = datetime.now(KYIV).isoformat(timespec="seconds")
    uid = str(user.id)
    name = user.full_name or (f"@{user.username}" if user.username else uid)
    entry = USERS.get(uid)
    if entry:
        entry["last_seen"] = now_iso
        entry["count"] = entry.get("count", 0) + 1
        entry["name"] = name
        entry["username"] = user.username
    else:
        USERS[uid] = {
            "name": name,
            "username": user.username,
            "first_seen": now_iso,
            "last_seen": now_iso,
            "count": 1,
        }


class UserTrackerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if event.from_user:
            track_user(event.from_user)
        return await handler(event, data)


dp.message.middleware(UserTrackerMiddleware())


def find_homework_for(date_str: str, subject_full: str) -> str | None:
    day_hw = HOMEWORK.get(date_str)
    if not day_hw or not subject_full:
        return None
    entry = day_hw.get(canonical_subject_key(subject_full))
    return entry["text"] if entry else None

ZOOM_ALL = (
    "🔗 **Всі посилання на Zoom (А-22):**\n\n"
    "• **Квітівництво** (Жупіньська): [Посилання](https://us05web.zoom.us/j/9856624171?pwd=vdxkCpVL6bpNo514BbcLE7iKNWLsGK.1)\n"
    "• **Фізра** (Дрокина): [Посилання](https://us04web.zoom.us/j/5318097982?pwd=aK3pQZ6y4arwePmfQlUIXpUQWPndkb.1)\n"
    "• **Грунтознавство** (Ковалжи): [Посилання](https://us02web.zoom.us/j/3188320656?pwd=QWgycFc4S2JjUXk5ZDhoNnhrYjljdz09&omn=82972358550)\n"
    "• **Основи права** (Циганенко): [Посилання](https://us05web.zoom.us/j/7399873325?pwd=SVFFQUsrK3dpSTZ5NHlOWTJPZ2cxQT09)\n"
    "• **Екологія** (Батіг): [Посилання](https://us02web.zoom.us/j/8467559257?pwd=emE1NzZuS0RiV0tOODN6OTFtU0twUT09)\n"
    "• **Інформатика** (Бембель): [Посилання](https://us07web.zoom.us/j/7546161590?pwd=Yk8vNWU2bnpXSFpsTHBPZHBGOWV3dz09)\n"
    "• **Креслення** (Переходович): [Посилання](https://us04web.zoom.us/j/74812602094?pwd=LtakeMi2lnjEbJZVqbnt2mbyXUhaxJ.1)\n"
    "• **Історія** (Орел): [Посилання](https://us04web.zoom.us/j/9790221936?pwd=1234567&omn=71559763873)\n"
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
    название группы указано только в первой строке блока, дальше пусто.

    В реальном CSV-экспорте этой таблицы есть невидимая в браузере пустая
    колонка A — поэтому данные на самом деле начинаются с колонки B (индекс 1),
    а не A (индекс 0): группа=1, № пары=2, предмет=3, препод=5."""
    result = []
    current_group = None
    target = _normalize_group(group_name)

    for row in rows:
        cells = [c.strip() for c in row]
        if not any(cells) or len(cells) < 4:
            continue

        group_cell, pair_cell, subject_cell = cells[1], cells[2], cells[3]
        teacher_cell = cells[5] if len(cells) > 5 else ""

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
        logging.info("Диагностика — первые строки таблицы как есть: %s", raw_rows[:6])
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


def resolve_day_lessons(day_name, target_date):
    """Возвращает список (индекс пары, время, предмет, ссылка) для конкретного
    дня и даты, уже с учётом замен и чередования недель над/під рискою.
    Используется и в выводе расписания, и в напоминаниях."""
    lessons = SCHEDULE.get(day_name)
    if not lessons:
        return []

    week_type = get_week_type(target_date)
    date_str = target_date.strftime("%d.%m.%Y")
    day_replacements = REPLACEMENTS.get(date_str, {})

    resolved = []
    for index, (time_slot, subject, link) in enumerate(lessons, start=1):
        if index in day_replacements:
            _, subject, link = day_replacements[index]
        elif "Над рискою:" in subject and "Під рискою:" in subject:
            lines = subject.split("\n")
            if week_type == "Над рискою":
                subject = lines[0].replace("Над рискою: ", "")
            else:
                subject = lines[1].replace("Під рискою: ", "")
        resolved.append((index, time_slot, subject, link))
    return resolved


def send_schedule_for_day(day_name, target_date):
    lessons = resolve_day_lessons(day_name, target_date)
    if not lessons:
        return f"📅 На **{day_name}** у групи А-22 занять немає (вихідний) 🎉"

    week_type = get_week_type(target_date)
    date_str_formatted = target_date.strftime("%d.%m.%Y")
    day_replacements = REPLACEMENTS.get(date_str_formatted, {})

    response = f"📅 **Розклад для групи А-22 — {day_name.upper()}** ({date_str_formatted})\n*(Тиждень: **{week_type}**)*:\n\n"

    for index, time_slot, subject, link in lessons:
        icon = subject_icon(subject)

        if link and "Вільно" not in subject and "Вільна" not in subject:
            response += f"🔹 **{time_slot}**\n   {icon} {subject}\n   🔗 [Підключитися до Zoom]({link})\n"
        else:
            response += f"🔹 **{time_slot}**\n   {icon} {subject}\n"

        hw_text = find_homework_for(date_str_formatted, subject)
        if hw_text:
            response += f"   📚 ДЗ: {hw_text}\n"
        response += "\n"

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

@dp.message(F.text == "🔔 Нагадування")
async def toggle_reminders(message: Message):
    uid = str(message.from_user.id)
    if uid in SUBSCRIBERS:
        SUBSCRIBERS.discard(uid)
        await message.answer("Нагадування вимкнено 🔕")
    else:
        SUBSCRIBERS.add(uid)
        await message.answer("Нагадування увімкнено 🔔 — писатиму за 5 хв до кожної пари (крім \"вільно\").")
    await save_bin()

@dp.message(Command("hw"))
async def add_homework(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Ця команда тільки для старости.")
        return

    args = (command.args or "").strip()
    parts = [p.strip() for p in args.split("|")]
    if len(parts) != 3:
        await message.answer(
            "Формат: /hw дд.мм.рррр | Предмет | текст завдання\n"
            "Приклад: /hw 29.09.2026 | Інформатика | Зробити лабу №3"
        )
        return

    date_str, subject, text = parts
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", date_str):
        await message.answer("Дата має бути у форматі дд.мм.рррр, наприклад 29.09.2026")
        return

    key = canonical_subject_key(subject)
    HOMEWORK.setdefault(date_str, {})[key] = {"subject": subject, "text": text}
    await save_bin()

    note = "" if (JSONBIN_API_KEY and JSONBIN_BIN_ID) else (
        "\n\n⚠️ JSONBin не налаштований — це збережеться тільки до перезапуску бота."
    )
    await message.answer(f"Збережено: {date_str} — {subject}: {text}{note}")

@dp.message(F.text.in_(["📚 Домашнє завдання", "Домашка"]))
async def show_homework(message: Message):
    now = datetime.now(KYIV)
    today_str = now.strftime("%d.%m.%Y")
    tomorrow_str = (now + timedelta(days=1)).strftime("%d.%m.%Y")

    parts = []
    for label, date_str in (("на сьогодні", today_str), ("на завтра", tomorrow_str)):
        day_hw = HOMEWORK.get(date_str)
        if day_hw:
            lines = "\n".join(f"  • {info['subject']}: {info['text']}" for info in day_hw.values())
            parts.append(f"**Домашнє завдання {label} ({date_str}):**\n{lines}")

    if parts:
        await message.answer("\n\n".join(parts), parse_mode="Markdown")
    else:
        await message.answer("На найближчі дні домашніх завдань не записано 🎉")

@dp.message(F.text == "🎓 Classroom")
async def show_classroom(message: Message):
    if not _classroom_configured():
        await message.answer(
            "Classroom ще не підключено — не налаштовані CLASSROOM_CLIENT_ID / "
            "CLASSROOM_CLIENT_SECRET / CLASSROOM_REFRESH_TOKEN."
        )
        return

    await message.answer("🎓 Перевіряю Classroom...")
    items = await fetch_classroom()

    if not items:
        await message.answer("Нічого не знайшов (або всі курси без активних завдань/оголошень).")
        return

    buckets = _group_classroom_actual(items)
    section_labels = [
        ("soon", "🔴 Найближчі (до 3 днів)"),
        ("week", "🟡 До тижня"),
        ("month", "🟢 До місяця"),
        ("later", "📅 Пізніше"),
        ("nodate", "📣 Свіжі оголошення"),
    ]

    sections = []
    for key, label in section_labels:
        group = buckets[key][:10]
        if not group:
            continue
        sections.append("\n\n".join([f"**{label}**"] + [_format_classroom_item(it) for it in group]))

    if not sections:
        await message.answer("Актуальних завдань немає 🎉")
        return

    await _send_long_text(message, "\n\n━━━━━━━━━━\n\n".join(sections))

@dp.message(F.text == "⚠️ Прострочені")
async def show_classroom_overdue(message: Message):
    if not _classroom_configured():
        await message.answer(
            "Classroom ще не підключено — не налаштовані CLASSROOM_CLIENT_ID / "
            "CLASSROOM_CLIENT_SECRET / CLASSROOM_REFRESH_TOKEN."
        )
        return

    await message.answer("⚠️ Перевіряю прострочені завдання...")
    items = await fetch_classroom()
    overdue = _filter_classroom_overdue(items)[:15]

    if not overdue:
        await message.answer("Прострочених завдань немає 🎉")
        return

    await _send_long_text(message, "\n\n".join(_format_classroom_item(it) for it in overdue))

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Ця команда тільки для старости.")
        return

    if not USERS:
        await message.answer("Поки що ніхто не писав боту.")
        return

    lines = [f"👥 Унікальних користувачів: {len(USERS)}", ""]
    for u in sorted(USERS.values(), key=lambda u: u["last_seen"], reverse=True):
        first = u["first_seen"][:16].replace("T", " ")
        last = u["last_seen"][:16].replace("T", " ")
        username = f" (@{u['username']})" if u.get("username") else ""
        lines.append(f"• {u['name']}{username}\n   повідомлень: {u['count']}, востаннє: {last}, вперше: {first}")

    text = "\n".join(lines)
    # Telegram режет сообщения длиннее ~4096 символов — на всякий случай рубим на части
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000])

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

REMINDER_STAGES = [
    (10, "⏰ Через 10 хвилин"),
    (5, "⚠️ Через 5 хвилин"),
    (1, "🚨 УВАГА! Через хвилину"),
]

async def check_reminders():
    """Раз в минуту проверяет: не начинается ли у какой-то пары через 10, 5
    или 1 хвилину — и если да, шлёт напоминание всем подписанным. Несколько
    напоминаний подряд сложнее пропустить, чем одно."""
    if not SUBSCRIBERS:
        return

    now = datetime.now(KYIV)

    for offset, prefix in REMINDER_STAGES:
        target = now + timedelta(minutes=offset)
        day_name = DAY_NAMES.get(target.weekday())
        if day_name not in SCHEDULE:
            continue

        for index, time_slot, subject, link in resolve_day_lessons(day_name, target):
            m = re.search(r"\((\d{1,2}):(\d{2})-", time_slot)
            if not m:
                continue
            hh, mm = int(m.group(1)), int(m.group(2))
            if (hh, mm) != (target.hour, target.minute):
                continue
            if "Вільно" in subject or "Вільна" in subject:
                continue

            icon = subject_icon(subject)
            text = f"{prefix} {index} пара:\n{icon} {subject}"
            if link:
                text += f"\n🔗 {link}"

            for uid in list(SUBSCRIBERS):
                try:
                    await bot.send_message(int(uid), text)
                except Exception:
                    logging.exception("Не вдалося надіслати нагадування %s", uid)


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
    scheduler.add_job(save_bin, 'interval', minutes=15)
    scheduler.add_job(check_reminders, 'interval', minutes=1)
    scheduler.start()
    
    await fetch_replacements()
    await load_bin()
    
    await web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
