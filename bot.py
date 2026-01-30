import asyncio
import logging
import os
import threading
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI
import uvicorn

# Токены из переменных окружения
API_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_ID')

if not API_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID не найден!")

ADMIN_ID = int(ADMIN_ID)

# Настройка логов
logging.basicConfig(level=logging.INFO)

# Flask/FastAPI для Render (чтобы он не убивал бота)
app = FastAPI()

@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "alive",
        "bot": "@photo_print_orders_bot",
        "time": datetime.now().strftime("%H:%M:%S")
    }

def run_web():
    """Запускаем веб-сервер в отдельном потоке"""
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

# Создаем бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ... (весь остальной код бота остается без изменений: OrderState, main_kb, start, order_start и т.д.) ...

class OrderState(StatesGroup):
    uploading = State()
    format = State()
    delivery = State()
    phone = State()
    confirm = State()

def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🖼 Оформить заказ"), 
                  KeyboardButton(text="📋 Мои заказы")]],
        resize_keyboard=True
    )

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для заказа печати фото.\n\nНажмите «Оформить заказ», чтобы начать.",
        reply_markup=main_kb()
    )

@dp.message(F.text == "🖼 Оформить заказ")
async def order_start(message: types.Message, state: FSMContext):
    await state.set_state(OrderState.uploading)
    await message.answer(
        "📤 Отправьте фотографии (можно несколько сразу).\nКогда закончите — напишите «Готово»",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="✅ Готово")]],
            resize_keyboard=True
        )
    )

@dp.message(OrderState.uploading, F.photo)
async def get_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get('photos', [])
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos, user_id=message.from_user.id)
    await message.answer(f"📸 Принято фото: {len(photos)}")

@dp.message(OrderState.uploading, F.text == "✅ Готово")
async def finish_photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not data.get('photos'):
        await message.answer("❌ Вы не отправили ни одной фотографии!")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10×15 см — 50₽", callback_data="fmt_10x15")],
        [InlineKeyboardButton(text="15×21 см — 90₽", callback_data="fmt_15x21")],
        [InlineKeyboardButton(text="21×30 см — 150₽", callback_data="fmt_21x30")]
    ])
    await state.set_state(OrderState.format)
    await message.answer("Выберите формат печати:", reply_markup=kb)

@dp.callback_query(OrderState.format, F.data.startswith("fmt_"))
async def set_format(callback: types.CallbackQuery, state: FSMContext):
    fmt = callback.data.split("_")[1]
    prices = {"10x15": 50, "15x21": 90, "21x30": 150}
    await state.update_data(format=fmt, price=prices[fmt])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏃 Самовывоз", callback_data="del_pickup")],
        [InlineKeyboardButton(text="🚚 Доставка (+200₽)", callback_data="del_delivery")]
    ])
    await callback.message.edit_text(f"Формат: {fmt}. Способ получения?", reply_markup=kb)
    await state.set_state(OrderState.delivery)

@dp.callback_query(OrderState.delivery, F.data.startswith("del_"))
async def set_delivery(callback: types.CallbackQuery, state: FSMContext):
    d_type = callback.data.split("_")[1]
    await state.update_data(delivery=d_type, extra=200 if d_type=="delivery" else 0)
    await state.set_state(OrderState.phone)
    await callback.message.edit_text(
        "📱 Отправьте свой номер телефона:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]],
            resize_keyboard=True
        )
    )

@dp.message(OrderState.phone, F.contact)
async def get_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    data = await state.get_data()
    
    total = len(data['photos']) * data['price'] + data['extra']
    await state.update_data(total=total)
    
    text = (f"📋 Проверьте заказ:\n\n"
            f"🖼 Фото: {len(data['photos'])} шт.\n"
            f"📏 Формат: {data['format']}\n"
            f"💰 Сумма: {total}₽\n"
            f"📞 Тел: {data['phone']}\n\n"
            f"Всё верно?")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])
    await state.set_state(OrderState.confirm)
    await message.answer(text, reply_markup=kb)

@dp.callback_query(OrderState.confirm, F.data == "confirm")
async def confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    # Отправляем админу
    admin_text = (f"🆕 Новый заказ!\n"
                  f"👤 @{callback.from_user.username or callback.from_user.id}\n"
                  f"📞 {data['phone']}\n"
                  f"🖼 Фотографий: {len(data['photos'])}\n"
                  f"💰 Сумма: {data['total']}₽")
    
    await bot.send_message(ADMIN_ID, admin_text)
    for i, photo in enumerate(data['photos'][:3]):
        await bot.send_photo(ADMIN_ID, photo, caption=f"Фото {i+1}")
    
    await callback.message.edit_text(
        f"✅ Заказ принят! Номер: #{datetime.now().strftime('%H%M%S')}\n"
        f"Мы свяжемся для подтверждения. Оплата при получении.",
        reply_markup=main_kb()
    )
    await state.clear()

@dp.message(F.text == "📋 Мои заказы")
async def history(message: types.Message):
    await message.answer("📭 История заказов будет доступна после первого заказа.", reply_markup=main_kb())

# Главная функция
async def main():
    # Запускаем веб-сервер в фоне (для Render)
    thread = threading.Thread(target=run_web, daemon=True)
    thread.start()
    logging.info("Web server started on port 8000")
    
    # Запускаем бота
    logging.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
