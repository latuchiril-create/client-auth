import asyncio
import os
from fastapi import FastAPI
from pydantic import BaseModel
import asyncpg
from aiogram import Bot, Dispatcher, types, F
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

def get_user_keyboard(username: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выдать доступ", callback_data=f"sub_give:{username}"),
            InlineKeyboardButton(text="⛔ Забанить", callback_data=f"sub_ban:{username}")
        ]
    ])

@app.post("/api/auth")
async def auth(data: AuthRequest):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE username = $1", data.username)

        # Новый юзер — создаем со статусом pending и шлем уведомление в ТГ
        if not user:
            await conn.execute(
                "INSERT INTO users (username, hwid, status) VALUES ($1, $2, 'pending')",
                data.username, data.hwid
            )
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🔔 <b>Новый пользователь!</b>\nНик: <code>{data.username}</code>\nHWID: <code>{data.hwid}</code>",
                    parse_mode="HTML",
                    reply_markup=get_user_keyboard(data.username)
                )
            except Exception as e:
                print(f"Ошибка отправки в TG: {e}")

            return {"status": "pending", "message": "Подписка не активирована. Ожидайте одобрения."}

        # Если забанен
        if user["status"] == "banned":
            return {"status": "banned", "message": user["ban_reason"] or "Вы заблокированы."}

        # Если не подтвержден
        if user["status"] == "pending":
            return {"status": "pending", "message": "У вас нет активной подписки."}

        # Привязка или проверка HWID
        if user["hwid"] is None:
            await conn.execute("UPDATE users SET hwid = $1 WHERE username = $2", data.hwid, data.username)
        elif user["hwid"] != data.hwid:
            return {"status": "hwid_mismatch", "message": "HWID не совпадает!"}

        return {"status": "ok", "message": "Доступ разрешен."}

@dp.callback_query(F.data.startswith("sub_give:"))
async def approve_sub(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    username = callback.data.split(":")[1]
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'active' WHERE username = $1", username)
    await callback.message.edit_text(f"✅ Доступ для <b>{username}</b> успешно активирован!", parse_mode="HTML")
    await callback.answer("Готово!")

@dp.callback_query(F.data.startswith("sub_ban:"))
async def ban_sub(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    username = callback.data.split(":")[1]
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'banned', ban_reason = 'Заблокирован админом' WHERE username = $1", username)
    await callback.message.edit_text(f"⛔ Пользователь <b>{username}</b> заблокирован.", parse_mode="HTML")
    await callback.answer("Забанен!")
