import os
import asyncio
import logging
import random
from datetime import datetime
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, 
    InlineKeyboardButton, CallbackQuery, Message, PreCheckoutQuery,
    LabeledPrice
)
import asyncpg
import requests
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8112176415"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "@TvistNFT")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "545818:AAvQLMQHJbxqEou37HutdklFOJEO1agzhLp")
CRYPTO_BOT_API_URL = os.getenv("CRYPTO_BOT_API_URL", "https://pay.crypt.bot/api")

# Подключение к БД из переменных окружения
DB_CONFIG = {
    "user": os.getenv("DB_USER", "bothost_db_a177582558c8"),
    "password": os.getenv("DB_PASSWORD", "HYiw9VzElZp5s1zihsFzTTRohv8co9sjODS7ob7ITPo"),
    "database": os.getenv("DB_NAME", "bothost_db_a177582558c8"),
    "host": os.getenv("DB_HOST", "node1.pghost.ru"),
    "port": int(os.getenv("DB_PORT", "32798"))
}

# Состояния FSM
class BetStates(StatesGroup):
    waiting_for_bet_amount = State()
    waiting_for_withdraw_amount = State()
    waiting_for_bot_token = State()
    waiting_for_broadcast = State()
    waiting_for_user_id_balance = State()
    waiting_for_new_balance = State()

# Класс для работы с БД
class Database:
    def __init__(self):
        self.pool = None
    
    async def connect(self):
        self.pool = await asyncpg.create_pool(**DB_CONFIG)
        await self.init_db()
    
    async def init_db(self):
        """Инициализация таблиц"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance DECIMAL(10,2) DEFAULT 0,
                    referrer_id BIGINT,
                    bot_token TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    is_admin BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Таблица ставок
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    game_type TEXT,
                    bet_amount DECIMAL(10,2),
                    outcome TEXT,
                    result TEXT,
                    win_amount DECIMAL(10,2),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица транзакций
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    type TEXT,
                    amount DECIMAL(10,2),
                    status TEXT,
                    external_id TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица ботов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    token TEXT PRIMARY KEY,
                    owner_id BIGINT,
                    bot_username TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица реферальных отчислений
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    id SERIAL PRIMARY KEY,
                    referrer_id BIGINT,
                    referral_id BIGINT,
                    bot_token TEXT,
                    earnings DECIMAL(10,2) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            return dict(row) if row else None
    
    async def create_user(self, user_id: int, username: str, referrer_id: int = None, bot_token: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, referrer_id, bot_token, balance)
                VALUES ($1, $2, $3, $4, 0)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, username, referrer_id, bot_token)
    
    async def update_balance(self, user_id: int, amount: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET balance = balance + $1 WHERE user_id = $2
            """, amount, user_id)
    
    async def get_balance(self, user_id: int) -> float:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id)
    
    async def add_bet(self, user_id: int, game_type: str, bet_amount: float, outcome: str, result: str, win_amount: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bets (user_id, game_type, bet_amount, outcome, result, win_amount)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, user_id, game_type, bet_amount, outcome, result, win_amount)
    
    async def get_user_bots_count(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM bots WHERE owner_id = $1", user_id)
    
    async def add_bot(self, token: str, owner_id: int, bot_username: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bots (token, owner_id, bot_username) VALUES ($1, $2, $3)
            """, token, owner_id, bot_username)
    
    async def add_referral(self, referrer_id: int, referral_id: int, bot_token: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO referrals (referrer_id, referral_id, bot_token) VALUES ($1, $2, $3)
            """, referrer_id, referral_id, bot_token)
    
    async def add_referral_earnings(self, referrer_id: int, amount: float, bot_token: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE referrals SET earnings = earnings + $1 
                WHERE referrer_id = $2 AND bot_token = $3
            """, amount, referrer_id, bot_token)
    
    async def get_stats(self):
        async with self.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_bets = await conn.fetchval("SELECT COUNT(*) FROM bets")
            total_volume = await conn.fetchval("SELECT COALESCE(SUM(bet_amount), 0) FROM bets")
            total_profit = await conn.fetchval("""
                SELECT COALESCE(SUM(bet_amount - win_amount), 0) FROM bets WHERE win_amount > 0
            """)
            return {
                "users": users_count,
                "bets": total_bets,
                "volume": float(total_volume),
                "profit": float(total_profit)
            }

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
db = Database()

# Клавиатуры
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 Играть")],
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="👥 Реферальная система")]
        ],
        resize_keyboard=True
    )

def get_games_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Кубик", callback_data="game_dice")],
            [InlineKeyboardButton(text="🏀 Баскетбол", callback_data="game_basketball")],
            [InlineKeyboardButton(text="⚽ Футбол", callback_data="game_football")]
        ]
    )

def get_dice_bet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Меньше (1-3) x1.5", callback_data="bet_dice_less")],
            [InlineKeyboardButton(text="Больше (4-6) x1.7", callback_data="bet_dice_more")]
        ]
    )

def get_sport_bet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Промах x1.3", callback_data="bet_sport_miss")],
            [InlineKeyboardButton(text="✅ Попадание x2", callback_data="bet_sport_hit")]
        ]
    )

def get_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="deposit")],
            [InlineKeyboardButton(text="💸 Вывод", callback_data="withdraw")]
        ]
    )

def get_referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать бота", callback_data="create_bot")]
        ]
    )

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="✏️ Изменить баланс", callback_data="admin_change_balance")]
        ]
    )

# Проверка подписки на канал
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ["left", "kicked"]
    except:
        return False

# Функции для работы с Crypto Bot API
async def create_crypto_invoice(amount: float, user_id: int) -> Optional[str]:
    """Создание инвойса в Crypto Bot"""
    url = f"{CRYPTO_BOT_API_URL}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(amount),
        "payload": f"deposit_{user_id}_{datetime.now().timestamp()}"
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                return data["result"]["pay_url"]
    except Exception as e:
        logger.error(f"Error creating invoice: {e}")
    return None

async def create_crypto_withdrawal(amount: float, user_id: int) -> Optional[str]:
    """Создание выплаты через Crypto Bot"""
    url = f"{CRYPTO_BOT_API_URL}/createTransfer"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {
        "user_id": user_id,
        "asset": "USDT",
        "amount": str(amount),
        "spend_id": f"withdraw_{user_id}_{datetime.now().timestamp()}"
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                return data["result"]["transfer_id"]
    except Exception as e:
        logger.error(f"Error creating withdrawal: {e}")
    return None

# Обработчики команд
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "NoUsername"
    
    # Проверяем, является ли бот реферальным (есть ли в БД токен бота)
    bot_info = await db.get_user(user_id)
    is_referral_bot = bot_info and bot_info.get("bot_token") is not None
    
    # Проверяем аргументы команды (для реферальных ссылок)
    args = message.text.split()
    referrer_id = None
    if len(args) > 1:
        try:
            referrer_id = int(args[1])
        except:
            pass
    
    # Создаем пользователя в БД
    await db.create_user(user_id, username, referrer_id)
    
    # Для основного бота проверяем подписку
    if not is_referral_bot:
        if not await check_subscription(user_id):
            await message.answer(
                f"🔒 Для доступа к боту необходимо подписаться на канал {CHANNEL_ID}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL_ID[1:]}")],
                        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
                    ]
                )
            )
            return
    
    await message.answer(
        f"👋 Добро пожаловать в Tvist Casino!\n\n"
        f"🎰 Играй и выигрывай!",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "check_sub")
async def check_subscription_callback(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Подписка подтверждена! Добро пожаловать.",
            reply_markup=None
        )
        await callback.message.answer(
            "👋 Добро пожаловать в главное меню!",
            reply_markup=get_main_keyboard()
        )
    else:
        await callback.answer("❌ Вы не подписаны на канал!", show_alert=True)

# Обработчик главного меню
@dp.message(F.text == "🎲 Играть")
async def games_menu(message: Message):
    await message.answer(
        "🎮 Выберите игру:",
        reply_markup=get_games_keyboard()
    )

@dp.message(F.text == "👤 Профиль")
async def profile_menu(message: Message):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    bots_count = await db.get_user_bots_count(user_id)
    
    await message.answer(
        f"👤 Профиль\n\n"
        f"ID: {user_id}\n"
        f"Username: @{message.from_user.username or 'None'}\n"
        f"Баланс: {user['balance']:.2f} $\n"
        f"Мои боты: {bots_count}",
        reply_markup=get_profile_keyboard()
    )

@dp.message(F.text == "👥 Реферальная система")
async def referral_menu(message: Message):
    user_id = message.from_user.id
    bots_count = await db.get_user_bots_count(user_id)
    
    await message.answer(
        f"👥 Реферальная система\n\n"
        f"📊 Создано ботов: {bots_count}\n"
        f"💰 Отчисления от проигрышей рефералов: 10%\n\n"
        f"🔗 Ваша реферальная ссылка:\n"
        f"https://t.me/{(await bot.me()).username}?start={user_id}",
        reply_markup=get_referral_keyboard()
    )

# Обработчики игр
@dp.callback_query(F.data.startswith("game_"))
async def game_selection(callback: CallbackQuery, state: FSMContext):
    game_type = callback.data.split("_")[1]
    
    await state.update_data(game_type=game_type)
    
    if game_type == "dice":
        await callback.message.edit_text(
            "🎲 Выберите сторону:",
            reply_markup=get_dice_bet_keyboard()
        )
    else:
        await callback.message.edit_text(
            f"{'🏀' if game_type == 'basketball' else '⚽'} Выберите исход:",
            reply_markup=get_sport_bet_keyboard()
        )

@dp.callback_query(F.data.startswith("bet_"))
async def bet_selection(callback: CallbackQuery, state: FSMContext):
    bet_data = callback.data.split("_")
    bet_type = bet_data[1]  # dice или sport
    bet_choice = bet_data[2]  # less/more или miss/hit
    
    await state.update_data(bet_type=bet_type, bet_choice=bet_choice)
    
    # Запрашиваем сумму ставки
    await callback.message.edit_text(
        "💰 Введите сумму ставки (минимум 0.01$):"
    )
    await state.set_state(BetStates.waiting_for_bet_amount)

@dp.message(BetStates.waiting_for_bet_amount)
async def process_bet_amount(message: Message, state: FSMContext):
    try:
        bet_amount = float(message.text)
        if bet_amount < 0.01:
            await message.answer("❌ Минимальная ставка 0.01$")
            return
        
        # Проверяем баланс
        user_id = message.from_user.id
        balance = await db.get_balance(user_id)
        
        if balance < bet_amount:
            await message.answer("❌ Недостаточно средств на балансе")
            await state.clear()
            return
        
        # Получаем данные ставки
        data = await state.get_data()
        game_type = data.get("game_type")
        bet_type = data.get("bet_type")
        bet_choice = data.get("bet_choice")
        
        # Генерируем результат
        if game_type == "dice":
            result = random.randint(1, 6)
            if bet_choice == "less":
                win = result <= 3
                multiplier = 1.5
            else:  # more
                win = result >= 4
                multiplier = 1.7
        else:  # basketball или football
            result = random.choice(["hit", "miss"])
            win = result == bet_choice
            multiplier = 1.3 if bet_choice == "miss" else 2
        
        # Рассчитываем выигрыш
        if win:
            win_amount = bet_amount * multiplier
            await db.update_balance(user_id, win_amount - bet_amount)
            result_text = f"✅ Вы выиграли! +{win_amount:.2f}$"
            
            # Отчисление рефереру (10% от проигрыша)
            user = await db.get_user(user_id)
            if user and user.get("referrer_id"):
                referrer_share = bet_amount * 0.1
                await db.update_balance(user["referrer_id"], referrer_share)
                await db.add_referral_earnings(user["referrer_id"], referrer_share, data.get("bot_token"))
        else:
            win_amount = 0
            await db.update_balance(user_id, -bet_amount)
            result_text = f"❌ Вы проиграли! -{bet_amount:.2f}$"
        
        # Сохраняем ставку
        await db.add_bet(
            user_id=user_id,
            game_type=game_type,
            bet_amount=bet_amount,
            outcome=bet_choice,
            result="win" if win else "lose",
            win_amount=win_amount
        )
        
        # Отправляем результат
        if game_type == "dice":
            await message.answer_dice(emoji="🎲")
            await asyncio.sleep(2)
            await message.answer(
                f"🎲 Выпало: {result}\n\n{result_text}\n\n"
                f"💰 Текущий баланс: {await db.get_balance(user_id):.2f}$"
            )
        else:
            await message.answer(
                f"🎯 Результат: {'✅' if result == 'hit' else '❌'}\n\n"
                f"{result_text}\n\n"
                f"💰 Текущий баланс: {await db.get_balance(user_id):.2f}$"
            )
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

# Обработчики пополнения и вывода
@dp.callback_query(F.data == "deposit")
async def deposit(callback: CallbackQuery):
    pay_url = await create_crypto_invoice(10, callback.from_user.id)  # Минимальная сумма 10 USDT
    
    if pay_url:
        await callback.message.edit_text(
            "💰 Пополнение баланса\n\n"
            "Для пополнения перейдите по ссылке и оплатите счет:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
                ]
            )
        )
    else:
        await callback.answer("❌ Ошибка создания счета", show_alert=True)

@dp.callback_query(F.data == "withdraw")
async def withdraw(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💸 Вывод средств\n\n"
        "Введите сумму для вывода (минимум 1$):"
    )
    await state.set_state(BetStates.waiting_for_withdraw_amount)

@dp.message(BetStates.waiting_for_withdraw_amount)
async def process_withdraw(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 1:
            await message.answer("❌ Минимальная сумма вывода 1$")
            return
        
        user_id = message.from_user.id
        balance = await db.get_balance(user_id)
        
        if balance < amount:
            await message.answer("❌ Недостаточно средств")
            await state.clear()
            return
        
        # Создаем выплату
        transfer_id = await create_crypto_withdrawal(amount, user_id)
        
        if transfer_id:
            # Списываем средства
            await db.update_balance(user_id, -amount)
            
            await message.answer(
                f"✅ Заявка на вывод создана!\n"
                f"Сумма: {amount}$\n"
                f"ID транзакции: {transfer_id}\n\n"
                f"Средства будут зачислены в течение нескольких минут."
            )
        else:
            await message.answer("❌ Ошибка создания выплаты")
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    bots_count = await db.get_user_bots_count(user_id)
    
    await callback.message.edit_text(
        f"👤 Профиль\n\n"
        f"ID: {user_id}\n"
        f"Username: @{callback.from_user.username or 'None'}\n"
        f"Баланс: {user['balance']:.2f} $\n"
        f"Мои боты: {bots_count}",
        reply_markup=get_profile_keyboard()
    )

# Обработчики реферальной системы
@dp.callback_query(F.data == "create_bot")
async def create_bot(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🤖 Создание нового бота\n\n"
        "1. Откройте @BotFather в Telegram\n"
        "2. Создайте нового бота командой /newbot\n"
        "3. Скопируйте токен нового бота\n"
        "4. Отправьте токен сюда:"
    )
    await state.set_state(BetStates.waiting_for_bot_token)

@dp.message(BetStates.waiting_for_bot_token)
async def process_bot_token(message: Message, state: FSMContext):
    token = message.text.strip()
    
    try:
        # Проверяем токен
        test_bot = Bot(token=token)
        me = await test_bot.me()
        
        # Сохраняем бота
        await db.add_bot(token, message.from_user.id, me.username)
        
        await message.answer(
            f"✅ Бот @{me.username} успешно создан!\n\n"
            f"Теперь этот бот работает на вашей реферальной ссылке.\n"
            f"Вы будете получать 10% от проигрышей ваших рефералов."
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nПроверьте токен и попробуйте снова.")
    
    await state.clear()

# Админ-панель
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    await message.answer(
        "🔧 Админ-панель",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    
    stats = await db.get_stats()
    
    await callback.message.edit_text(
        f"📊 Статистика\n\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"🎮 Всего ставок: {stats['bets']}\n"
        f"💰 Общий оборот: {stats['volume']:.2f}$\n"
        f"📈 Прибыль: {stats['profit']:.2f}$",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    
    await callback.message.edit_text(
        "📢 Введите сообщение для рассылки всем пользователям:"
    )
    await state.set_state(BetStates.waiting_for_broadcast)

@dp.message(BetStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    
    broadcast_text = message.text
    
    # Получаем всех пользователей
    async with db.pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
    
    sent = 0
    for user in users:
        try:
            await bot.send_message(user['user_id'], f"📢 Рассылка:\n\n{broadcast_text}")
            sent += 1
            await asyncio.sleep(0.05)  # Чтобы не флудить
        except:
            pass
    
    await message.answer(f"✅ Рассылка завершена. Отправлено {sent} пользователям.")
    await state.clear()

@dp.callback_query(F.data == "admin_change_balance")
async def admin_change_balance(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    
    await callback.message.edit_text(
        "✏️ Введите ID пользователя для изменения баланса:"
    )
    await state.set_state(BetStates.waiting_for_user_id_balance)

@dp.message(BetStates.waiting_for_user_id_balance)
async def process_user_id_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    
    try:
        user_id = int(message.text)
        user = await db.get_user(user_id)
        
        if not user:
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return
        
        await state.update_data(target_user_id=user_id)
        await message.answer(
            f"👤 Пользователь: {user_id}\n"
            f"💰 Текущий баланс: {user['balance']:.2f}$\n\n"
            f"Введите новую сумму баланса (можно с минусом):"
        )
        await state.set_state(BetStates.waiting_for_new_balance)
        
    except ValueError:
        await message.answer("❌ Введите корректный ID")

@dp.message(BetStates.waiting_for_new_balance)
async def process_new_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    
    try:
        new_balance = float(message.text)
        data = await state.get_data()
        user_id = data['target_user_id']
        
        # Получаем текущий баланс
        current_balance = await db.get_balance(user_id)
        
        # Изменяем баланс
        await db.update_balance(user_id, new_balance - current_balance)
        
        await message.answer(
            f"✅ Баланс пользователя {user_id} изменен\n"
            f"Старый баланс: {current_balance:.2f}$\n"
            f"Новый баланс: {new_balance:.2f}$"
        )
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")
    
    await state.clear()

# Обработчик для реферальных ботов
async def process_referral_bot_start(bot_token: str, message: Message):
    """Обработчик старта в реферальном боте"""
    user_id = message.from_user.id
    username = message.from_user.username or "NoUsername"
    
    # Проверяем, кто владелец бота
    async with db.pool.acquire() as conn:
        bot_info = await conn.fetchrow("SELECT * FROM bots WHERE token = $1", bot_token)
    
    if bot_info:
        owner_id = bot_info['owner_id']
        
        # Создаем пользователя с привязкой к рефереру
        await db.create_user(user_id, username, owner_id, bot_token)
        
        # Уведомляем владельца
        try:
            await bot.send_message(
                owner_id,
                f"🎉 Новый реферал в вашем боте!\n"
                f"👤 ID: {user_id}\n"
                f"📝 Username: @{username}"
            )
        except:
            pass
    
    await message.answer(
        f"👋 Добро пожаловать в бота от @{bot_info['bot_username'] if bot_info else 'Tvist Casino'}!\n\n"
        f"🎰 Играй и выигрывай!",
        reply_markup=get_main_keyboard()
    )

# Запуск бота
async def main():
    # Подключаемся к БД
    await db.connect()
    
    # Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
