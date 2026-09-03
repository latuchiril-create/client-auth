import asyncio
import os
from datetime import datetime
from fastapi import FastAPI
from pydantic import BaseModel
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_URL = os.getenv("DB_URL")

app = FastAPI()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None

class AuthRequest(BaseModel):
    username: str
    hwid: str

@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DB_URL)
    asyncio.create_task(dp.start_polling(bot))

# ----------------- КЛАВИАТУРЫ -----------------

def kb_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Активные", callback_data="list:active:0"),
            InlineKeyboardButton(text="⏳ Заявки", callback_data="list:pending:0")
        ],
        [
            InlineKeyboardButton(text="⛔ Забаненные", callback_data="list:banned:0"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats")
        ],
        [InlineKeyboardButton(text="🔄 Обновить меню", callback_data="menu")]
    ])

def kb_user_card(username: str, status: str):
    buttons = []
    if status == "active":
        buttons.append([
            InlineKeyboardButton(text="🔄 Сбросить HWID", callback_data=f"act:resethwid:{username}"),
            InlineKeyboardButton(text="⏳ В ожидание", callback_data=f"act:topending:{username}")
        ])
        buttons.append([InlineKeyboardButton(text="⛔ Забанить", callback_data=f"act:ban:{username}")])
    elif status == "pending":
        buttons.append([
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"act:give:{username}"),
            InlineKeyboardButton(text="⛔ Забанить", callback_data=f"act:ban:{username}")
        ])
    elif status == "banned":
        buttons.append([
            InlineKeyboardButton(text="♻️ Разбанить (в актив)", callback_data=f"act:give:{username}"),
            InlineKeyboardButton(text="⏳ В ожидание", callback_data=f"act:topending:{username}")
        ])

    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить из базы", callback_data=f"act:delete:{username}"),
        InlineKeyboardButton(text="◀️ В меню", callback_data="menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_user_list(users: list, status: str, page: int, total_count: int):
    buttons = []
    for u in users:
        nick = u["username"]
        buttons.append([InlineKeyboardButton(text=f"👤 {nick}", callback_data=f"view:{nick}")])
    
    # Пагинация
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"list:{status}:{page - 1}"))
    if (page + 1) * 6 < total_count:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"list:{status}:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ----------------- API ENDPOINTS -----------------

@app.post("/api/auth")
async def auth(data: AuthRequest):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE username = $1", data.username)

        # Новый юзер
        if not user:
            await conn.execute(
                "INSERT INTO users (username, hwid, status) VALUES ($1, $2, 'pending')",
                data.username, data.hwid
            )
            try:
                card = (
                    f"🔔 <b>Новая заявка на доступ!</b>\n\n"
                    f"👤 <b>Ник:</b> <code>{data.username}</code>\n"
                    f"🔑 <b>HWID:</b> <code>{data.hwid}</code>\n"
                    f"⏰ <b>Время:</b> {datetime.utcnow().strftime('%H:%M:%S UTC')}"
                )
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=card,
                    parse_mode="HTML",
                    reply_markup=kb_user_card(data.username, "pending")
                )
            except Exception as e:
                print(f"Ошибка TG: {e}")

            return {"status": "pending", "message": "Подписка не активирована. Ожидайте одобрения."}

        # Проверки статуса
        if user["status"] == "banned":
            return {"status": "banned", "message": user["ban_reason"] or "Вы заблокированы."}

        if user["status"] == "pending":
            return {"status": "pending", "message": "Подписка еще не одобрена."}

        # Проверка и привязка HWID
        if not user["hwid"]:
            await conn.execute("UPDATE users SET hwid = $1 WHERE username = $2", data.hwid, data.username)
        elif user["hwid"] != data.hwid:
            return {"status": "hwid_mismatch", "message": "HWID не совпадает с привязанным!"}

        return {"status": "ok", "message": "Доступ разрешен."}

# ----------------- TELEGRAM BOT HANDLERS -----------------

@dp.message(Command("start", "menu"))
async def cmd_menu(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Доступ запрещен. Вы не являетесь администратором.")
        return
    await message.answer(
        "🎛 <b>Панель управления клиентом</b>\nВыберите действие из меню ниже:",
        parse_mode="HTML",
        reply_markup=kb_main_menu()
    )

@dp.callback_query(F.data == "menu")
async def cb_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "🎛 <b>Панель управления клиентом</b>\nВыберите действие из меню ниже:",
        parse_mode="HTML",
        reply_markup=kb_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def cb_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        active = await conn.fetchval("SELECT count(*) FROM users WHERE status = 'active'")
        pending = await conn.fetchval("SELECT count(*) FROM users WHERE status = 'pending'")
        banned = await conn.fetchval("SELECT count(*) FROM users WHERE status = 'banned'")
    
    text = (
        "📊 <b>Текущая статистика:</b>\n\n"
        f"✅ Активных подписок: <b>{active}</b>\n"
        f"⏳ Ожидают выдачи: <b>{pending}</b>\n"
        f"⛔ Заблокировано: <b>{banned}</b>\n"
        f"👥 Всего в базе: <b>{active + pending + banned}</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_main_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("list:"))
async def cb_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, status, page_str = callback.data.split(":")
    page = int(page_str)
    limit = 6
    offset = page * limit

    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM users WHERE status = $1", status)
        users = await conn.fetch("SELECT username FROM users WHERE status = $1 ORDER BY id DESC LIMIT $2 OFFSET $3", status, limit, offset)

    titles = {"active": "✅ Активные пользователи", "pending": "⏳ Заявки на доступ", "banned": "⛔ Заблокированные"}
    title = titles.get(status, "Пользователи")

    if not users:
        await callback.message.edit_text(
            f"Список <b>{title}</b> пуст.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="menu")]])
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"📋 <b>{title}</b> (Страница {page + 1}):",
        parse_mode="HTML",
        reply_markup=kb_user_list(users, status, page, total)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("view:"))
async def cb_view(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    username = callback.data.split(":")[1]
    async with db_pool.acquire() as conn:
        u = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)

    if not u:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    status_icon = {"active": "✅ Активен", "pending": "⏳ Ожидание", "banned": "⛔ Заблокирован"}.get(u["status"], u["status"])
    hwid_display = f"<code>{u['hwid']}</code>" if u["hwid"] else "<i>Не привязан</i>"

    card = (
        f"👤 <b>Пользователь:</b> <code>{u['username']}</code>\n"
        f"📌 <b>Статус:</b> {status_icon}\n"
        f"🔑 <b>HWID:</b> {hwid_display}\n"
        f"📅 <b>Создан:</b> {u['created_at'].strftime('%d.%m.%Y %H:%M') if u['created_at'] else 'Неизвестно'}"
    )
    await callback.message.edit_text(card, parse_mode="HTML", reply_markup=kb_user_card(u["username"], u["status"]))
    await callback.answer()

@dp.callback_query(F.data.startswith("act:"))
async def cb_action(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, action, username = callback.data.split(":")

    async with db_pool.acquire() as conn:
        if action == "give":
            await conn.execute("UPDATE users SET status = 'active' WHERE username = $1", username)
            await callback.answer("✅ Доступ выдан!", show_alert=True)
        elif action == "ban":
            await conn.execute("UPDATE users SET status = 'banned', ban_reason = 'Заблокирован админом' WHERE username = $1", username)
            await callback.answer("⛔ Пользователь забанен!", show_alert=True)
        elif action == "topending":
            await conn.execute("UPDATE users SET status = 'pending' WHERE username = $1", username)
            await callback.answer("⏳ Переведен в ожидание!", show_alert=True)
        elif action == "resethwid":
            await conn.execute("UPDATE users SET hwid = NULL WHERE username = $1", username)
            await callback.answer("🔄 HWID успешно сброшен!", show_alert=True)
        elif action == "delete":
            await conn.execute("DELETE FROM users WHERE username = $1", username)
            await callback.answer("🗑 Удален из базы!", show_alert=True)
            await cb_menu(callback)
            return

    # Обновляем карточку
    callback.data = f"view:{username}"
    await cb_view(callback)
