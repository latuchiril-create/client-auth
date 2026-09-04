# main.py
import asyncio
import csv
import html
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import asyncpg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto)

try:  # необязательно: .env
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================ CONFIG ============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_URL = os.getenv("DB_URL")
API_KEY = os.getenv("API_KEY")                       # опционально: X-Api-Key для /api/auth
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "3"))))
PAGE_SIZE = 6
LOG_PAGE_SIZE = 8
BRAND = os.getenv("BRAND", "LICENSE CONTROL")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
admin = Router()
admin.message.filter(F.from_user.id == ADMIN_ID)
admin.callback_query.filter(F.from_user.id == ADMIN_ID)

db_pool: Optional[asyncpg.Pool] = None
SETTINGS: dict[str, str] = {}
DEFAULT_SETTINGS = {
    "auto_approve": "0",   # автоматически одобрять новых
    "maintenance": "0",    # техработы (всем отказ)
    "hwid_lock": "1",      # проверять HWID
    "notify_hwid": "1",    # уведомлять о несовпадении HWID
    "default_days": "30",  # срок подписки при одобрении (0 = навсегда)
}
BANNER_CACHE: dict[str, str] = {}     # key -> telegram file_id
HWID_ALERT_AT: dict[str, datetime] = {}

STATUS = {
    "active":  {"icon": "✅", "name": "Активен",       "theme": "active",  "title": "Active"},
    "pending": {"icon": "⏳", "name": "Ожидание",      "theme": "pending", "title": "Pending"},
    "banned":  {"icon": "⛔", "name": "Заблокирован",  "theme": "banned",  "title": "Banned"},
    "expired": {"icon": "⌛", "name": "Истёк",         "theme": "warn",    "title": "Expired"},
}
ACTION_ICONS = {
    "approve": "✅", "ban": "⛔", "unban": "♻️", "pending": "⏸", "reset_hwid": "🔑",
    "delete": "🗑", "sub": "⏳", "note": "📝", "add": "➕", "new_request": "🔔",
    "hwid_mismatch": "⚠️", "setting": "⚙️", "bulk": "📦", "export": "📤",
}


class Form(StatesGroup):
    search = State()
    add_user = State()
    ban_reason = State()
    note = State()
    sub_days = State()
    default_days = State()


# ============================ HELPERS ============================
def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_dt(dt: Optional[datetime], with_time: bool = True) -> str:
    dt = aware(dt)
    if not dt:
        return "—"
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def time_left(exp: Optional[datetime]) -> str:
    exp = aware(exp)
    if not exp:
        return "♾ бессрочно"
    s = (exp - now_utc()).total_seconds()
    if s <= 0:
        return "⌛ истекла"
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m}м"
    return f"{m}м"


def bar(part: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = round(width * part / total)
    return "▰" * filled + "▱" * (width - filled)


def eff_status(u) -> str:
    if u["status"] == "active" and u["expires_at"] and aware(u["expires_at"]) < now_utc():
        return "expired"
    return u["status"]


def list_where(status: str) -> str:
    return {
        "active":  "status='active' AND (expires_at IS NULL OR expires_at >= now())",
        "expired": "status='active' AND expires_at IS NOT NULL AND expires_at < now()",
        "pending": "status='pending'",
        "banned":  "status='banned'",
    }[status]


def get_setting(key: str) -> str:
    return SETTINGS.get(key, DEFAULT_SETTINGS.get(key, ""))


def flag(key: str) -> bool:
    return get_setting(key) == "1"


async def set_setting(key: str, value: str):
    SETTINGS[key] = value
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES($1,$2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, value)


async def log_action(actor: str, action: str, target: str = None, details: str = None):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO action_log(actor, action, target, details) VALUES($1,$2,$3,$4)",
            actor, action, target, details)


def default_expiry() -> Optional[datetime]:
    days = int(get_setting("default_days") or 0)
    return now_utc() + timedelta(days=days) if days > 0 else None


# ============================ DATABASE ============================
async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          SERIAL PRIMARY KEY,
            username    TEXT UNIQUE NOT NULL,
            hwid        TEXT,
            status      TEXT NOT NULL DEFAULT 'pending',
            ban_reason  TEXT,
            note        TEXT,
            expires_at  TIMESTAMPTZ,
            last_login  TIMESTAMPTZ,
            login_count INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason  TEXT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS note        TEXT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS expires_at  TIMESTAMPTZ;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login  TIMESTAMPTZ;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS login_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ NOT NULL DEFAULT now();

        CREATE TABLE IF NOT EXISTS action_log (
            id         SERIAL PRIMARY KEY,
            actor      TEXT NOT NULL,
            action     TEXT NOT NULL,
            target     TEXT,
            details    TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        rows = await conn.fetch("SELECT key, value FROM settings")
        SETTINGS.update(DEFAULT_SETTINGS)
        SETTINGS.update({r["key"]: r["value"] for r in rows})


async def get_counts(conn) -> dict:
    row = await conn.fetchrow(f"""
        SELECT
          count(*) FILTER (WHERE {list_where('active')})  AS active,
          count(*) FILTER (WHERE {list_where('pending')}) AS pending,
          count(*) FILTER (WHERE {list_where('banned')})  AS banned,
          count(*) FILTER (WHERE {list_where('expired')}) AS expired,
          count(*) AS total
        FROM users""")
    return dict(row)


# ============================ BANNER GENERATOR ============================
THEMES = {  # (тёмный, светлый, акцент)
    "menu":     ((22, 26, 64),  (84, 60, 190),  (255, 120, 210)),
    "active":   ((8, 56, 40),   (24, 150, 92),  (120, 255, 190)),
    "pending":  ((78, 48, 8),   (205, 140, 28), (255, 224, 120)),
    "banned":   ((70, 14, 26),  (192, 40, 62),  (255, 132, 142)),
    "expired":  ((60, 40, 20),  (160, 100, 40), (255, 200, 120)),
    "stats":    ((8, 40, 72),   (18, 112, 176), (120, 222, 255)),
    "user":     ((28, 30, 44),  (72, 74, 110),  (200, 204, 255)),
    "settings": ((32, 32, 36),  (92, 92, 104),  (222, 222, 230)),
    "logs":     ((30, 18, 52),  (112, 60, 156), (222, 170, 255)),
    "warn":     ((84, 20, 20),  (204, 62, 30),  (255, 182, 100)),
    "search":   ((8, 52, 62),   (18, 132, 142), (120, 240, 240)),
}
FONT_CANDIDATES = [
    os.getenv("BANNER_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font(size: int):
    for p in FONT_CANDIDATES:
        if p and os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_banner(title: str, subtitle: str = "", theme: str = "menu", badge: str = "") -> bytes:
    W, H = 1200, 480
    c1, c2, accent = THEMES.get(theme, THEMES["menu"])

    # градиент
    base = Image.new("RGB", (W, H), c1)
    d = ImageDraw.Draw(base)
    for x in range(W):
        t = x / (W - 1)
        d.line([(x, 0), (x, H)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    base = base.convert("RGBA")

    # мягкое свечение
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for cx, cy, r, a in [(W - 140, 70, 260, 80), (220, H + 80, 320, 60), (W // 2, -120, 220, 45)]:
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*accent, a))
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(70)))

    # сетка точек
    d = ImageDraw.Draw(base)
    for gx in range(40, W, 40):
        for gy in range(40, H, 40):
            d.point((gx, gy), fill=(255, 255, 255, 40))

    # стеклянная карточка
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle([60, 70, W - 60, H - 70], radius=42,
                         fill=(255, 255, 255, 26), outline=(255, 255, 255, 70), width=2)
    base = Image.alpha_composite(base, card)
    d = ImageDraw.Draw(base)

    # акцентная полоса
    d.rounded_rectangle([112, 135, 124, H - 135], radius=6, fill=(*accent, 255))

    # заголовок (авто‑подгонка размера)
    size = 92
    title = title.upper()
    while size > 36 and d.textlength(title, font=_font(size)) > W - 340:
        size -= 4
    f_title = _font(size)
    d.text((164, 190 - size // 2 - 20), title, font=f_title, fill=(255, 255, 255, 255))

    if subtitle:
        d.text((168, 262), subtitle[:60], font=_font(34), fill=(236, 236, 246, 225))

    if badge:
        f_b = _font(26)
        bw = d.textlength(badge, font=f_b) + 44
        d.rounded_rectangle([W - 110 - bw, 108, W - 110, 160], radius=26, fill=(*accent, 210))
        d.text((W - 110 - bw + 22, 119), badge, font=f_b, fill=(18, 18, 30, 255))

    d.text((168, H - 128), f"ADMIN PANEL  •  {BRAND}", font=_font(24), fill=(255, 255, 255, 130))

    buf = io.BytesIO()
    base.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def get_banner(title, subtitle, theme, badge, fresh=False):
    key = f"{theme}|{title}|{subtitle}|{badge}"
    if not fresh and key in BANNER_CACHE:
        return BANNER_CACHE[key], key
    data = await asyncio.to_thread(make_banner, title, subtitle, theme, badge)
    return BufferedInputFile(data, filename="banner.png"), key


Target = Union[types.CallbackQuery, types.Message, tuple]


async def show(target: Target, *, title: str, caption: str, kb: InlineKeyboardMarkup,
               theme: str = "menu", subtitle: str = "", badge: str = ""):
    """Единая точка рендера: картинка + подпись + клавиатура. Редактирует, если может."""
    photo, key = await get_banner(title, subtitle, theme, badge)
    media = InputMediaPhoto(media=photo, caption=caption[:1024])
    result = None
    try:
        if isinstance(target, types.CallbackQuery):
            try:
                result = await target.message.edit_media(media=media, reply_markup=kb)
            except TelegramBadRequest as e:
                if "not modified" in str(e).lower():
                    return
                if "file" in str(e).lower():           # протух file_id -> перегенерируем
                    BANNER_CACHE.pop(key, None)
                    photo, key = await get_banner(title, subtitle, theme, badge, fresh=True)
                try:
                    await target.message.delete()
                except Exception:
                    pass
                result = await target.message.answer_photo(photo=photo, caption=caption[:1024], reply_markup=kb)
        elif isinstance(target, types.Message):
            result = await target.answer_photo(photo=photo, caption=caption[:1024], reply_markup=kb)
        else:
            chat_id, msg_id = target
            try:
                result = await bot.edit_message_media(chat_id=chat_id, message_id=msg_id,
                                                      media=media, reply_markup=kb)
            except TelegramBadRequest as e:
                if "not modified" in str(e).lower():
                    return
                result = await bot.send_photo(chat_id, photo=photo, caption=caption[:1024], reply_markup=kb)
    finally:
        if isinstance(result, types.Message) and result.photo:
            BANNER_CACHE[key] = result.photo[-1].file_id


# ============================ KEYBOARDS ============================
def B(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_main(c: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(f"✅ Активные ({c['active']})", "list:active:0"), B(f"⏳ Заявки ({c['pending']})", "list:pending:0")],
        [B(f"⛔ Бан ({c['banned']})", "list:banned:0"),     B(f"⌛ Истёкшие ({c['expired']})", "list:expired:0")],
        [B("🔍 Поиск", "search"),                           B("➕ Добавить юзера", "adduser")],
        [B("📊 Статистика", "stats"),                       B("📜 Журнал", "logs:0")],
        [B("⚙️ Настройки", "settings"),                     B("📤 Экспорт CSV", "export")],
        [B("🔄 Обновить", "menu")],
    ])


def kb_user_card(u) -> InlineKeyboardMarkup:
    n, st = u["username"], eff_status(u)
    rows = []
    if st == "pending":
        rows.append([B("✅ Одобрить", f"act:give:{n}"), B("♾ Одобрить навсегда", f"act:giveforever:{n}")])
        rows.append([B("⛔ Забанить", f"act:ban:{n}"), B("⚡ Быстрый бан", f"act:banq:{n}")])
    elif st == "active":
        rows.append([B("⏳ Подписка", f"act:sub:{n}"), B("⏸ В ожидание", f"act:topending:{n}")])
        rows.append([B("⛔ Забанить", f"act:ban:{n}"), B("⚡ Быстрый бан", f"act:banq:{n}")])
    elif st == "expired":
        rows.append([B("🔁 Продлить", f"act:sub:{n}"), B("♾ Навсегда", f"act:giveforever:{n}")])
        rows.append([B("⏸ В ожидание", f"act:topending:{n}"), B("⛔ Забанить", f"act:ban:{n}")])
    elif st == "banned":
        rows.append([B("♻️ Разбанить", f"act:unban:{n}"), B("⏸ В ожидание", f"act:topending:{n}")])
    rows.append([B("🔑 Сбросить HWID", f"act:resethwid:{n}"), B("📝 Заметка", f"act:note:{n}")])
    rows.append([B("🗑 Удалить", f"act:delete:{n}"), B("🔄 Обновить", f"act:refresh:{n}")])
    rows.append([B("◀️ К списку", f"list:{st}:0"), B("🏠 Меню", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_list(users, status: str, page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        hw = "🔑" if u["hwid"] else "🆓"
        extra = ""
        if status in ("active", "expired") and u["expires_at"]:
            extra = f" · {time_left(u['expires_at'])}"
        rows.append([B(f"{STATUS[eff_status(u)]['icon']} {u['username']} {hw}{extra}", f"view:{u['username']}")])

    pages = max(1, -(-total // PAGE_SIZE))
    nav = []
    if page > 0:
        nav.append(B("⬅️", f"list:{status}:{page - 1}"))
    nav.append(B(f"📄 {page + 1}/{pages}", "noop"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(B("➡️", f"list:{status}:{page + 1}"))
    rows.append(nav)

    if status == "pending" and total:
        rows.append([B(f"✅ Одобрить всех ({total})", "bulk:approve_pending")])
    if status == "expired" and total:
        rows.append([B(f"⏸ Всех истёкших в ожидание ({total})", "bulk:expired_to_pending")])
    rows.append([B("🔍 Поиск", "search"), B("🏠 Меню", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_sub(n: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [B("+7 дн", f"sub:{n}:7"), B("+30 дн", f"sub:{n}:30"), B("+90 дн", f"sub:{n}:90"), B("+180 дн", f"sub:{n}:180")],
        [B("♾ Навсегда", f"sub:{n}:0"), B("✏️ Своё число", f"sub:{n}:custom")],
        [B("⛔ Завершить сейчас", f"sub:{n}:end")],
        [B("◀️ Назад к карточке", f"view:{n}")],
    ])


def kb_confirm(yes_data: str, no_data: str, yes_text: str = "✅ Да, подтверждаю") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[B(yes_text, yes_data), B("❌ Отмена", no_data)]])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[B("❌ Отмена", "cancel")]])


def kb_settings() -> InlineKeyboardMarkup:
    def sw(key, label):
        return B(f"{'🟢' if flag(key) else '🔴'} {label}", f"set:toggle:{key}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [sw("auto_approve", "Автоодобрение")],
        [sw("hwid_lock", "Проверка HWID"), sw("notify_hwid", "Алерты HWID")],
        [sw("maintenance", "Режим техработ")],
        [B(f"📆 Срок по умолчанию: {get_setting('default_days')} дн", "set:days")],
        [B("🏠 Меню", "menu")],
    ])


def kb_logs(page: int, total: int) -> InlineKeyboardMarkup:
    pages = max(1, -(-total // LOG_PAGE_SIZE))
    nav = []
    if page > 0:
        nav.append(B("⬅️", f"logs:{page - 1}"))
    nav.append(B(f"📄 {page + 1}/{pages}", "noop"))
    if (page + 1) * LOG_PAGE_SIZE < total:
        nav.append(B("➡️", f"logs:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[nav, [B("🧹 Очистить журнал", "logs:clear"), B("🏠 Меню", "menu")]])


# ============================ SCREENS ============================
async def show_menu(target: Target):
    async with db_pool.acquire() as conn:
        c = await get_counts(conn)
    system = "🔧 <b>Режим техработ</b> — доступ закрыт всем" if flag("maintenance") else "🟢 Система работает"
    caption = (
        "🎛 <b>Панель управления</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Активных: <b>{c['active']}</b>    ⏳ Заявок: <b>{c['pending']}</b>\n"
        f"⛔ В бане: <b>{c['banned']}</b>    ⌛ Истекло: <b>{c['expired']}</b>\n"
        f"👥 Всего в базе: <b>{c['total']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{system}\n"
        f"🕒 {now_utc().astimezone(TZ).strftime('%d.%m.%Y %H:%M:%S')}"
    )
    await show(target, title="Menu", subtitle="Control panel", theme="menu",
               badge=f"{c['total']} users", caption=caption, kb=kb_main(c))


async def show_list(target: Target, status: str, page: int):
    if status not in STATUS:
        status = "active"
    async with db_pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) FROM users WHERE {list_where(status)}")
        users = await conn.fetch(
            f"SELECT * FROM users WHERE {list_where(status)} ORDER BY id DESC LIMIT $1 OFFSET $2",
            PAGE_SIZE, page * PAGE_SIZE)
    m = STATUS[status]
    caption = (
        f"{m['icon']} <b>{m['name']} — список</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Всего: <b>{total}</b>   •   Страница <b>{page + 1}</b>\n"
        "🔑 — HWID привязан, 🆓 — свободен\n"
        + ("\n<i>Список пуст.</i>" if not users else "\nВыберите пользователя 👇")
    )
    await show(target, title=m["title"], subtitle=f"{total} users", theme=m["theme"],
               badge=f"page {page + 1}", caption=caption, kb=kb_list(users, status, page, total))


async def show_card(target: Target, username: str, header: str = ""):
    async with db_pool.acquire() as conn:
        u = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)
    if not u:
        await show(target, title="Not found", theme="warn",
                   caption=f"❌ Пользователь <code>{esc(username)}</code> не найден в базе.",
                   kb=InlineKeyboardMarkup(inline_keyboard=[[B("🏠 Меню", "menu")]]))
        return
    st = eff_status(u)
    m = STATUS[st]
    lines = [
        f"👤 <b>{esc(u['username'])}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📌 Статус: <b>{m['icon']} {m['name']}</b>",
        f"🔑 HWID: {f'<code>{esc(u[chr(104)+chr(119)+chr(105)+chr(100)])}</code>' if u['hwid'] else '<i>не привязан</i>'}",
    ]
    if u["status"] == "active":
        exp = "♾ бессрочно" if not u["expires_at"] else f"до {fmt_dt(u['expires_at'])} ({time_left(u['expires_at'])})"
        lines.append(f"⏳ Подписка: <b>{exp}</b>")
    if u["status"] == "banned":
        lines.append(f"🚫 Причина: <i>{esc(u['ban_reason'] or 'не указана')}</i>")
    lines += [
        f"🕒 Последний вход: {fmt_dt(u['last_login'])}",
        f"🔢 Входов: <b>{u['login_count']}</b>",
        f"📅 Создан: {fmt_dt(u['created_at'])}",
    ]
    if u["note"]:
        lines.append(f"📝 Заметка: <i>{esc(u['note'])}</i>")
    if header:
        lines.insert(0, header + "\n")
    await show(target, title=u["username"], subtitle=f"{m['name']}  •  #{u['id']}", theme=m["theme"],
               badge=m["title"], caption="\n".join(lines), kb=kb_user_card(u))


async def show_stats(target: Target):
    async with db_pool.acquire() as conn:
        c = await get_counts(conn)
        today = await conn.fetchval("SELECT count(*) FROM users WHERE created_at >= date_trunc('day', now())")
        week = await conn.fetchval("SELECT count(*) FROM users WHERE created_at >= now() - interval '7 days'")
        logins_today = await conn.fetchval("SELECT count(*) FROM users WHERE last_login >= date_trunc('day', now())")
        with_hwid = await conn.fetchval("SELECT count(*) FROM users WHERE hwid IS NOT NULL")
        soon = await conn.fetchval(
            "SELECT count(*) FROM users WHERE status='active' AND expires_at BETWEEN now() AND now() + interval '3 days'")
        total_logins = await conn.fetchval("SELECT coalesce(sum(login_count),0) FROM users")
    t = c["total"]

    def row(icon, name, v):
        pct = round(100 * v / t) if t else 0
        return f"{icon} {name:<9} {bar(v, t)} <b>{v}</b> ({pct}%)"

    caption = (
        "📊 <b>Статистика</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{row('✅', 'Активные', c['active'])}\n"
        f"{row('⏳', 'Заявки', c['pending'])}\n"
        f"{row('⛔', 'Бан', c['banned'])}\n"
        f"{row('⌛', 'Истёкшие', c['expired'])}</code>\n"
        f"👥 Всего: <b>{t}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🆕 Новых сегодня: <b>{today}</b>  •  за 7 дней: <b>{week}</b>\n"
        f"🔓 Входов сегодня: <b>{logins_today}</b>  •  всего: <b>{total_logins}</b>\n"
        f"🔑 С привязанным HWID: <b>{with_hwid}</b>\n"
        f"⚠️ Истекает в ближайшие 3 дня: <b>{soon}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ Автоодобрение: {'🟢' if flag('auto_approve') else '🔴'}  •  "
        f"HWID‑lock: {'🟢' if flag('hwid_lock') else '🔴'}  •  "
        f"Техработы: {'🟢' if flag('maintenance') else '🔴'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [B("🔄 Обновить", "stats"), B("📤 Экспорт CSV", "export")],
        [B("🏠 Меню", "menu")],
    ])
    await show(target, title="Stats", subtitle="Analytics overview", theme="stats",
               badge=f"{t} total", caption=caption, kb=kb)


async def show_settings(target: Target):
    caption = (
        "⚙️ <b>Настройки</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 — включено, 🔴 — выключено. Нажмите, чтобы переключить.\n\n"
        "• <b>Автоодобрение</b> — новые юзеры сразу получают доступ\n"
        "• <b>Проверка HWID</b> — отказ при несовпадении железа\n"
        "• <b>Алерты HWID</b> — уведомлять о попытках с чужого ПК\n"
        "• <b>Техработы</b> — всем клиентам ответ <code>maintenance</code>\n"
        "• <b>Срок по умолчанию</b> — дней подписки при одобрении (0 = навсегда)"
    )
    await show(target, title="Settings", subtitle="System configuration", theme="settings",
               caption=caption, kb=kb_settings())


async def show_logs(target: Target, page: int):
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM action_log")
        rows = await conn.fetch("SELECT * FROM action_log ORDER BY id DESC LIMIT $1 OFFSET $2",
                                LOG_PAGE_SIZE, page * LOG_PAGE_SIZE)
    lines = ["📜 <b>Журнал действий</b>", "━━━━━━━━━━━━━━━━━━━━"]
    if not rows:
        lines.append("<i>Пока пусто.</i>")
    for r in rows:
        ico = ACTION_ICONS.get(r["action"], "•")
        tgt = f" → <code>{esc(r['target'])}</code>" if r["target"] else ""
        det = f" <i>({esc(r['details'][:40])})</i>" if r["details"] else ""
        lines.append(f"{fmt_dt(r['created_at'])} {ico} {esc(r['actor'])}{tgt}{det}")
    await show(target, title="Logs", subtitle=f"{total} records", theme="logs",
               badge=f"page {page + 1}", caption="\n".join(lines), kb=kb_logs(page, total))


# ============================ BOT: COMMANDS ============================
@admin.message(Command("start", "menu", "panel"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await show_menu(message)


@admin.message(Command("stats"))
async def cmd_stats(message: types.Message):
    await show_stats(message)


@admin.callback_query(F.data == "noop")
async def cb_noop(cb: types.CallbackQuery):
    await cb.answer()


@admin.callback_query(F.data == "menu")
async def cb_menu(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_menu(cb)
    await cb.answer()


@admin.callback_query(F.data == "stats")
async def cb_stats(cb: types.CallbackQuery):
    await show_stats(cb)
    await cb.answer("📊 Обновлено")


@admin.callback_query(F.data.startswith("list:"))
async def cb_list(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    _, status, page = cb.data.split(":")
    await show_list(cb, status, int(page))
    await cb.answer()


@admin.callback_query(F.data.startswith("view:"))
async def cb_view(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_card(cb, cb.data.split(":", 1)[1])
    await cb.answer()


# ============================ BOT: USER ACTIONS ============================
@admin.callback_query(F.data.startswith("act:"))
async def cb_action(cb: types.CallbackQuery, state: FSMContext):
    _, action, n = cb.data.split(":", 2)

    if action == "refresh":
        await show_card(cb, n)
        await cb.answer("🔄 Обновлено")
        return

    if action == "sub":
        async with db_pool.acquire() as conn:
            u = await conn.fetchrow("SELECT * FROM users WHERE username=$1", n)
        if not u:
            await cb.answer("Не найден", show_alert=True)
            return
        cur = "♾ бессрочно" if not u["expires_at"] else f"до {fmt_dt(u['expires_at'])} ({time_left(u['expires_at'])})"
        await show(cb, title="Subscription", subtitle=n, theme="active", badge="Manage",
                   caption=(f"⏳ <b>Подписка пользователя</b> <code>{esc(n)}</code>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"Сейчас: <b>{cur}</b>\n\n"
                            "Дни <b>добавляются</b> к текущему сроку (если он ещё не истёк)."),
                   kb=kb_sub(n))
        await cb.answer()
        return

    if action == "delete":
        await show(cb, title="Delete?", subtitle=n, theme="warn", badge="Danger",
                   caption=(f"⚠️ <b>Удалить пользователя</b> <code>{esc(n)}</code> из базы?\n\n"
                            "Это действие <b>необратимо</b>. HWID, заметки и история будут потеряны."),
                   kb=kb_confirm(f"do:delete:{n}", f"view:{n}", "🗑 Да, удалить"))
        await cb.answer()
        return

    if action == "ban":
        await start_input(cb, state, Form.ban_reason, username=n, title="Ban reason", theme="banned",
                          caption=(f"⛔ <b>Бан пользователя</b> <code>{esc(n)}</code>\n\n"
                                   "✍️ Отправьте <b>причину блокировки</b> сообщением.\n"
                                   "Она будет показана пользователю в клиенте."))
        return

    if action == "note":
        await start_input(cb, state, Form.note, username=n, title="Note", theme="user",
                          caption=(f"📝 <b>Заметка для</b> <code>{esc(n)}</code>\n\n"
                                   "✍️ Отправьте текст заметки. Отправьте <code>-</code>, чтобы удалить."))
        return

    # --- мгновенные действия ---
    async with db_pool.acquire() as conn:
        if action == "give":
            await conn.execute("UPDATE users SET status='active', ban_reason=NULL, expires_at=$2 WHERE username=$1",
                               n, default_expiry())
            await log_action("admin", "approve", n, f"{get_setting('default_days')}d")
            msg = "✅ Доступ выдан"
        elif action == "giveforever":
            await conn.execute("UPDATE users SET status='active', ban_reason=NULL, expires_at=NULL WHERE username=$1", n)
            await log_action("admin", "approve", n, "forever")
            msg = "♾ Доступ выдан навсегда"
        elif action == "unban":
            await conn.execute("UPDATE users SET status='active', ban_reason=NULL WHERE username=$1", n)
            await log_action("admin", "unban", n)
            msg = "♻️ Разбанен"
        elif action == "banq":
            await conn.execute("UPDATE users SET status='banned', ban_reason='Заблокирован администратором' WHERE username=$1", n)
            await log_action("admin", "ban", n, "quick")
            msg = "⚡ Забанен"
        elif action == "topending":
            await conn.execute("UPDATE users SET status='pending', ban_reason=NULL WHERE username=$1", n)
            await log_action("admin", "pending", n)
            msg = "⏸ Переведён в ожидание"
        elif action == "resethwid":
            await conn.execute("UPDATE users SET hwid=NULL WHERE username=$1", n)
            await log_action("admin", "reset_hwid", n)
            msg = "🔑 HWID сброшен"
        else:
            msg = "❓ Неизвестное действие"
    await cb.answer(msg, show_alert=False)
    await show_card(cb, n, header=f"<b>{msg}</b>")


@admin.callback_query(F.data.startswith("do:delete:"))
async def cb_do_delete(cb: types.CallbackQuery):
    n = cb.data.split(":", 2)[2]
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username=$1", n)
    await log_action("admin", "delete", n)
    await cb.answer("🗑 Пользователь удалён", show_alert=True)
    await show_menu(cb)


@admin.callback_query(F.data.startswith("sub:"))
async def cb_sub(cb: types.CallbackQuery, state: FSMContext):
    _, n, val = cb.data.split(":", 2)
    if val == "custom":
        await start_input(cb, state, Form.sub_days, username=n, title="Custom days", theme="active",
                          caption=(f"✏️ <b>Своё число дней для</b> <code>{esc(n)}</code>\n\n"
                                   "Отправьте целое число (1–3650). Дни добавятся к текущему сроку."))
        return
    async with db_pool.acquire() as conn:
        if val == "0":
            await conn.execute("UPDATE users SET status='active', expires_at=NULL WHERE username=$1", n)
            await log_action("admin", "sub", n, "forever")
            msg = "♾ Подписка бессрочная"
        elif val == "end":
            await conn.execute("UPDATE users SET expires_at=now() WHERE username=$1", n)
            await log_action("admin", "sub", n, "ended")
            msg = "⛔ Подписка завершена"
        else:
            days = int(val)
            await apply_days(conn, n, days)
            msg = f"⏳ +{days} дн."
    await cb.answer(msg)
    await show_card(cb, n, header=f"<b>{msg}</b>")


async def apply_days(conn, n: str, days: int):
    u = await conn.fetchrow("SELECT expires_at FROM users WHERE username=$1", n)
    base = now_utc()
    if u and u["expires_at"] and aware(u["expires_at"]) > base:
        base = aware(u["expires_at"])
    await conn.execute("UPDATE users SET status='active', ban_reason=NULL, expires_at=$2 WHERE username=$1",
                       n, base + timedelta(days=days))
    await log_action("admin", "sub", n, f"+{days}d")


# ============================ BOT: BULK ============================
@admin.callback_query(F.data.startswith("bulk:"))
async def cb_bulk(cb: types.CallbackQuery):
    parts = cb.data.split(":")
    op = parts[1]
    confirmed = len(parts) > 2 and parts[2] == "yes"

    if op == "approve_pending":
        if not confirmed:
            async with db_pool.acquire() as conn:
                cnt = await conn.fetchval("SELECT count(*) FROM users WHERE status='pending'")
            await show(cb, title="Approve all", theme="pending", badge=f"{cnt} users",
                       caption=(f"📦 <b>Одобрить все заявки?</b>\n\nБудет активировано <b>{cnt}</b> пользователей "
                                f"на <b>{get_setting('default_days') or '∞'}</b> дн."),
                       kb=kb_confirm("bulk:approve_pending:yes", "list:pending:0"))
            await cb.answer()
            return
        async with db_pool.acquire() as conn:
            res = await conn.execute("UPDATE users SET status='active', expires_at=$1 WHERE status='pending'", default_expiry())
        await log_action("admin", "bulk", None, f"approve_pending {res}")
        await cb.answer(f"✅ Готово: {res}", show_alert=True)
        await show_list(cb, "active", 0)

    elif op == "expired_to_pending":
        if not confirmed:
            await show(cb, title="Move expired", theme="warn",
                       caption="📦 <b>Перевести всех истёкших в ожидание?</b>\n\nОни потеряют доступ до повторного одобрения.",
                       kb=kb_confirm("bulk:expired_to_pending:yes", "list:expired:0"))
            await cb.answer()
            return
        async with db_pool.acquire() as conn:
            res = await conn.execute(f"UPDATE users SET status='pending', expires_at=NULL WHERE {list_where('expired')}")
        await log_action("admin", "bulk", None, f"expired_to_pending {res}")
        await cb.answer(f"⏸ Готово: {res}", show_alert=True)
        await show_list(cb, "pending", 0)


# ============================ BOT: SETTINGS / LOGS / EXPORT ============================
@admin.callback_query(F.data == "settings")
async def cb_settings(cb: types.CallbackQuery):
    await show_settings(cb)
    await cb.answer()


@admin.callback_query(F.data.startswith("set:"))
async def cb_set(cb: types.CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    if parts[1] == "toggle":
        key = parts[2]
        new = "0" if flag(key) else "1"
        await set_setting(key, new)
        await log_action("admin", "setting", key, new)
        await show_settings(cb)
        await cb.answer(f"{'🟢 Включено' if new == '1' else '🔴 Выключено'}")
    elif parts[1] == "days":
        await start_input(cb, state, Form.default_days, title="Default days", theme="settings",
                          caption=("📆 <b>Срок подписки по умолчанию</b>\n\n"
                                   f"Сейчас: <b>{get_setting('default_days')}</b> дн.\n"
                                   "Отправьте число дней (0 = навсегда)."))


@admin.callback_query(F.data.startswith("logs:"))
async def cb_logs(cb: types.CallbackQuery):
    arg = cb.data.split(":")[1]
    if arg == "clear":
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM action_log")
        await cb.answer("🧹 Журнал очищен", show_alert=True)
        await show_logs(cb, 0)
        return
    await show_logs(cb, int(arg))
    await cb.answer()


@admin.callback_query(F.data == "export")
async def cb_export(cb: types.CallbackQuery):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users ORDER BY id")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "username", "status", "hwid", "expires_at", "last_login", "login_count",
                "ban_reason", "note", "created_at"])
    for r in rows:
        w.writerow([r["id"], r["username"], eff_status(r), r["hwid"] or "", fmt_dt(r["expires_at"]),
                    fmt_dt(r["last_login"]), r["login_count"], r["ban_reason"] or "", r["note"] or "",
                    fmt_dt(r["created_at"])])
    data = buf.getvalue().encode("utf-8-sig")
    fname = f"users_{now_utc().astimezone(TZ).strftime('%Y%m%d_%H%M')}.csv"
    await cb.message.answer_document(BufferedInputFile(data, filename=fname),
                                     caption=f"📤 Экспорт базы: <b>{len(rows)}</b> пользователей")
    await log_action("admin", "export", None, f"{len(rows)} rows")
    await cb.answer("📤 Файл отправлен")


# ============================ BOT: FSM (ввод текста) ============================
async def start_input(cb: types.CallbackQuery, state: FSMContext, st: State, *, title: str,
                      theme: str, caption: str, username: str = None):
    await state.set_state(st)
    await state.update_data(panel=cb.message.message_id, chat=cb.message.chat.id, username=username)
    await show(cb, title=title, subtitle=username or "", theme=theme, badge="Input",
               caption=caption, kb=kb_cancel())
    await cb.answer()


async def take_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        await message.delete()
    except Exception:
        pass
    return data, (data["chat"], data["panel"])


@admin.callback_query(F.data == "cancel")
async def cb_cancel(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if data.get("username"):
        await show_card(cb, data["username"])
    else:
        await show_menu(cb)
    await cb.answer("❌ Отменено")


@admin.callback_query(F.data == "search")
async def cb_search(cb: types.CallbackQuery, state: FSMContext):
    await start_input(cb, state, Form.search, title="Search", theme="search",
                      caption=("🔍 <b>Поиск пользователя</b>\n\n"
                               "Отправьте часть <b>ника</b> или <b>HWID</b>.\n"
                               "Регистр не важен, покажу до 10 совпадений."))


@admin.message(Form.search)
async def fsm_search(message: types.Message, state: FSMContext):
    q = (message.text or "").strip()
    data, panel = await take_input(message, state)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM users WHERE username ILIKE '%' || $1 || '%' OR hwid ILIKE '%' || $1 || '%' "
            "ORDER BY id DESC LIMIT 10", q)
    await state.clear()
    if len(rows) == 1:
        await show_card(panel, rows[0]["username"], header=f"🔍 Найдено по запросу <code>{esc(q)}</code>")
        return
    kb_rows = [[B(f"{STATUS[eff_status(u)]['icon']} {u['username']}", f"view:{u['username']}")] for u in rows]
    kb_rows.append([B("🔍 Новый поиск", "search"), B("🏠 Меню", "menu")])
    await show(panel, title="Search", subtitle=f"{len(rows)} results", theme="search", badge="Results",
               caption=(f"🔍 Запрос: <code>{esc(q)}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
                        + (f"Найдено: <b>{len(rows)}</b>" if rows else "<i>Ничего не найдено.</i>")),
               kb=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@admin.callback_query(F.data == "adduser")
async def cb_adduser(cb: types.CallbackQuery, state: FSMContext):
    await start_input(cb, state, Form.add_user, title="Add user", theme="active",
                      caption=("➕ <b>Добавить пользователя вручную</b>\n\n"
                               "Формат: <code>ник</code> или <code>ник дни</code>\n"
                               "Примеры: <code>Steve</code>, <code>Steve 30</code>, <code>Steve 0</code> (навсегда)\n"
                               f"Без числа — {get_setting('default_days')} дн. (настройка по умолчанию)."))


@admin.message(Form.add_user)
async def fsm_add_user(message: types.Message, state: FSMContext):
    parts = (message.text or "").split()
    data, panel = await take_input(message, state)
    if not parts:
        return
    n = parts[0][:32]
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else int(get_setting("default_days") or 0)
    exp = now_utc() + timedelta(days=days) if days > 0 else None
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM users WHERE username=$1", n)
        if exists:
            await state.clear()
            await show_card(panel, n, header="⚠️ <b>Такой пользователь уже есть</b>")
            return
        await conn.execute("INSERT INTO users(username, status, expires_at) VALUES($1,'active',$2)", n, exp)
    await log_action("admin", "add", n, f"{days}d" if days else "forever")
    await state.clear()
    await show_card(panel, n, header="➕ <b>Пользователь добавлен</b>")


@admin.message(Form.ban_reason)
async def fsm_ban_reason(message: types.Message, state: FSMContext):
    reason = (message.text or "").strip()[:200] or "Заблокирован администратором"
    data, panel = await take_input(message, state)
    n = data["username"]
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET status='banned', ban_reason=$2 WHERE username=$1", n, reason)
    await log_action("admin", "ban", n, reason)
    await state.clear()
    await show_card(panel, n, header="⛔ <b>Пользователь заблокирован</b>")


@admin.message(Form.note)
async def fsm_note(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()[:300]
    data, panel = await take_input(message, state)
    n = data["username"]
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET note=$2 WHERE username=$1", n, None if text == "-" else text)
    await log_action("admin", "note", n, text)
    await state.clear()
    await show_card(panel, n, header="📝 <b>Заметка сохранена</b>")


@admin.message(Form.sub_days)
async def fsm_sub_days(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    data, panel = await take_input(message, state)
    n = data["username"]
    if not txt.isdigit() or not (1 <= int(txt) <= 3650):
        await show(panel, title="Custom days", subtitle=n, theme="warn", badge="Error",
                   caption="❌ Введите целое число от 1 до 3650.", kb=kb_cancel())
        return  # состояние остаётся, ждём ещё
    async with db_pool.acquire() as conn:
        await apply_days(conn, n, int(txt))
    await state.clear()
    await show_card(panel, n, header=f"⏳ <b>+{txt} дн. добавлено</b>")


@admin.message(Form.default_days)
async def fsm_default_days(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    data, panel = await take_input(message, state)
    if not txt.isdigit() or int(txt) > 3650:
        await show(panel, title="Default days", theme="warn", badge="Error",
                   caption="❌ Введите целое число от 0 до 3650.", kb=kb_cancel())
        return
    await set_setting("default_days", txt)
    await log_action("admin", "setting", "default_days", txt)
    await state.clear()
    await show_settings(panel)


# ---- всё, что не админ ----
@dp.message()
async def not_admin_msg(message: types.Message):
    await message.answer("⛔ <b>Доступ запрещён.</b>\nЭта панель только для администратора.")


@dp.callback_query()
async def not_admin_cb(cb: types.CallbackQuery):
    await cb.answer("⛔ Доступ запрещён", show_alert=True)


dp.include_router(admin)


# ============================ FASTAPI ============================
@asynccontextmanager
async def lifespan(_: FastAPI):
    global db_pool
   db_pool = await asyncpg.create_pool(DB_URL, statement_cache_size=0)
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    task.cancel()
    await bot.session.close()
    await db_pool.close()


app = FastAPI(title="License Control API", lifespan=lifespan)


class AuthRequest(BaseModel):
    username: str
    hwid: str


async def notify_new_request(u):
    try:
        caption = (
            "🔔 <b>Новая заявка на доступ!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Ник: <code>{esc(u['username'])}</code>\n"
            f"🔑 HWID: <code>{esc(u['hwid'])}</code>\n"
            f"🕒 {fmt_dt(u['created_at'])}"
        )
        photo, key = await get_banner("New request", u["username"], "pending", "🔔")
        msg = await bot.send_photo(ADMIN_ID, photo=photo, caption=caption, reply_markup=kb_user_card(u))
        if msg.photo:
            BANNER_CACHE[key] = msg.photo[-1].file_id
    except Exception as e:
        print(f"[TG] notify_new_request error: {e}")


async def notify_hwid_mismatch(u, new_hwid: str):
    last = HWID_ALERT_AT.get(u["username"])
    if last and (now_utc() - last) < timedelta(minutes=10):
        return
    HWID_ALERT_AT[u["username"]] = now_utc()
    try:
        caption = (
            "⚠️ <b>Попытка входа с другого HWID</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Ник: <code>{esc(u['username'])}</code>\n"
            f"🔑 Привязан: <code>{esc(u['hwid'])}</code>\n"
            f"🆕 Новый: <code>{esc(new_hwid)}</code>\n"
            f"🕒 {fmt_dt(now_utc())}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [B("🔑 Сбросить HWID", f"act:resethwid:{u['username']}"), B("⛔ Забанить", f"act:banq:{u['username']}")],
            [B("👤 Открыть карточку", f"view:{u['username']}")],
        ])
        photo, key = await get_banner("HWID alert", u["username"], "warn", "⚠️")
        msg = await bot.send_photo(ADMIN_ID, photo=photo, caption=caption, reply_markup=kb)
        if msg.photo:
            BANNER_CACHE[key] = msg.photo[-1].file_id
    except Exception as e:
        print(f"[TG] notify_hwid error: {e}")


@app.get("/health")
async def health():
    return {"ok": True, "time": now_utc().isoformat()}


@app.post("/api/auth")
async def auth(data: AuthRequest, x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")

    username, hwid = data.username.strip()[:32], data.hwid.strip()[:128]
    if not username or not hwid:
        raise HTTPException(status_code=400, detail="username/hwid required")

    if flag("maintenance"):
        return {"status": "maintenance", "message": "Ведутся технические работы. Попробуйте позже."}

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)

        # ---- новый пользователь ----
        if not user:
            auto = flag("auto_approve")
            u = await conn.fetchrow(
                "INSERT INTO users (username, hwid, status, expires_at) VALUES ($1, $2, $3, $4) RETURNING *",
                username, hwid, "active" if auto else "pending", default_expiry() if auto else None)
            await log_action("api", "new_request", username, "auto-approved" if auto else None)
            if auto:
                await conn.execute("UPDATE users SET last_login=now(), login_count=1 WHERE username=$1", username)
                return {"status": "ok", "message": "Доступ разрешён.",
                        "expires_at": u["expires_at"].isoformat() if u["expires_at"] else None}
            asyncio.create_task(notify_new_request(u))
            return {"status": "pending", "message": "Заявка отправлена. Ожидайте одобрения."}

        # ---- статусы ----
        if user["status"] == "banned":
            return {"status": "banned", "message": user["ban_reason"] or "Вы заблокированы."}
        if user["status"] == "pending":
            return {"status": "pending", "message": "Подписка ещё не одобрена."}
        if user["expires_at"] and aware(user["expires_at"]) < now_utc():
            return {"status": "expired", "message": "Срок подписки истёк.",
                    "expires_at": user["expires_at"].isoformat()}

        # ---- HWID ----
        if not user["hwid"]:
            await conn.execute("UPDATE users SET hwid = $1 WHERE username = $2", hwid, username)
        elif flag("hwid_lock") and user["hwid"] != hwid:
            await log_action("api", "hwid_mismatch", username, hwid[:40])
            if flag("notify_hwid"):
                asyncio.create_task(notify_hwid_mismatch(user, hwid))
            return {"status": "hwid_mismatch", "message": "HWID не совпадает с привязанным!"}

        await conn.execute("UPDATE users SET last_login=now(), login_count=login_count+1 WHERE username=$1", username)

    days_left = None
    if user["expires_at"]:
        days_left = max(0, (aware(user["expires_at"]) - now_utc()).days)
    return {"status": "ok", "message": "Доступ разрешён.",
            "expires_at": user["expires_at"].isoformat() if user["expires_at"] else None,
            "days_left": days_left}
