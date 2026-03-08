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
    InlineKeyboardButton, CallbackQuery, Message
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

# Состояния FSM для основного бота
class MainBotStates(StatesGroup):
    waiting_for_bet_amount = State()
    waiting_for_deposit_amount = State()
    waiting_for_withdraw_amount = State()
    waiting_for_bot_token = State()
    waiting_for_broadcast = State()
    waiting_for_user_id_balance = State()
    waiting_for_new_balance = State()
    waiting_for_bot_settings = State()

# Состояния FSM для реферальных ботов
class ReferralBotStates(StatesGroup):
    waiting_for_bet_amount = State()
    waiting_for_deposit_amount = State()
    waiting_for_withdraw_amount = State()

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
                    is_admin BOOLEAN DEFAULT FALSE,
                    notifications_enabled BOOLEAN DEFAULT TRUE
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
                    bot_name TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    notifications_enabled BOOLEAN DEFAULT TRUE
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
            
            # Добавляем поле для уведомлений, если его нет
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT TRUE")
                await conn.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT TRUE")
                await conn.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS bot_name TEXT")
            except:
                pass
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            return dict(row) if row else None
    
    async def create_user(self, user_id: int, username: str, referrer_id: int = None, bot_token: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, referrer_id, bot_token, balance, notifications_enabled)
                VALUES ($1, $2, $3, $4, 0, TRUE)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, username, referrer_id, bot_token)
    
    async def update_balance(self, user_id: int, amount: float):
        async with self.pool.acquire() as conn:
            # Сначала проверяем текущий баланс
            current = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id)
            if current is not None:
                new_balance = current + amount
                await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", new_balance, user_id)
                logger.info(f"Баланс пользователя {user_id} изменен: {current} -> {new_balance}")
                return True
            return False
    
    async def get_balance(self, user_id: int) -> float:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id) or 0
    
    async def add_bet(self, user_id: int, game_type: str, bet_amount: float, outcome: str, result: str, win_amount: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bets (user_id, game_type, bet_amount, outcome, result, win_amount)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, user_id, game_type, bet_amount, outcome, result, win_amount)
    
    async def get_user_bots_count(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM bots WHERE owner_id = $1", user_id)
    
    async def get_user_bots(self, user_id: int) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bots WHERE owner_id = $1 ORDER BY created_at DESC", user_id)
            return [dict(row) for row in rows]
    
    async def add_bot(self, token: str, owner_id: int, bot_username: str, bot_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bots (token, owner_id, bot_username, bot_name, notifications_enabled)
                VALUES ($1, $2, $3, $4, TRUE)
            """, token, owner_id, bot_username, bot_name)
    
    async def update_bot_notifications(self, token: str, enabled: bool):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE bots SET notifications_enabled = $1 WHERE token = $2", enabled, token)
    
    async def get_bot_notifications(self, token: str) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT notifications_enabled FROM bots WHERE token = $1", token) or True
    
    async def add_referral(self, referrer_id: int, referral_id: int, bot_token: str):
        async with self.pool.acquire() as conn:
            # Проверяем, существует ли уже такая запись
            exists = await conn.fetchval("""
                SELECT COUNT(*) FROM referrals 
                WHERE referrer_id = $1 AND referral_id = $2 AND bot_token = $3
            """, referrer_id, referral_id, bot_token)
            
            if not exists:
                await conn.execute("""
                    INSERT INTO referrals (referrer_id, referral_id, bot_token, earnings)
                    VALUES ($1, $2, $3, 0)
                """, referrer_id, referral_id, bot_token)
                return True
            return False
    
    async def add_referral_earnings(self, referrer_id: int, amount: float, bot_token: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE referrals SET earnings = earnings + $1 
                WHERE referrer_id = $2 AND bot_token = $3
            """, amount, referrer_id, bot_token)
    
    async def get_referral_earnings(self, referrer_id: int, bot_token: str = None) -> float:
        async with self.pool.acquire() as conn:
            if bot_token:
                result = await conn.fetchval("""
                    SELECT COALESCE(earnings, 0) FROM referrals 
                    WHERE referrer_id = $1 AND bot_token = $2
                """, referrer_id, bot_token)
            else:
                result = await conn.fetchval("""
                    SELECT COALESCE(SUM(earnings), 0) FROM referrals 
                    WHERE referrer_id = $1
                """, referrer_id)
            return float(result) if result else 0
    
    async def is_user_exists(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id = $1", user_id) > 0
    
    async def get_all_bots(self) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.*, u.username as owner_username, 
                       (SELECT COUNT(*) FROM users WHERE bot_token = b.token) as users_count,
                       (SELECT COALESCE(SUM(earnings), 0) FROM referrals WHERE bot_token = b.token) as total_earnings
                FROM bots b
                LEFT JOIN users u ON b.owner_id = u.user_id
                ORDER BY b.created_at DESC
            """)
            return [dict(row) for row in rows]
    
    async def get_stats(self):
        async with self.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            bots_count = await conn.fetchval("SELECT COUNT(*) FROM bots")
            total_bets = await conn.fetchval("SELECT COUNT(*) FROM bets")
            total_volume = await conn.fetchval("SELECT COALESCE(SUM(bet_amount), 0) FROM bets")
            total_profit = await conn.fetchval("""
                SELECT COALESCE(SUM(bet_amount - win_amount), 0) FROM bets WHERE win_amount > 0
            """)
            return {
                "users": users_count,
                "bots": bots_count,
                "bets": total_bets,
                "volume": float(total_volume),
                "profit": float(total_profit)
            }

# Инициализация основного бота и диспетчера
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
            [InlineKeyboardButton(text="🎲 Играть в кости", callback_data="game_dice")],
            [InlineKeyboardButton(text="🏀 Баскетбол", callback_data="game_basketball")],
            [InlineKeyboardButton(text="⚽ Футбол", callback_data="game_football")]
        ]
    )

def get_dice_bet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔽 Меньше (1-3) x1.5", callback_data="bet_dice_less")],
            [InlineKeyboardButton(text="🔼 Больше (4-6) x1.7", callback_data="bet_dice_more")]
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
            [InlineKeyboardButton(text="💰 Пополнить", callback_data="deposit")],
            [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
            [InlineKeyboardButton(text="📊 История", callback_data="history")]
        ]
    )

def get_referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать нового бота", callback_data="create_bot")],
            [InlineKeyboardButton(text="📋 Мои боты", callback_data="my_bots")]
        ]
    )

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🤖 Список ботов", callback_data="admin_bots_list")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="✏️ Изменить баланс", callback_data="admin_change_balance")]
        ]
    )

def get_bot_settings_keyboard(token: str, notifications_enabled: bool) -> InlineKeyboardMarkup:
    status = "✅ Вкл" if notifications_enabled else "❌ Выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🔔 Уведомления: {status}", callback_data=f"toggle_notif_{token}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="my_bots")]
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

# Обработчики команд основного бота
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "NoUsername"
    
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
    
    # Проверяем подписку на канал
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

# Обработчик главного меню основного бота
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
    if not user:
        user = {"balance": 0}
    
    bots_count = await db.get_user_bots_count(user_id)
    
    # Получаем статистику игр
    async with db.pool.acquire() as conn:
        games_played = await conn.fetchval("SELECT COUNT(*) FROM bets WHERE user_id = $1", user_id) or 0
        total_won = await conn.fetchval("SELECT COALESCE(SUM(win_amount), 0) FROM bets WHERE user_id = $1 AND win_amount > 0", user_id) or 0
    
    await message.answer(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📝 Username: @{message.from_user.username or 'None'}\n"
        f"💰 Баланс: <b>{user['balance']:.2f} $</b>\n"
        f"🤖 Мои боты: {bots_count}\n"
        f"🎮 Сыграно игр: {games_played}\n"
        f"🏆 Выиграно: {total_won:.2f}$",
        reply_markup=get_profile_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "👥 Реферальная система")
async def referral_menu(message: Message):
    user_id = message.from_user.id
    bots_count = await db.get_user_bots_count(user_id)
    bots = await db.get_user_bots(user_id)
    
    # Получаем общий заработок с рефералов
    total_earnings = await db.get_referral_earnings(user_id)
    
    # Получаем количество рефералов
    async with db.pool.acquire() as conn:
        referrals_count = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE referrer_id = $1", user_id) or 0
    
    text = (
        f"👥 <b>Реферальная система</b>\n\n"
        f"📊 Создано ботов: {bots_count}\n"
        f"👥 Рефералов: {referrals_count}\n"
        f"💰 Заработано: <b>{total_earnings:.2f}$</b>\n"
        f"💎 Отчисления: 10% от проигрышей\n\n"
    )
    
    if bots:
        text += "📋 <b>Ваши боты:</b>\n"
        for bot in bots[:3]:  # Показываем только первые 3
            bot_earnings = await db.get_referral_earnings(user_id, bot['token'])
            text += f"• @{bot['bot_username']} — {bot_earnings:.2f}$\n"
    
    text += f"\n🔗 <b>Ваша реферальная ссылка:</b>\n"
    text += f"https://t.me/{(await bot.me()).username}?start={user_id}"
    
    await message.answer(text, reply_markup=get_referral_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "history")
async def show_history(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    async with db.pool.acquire() as conn:
        bets = await conn.fetch("""
            SELECT * FROM bets WHERE user_id = $1 
            ORDER BY created_at DESC LIMIT 10
        """, user_id)
    
    if not bets:
        await callback.message.edit_text(
            "📊 У вас пока нет истории игр.\n"
            "Нажмите 🎲 Играть чтобы начать!",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
            )
        )
        return
    
    text = "📊 <b>Последние игры:</b>\n\n"
    for bet in bets:
        emoji = "🎲" if bet['game_type'] == 'dice' else "🏀" if bet['game_type'] == 'basketball' else "⚽"
        result_emoji = "✅" if bet['result'] == 'win' else "❌"
        text += f"{emoji} {result_emoji} | {bet['bet_amount']:.2f}$ → {bet['win_amount']:.2f}$\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
        ),
        parse_mode="HTML"
    )

# Обработчики игр основного бота
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
        sport_emoji = "🏀" if game_type == "basketball" else "⚽"
        await callback.message.edit_text(
            f"{sport_emoji} Выберите исход:",
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
    await state.set_state(MainBotStates.waiting_for_bet_amount)

@dp.message(MainBotStates.waiting_for_bet_amount)
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
        
        # Получаем информацию о пользователе
        user = await db.get_user(user_id)
        
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
            await db.update_balance(user_id, win_amount)
            result_text = f"✅ <b>ВЫ ВЫИГРАЛИ!</b> +{win_amount:.2f}$"
            
            # Отчисление рефереру (10% от проигрыша) - только если проиграл
            if user and user.get("referrer_id") and not win:
                referrer_share = bet_amount * 0.1
                await db.update_balance(user["referrer_id"], referrer_share)
                await db.add_referral_earnings(user["referrer_id"], referrer_share, None)
        else:
            win_amount = 0
            await db.update_balance(user_id, -bet_amount)
            result_text = f"❌ <b>ВЫ ПРОИГРАЛИ!</b> -{bet_amount:.2f}$"
        
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
                f"🎲 <b>Выпало: {result}</b>\n\n"
                f"{result_text}\n\n"
                f"💰 <b>Текущий баланс:</b> {await db.get_balance(user_id):.2f}$",
                parse_mode="HTML"
            )
        else:
            sport_emoji = "🏀" if game_type == "basketball" else "⚽"
            result_emoji = "✅" if result == 'hit' else "❌"
            await message.answer(
                f"{sport_emoji} <b>Результат: {result_emoji}</b>\n\n"
                f"{result_text}\n\n"
                f"💰 <b>Текущий баланс:</b> {await db.get_balance(user_id):.2f}$",
                parse_mode="HTML"
            )
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

# Обработчики пополнения и вывода основного бота
@dp.callback_query(F.data == "deposit")
async def deposit(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💰 <b>Пополнение баланса</b>\n\n"
        "Введите сумму пополнения (минимум 0.01$):",
        parse_mode="HTML"
    )
    await state.set_state(MainBotStates.waiting_for_deposit_amount)

@dp.message(MainBotStates.waiting_for_deposit_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0.01:
            await message.answer("❌ Минимальная сумма пополнения 0.01$")
            return
        
        pay_url = await create_crypto_invoice(amount, message.from_user.id)
        
        if pay_url:
            await message.answer(
                f"💰 <b>Счет на {amount}$ создан!</b>\n\n"
                f"Для пополнения перейдите по ссылке и оплатите счет:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
                        [InlineKeyboardButton(text="🔙 В профиль", callback_data="back_to_profile")]
                    ]
                ),
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Ошибка создания счета. Попробуйте позже.")
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

@dp.callback_query(F.data == "withdraw")
async def withdraw(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💸 <b>Вывод средств</b>\n\n"
        "Введите сумму для вывода (минимум 1$):",
        parse_mode="HTML"
    )
    await state.set_state(MainBotStates.waiting_for_withdraw_amount)

@dp.message(MainBotStates.waiting_for_withdraw_amount)
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
            
            # Сохраняем транзакцию
            async with db.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO transactions (user_id, type, amount, status, external_id)
                    VALUES ($1, 'withdraw', $2, 'completed', $3)
                """, user_id, amount, transfer_id)
            
            await message.answer(
                f"✅ <b>Заявка на вывод создана!</b>\n"
                f"Сумма: {amount}$\n"
                f"ID транзакции: <code>{transfer_id}</code>\n\n"
                f"Средства будут зачислены в течение нескольких минут.",
                parse_mode="HTML"
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
    if not user:
        user = {"balance": 0}
    
    bots_count = await db.get_user_bots_count(user_id)
    
    await callback.message.edit_text(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📝 Username: @{callback.from_user.username or 'None'}\n"
        f"💰 Баланс: <b>{user['balance']:.2f} $</b>\n"
        f"🤖 Мои боты: {bots_count}",
        reply_markup=get_profile_keyboard(),
        parse_mode="HTML"
    )

# Обработчики реферальной системы основного бота
@dp.callback_query(F.data == "create_bot")
async def create_bot(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🤖 <b>Создание нового бота</b>\n\n"
        "1. Откройте @BotFather в Telegram\n"
        "2. Создайте нового бота командой /newbot\n"
        "3. Скопируйте токен нового бота\n"
        "4. Отправьте токен сюда:",
        parse_mode="HTML"
    )
    await state.set_state(MainBotStates.waiting_for_bot_token)

@dp.callback_query(F.data == "my_bots")
async def my_bots(callback: CallbackQuery):
    user_id = callback.from_user.id
    bots = await db.get_user_bots(user_id)
    
    if not bots:
        await callback.message.edit_text(
            "📋 <b>У вас пока нет созданных ботов</b>\n\n"
            "Нажмите ➕ Создать бота чтобы начать зарабатывать!",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Создать бота", callback_data="create_bot")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_referral")]
                ]
            ),
            parse_mode="HTML"
        )
        return
    
    text = "📋 <b>Ваши боты:</b>\n\n"
    
    for bot in bots:
        earnings = await db.get_referral_earnings(user_id, bot['token'])
        async with db.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE bot_token = $1", bot['token']) or 0
        
        notif_status = "✅" if bot['notifications_enabled'] else "❌"
        text += f"🤖 @{bot['bot_username']}\n"
        text += f"   👥 Игроков: {users_count} | 💰 Заработано: {earnings:.2f}$\n"
        text += f"   🔔 Уведомления: {notif_status}\n"
        text += f"   ⚙️ <a href='https://t.me/{bot['bot_username']}?start=settings'>Настройки</a>\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать нового", callback_data="create_bot")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_referral")]
            ]
        ),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

@dp.callback_query(F.data.startswith("bot_settings_"))
async def bot_settings(callback: CallbackQuery):
    token = callback.data.replace("bot_settings_", "")
    
    async with db.pool.acquire() as conn:
        bot_info = await conn.fetchrow("SELECT * FROM bots WHERE token = $1", token)
    
    if bot_info and bot_info['owner_id'] == callback.from_user.id:
        await callback.message.edit_text(
            f"⚙️ <b>Настройки бота @{bot_info['bot_username']}</b>\n\n"
            f"Здесь вы можете настроить уведомления о новых игроках.",
            reply_markup=get_bot_settings_keyboard(token, bot_info['notifications_enabled']),
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("toggle_notif_"))
async def toggle_notifications(callback: CallbackQuery):
    token = callback.data.replace("toggle_notif_", "")
    
    async with db.pool.acquire() as conn:
        current = await conn.fetchval("SELECT notifications_enabled FROM bots WHERE token = $1", token)
        new_value = not current
        await conn.execute("UPDATE bots SET notifications_enabled = $1 WHERE token = $2", new_value, token)
        bot_info = await conn.fetchrow("SELECT * FROM bots WHERE token = $1", token)
    
    await callback.message.edit_text(
        f"⚙️ <b>Настройки бота @{bot_info['bot_username']}</b>\n\n"
        f"✅ Настройки сохранены!",
        reply_markup=get_bot_settings_keyboard(token, new_value),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "back_to_referral")
async def back_to_referral(callback: CallbackQuery):
    user_id = callback.from_user.id
    bots_count = await db.get_user_bots_count(user_id)
    total_earnings = await db.get_referral_earnings(user_id)
    
    await callback.message.edit_text(
        f"👥 <b>Реферальная система</b>\n\n"
        f"📊 Создано ботов: {bots_count}\n"
        f"💰 Заработано: <b>{total_earnings:.2f}$</b>\n"
        f"💎 Отчисления: 10% от проигрышей\n\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n"
        f"https://t.me/{(await bot.me()).username}?start={user_id}",
        reply_markup=get_referral_keyboard(),
        parse_mode="HTML"
    )

@dp.message(MainBotStates.waiting_for_bot_token)
async def process_bot_token(message: Message, state: FSMContext):
    token = message.text.strip()
    
    try:
        # Проверяем токен
        test_bot = Bot(token=token)
        me = await test_bot.me()
        
        # Проверяем, не существует ли уже такой бот
        async with db.pool.acquire() as conn:
            exists = await conn.fetchval("SELECT COUNT(*) FROM bots WHERE token = $1", token)
            if exists:
                await message.answer("❌ Этот бот уже зарегистрирован в системе!")
                await state.clear()
                return
        
        # Сохраняем бота
        await db.add_bot(token, message.from_user.id, me.username, me.full_name)
        
        # Создаем ссылку для запуска реферального бота
        bot_link = f"https://t.me/{me.username}?start=bot_{token}"
        
        # Запускаем реферального бота
        asyncio.create_task(run_referral_bot(token))
        
        await message.answer(
            f"✅ <b>Бот @{me.username} успешно создан!</b>\n\n"
            f"📊 <b>Информация:</b>\n"
            f"• Имя: {me.full_name}\n"
            f"• Username: @{me.username}\n"
            f"• Токен: <code>{token[:10]}...{token[-5:]}</code>\n\n"
            f"💰 <b>Условия:</b>\n"
            f"• Вы получаете 10% от проигрышей игроков\n"
            f"• Можно отключить уведомления в настройках\n\n"
            f"🔗 <b>Ссылка для приглашения игроков:</b>\n"
            f"{bot_link}\n\n"
            f"📝 <b>Важно:</b> Скопируйте эту ссылку и делитесь ей!",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nПроверьте токен и попробуйте снова.")
    
    await state.clear()

# Админ-панель основного бота
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    await message.answer(
        "🔧 <b>Админ-панель</b>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    
    stats = await db.get_stats()
    
    await callback.message.edit_text(
        f"📊 <b>Статистика системы</b>\n\n"
        f"👥 Пользователей: <b>{stats['users']}</b>\n"
        f"🤖 Всего ботов: <b>{stats['bots']}</b>\n"
        f"🎮 Всего ставок: <b>{stats['bets']}</b>\n"
        f"💰 Общий оборот: <b>{stats['volume']:.2f}$</b>\n"
        f"📈 Прибыль: <b>{stats['profit']:.2f}$</b>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_bots_list")
async def admin_bots_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    
    bots = await db.get_all_bots()
    
    if not bots:
        await callback.message.edit_text(
            "📋 <b>Список ботов пуст</b>",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
        return
    
    text = "🤖 <b>Все боты системы:</b>\n\n"
    
    for bot in bots:
        text += f"• @{bot['bot_username']}\n"
        text += f"  👤 Владелец: @{bot['owner_username'] or 'None'} (ID: {bot['owner_id']})\n"
        text += f"  👥 Игроков: {bot['users_count']} | 💰 Заработано: {bot['total_earnings']:.2f}$\n"
        text += f"  🔔 Уведомления: {'✅' if bot['notifications_enabled'] else '❌'}\n"
        text += f"  📅 Создан: {bot['created_at'].strftime('%d.%m.%Y')}\n\n"
    
    # Разбиваем на части, если слишком длинное сообщение
    if len(text) > 4000:
        for i in range(0, len(text), 3500):
            await callback.message.answer(text[i:i+3500])
    else:
        await callback.message.edit_text(text, parse_mode="HTML")

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    
    await callback.message.edit_text(
        "📢 Введите сообщение для рассылки всем пользователям:"
    )
    await state.set_state(MainBotStates.waiting_for_broadcast)

@dp.message(MainBotStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    
    broadcast_text = message.text
    
    # Получаем всех пользователей
    async with db.pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
    
    sent = 0
    failed = 0
    
    await message.answer(f"📢 Начинаю рассылку... Всего пользователей: {len(users)}")
    
    for user in users:
        try:
            await bot.send_message(
                user['user_id'], 
                f"📢 <b>Рассылка от администрации</b>\n\n{broadcast_text}",
                parse_mode="HTML"
            )
            sent += 1
            await asyncio.sleep(0.05)  # Чтобы не флудить
        except:
            failed += 1
    
    await message.answer(
        f"✅ <b>Рассылка завершена!</b>\n"
        f"📊 Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data == "admin_change_balance")
async def admin_change_balance(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    
    await callback.message.edit_text(
        "✏️ Введите ID пользователя для изменения баланса:"
    )
    await state.set_state(MainBotStates.waiting_for_user_id_balance)

@dp.message(MainBotStates.waiting_for_user_id_balance)
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
            f"👤 Пользователь: <code>{user_id}</code>\n"
            f"💰 Текущий баланс: <b>{user['balance']:.2f}$</b>\n\n"
            f"Введите новую сумму баланса (можно с минусом):",
            parse_mode="HTML"
        )
        await state.set_state(MainBotStates.waiting_for_new_balance)
        
    except ValueError:
        await message.answer("❌ Введите корректный ID")

@dp.message(MainBotStates.waiting_for_new_balance)
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
        
        # Устанавливаем новый баланс напрямую
        async with db.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", new_balance, user_id)
        
        # Проверяем, что баланс действительно изменился
        updated_balance = await db.get_balance(user_id)
        
        await message.answer(
            f"✅ <b>Баланс пользователя изменен!</b>\n"
            f"👤 ID: <code>{user_id}</code>\n"
            f"📊 Старый баланс: <b>{current_balance:.2f}$</b>\n"
            f"📊 Новый баланс: <b>{updated_balance:.2f}$</b>",
            parse_mode="HTML"
        )
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")
    
    await state.clear()

# Функция для запуска реферального бота
async def run_referral_bot(bot_token: str):
    """Запуск реферального бота"""
    try:
        referral_bot = Bot(token=bot_token)
        referral_storage = MemoryStorage()
        referral_dp = Dispatcher(storage=referral_storage)
        
        @referral_dp.message(CommandStart())
        async def referral_start(message: Message, state: FSMContext):
            user_id = message.from_user.id
            username = message.from_user.username or "NoUsername"
            
            # Проверяем аргументы команды
            args = message.text.split()
            is_new_user = False
            
            # Получаем информацию о боте
            async with db.pool.acquire() as conn:
                bot_info = await conn.fetchrow("SELECT * FROM bots WHERE token = $1", bot_token)
            
            if bot_info:
                owner_id = bot_info['owner_id']
                
                # Проверяем, новый ли это пользователь
                user_exists = await db.is_user_exists(user_id)
                
                if not user_exists:
                    # Создаем пользователя с привязкой к рефереру и токеном бота
                    await db.create_user(user_id, username, owner_id, bot_token)
                    is_new_user = True
                    
                    # Добавляем запись в рефералы
                    await db.add_referral(owner_id, user_id, bot_token)
                    
                    # Отправляем уведомление владельцу, если они включены
                    if bot_info['notifications_enabled']:
                        try:
                            main_bot = Bot(token=BOT_TOKEN)
                            await main_bot.send_message(
                                owner_id,
                                f"🎉 <b>Новый игрок в вашем боте!</b>\n\n"
                                f"🤖 Бот: @{bot_info['bot_username']}\n"
                                f"👤 ID: <code>{user_id}</code>\n"
                                f"📝 Username: @{username}\n"
                                f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                                parse_mode="HTML"
                            )
                        except:
                            pass
                else:
                    # Обновляем существующего пользователя, если нужно
                    pass
            
            await message.answer(
                f"👋 <b>Добро пожаловать в игрового бота!</b>\n\n"
                f"🎰 Играй и выигрывай!",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
        
        # Копируем все обработчики из основного бота, но без проверки подписки
        @referral_dp.message(F.text == "🎲 Играть")
        async def referral_games(message: Message):
            await message.answer(
                "🎮 Выберите игру:",
                reply_markup=get_games_keyboard()
            )
        
        @referral_dp.message(F.text == "👤 Профиль")
        async def referral_profile(message: Message):
            user_id = message.from_user.id
            user = await db.get_user(user_id)
            if not user:
                user = {"balance": 0}
            
            # Получаем статистику игр
            async with db.pool.acquire() as conn:
                games_played = await conn.fetchval("SELECT COUNT(*) FROM bets WHERE user_id = $1", user_id) or 0
                total_won = await conn.fetchval("SELECT COALESCE(SUM(win_amount), 0) FROM bets WHERE user_id = $1 AND win_amount > 0", user_id) or 0
            
            await message.answer(
                f"👤 <b>Ваш профиль</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"📝 Username: @{message.from_user.username or 'None'}\n"
                f"💰 Баланс: <b>{user['balance']:.2f} $</b>\n"
                f"🎮 Сыграно игр: {games_played}\n"
                f"🏆 Выиграно: {total_won:.2f}$",
                reply_markup=get_profile_keyboard(),
                parse_mode="HTML"
            )
        
        @referral_dp.message(F.text == "👥 Реферальная система")
        async def referral_referral(message: Message):
            await message.answer(
                f"👥 <b>В этом боте реферальная система отключена.</b>\n\n"
                f"Вы можете создать своего бота в основном боте Tvist Casino и зарабатывать 10% от проигрышей игроков!",
                parse_mode="HTML"
            )
        
        @referral_dp.callback_query(F.data == "history")
        async def referral_history(callback: CallbackQuery):
            user_id = callback.from_user.id
            
            async with db.pool.acquire() as conn:
                bets = await conn.fetch("""
                    SELECT * FROM bets WHERE user_id = $1 
                    ORDER BY created_at DESC LIMIT 10
                """, user_id)
            
            if not bets:
                await callback.message.edit_text(
                    "📊 У вас пока нет истории игр.\n"
                    "Нажмите 🎲 Играть чтобы начать!",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
                    )
                )
                return
            
            text = "📊 <b>Последние игры:</b>\n\n"
            for bet in bets:
                emoji = "🎲" if bet['game_type'] == 'dice' else "🏀" if bet['game_type'] == 'basketball' else "⚽"
                result_emoji = "✅" if bet['result'] == 'win' else "❌"
                text += f"{emoji} {result_emoji} | {bet['bet_amount']:.2f}$ → {bet['win_amount']:.2f}$\n"
            
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
                ),
                parse_mode="HTML"
            )
        
        # Игровые обработчики для реферального бота
        @referral_dp.callback_query(F.data.startswith("game_"))
        async def referral_game_selection(callback: CallbackQuery, state: FSMContext):
            game_type = callback.data.split("_")[1]
            await state.update_data(game_type=game_type)
            
            if game_type == "dice":
                await callback.message.edit_text(
                    "🎲 Выберите сторону:",
                    reply_markup=get_dice_bet_keyboard()
                )
            else:
                sport_emoji = "🏀" if game_type == "basketball" else "⚽"
                await callback.message.edit_text(
                    f"{sport_emoji} Выберите исход:",
                    reply_markup=get_sport_bet_keyboard()
                )
        
        @referral_dp.callback_query(F.data.startswith("bet_"))
        async def referral_bet_selection(callback: CallbackQuery, state: FSMContext):
            bet_data = callback.data.split("_")
            bet_type = bet_data[1]
            bet_choice = bet_data[2]
            
            await state.update_data(bet_type=bet_type, bet_choice=bet_choice)
            await callback.message.edit_text("💰 Введите сумму ставки (минимум 0.01$):")
            await state.set_state(ReferralBotStates.waiting_for_bet_amount)
        
        @referral_dp.message(ReferralBotStates.waiting_for_bet_amount)
        async def referral_process_bet(message: Message, state: FSMContext):
            try:
                bet_amount = float(message.text)
                if bet_amount < 0.01:
                    await message.answer("❌ Минимальная ставка 0.01$")
                    return
                
                user_id = message.from_user.id
                balance = await db.get_balance(user_id)
                
                if balance < bet_amount:
                    await message.answer("❌ Недостаточно средств на балансе")
                    await state.clear()
                    return
                
                data = await state.get_data()
                game_type = data.get("game_type")
                bet_choice = data.get("bet_choice")
                
                # Генерируем результат
                if game_type == "dice":
                    result = random.randint(1, 6)
                    if bet_choice == "less":
                        win = result <= 3
                        multiplier = 1.5
                    else:
                        win = result >= 4
                        multiplier = 1.7
                else:
                    result = random.choice(["hit", "miss"])
                    win = result == bet_choice
                    multiplier = 1.3 if bet_choice == "miss" else 2
                
                # Рассчитываем выигрыш
                if win:
                    win_amount = bet_amount * multiplier
                    await db.update_balance(user_id, win_amount)
                    result_text = f"✅ <b>ВЫ ВЫИГРАЛИ!</b> +{win_amount:.2f}$"
                else:
                    win_amount = 0
                    await db.update_balance(user_id, -bet_amount)
                    result_text = f"❌ <b>ВЫ ПРОИГРАЛИ!</b> -{bet_amount:.2f}$"
                    
                    # Отчисление владельцу бота (10%) - только при проигрыше
                    async with db.pool.acquire() as conn:
                        bot_info = await conn.fetchrow("SELECT owner_id FROM bots WHERE token = $1", bot_token)
                        if bot_info:
                            owner_share = bet_amount * 0.1
                            await db.update_balance(bot_info['owner_id'], owner_share)
                            await db.add_referral_earnings(bot_info['owner_id'], owner_share, bot_token)
                
                # Сохраняем ставку
                await db.add_bet(
                    user_id=user_id,
                    game_type=game_type,
                    bet_amount=bet_amount,
                    outcome=bet_choice,
                    result="win" if win else "lose",
                    win_amount=win_amount
                )
                
                if game_type == "dice":
                    await message.answer_dice(emoji="🎲")
                    await asyncio.sleep(2)
                    await message.answer(
                        f"🎲 <b>Выпало: {result}</b>\n\n"
                        f"{result_text}\n\n"
                        f"💰 <b>Текущий баланс:</b> {await db.get_balance(user_id):.2f}$",
                        parse_mode="HTML"
                    )
                else:
                    sport_emoji = "🏀" if game_type == "basketball" else "⚽"
                    result_emoji = "✅" if result == 'hit' else "❌"
                    await message.answer(
                        f"{sport_emoji} <b>Результат: {result_emoji}</b>\n\n"
                        f"{result_text}\n\n"
                        f"💰 <b>Текущий баланс:</b> {await db.get_balance(user_id):.2f}$",
                        parse_mode="HTML"
                    )
                
                await state.clear()
                
            except ValueError:
                await message.answer("❌ Пожалуйста, введите число")
        
        # Обработчики пополнения и вывода для реферального бота
        @referral_dp.callback_query(F.data == "deposit")
        async def referral_deposit(callback: CallbackQuery, state: FSMContext):
            await callback.message.edit_text(
                "💰 <b>Пополнение баланса</b>\n\nВведите сумму пополнения (минимум 0.01$):",
                parse_mode="HTML"
            )
            await state.set_state(ReferralBotStates.waiting_for_deposit_amount)
        
        @referral_dp.message(ReferralBotStates.waiting_for_deposit_amount)
        async def referral_process_deposit(message: Message, state: FSMContext):
            try:
                amount = float(message.text)
                if amount < 0.01:
                    await message.answer("❌ Минимальная сумма пополнения 0.01$")
                    return
                
                pay_url = await create_crypto_invoice(amount, message.from_user.id)
                
                if pay_url:
                    await message.answer(
                        f"💰 <b>Счет на {amount}$ создан!</b>\n\n"
                        f"Для пополнения перейдите по ссылке:",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
                                [InlineKeyboardButton(text="🔙 В профиль", callback_data="back_to_profile")]
                            ]
                        ),
                        parse_mode="HTML"
                    )
                else:
                    await message.answer("❌ Ошибка создания счета")
                
                await state.clear()
            except ValueError:
                await message.answer("❌ Пожалуйста, введите число")
        
        @referral_dp.callback_query(F.data == "withdraw")
        async def referral_withdraw(callback: CallbackQuery, state: FSMContext):
            await callback.message.edit_text(
                "💸 <b>Вывод средств</b>\n\nВведите сумму для вывода (минимум 1$):",
                parse_mode="HTML"
            )
            await state.set_state(ReferralBotStates.waiting_for_withdraw_amount)
        
        @referral_dp.message(ReferralBotStates.waiting_for_withdraw_amount)
        async def referral_process_withdraw(message: Message, state: FSMContext):
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
                
                transfer_id = await create_crypto_withdrawal(amount, user_id)
                
                if transfer_id:
                    await db.update_balance(user_id, -amount)
                    await message.answer(
                        f"✅ <b>Заявка на вывод создана!</b>\n"
                        f"Сумма: {amount}$\n"
                        f"ID: <code>{transfer_id}</code>",
                        parse_mode="HTML"
                    )
                else:
                    await message.answer("❌ Ошибка создания выплаты")
                
                await state.clear()
            except ValueError:
                await message.answer("❌ Пожалуйста, введите число")
        
        @referral_dp.callback_query(F.data == "back_to_profile")
        async def referral_back_to_profile(callback: CallbackQuery):
            user_id = callback.from_user.id
            user = await db.get_user(user_id)
            if not user:
                user = {"balance": 0}
            
            await callback.message.edit_text(
                f"👤 <b>Ваш профиль</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"📝 Username: @{callback.from_user.username or 'None'}\n"
                f"💰 Баланс: <b>{user['balance']:.2f} $</b>",
                reply_markup=get_profile_keyboard(),
                parse_mode="HTML"
            )
        
        # Запускаем реферального бота
        logger.info(f"✅ Запуск реферального бота с токеном {bot_token[:10]}...")
        await referral_dp.start_polling(referral_bot)
        
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске реферального бота {bot_token[:10]}: {e}")

# Запуск основного бота
async def main():
    # Подключаемся к БД
    await db.connect()
    logger.info("✅ Подключение к БД установлено")
    
    # Запускаем всех реферальных ботов из БД
    async with db.pool.acquire() as conn:
        bots = await conn.fetch("SELECT token FROM bots")
        for bot_record in bots:
            # Запускаем каждого реферального бота в отдельной задаче
            asyncio.create_task(run_referral_bot(bot_record['token']))
            logger.info(f"🔄 Запланирован запуск реферального бота {bot_record['token'][:10]}...")
    
    # Запускаем основного бота
    logger.info("🚀 Запуск основного бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
