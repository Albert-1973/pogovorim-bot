cat > app.py << 'EOF'
import os
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("В .env нет BOT_TOKEN. Откройте файл .env и вставьте токен бота.")

bot = Bot(TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def on_start(message: Message):
    await message.answer("Привет! Я на связи ✨ Напиши что-нибудь.")

@dp.message(F.text)
async def on_text(message: Message):
    # Тут позже подключим нейросеть/DeepSeek. Пока — простое эхо.
    await message.answer(f"Ты написал: {message.text}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
EOF
