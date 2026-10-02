import asyncio
import html
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SCHEDULE_URL = os.getenv("SCHEDULE_URL", "https://guap.ru/rasp?gr=7787")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
CACHE_TTL = int(os.getenv("CACHE_TTL", "600"))
SEMESTER_START = os.getenv("SEMESTER_START", "").strip()
DAILY_HOUR, DAILY_MIN = (int(x) for x in os.getenv("DAILY_TIME", "20:00").split(":"))
TZ = ZoneInfo("Europe/Moscow")

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

NOTIFY = {
    "hw": ("notify_hw", "Уведомления о новых домашних заданиях"),
    "ann": ("notify_ann", "Уведомления о новых объявлениях"),
    "daily": ("notify_daily", "Уведомление о расписании на следующий день"),
}


class DB:
    conn: aiosqlite.Connection

    async def open(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, added_at TEXT);
            CREATE TABLE IF NOT EXISTS announcements(
                id INTEGER PRIMARY KEY AUTOINCREMENT, author_id INTEGER, author_name TEXT,
                text TEXT, created_at TEXT, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS homework(
                id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, text TEXT, due TEXT,
                author_id INTEGER, author_name TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS ann_files(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ann_id INTEGER, file_id TEXT, name TEXT, kind TEXT);
            CREATE TABLE IF NOT EXISTS hw_files(
                id INTEGER PRIMARY KEY AUTOINCREMENT, hw_id INTEGER, file_id TEXT, name TEXT, kind TEXT);
            """
        )
        cols = {r[1] for r in await (await self.conn.execute("PRAGMA table_info(users)")).fetchall()}
        for column, _label in NOTIFY.values():
            if column not in cols:
                await self.conn.execute(f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1")
        await self.conn.commit()

    async def run(self, sql: str, args: tuple = ()):
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur

    async def all(self, sql: str, args: tuple = ()):
        cur = await self.conn.execute(sql, args)
        return await cur.fetchall()


db = DB()
ADMINS: set[int] = set()


def is_admin(uid: int) -> bool:
    return uid == OWNER_ID or uid in ADMINS


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    return text.strip()


def _parse_lesson(block) -> tuple[str, str]:
    """(маркер недели '▲'/'▼'/'', текст: тип, предмет, аудитория)."""
    first = block.find("div", recursive=False)
    classes = first.get("class") or [] if first else []
    marker = "▲" if "week1" in classes else "▼" if "week2" in classes else ""
    kind = block.select_one("div.fs-6")
    subject = block.select_one("div.lead")
    details = block.select_one("div.opacity-75")
    room = ""
    if details:
        for br in details.find_all("br"):
            br.replace_with("|||")
        first_line = _clean(details.get_text(" ").split("|||")[0])
        room = first_line.split(" — ")[0].strip()
        if "вне сетки" in room:
            room = ""
    text = "<b>" + html.escape(_clean(subject.get_text(" "))) + "</b>"
    if kind and _clean(kind.get_text(" ")):
        text = html.escape(_clean(kind.get_text(" "))) + "\n" + text
    if room:
        text += "\n" + html.escape(room)
    return marker, text


def parse_schedule(page: str, today: date) -> dict:
    soup = BeautifulSoup(page, "html.parser")
    days: dict[int, list] = {}
    offgrid: list = []
    section = None
    pair = ""

    for el in soup.find_all(["h4", "div"]):
        classes = el.get("class") or []
        if el.name == "h4":
            title = _clean(el.get_text(" ")).lower()
            pair = ""
            if title.startswith("вне сетки"):
                section = "off"
            else:
                section = next((i for i, d in enumerate(DAYS) if title == d.lower()), None)
        elif section is None:
            continue
        elif "text-danger" in classes and "mt-3" in classes:
            pair = _clean(el.get_text(" "))
        elif "d-flex" in classes and "gap-2" in classes and "py-2" in classes and el.select_one("div.lead"):
            marker, text = _parse_lesson(el)
            if section == "off":
                offgrid.append((marker, text))
            else:
                days.setdefault(section, []).append((pair, marker, text))

    anchor = None
    badge = soup.select_one(".alert span.week1, .alert span.week2")
    if badge:
        anchor = (today, "▲" if "week1" in (badge.get("class") or []) else "▼")

    if not days and not offgrid:
        log.warning("Парсер не нашёл ни одного занятия — возможно, изменилась вёрстка сайта")
    return {"days": days, "offgrid": offgrid, "anchor": anchor}


class ScheduleService:
    def __init__(self):
        self.cache: dict | None = None
        self.raw: str = ""
        self.ts = 0.0
        self.lock = asyncio.Lock()
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15), headers={"User-Agent": "Mozilla/5.0 (group-bot)"}
        )

    async def stop(self):
        if self.session:
            await self.session.close()

    async def get(self) -> dict:
        if self.cache is not None and time.monotonic() - self.ts < CACHE_TTL:
            return self.cache
        async with self.lock:
            if self.cache is not None and time.monotonic() - self.ts < CACHE_TTL:
                return self.cache
            try:
                async with self.session.get(SCHEDULE_URL) as r:
                    r.raise_for_status()
                    self.raw = await r.text()
                self.cache = await asyncio.to_thread(parse_schedule, self.raw, datetime.now(TZ).date())
                self.ts = time.monotonic()
            except Exception:
                log.exception("Не удалось обновить расписание")
                if self.cache is None:
                    raise
            return self.cache


schedule = ScheduleService()


def week_marker(d: date, sched: dict) -> str | None:
    """'▲' или '▼' для даты d: от плашки текущей недели на сайте, иначе от SEMESTER_START."""
    anchor = sched.get("anchor")
    if anchor:
        a_date, a_mark = anchor
        monday = lambda x: x - timedelta(days=x.weekday())
        diff = (monday(d) - monday(a_date)).days // 7
        return a_mark if diff % 2 == 0 else ("▼" if a_mark == "▲" else "▲")
    if SEMESTER_START:
        weeks = (d - date.fromisoformat(SEMESTER_START)).days // 7
        return "▲" if weeks % 2 == 0 else "▼"
    return None


def format_day(sched: dict, d: date) -> str:
    marker = week_marker(d, sched)
    head = f"<b>{DAYS[d.weekday()]}, {d:%d.%m}</b>" + (f" {marker}" if marker else "")
    lines = [f"<b>{html.escape(pair)}</b>\n{text}" if pair else text for pair, text in lessons_for(sched, d)]
    if not lines:
        return head + "\nЗанятий нет 🎉"
    return head + "\n\n" + "\n\n".join(lines)


def format_offgrid(sched: dict) -> str:
    items = [t for _, t in sched["offgrid"]]
    return "<b>Вне сетки расписания</b>\n\n" + "\n\n".join(items) if items else ""


async def send_long(message: Message, text: str):
    while text:
        chunk, text = text[:4000], text[4000:]
        if text and "\n" in chunk:
            cut = chunk.rfind("\n")
            chunk, text = chunk[:cut], chunk[cut:] + text
        await message.answer(chunk.strip())


class Announce(StatesGroup):
    content = State()


class AddAdmin(StatesGroup):
    waiting = State()


class AddHW(StatesGroup):
    subject = State()
    due = State()
    content = State()


router = Router()

BTN_HW = "📚 Домашние задания"
BTN_ANN = "📢 Объявления"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMIN = "🛠 Админ-панель"
MENU_BUTTONS = {"Сегодня", "Завтра", "Неделя", BTN_HW, BTN_ANN, BTN_SETTINGS, BTN_ADMIN, "⚙️ Админ-панель"}


def menu_for(uid: int) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра"), KeyboardButton(text="Неделя")],
        [KeyboardButton(text=BTN_HW), KeyboardButton(text=BTN_ANN)],
        [KeyboardButton(text=BTN_SETTINGS)],
    ]
    if is_admin(uid):
        kb[-1].append(KeyboardButton(text=BTN_ADMIN))
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


@router.message(CommandStart())
async def cmd_start(m: Message):
    await db.run(
        "INSERT INTO users(id, name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
        (m.from_user.id, m.from_user.full_name),
    )
    await m.answer(
        "Бот группы 1646. Выбирай раздел в меню.",
        reply_markup=menu_for(m.from_user.id),
    )


CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("Отменено.")
    except TelegramBadRequest:
        pass
    await cb.answer()


@router.callback_query(F.data == "dbg:html")
async def cb_html(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        return await cb.answer("Только для владельца", show_alert=True)
    await cb.answer()
    try:
        await schedule.get()
    except Exception:
        return await cb.message.answer("Не удалось загрузить страницу.")
    await cb.message.answer_document(BufferedInputFile(schedule.raw.encode(), filename="rasp.html"))


BACKGROUND: set[asyncio.Task] = set()
MAX_TEXT = 3000
TXT = (F.text, ~F.text.startswith("/"), F.text.not_in(MENU_BUTTONS))


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def pager(prefix: str, i: int, total: int) -> list[InlineKeyboardButton]:
    row = []
    if i > 0:
        row.append(btn("◀", f"{prefix}:p:{i - 1}"))
    row.append(btn(f"{i + 1}/{total}", "noop"))
    if i < total - 1:
        row.append(btn("▶", f"{prefix}:p:{i + 1}"))
    return row


async def show(target: Message | CallbackQuery, text: str, rows: list):
    """Сообщение-карточка: из кнопки меню — новое сообщение, из inline-кнопки — редактируем текущее."""
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


async def user_settings_view(target: Message | CallbackQuery, uid: int):
    columns = ", ".join(col for col, _ in NOTIFY.values())
    row = await db.all(f"SELECT {columns} FROM users WHERE id=?", (uid,))
    flags = dict(zip(NOTIFY, row[0])) if row else {key: 1 for key in NOTIFY}
    rows = [[btn(f"{'✅' if flags[key] else '❌'} {label}", f"us:{key}")] for key, (_c, label) in NOTIFY.items()]
    text = "⚙️ <b>Настройки</b>\n\nНажми на пункт, чтобы включить или выключить уведомление."
    if await get_setting("daily_schedule", "1") != "1":
        text += "\n\n<i>Ежедневная рассылка расписания сейчас отключена администратором.</i>"
    await show(target, text, rows)


@router.message(F.text == BTN_SETTINGS)
async def sec_settings(m: Message, state: FSMContext):
    await state.clear()
    await user_settings_view(m, m.from_user.id)


@router.callback_query(F.data.startswith("us:"))
async def cb_user_toggle(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key not in NOTIFY:
        return await cb.answer()
    column = NOTIFY[key][0]
    await db.run("INSERT OR IGNORE INTO users(id, name) VALUES(?,?)", (cb.from_user.id, cb.from_user.full_name))
    await db.run(f"UPDATE users SET {column} = 1 - {column} WHERE id=?", (cb.from_user.id,))
    await user_settings_view(cb, cb.from_user.id)


def parse_due(s: str) -> str | None:
    """'' — без срока; None — не удалось разобрать; иначе дата ISO."""
    s = s.strip()
    if s in {"-", "—", ""}:
        return ""
    mt = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", s)
    if not mt:
        return None
    day, month, year = int(mt[1]), int(mt[2]), mt[3]
    today = datetime.now(TZ).date()
    y = (int(year) + (2000 if len(year) == 2 else 0)) if year else today.year
    try:
        dt = date(y, month, day)
        if not year and dt < today - timedelta(days=30):
            dt = date(y + 1, month, day)
    except ValueError:
        return None
    return dt.isoformat()


async def hw_view(target: Message | CallbackQuery, i: int, uid: int):
    admin = is_admin(uid)
    total = (await db.all("SELECT COUNT(*) FROM homework"))[0][0]
    rows: list = []
    if total == 0:
        text = "📚 Домашних заданий пока нет."
        if admin:
            rows.append([btn("➕ Добавить", "hw:add")])
        return await show(target, text, rows)

    i = max(0, min(i, total - 1))
    hid, subject, body, due, author, created = (
        await db.all(
            "SELECT id, subject, text, due, author_name, created_at FROM homework "
            "ORDER BY id DESC LIMIT 1 OFFSET ?",
            (i,),
        )
    )[0]
    nfiles = (await db.all("SELECT COUNT(*) FROM hw_files WHERE hw_id=?", (hid,)))[0][0]

    parts = [f"📚 <b>{html.escape(subject)}</b>"]
    if body:
        parts.append(body)
    if due:
        d = date.fromisoformat(due)
        late = " (срок прошёл)" if d < datetime.now(TZ).date() else ""
        parts.append(f"📅 Срок: <b>{d:%d.%m.%Y}</b>{late}")
    parts.append(f"<i>Добавил(а) {html.escape(author)}, {created}</i>")

    if total > 1:
        rows.append(pager("hw", i, total))
    if nfiles:
        rows.append([btn(f"📎 Файлы ({nfiles})", f"hw:files:{hid}")])
    if admin:
        rows.append([btn("➕ Добавить", "hw:add"), btn("🗑 Удалить", f"hw:del:{hid}:{i}")])
    await show(target, "\n\n".join(parts), rows)


@router.message(F.text == BTN_HW)
async def sec_homework(m: Message, state: FSMContext):
    await state.clear()
    await hw_view(m, 0, m.from_user.id)


@router.callback_query(F.data.startswith("hw:p:"))
async def cb_hw_page(cb: CallbackQuery):
    await hw_view(cb, int(cb.data.split(":")[2]), cb.from_user.id)


@router.callback_query(F.data.startswith("hw:files:"))
async def cb_hw_files(cb: CallbackQuery):
    rows = await db.all("SELECT file_id, kind FROM hw_files WHERE hw_id=? ORDER BY id", (int(cb.data.split(":")[2]),))
    if not rows:
        return await cb.answer("Файлов нет", show_alert=True)
    await cb.answer()
    for file_id, kind in rows:
        await send_media(cb.bot, cb.message.chat.id, kind, file_id)


@router.callback_query(F.data.startswith("hw:del:"))
async def cb_hw_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    _, _, hid, i = cb.data.split(":")
    kb = [[btn("Да, удалить", f"hw:delyes:{hid}:{i}"), btn("Отмена", f"hw:p:{i}")]]
    await show(cb, "Удалить задание?", kb)


@router.callback_query(F.data.startswith("hw:delyes:"))
async def cb_hw_delyes(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    _, _, hid, i = cb.data.split(":")
    await db.run("DELETE FROM hw_files WHERE hw_id=?", (int(hid),))
    await db.run("DELETE FROM homework WHERE id=?", (int(hid),))
    await hw_view(cb, int(i), cb.from_user.id)


def extract_media(m: Message) -> tuple[str, str, str] | None:
    """(file_id, имя, тип) для файла, фото, видео или аудио в сообщении."""
    if m.document:
        return m.document.file_id, m.document.file_name or "файл", "doc"
    if m.photo:
        return m.photo[-1].file_id, "фото", "photo"
    if m.video:
        return m.video.file_id, m.video.file_name or "видео", "video"
    if m.audio:
        return m.audio.file_id, m.audio.file_name or "аудио", "audio"
    return None


async def send_media(bot: Bot, chat_id: int, kind: str, file_id: str):
    send = {"photo": bot.send_photo, "video": bot.send_video, "audio": bot.send_audio}.get(kind, bot.send_document)
    await send(chat_id, file_id)


def is_content(m: Message) -> bool:
    """Сообщение-«содержимое»: любой текст (кроме команд и кнопок меню), подпись или вложение."""
    if m.text and (m.text.startswith("/") or m.text in MENU_BUTTONS):
        return False
    return bool(m.text or m.caption or extract_media(m))


async def collect(m: Message, state: FSMContext) -> bool:
    """Добавляет текст и вложения сообщения в черновик (поля parts/files в FSM).

    Работает и для сообщения без текста, и для файла без подписи, и для альбомов.
    Возвращает True, если на сообщение стоит ответить (внутри альбома отвечаем один раз)."""
    data = await state.get_data()
    parts: list = data.get("parts", [])
    files: list = data.get("files", [])
    chunk = m.html_text if (m.text or m.caption) else ""
    if chunk:
        if len("\n".join(parts + [chunk])) > MAX_TEXT:
            await m.answer(f"Слишком длинно (максимум {MAX_TEXT} символов)")
            return False
        parts.append(chunk)
    media = extract_media(m)
    if media:
        files.append(media)
    gid = m.media_group_id
    await state.update_data(parts=parts, files=files, last_group=gid)
    return not (gid and gid == data.get("last_group"))


async def _begin_draft(state: FSMContext, new_state: State, **extra):
    await state.clear()
    await state.set_state(new_state)
    await state.update_data(parts=[], files=[], last_group=None, **extra)


HINT = "Пришли текст, фото или файлы."


@router.callback_query(F.data == "hw:add")
async def cb_hw_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    await state.clear()
    await state.set_state(AddHW.subject)
    await cb.message.answer("Предмет?", reply_markup=CANCEL_KB)
    await cb.answer()


SAVE_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data="hw:save"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"),
        ]
    ]
)
NODUE_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Без срока", callback_data="hw:nodue"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"),
        ]
    ]
)


@router.message(AddHW.subject, *TXT)
async def hw_subject(m: Message, state: FSMContext):
    await state.update_data(subject=m.text.strip()[:100])
    await state.set_state(AddHW.due)
    await m.answer("Срок сдачи? (25.10 или 25.10.2026)", reply_markup=NODUE_KB)


async def _hw_ask_content(target: Message, state: FSMContext, due: str):
    await state.update_data(due=due, parts=[], files=[], last_group=None)
    await state.set_state(AddHW.content)
    await target.answer(HINT, reply_markup=SAVE_KB)


@router.message(AddHW.due, *TXT)
async def hw_due(m: Message, state: FSMContext):
    due = parse_due(m.text)
    if due is None:
        return await m.answer("Формат даты: 25.10 или 25.10.2026", reply_markup=NODUE_KB)
    await _hw_ask_content(m, state, due)


@router.callback_query(AddHW.due, F.data == "hw:nodue")
async def cb_hw_nodue(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _hw_ask_content(cb.message, state, "")


@router.message(AddHW.content, is_content)
async def hw_content(m: Message, state: FSMContext):
    if await collect(m, state):
        await m.answer("Принято ✅. Пришли ещё если нужно", reply_markup=SAVE_KB)


@router.callback_query(AddHW.content, F.data == "hw:save")
async def hw_save(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    data = await state.get_data()
    parts, files = data.get("parts", []), data.get("files", [])
    if not parts and not files:
        return await cb.answer("Пока пусто", show_alert=True)
    await state.clear()
    body = "\n".join(parts)
    cur = await db.run(
        "INSERT INTO homework(subject, text, due, author_id, author_name, created_at) VALUES(?,?,?,?,?,?)",
        (data["subject"], body, data.get("due", ""), cb.from_user.id, cb.from_user.full_name, now_str()),
    )
    for file_id, name, kind in files:
        await db.run(
            "INSERT INTO hw_files(hw_id, file_id, name, kind) VALUES(?,?,?,?)", (cur.lastrowid, file_id, name, kind)
        )
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Сохранено")
    spawn(broadcast_hw(bot, data["subject"], body, data.get("due", ""), files, cb.from_user.id))
    await hw_view(cb.message, 0, cb.from_user.id)


async def ann_view(target: Message | CallbackQuery, i: int, uid: int):
    admin = is_admin(uid)
    total = (await db.all("SELECT COUNT(*) FROM announcements"))[0][0]
    rows: list = []
    if total == 0:
        if admin:
            rows.append([btn("➕ Новое объявление", "ann:new")])
        return await show(target, "📢 Объявлений пока нет.", rows)

    i = max(0, min(i, total - 1))
    aid, author, body, created, sent, failed = (
        await db.all(
            "SELECT id, author_name, text, created_at, sent, failed FROM announcements "
            "ORDER BY id DESC LIMIT 1 OFFSET ?",
            (i,),
        )
    )[0]
    nfiles = (await db.all("SELECT COUNT(*) FROM ann_files WHERE ann_id=?", (aid,)))[0][0]
    text = f"📢 <b>Объявление #{aid}</b>\n\n" + (f"{body}\n\n" if body else "") + f"<i>{html.escape(author)}, {created}</i>"
    if admin:
        text += f"\n<i>Доставлено: {sent}, не доставлено: {failed}</i>"
    if total > 1:
        rows.append(pager("annpage", i, total))
    if nfiles:
        rows.append([btn(f"📎 Файлы ({nfiles})", f"ann:files:{aid}")])
    if admin:
        rows.append([btn("➕ Новое объявление", "ann:new"), btn("🗑 Удалить", f"ann:del:{aid}:{i}")])
    await show(target, text, rows)


@router.message(F.text == BTN_ANN)
async def sec_announcements(m: Message, state: FSMContext):
    await state.clear()
    await ann_view(m, 0, m.from_user.id)


@router.callback_query(F.data.startswith("annpage:p:"))
async def cb_ann_page(cb: CallbackQuery):
    await ann_view(cb, int(cb.data.split(":")[2]), cb.from_user.id)


@router.callback_query(F.data.startswith("ann:files:"))
async def cb_ann_files(cb: CallbackQuery):
    rows = await db.all("SELECT file_id, kind FROM ann_files WHERE ann_id=? ORDER BY id", (int(cb.data.split(":")[2]),))
    if not rows:
        return await cb.answer("Файлов нет", show_alert=True)
    await cb.answer()
    for file_id, kind in rows:
        await send_media(cb.bot, cb.message.chat.id, kind, file_id)


@router.callback_query(F.data.startswith("ann:del:"))
async def cb_ann_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    _, _, aid, i = cb.data.split(":")
    kb = [[btn("Да, удалить", f"ann:delyes:{aid}:{i}"), btn("Отмена", f"annpage:p:{i}")]]
    await show(cb, "Удалить объявление?", kb)


@router.callback_query(F.data.startswith("ann:delyes:"))
async def cb_ann_delyes(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    _, _, aid, i = cb.data.split(":")
    await db.run("DELETE FROM ann_files WHERE ann_id=?", (int(aid),))
    await db.run("DELETE FROM announcements WHERE id=?", (int(aid),))
    await ann_view(cb, int(i), cb.from_user.id)


SEND_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить всем", callback_data="ann:send"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="ann:cancel"),
        ]
    ]
)


@router.callback_query(F.data == "ann:new")
async def cb_ann_new(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    await _begin_draft(state, Announce.content)
    await cb.message.answer(HINT, reply_markup=CANCEL_KB)
    await cb.answer()


@router.message(Announce.content, is_content)
async def announce_content(m: Message, state: FSMContext):
    if await collect(m, state):
        await m.answer("Принято ✅", reply_markup=SEND_KB)


def spawn(coro):
    """Запускает фоновую задачу и держит на неё ссылку, чтобы её не собрал GC."""
    task = asyncio.create_task(coro)
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
    return task


@router.callback_query(Announce.content, F.data.in_({"ann:send", "ann:cancel"}))
async def announce_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    data = await state.get_data()
    parts, files = data.get("parts", []), data.get("files", [])
    if cb.data == "ann:cancel":
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        return await cb.answer("Отменено")
    if not parts and not files:
        return await cb.answer("Пока пусто", show_alert=True)
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    text = "\n".join(parts)
    cur = await db.run(
        "INSERT INTO announcements(author_id, author_name, text, created_at) VALUES(?,?,?,?)",
        (cb.from_user.id, cb.from_user.full_name, text, now_str()),
    )
    for file_id, name, kind in files:
        await db.run(
            "INSERT INTO ann_files(ann_id, file_id, name, kind) VALUES(?,?,?,?)", (cur.lastrowid, file_id, name, kind)
        )
    await cb.answer("Рассылка запущена")
    spawn(broadcast(bot, cur.lastrowid, text, files, cb.from_user.id))


async def deliver(bot: Bot, uid: int, items: list[tuple[str, str]]) -> bool:
    """Отправляет по очереди ('text', текст) и (тип, file_id); с повтором при лимитах Telegram."""
    for kind, payload in items:
        for _ in range(3):
            try:
                if kind == "text":
                    await bot.send_message(uid, payload)
                else:
                    await send_media(bot, uid, kind, payload)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await db.run("DELETE FROM users WHERE id=?", (uid,))
                return False
            except Exception:
                log.exception("Ошибка отправки %s", uid)
                return False
        else:
            return False
        await asyncio.sleep(0.04)
    return True


async def safe_send(bot: Bot, uid: int, text: str) -> bool:
    return await deliver(bot, uid, [("text", text)])


async def broadcast_items(bot: Bot, key: str, items: list[tuple[str, str]], exclude: int | None = None) -> tuple[int, int]:
    """Рассылает items всем, у кого включено уведомление `key` (hw / ann / daily). Возвращает (ok, fail)."""
    column = NOTIFY[key][0]
    ids = [r[0] for r in await db.all(f"SELECT id FROM users WHERE {column} = 1")]
    ok = fail = 0
    for uid in ids:
        if uid == exclude:
            continue
        if await deliver(bot, uid, items):
            ok += 1
        else:
            fail += 1
    return ok, fail


async def broadcast(bot: Bot, ann_id: int, text: str, files: list, author_id: int):
    """Рассылка объявления в фоне: бот при этом продолжает отвечать всем остальным."""
    header = "📢 <b>Объявление</b>" + (f"\n\n{text}" if text else "")
    items = [("text", header)] + [(kind, file_id) for file_id, _name, kind in files]
    ok, fail = await broadcast_items(bot, "ann", items)
    await db.run("UPDATE announcements SET sent=?, failed=? WHERE id=?", (ok, fail, ann_id))
    await safe_send(bot, author_id, f"Объявление #{ann_id} разослано: доставлено {ok}, не доставлено {fail}.")


async def broadcast_hw(bot: Bot, subject: str, body: str, due: str, files: list, author_id: int):
    """Уведомление о новом домашнем задании (автору не отправляем — он только что его создал)."""
    text = f"📚 <b>Новое домашнее задание</b>\n\n<b>{html.escape(subject)}</b>"
    if body:
        text += f"\n\n{body}"
    if due:
        text += f"\n\n📅 Срок: <b>{date.fromisoformat(due):%d.%m.%Y}</b>"
    items = [("text", text)] + [(kind, file_id) for file_id, _name, kind in files]
    ok, fail = await broadcast_items(bot, "hw", items, exclude=author_id)
    log.info("Уведомление о ДЗ: доставлено %s, не доставлено %s", ok, fail)


def panel_rows(uid: int) -> list:
    rows = [
        [btn("➕ Домашнее задание", "hw:add"), btn("📚 Все задания", "hw:p:0")],
        [btn("📢 Новое объявление", "ann:new")],
        [btn("🗓 Рассылка расписания", "set:open")],
    ]
    if uid == OWNER_ID:
        rows.append([btn("👥 Админы", "adm:list"), btn("📄 HTML расписания", "dbg:html")])
    return rows


@router.message(F.text.in_({BTN_ADMIN, "⚙️ Админ-панель"}))
async def sec_admin(m: Message, state: FSMContext):
    await state.clear()
    if not is_admin(m.from_user.id):
        return
    await show(m, "🛠 <b>Админ-панель</b>", panel_rows(m.from_user.id))


@router.callback_query(F.data == "adm:panel")
async def cb_panel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    await show(cb, "🛠 <b>Админ-панель</b>", panel_rows(cb.from_user.id))


async def admins_view(target: Message | CallbackQuery):
    rows = await db.all(
        "SELECT a.id, u.name FROM admins a LEFT JOIN users u ON u.id = a.id ORDER BY a.added_at"
    )
    lines = [f"👑 <code>{OWNER_ID}</code> — владелец"]
    kb = []
    for uid, name in rows:
        label = f"{name} ({uid})" if name else str(uid)
        lines.append(f"• {html.escape(label)}")
        kb.append([btn(f"❌ {label}"[:60], f"adm:del:{uid}")])
    if not rows:
        lines.append("Других админов пока нет.")
    kb.append([btn("➕ Добавить админа", "adm:add")])
    kb.append([btn("◀ Назад", "adm:panel")])
    await show(target, "👥 <b>Админы</b>\n\n" + "\n".join(lines), kb)


@router.callback_query(F.data == "adm:list")
async def cb_adm_list(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        return await cb.answer("Только для владельца", show_alert=True)
    await admins_view(cb)


@router.callback_query(F.data.startswith("adm:del:"))
async def cb_adm_del(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        return await cb.answer("Только для владельца", show_alert=True)
    uid = int(cb.data.split(":")[2])
    await db.run("DELETE FROM admins WHERE id=?", (uid,))
    ADMINS.discard(uid)
    await admins_view(cb)


@router.callback_query(F.data == "adm:add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        return await cb.answer("Только для владельца", show_alert=True)
    await state.clear()
    await state.set_state(AddAdmin.waiting)
    await cb.message.answer("Перешли сообщение нового админа или пришли его ID.", reply_markup=CANCEL_KB)
    await cb.answer()


@router.message(AddAdmin.waiting)
async def adm_receive(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id != OWNER_ID:
        return
    if m.text and (m.text.startswith("/") or m.text in MENU_BUTTONS):
        await state.clear()
        return await m.answer("Отменено.")
    sender = getattr(m.forward_origin, "sender_user", None)
    if sender:
        uid = sender.id
    elif m.text and m.text.strip().isdigit():
        uid = int(m.text.strip())
    else:
        return await m.answer("Нужен числовой ID или пересланное сообщение.", reply_markup=CANCEL_KB)
    await db.run("INSERT OR IGNORE INTO admins(id, added_at) VALUES(?,?)", (uid, now_str()))
    ADMINS.add(uid)
    await state.clear()
    try:
        await bot.send_message(uid, "Тебя назначили админом.",
                               reply_markup=menu_for(uid))
    except Exception:
        pass
    await admins_view(m)


async def get_setting(key: str, default: str = "") -> str:
    rows = await db.all("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0][0] if rows else default


async def set_setting(key: str, value: str):
    await db.run(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def settings_view(target: Message | CallbackQuery):
    on = await get_setting("daily_schedule", "1") == "1"
    text = (
        "🗓 <b>Рассылка расписания</b>\n\n"
        f"Объявлять расписание на следующий день: <b>{'да ✅' if on else 'нет'}</b>\n\n"
        f"<i>Рассылка в {DAILY_HOUR:02d}:{DAILY_MIN:02d} (МСК). Участники могут отключить её у себя в «Настройках».</i>"
    )
    rows = [
        [btn("🔕 Выключить" if on else "🔔 Включить", "set:daily")],
        [btn("◀ Назад", "adm:panel")],
    ]
    await show(target, text, rows)


@router.callback_query(F.data == "set:open")
async def cb_settings(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    await settings_view(cb)


@router.callback_query(F.data == "set:daily")
async def cb_settings_daily(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    turn_on = await get_setting("daily_schedule", "1") != "1"
    await set_setting("daily_schedule", "1" if turn_on else "0")
    now = datetime.now(TZ)
    if turn_on and (now.hour, now.minute) >= (DAILY_HOUR, DAILY_MIN):
        await set_setting("daily_last", now.date().isoformat())
    await settings_view(cb)


def lessons_for(sched: dict, d: date) -> list[tuple[str, str]]:
    """Занятия на дату d с учётом верхней/нижней недели."""
    marker = week_marker(d, sched)
    return [
        (pair, text)
        for pair, lesson_marker, text in sched["days"].get(d.weekday(), [])
        if not (marker and lesson_marker and lesson_marker != marker)
    ]


async def daily_tick(bot: Bot):
    if await get_setting("daily_schedule", "1") != "1":
        return
    now = datetime.now(TZ)
    today = now.date()
    if (now.hour, now.minute) < (DAILY_HOUR, DAILY_MIN):
        return
    if await get_setting("daily_last") == today.isoformat():
        return

    sched = await schedule.get()
    await set_setting("daily_last", today.isoformat())
    tomorrow = today + timedelta(days=1)
    if not sched["days"]:
        log.warning("В расписании нет ни одного занятия — ежедневная рассылка пропущена (проверь парсер)")
        return
    if not lessons_for(sched, tomorrow) and tomorrow.weekday() >= 5:
        return

    text = "📅 <b>Расписание на завтра</b>\n\n" + format_day(sched, tomorrow)
    spawn(broadcast_text(bot, text))


async def broadcast_text(bot: Bot, text: str):
    ok, fail = await broadcast_items(bot, "daily", [("text", text)])
    log.info("Расписание на завтра: доставлено %s, не доставлено %s", ok, fail)


async def daily_loop(bot: Bot):
    while True:
        try:
            await daily_tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка ежедневной рассылки расписания")
        await asyncio.sleep(60)


async def show_days(m: Message, days: list[date], offgrid: bool = False):
    try:
        sched = await schedule.get()
    except Exception:
        return await m.answer("Не удалось получить расписание, попробуй позже.")
    parts = [format_day(sched, d) for d in days]
    if offgrid and sched["offgrid"]:
        parts.append(format_offgrid(sched))
    await send_long(m, "\n\n".join(parts))


@router.message(F.text == "Сегодня")
async def cmd_today(m: Message, state: FSMContext):
    await state.clear()
    await show_days(m, [datetime.now(TZ).date()])


@router.message(F.text == "Завтра")
async def cmd_tomorrow(m: Message, state: FSMContext):
    await state.clear()
    await show_days(m, [datetime.now(TZ).date() + timedelta(days=1)])


@router.message(F.text == "Неделя")
async def cmd_week(m: Message, state: FSMContext):
    await state.clear()
    today = datetime.now(TZ).date()
    monday = today - timedelta(days=today.weekday())
    if today.weekday() == 6:
        monday += timedelta(days=7)
    await show_days(m, [monday + timedelta(days=i) for i in range(6)], offgrid=True)


async def main():
    await db.open(DB_PATH)
    ADMINS.update(r[0] for r in await db.all("SELECT id FROM admins"))
    await schedule.start()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(router)
    daily_task = asyncio.create_task(daily_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        daily_task.cancel()
        await schedule.stop()
        await db.conn.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
