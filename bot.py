import os
import asyncio
import logging
import random
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Union

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
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

# Crypto Bot API (поддерживает USDT и TON)
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "545818:AAvQLMQHJbxqEou37HutdklFOJEO1agzhLp")
CRYPTO_BOT_API_URL = os.getenv("CRYPTO_BOT_API_URL", "https://pay.crypt.bot/api")

# Курс TON к USDT (1 TON = 1.8 USDT)
TON_TO_USDT_RATE = 1.8

# Подключение к БД из переменных окружения
DB_CONFIG = {
    "user": os.getenv("DB_USER", "bothost_db_a177582558c8"),
    "password": os.getenv("DB_PASSWORD", "HYiw9VzElZp5s1zihsFzTTRohv8co9sjODS7ob7ITPo"),
    "database": os.getenv("DB_NAME", "bothost_db_a177582558c8"),
    "host": os.getenv("DB_HOST", "node1.pghost.ru"),
    "port": int(os.getenv("DB_PORT", "32798"))
}

# Состояния FSM
class BotStates(StatesGroup):
    waiting_for_bet_amount = State()
    waiting_for_deposit_amount = State()
    waiting_for_deposit_currency = State()
    waiting_for_withdraw_amount = State()
    waiting_for_bot_token = State()
    waiting_for_broadcast = State()
    waiting_for_bot_broadcast = State()
    waiting_for_user_id_balance = State()
    waiting_for_new_balance = State()
    waiting_for_bonus_code = State()
    waiting_for_mines_bombs = State()
    waiting_for_mines_bet = State()
    waiting_for_mines_move = State()
    waiting_for_welcome_message = State()

# Класс для работы с БД
class Database:
    def __init__(self):
        self.pool = None
    
    async def connect(self):
        self.pool = await asyncpg.create_pool(**DB_CONFIG)
        await self.init_db()
        await self.update_schema()
    
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
                    notifications_enabled BOOLEAN DEFAULT TRUE,
                    total_bets INT DEFAULT 0,
                    total_wins INT DEFAULT 0,
                    auth_token TEXT UNIQUE
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
                    multiplier DECIMAL(10,2),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица транзакций
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    type TEXT,
                    currency TEXT,
                    amount DECIMAL(10,2),
                    amount_usdt DECIMAL(10,2),
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
                    welcome_message TEXT DEFAULT '👋 Добро пожаловать в игрового бота!\n\n🎰 Играй и выигрывай!',
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
            
            # Таблица бонусных кодов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bonus_codes (
                    code TEXT PRIMARY KEY,
                    amount DECIMAL(10,2),
                    max_uses INT,
                    used_count INT DEFAULT 0,
                    expires_at TIMESTAMP,
                    created_by BIGINT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица использованных бонусов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS used_bonuses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    code TEXT,
                    amount DECIMAL(10,2),
                    used_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица игр Mines
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mines_games (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    game_id TEXT UNIQUE,
                    bet_amount DECIMAL(10,2),
                    bombs_count INT,
                    field TEXT,
                    opened_cells TEXT[] DEFAULT '{}',
                    multiplier DECIMAL(10,2) DEFAULT 1.0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
    
    async def update_schema(self):
        """Обновление схемы БД"""
        async with self.pool.acquire() as conn:
            # Добавляем новые поля, если их нет
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS balance DECIMAL(10,2) DEFAULT 0")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_token TEXT UNIQUE")
            await conn.execute("ALTER TABLE bots ADD COLUMN IF NOT EXISTS welcome_message TEXT DEFAULT '👋 Добро пожаловать в игрового бота!\n\n🎰 Играй и выигрывай!'")
            
            # Генерируем уникальные токены для существующих пользователей
            await conn.execute("""
                UPDATE users 
                SET auth_token = md5(user_id::text || created_at::text || random()::text)
                WHERE auth_token IS NULL
            """)
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            if row:
                return dict(row)
            return None
    
    async def create_user(self, user_id: int, username: str, referrer_id: int = None, bot_token: str = None):
        async with self.pool.acquire() as conn:
            # Генерируем уникальный токен
            auth_token = hashlib.md5(f"{user_id}_{datetime.now()}_{random.randint(1, 999999)}".encode()).hexdigest()
            
            await conn.execute("""
                INSERT INTO users (user_id, username, referrer_id, bot_token, balance, 
                                  notifications_enabled, total_bets, total_wins, auth_token)
                VALUES ($1, $2, $3, $4, 0, TRUE, 0, 0, $5)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, username, referrer_id, bot_token, auth_token)
    
    async def update_balance(self, user_id: int, amount: float):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)
            return True
    
    async def get_balance(self, user_id: int) -> float:
        async with self.pool.acquire() as conn:
            return float(await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id) or 0)
    
    async def add_bet(self, user_id: int, game_type: str, bet_amount: float, 
                      outcome: str, result: str, win_amount: float, multiplier: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bets (user_id, game_type, bet_amount, outcome, result, win_amount, multiplier, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """, user_id, game_type, bet_amount, outcome, result, win_amount, multiplier)
            
            # Обновляем статистику пользователя
            if result == 'win':
                await conn.execute("""
                    UPDATE users SET total_bets = total_bets + 1, total_wins = total_wins + 1 
                    WHERE user_id = $1
                """, user_id)
            else:
                await conn.execute("UPDATE users SET total_bets = total_bets + 1 WHERE user_id = $1", user_id)
    
    async def get_user_bots_count(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM bots WHERE owner_id = $1", user_id) or 0
    
    async def get_user_bots(self, user_id: int) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bots WHERE owner_id = $1 ORDER BY created_at DESC", user_id)
            return [dict(row) for row in rows]
    
    async def get_bot(self, token: str) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM bots WHERE token = $1", token)
            return dict(row) if row else None
    
    async def add_bot(self, token: str, owner_id: int, bot_username: str, bot_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bots (token, owner_id, bot_username, bot_name, welcome_message, notifications_enabled)
                VALUES ($1, $2, $3, $4, '👋 Добро пожаловать в игрового бота!\n\n🎰 Играй и выигрывай!', TRUE)
            """, token, owner_id, bot_username, bot_name)
    
    async def update_bot_notifications(self, token: str, enabled: bool):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE bots SET notifications_enabled = $1 WHERE token = $2", enabled, token)
    
    async def update_bot_welcome(self, token: str, message: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE bots SET welcome_message = $1 WHERE token = $2", message, token)
    
    async def get_bot_users(self, bot_token: str) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users WHERE bot_token = $1", bot_token)
            return [dict(row) for row in rows]
    
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
    
    async def get_stats(self) -> Dict[str, Union[int, float]]:
        async with self.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
            bots_count = await conn.fetchval("SELECT COUNT(*) FROM bots") or 0
            total_bets = await conn.fetchval("SELECT COUNT(*) FROM bets") or 0
            total_volume = await conn.fetchval("SELECT COALESCE(SUM(bet_amount), 0) FROM bets") or 0
            total_profit = await conn.fetchval("""
                SELECT COALESCE(SUM(bet_amount - win_amount), 0) 
                FROM bets WHERE win_amount > 0
            """) or 0
            
            today = datetime.now().date()
            today_bets = await conn.fetchval("SELECT COUNT(*) FROM bets WHERE DATE(created_at) = $1", today) or 0
            today_volume = await conn.fetchval("SELECT COALESCE(SUM(bet_amount), 0) FROM bets WHERE DATE(created_at) = $1", today) or 0
            
            return {
                "users": users_count,
                "bots": bots_count,
                "bets": total_bets,
                "volume": float(total_volume),
                "profit": float(total_profit),
                "today_bets": today_bets,
                "today_volume": float(today_volume)
            }
    
    async def create_bonus_code(self, code: str, amount: float, max_uses: int, expires_days: int, created_by: int):
        async with self.pool.acquire() as conn:
            expires_at = datetime.now() + timedelta(days=expires_days)
            await conn.execute("""
                INSERT INTO bonus_codes (code, amount, max_uses, used_count, expires_at, created_by)
                VALUES ($1, $2, $3, 0, $4, $5)
            """, code.upper(), amount, max_uses, expires_at, created_by)
    
    async def use_bonus_code(self, user_id: int, code: str) -> Optional[float]:
        async with self.pool.acquire() as conn:
            # Проверяем код
            bonus = await conn.fetchrow("SELECT * FROM bonus_codes WHERE code = $1", code.upper())
            if not bonus:
                return None
            
            # Проверяем срок действия
            if bonus['expires_at'] < datetime.now():
                return -1
            
            # Проверяем лимит использований
            if bonus['used_count'] >= bonus['max_uses']:
                return -2
            
            # Проверяем, не использовал ли пользователь этот код
            used = await conn.fetchval("SELECT COUNT(*) FROM used_bonuses WHERE user_id = $1 AND code = $2", 
                                      user_id, code.upper())
            if used > 0:
                return -3
            
            # Начисляем бонус
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", 
                             bonus['amount'], user_id)
            
            # Обновляем счетчик использований
            await conn.execute("UPDATE bonus_codes SET used_count = used_count + 1 WHERE code = $1", code.upper())
            
            # Записываем использование
            await conn.execute("INSERT INTO used_bonuses (user_id, code, amount) VALUES ($1, $2, $3)", 
                             user_id, code.upper(), bonus['amount'])
            
            return float(bonus['amount'])

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
            [KeyboardButton(text="👥 Рефералы"), KeyboardButton(text="🎁 Бонусы")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )

def get_games_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Кости", callback_data="game_dice"),
             InlineKeyboardButton(text="💣 Майнс", callback_data="game_mines")],
            [InlineKeyboardButton(text="🏀 Баскетбол", callback_data="game_basketball"),
             InlineKeyboardButton(text="⚽ Футбол", callback_data="game_football")],
            [InlineKeyboardButton(text="🎯 Быстрая игра (x2)", callback_data="game_quick")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
    )

def get_dice_bet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔽 Меньше (1-3) x1.5", callback_data="bet_dice_less"),
             InlineKeyboardButton(text="🔼 Больше (4-6) x1.7", callback_data="bet_dice_more")],
            [InlineKeyboardButton(text="🎲 Четное x1.6", callback_data="bet_dice_even"),
             InlineKeyboardButton(text="🎲 Нечетное x1.6", callback_data="bet_dice_odd")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_games")]
        ]
    )

def get_sport_bet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Промах x1.3", callback_data="bet_sport_miss"),
             InlineKeyboardButton(text="✅ Попадание x2", callback_data="bet_sport_hit")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_games")]
        ]
    )

def get_quick_bet_keyboard() -> InlineKeyboardMarkup:
    amounts = [1, 5, 10, 25, 50, 100]
    keyboard = []
    row = []
    for i, amount in enumerate(amounts):
        row.append(InlineKeyboardButton(text=f"{amount}$", callback_data=f"quick_{amount}"))
        if (i + 1) % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="💰 Своя сумма", callback_data="quick_custom")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_games")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_mines_bombs_keyboard() -> InlineKeyboardMarkup:
    bombs = [1, 3, 5, 7, 10]
    keyboard = []
    row = []
    for i, bomb in enumerate(bombs):
        row.append(InlineKeyboardButton(text=f"💣 {bomb}", callback_data=f"mines_bombs_{bomb}"))
        if (i + 1) % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_games")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_mines_bet_keyboard() -> InlineKeyboardMarkup:
    amounts = [1, 5, 10, 25, 50]
    keyboard = []
    row = []
    for i, amount in enumerate(amounts):
        row.append(InlineKeyboardButton(text=f"{amount}$", callback_data=f"mines_bet_{amount}"))
        if (i + 1) % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="💰 Своя сумма", callback_data="mines_bet_custom")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_games")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_mines_field_keyboard(opened_cells: list, game_over: bool = False) -> InlineKeyboardMarkup:
    keyboard = []
    for i in range(5):
        row = []
        for j in range(5):
            cell_num = i * 5 + j
            if str(cell_num) in opened_cells:
                if game_over:
                    row.append(InlineKeyboardButton(text="💎", callback_data=f"mines_cell_{cell_num}"))
                else:
                    row.append(InlineKeyboardButton(text="✅", callback_data=f"mines_cell_{cell_num}"))
            else:
                row.append(InlineKeyboardButton(text="⬜", callback_data=f"mines_cell_{cell_num}"))
        keyboard.append(row)
    
    if not game_over and opened_cells:
        keyboard.append([InlineKeyboardButton(text="💰 Забрать выигрыш", callback_data="mines_cashout")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_games")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Пополнить", callback_data="deposit"),
             InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
            [InlineKeyboardButton(text="📊 История", callback_data="history"),
             InlineKeyboardButton(text="📈 Статистика", callback_data="stats")]
        ]
    )

def get_deposit_currency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 USDT (крипто)", callback_data="deposit_usdt")],
            [InlineKeyboardButton(text="💎 TON (1 TON = 1.8 USDT)", callback_data="deposit_ton")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
        ]
    )

def get_withdraw_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 USDT", callback_data="withdraw_usdt")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
        ]
    )

def get_bonus_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Активировать код", callback_data="activate_code")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
    )

def get_referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать бота", callback_data="create_bot")],
            [InlineKeyboardButton(text="📋 Мои боты", callback_data="my_bots")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="referral_stats")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
    )

def get_bot_owner_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data=f"bot_broadcast_{token}")],
            [InlineKeyboardButton(text="✏️ Изменить приветствие", callback_data=f"bot_welcome_{token}")],
            [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data=f"bot_settings_{token}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="my_bots")]
        ]
    )

def get_bot_settings_keyboard(token: str, notifications_enabled: bool) -> InlineKeyboardMarkup:
    status = "✅ Вкл" if notifications_enabled else "❌ Выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🔔 Уведомления: {status}", callback_data=f"toggle_notif_{token}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"bot_owner_{token}")]
        ]
    )

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🤖 Список ботов", callback_data="admin_bots_list")],
            [InlineKeyboardButton(text="📢 Рассылка (всем ботам)", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="✏️ Изменить баланс", callback_data="admin_change_balance")],
            [InlineKeyboardButton(text="🎁 Создать бонус-код", callback_data="admin_create_bonus")]
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
async def create_crypto_invoice(amount: float, currency: str, user_id: int) -> Optional[str]:
    """Создание инвойса в Crypto Bot"""
    url = f"{CRYPTO_BOT_API_URL}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    
    # Конвертируем валюту в формат Crypto Bot
    asset = "USDT" if currency == "USDT" else "TON"
    
    payload = {
        "asset": asset,
        "amount": str(amount),
        "payload": f"deposit_{currency}_{user_id}_{datetime.now().timestamp()}"
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                return data["result"]["pay_url"]
    except Exception as e:
        logger.error(f"Error creating {currency} invoice: {e}")
    return None

async def create_crypto_withdrawal(amount: float, user_id: int) -> Optional[str]:
    """Создание выплаты через Crypto Bot (только USDT)"""
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
            # Проверяем, не токен ли это бота
            if args[1].startswith("bot_"):
                bot_token = args[1][4:]
                async with db.pool.acquire() as conn:
                    bot_info = await conn.fetchrow("SELECT * FROM bots WHERE token = $1", bot_token)
                    if bot_info:
                        referrer_id = bot_info['owner_id']
    
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
        f"👋 <b>Добро пожаловать в Tvist Casino!</b>\n\n"
        f"🎰 Играй и выигрывай!",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
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

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "👋 Главное меню:",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "back_to_games")
async def back_to_games(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎮 Выберите игру:",
        reply_markup=get_games_keyboard()
    )

# Обработчик главного меню
@dp.message(F.text == "🎲 Играть")
async def games_menu(message: Message):
    await message.answer(
        "🎮 <b>Выберите игру:</b>",
        reply_markup=get_games_keyboard(),
        parse_mode="HTML"
    )

# Обработчики игр
@dp.callback_query(F.data.startswith("game_"))
async def game_selection(callback: CallbackQuery, state: FSMContext):
    game_type = callback.data.split("_")[1]
    
    await state.update_data(game_type=game_type)
    
    if game_type == "dice":
        await callback.message.edit_text(
            "🎲 <b>Выберите сторону:</b>",
            reply_markup=get_dice_bet_keyboard(),
            parse_mode="HTML"
        )
    elif game_type == "quick":
        await callback.message.edit_text(
            "🎯 <b>Быстрая игра</b>\n\nВыберите сумму ставки:",
            reply_markup=get_quick_bet_keyboard(),
            parse_mode="HTML"
        )
    elif game_type == "mines":
        await callback.message.edit_text(
            "💣 <b>Майнс</b>\n\nВыберите количество мин:",
            reply_markup=get_mines_bombs_keyboard(),
            parse_mode="HTML"
        )
    else:
        sport_emoji = "🏀" if game_type == "basketball" else "⚽"
        await callback.message.edit_text(
            f"{sport_emoji} <b>Выберите исход:</b>",
            reply_markup=get_sport_bet_keyboard(),
            parse_mode="HTML"
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
    await state.set_state(BotStates.waiting_for_bet_amount)

@dp.callback_query(F.data.startswith("quick_"))
async def quick_bet_selection(callback: CallbackQuery, state: FSMContext):
    if callback.data == "quick_custom":
        await callback.message.edit_text(
            "💰 Введите сумму ставки (минимум 0.01$):"
        )
        await state.set_state(BotStates.waiting_for_bet_amount)
        await state.update_data(game_type="quick", bet_type="quick", bet_choice="quick")
        return
    
    try:
        bet_amount = float(callback.data.replace("quick_", ""))
        
        # Списываем ставку сразу
        user_id = callback.from_user.id
        balance = await db.get_balance(user_id)
        
        if balance < bet_amount:
            await callback.answer("❌ Недостаточно средств!", show_alert=True)
            return
        
        await db.update_balance(user_id, -bet_amount)
        
        # Генерируем результат
        win = random.choice([True, False])
        multiplier = 2.0
        
        if win:
            win_amount = bet_amount * multiplier
            await db.update_balance(user_id, win_amount)
            result_text = f"✅ <b>ВЫ ВЫИГРАЛИ!</b> +{win_amount:.2f}$"
        else:
            win_amount = 0
            result_text = f"❌ <b>ВЫ ПРОИГРАЛИ!</b> -{bet_amount:.2f}$"
            
            # Отчисление владельцу реферального бота
            user = await db.get_user(user_id)
            if user and user.get("bot_token"):
                bot_token = user.get("bot_token")
                async with db.pool.acquire() as conn:
                    bot_info = await conn.fetchrow("SELECT owner_id FROM bots WHERE token = $1", bot_token)
                    if bot_info:
                        owner_share = bet_amount * 0.1
                        await db.update_balance(bot_info['owner_id'], owner_share)
                        await db.add_referral_earnings(bot_info['owner_id'], owner_share, bot_token)
        
        # Сохраняем ставку
        await db.add_bet(
            user_id=user_id,
            game_type="quick",
            bet_amount=bet_amount,
            outcome="quick",
            result="win" if win else "lose",
            win_amount=win_amount,
            multiplier=multiplier
        )
        
        # Отправляем результат
        await callback.message.answer_dice(emoji="🎲")
        await asyncio.sleep(2)
        await callback.message.answer(
            f"🎯 <b>Быстрая игра</b>\n\n"
            f"{result_text}\n\n"
            f"💰 <b>Текущий баланс:</b> {await db.get_balance(user_id):.2f}$",
            parse_mode="HTML"
        )
        
        await callback.message.delete()
        
    except Exception as e:
        logger.error(f"Error in quick bet: {e}")
        await callback.answer("❌ Ошибка при создании ставки", show_alert=True)

@dp.callback_query(F.data.startswith("mines_bombs_"))
async def mines_bombs_selection(callback: CallbackQuery, state: FSMContext):
    bombs = int(callback.data.replace("mines_bombs_", ""))
    await state.update_data(mines_bombs=bombs)
    
    await callback.message.edit_text(
        f"💣 <b>Майнс</b> (💣 {bombs} мин)\n\n"
        f"Введите сумму ставки:",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_mines_bet)

@dp.message(BotStates.waiting_for_bet_amount)
async def process_bet_amount(message: Message, state: FSMContext):
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
        
        # Получаем данные ставки
        data = await state.get_data()
        game_type = data.get("game_type")
        bet_type = data.get("bet_type")
        bet_choice = data.get("bet_choice")
        
        # Списываем ставку
        await db.update_balance(user_id, -bet_amount)
        
        # Получаем информацию о пользователе
        user = await db.get_user(user_id)
        
        # Генерируем результат
        if game_type == "dice":
            result = random.randint(1, 6)
            if bet_choice == "less":
                win = result <= 3
                multiplier = 1.5
            elif bet_choice == "more":
                win = result >= 4
                multiplier = 1.7
            elif bet_choice == "even":
                win = result % 2 == 0
                multiplier = 1.6
            else:  # odd
                win = result % 2 == 1
                multiplier = 1.6
        else:  # basketball или football
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
            result_text = f"❌ <b>ВЫ ПРОИГРАЛИ!</b> -{bet_amount:.2f}$"
            
            # Отчисление владельцу реферального бота
            if user and user.get("bot_token"):
                bot_token = user.get("bot_token")
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
            win_amount=win_amount,
            multiplier=multiplier
        )
        
        # Отправляем результат
        if game_type == "dice":
            await message.answer_dice(emoji="🎲")
            await asyncio.sleep(2)
            result_display = result
            if bet_choice in ["even", "odd"]:
                result_display = "четное" if result % 2 == 0 else "нечетное"
            await message.answer(
                f"🎲 <b>Выпало: {result} ({result_display})</b>\n\n"
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
    except Exception as e:
        logger.error(f"Error in process_bet_amount: {e}")
        await message.answer("❌ Произошла ошибка")
        await state.clear()

@dp.message(BotStates.waiting_for_mines_bet)
async def process_mines_bet(message: Message, state: FSMContext):
    try:
        bet_amount = float(message.text)
        if bet_amount < 0.01:
            await message.answer("❌ Минимальная ставка 0.01$")
            return
        
        data = await state.get_data()
        bombs = data.get('mines_bombs', 3)
        user_id = message.from_user.id
        
        balance = await db.get_balance(user_id)
        
        if balance < bet_amount:
            await message.answer("❌ Недостаточно средств на балансе")
            await state.clear()
            return
        
        # Списываем ставку
        await db.update_balance(user_id, -bet_amount)
        
        # Генерируем уникальный ID игры
        game_id = hashlib.md5(f"{user_id}_{datetime.now()}_{random.randint(1,999999)}".encode()).hexdigest()[:8]
        
        # Создаем поле
        field = []
        for i in range(25):
            field.append("bomb" if i < bombs else "gem")
        random.shuffle(field)
        field_str = ",".join(field)
        
        # Сохраняем игру
        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO mines_games (user_id, game_id, bet_amount, bombs_count, field, opened_cells, multiplier, is_active)
                VALUES ($1, $2, $3, $4, $5, ARRAY[]::TEXT[], 1.0, TRUE)
            """, user_id, game_id, bet_amount, bombs, field_str)
        
        # Отправляем поле
        await message.answer(
            f"💣 <b>Майнс</b>\n\n"
            f"💰 Ставка: {bet_amount}$\n"
            f"💣 Мин: {bombs}\n"
            f"📈 Текущий множитель: x1.00\n\n"
            f"Выбирайте клетки:",
            reply_markup=get_mines_field_keyboard([]),
            parse_mode="HTML"
        )
        
        await state.update_data(mines_game_id=game_id, mines_bet=bet_amount)
        await state.set_state(BotStates.waiting_for_mines_move)
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

@dp.callback_query(F.data.startswith("mines_cell_"), BotStates.waiting_for_mines_move)
async def mines_cell_click(callback: CallbackQuery, state: FSMContext):
    cell = callback.data.replace("mines_cell_", "")
    data = await state.get_data()
    game_id = data.get('mines_game_id')
    
    async with db.pool.acquire() as conn:
        game = await conn.fetchrow("SELECT * FROM mines_games WHERE game_id = $1", game_id)
    
    if not game or not game['is_active']:
        await callback.answer("❌ Игра уже закончена!", show_alert=True)
        return
    
    field = game['field'].split(',')
    opened = game['opened_cells']
    
    if cell in opened:
        await callback.answer("❌ Клетка уже открыта!", show_alert=True)
        return
    
    cell_index = int(cell)
    if field[cell_index] == "bomb":
        # Проигрыш
        async with db.pool.acquire() as conn:
            await conn.execute("UPDATE mines_games SET is_active = FALSE WHERE game_id = $1", game_id)
        
        await callback.message.edit_text(
            f"💣 <b>Майнс</b>\n\n"
            f"💥 <b>ВЗОРВАЛОСЬ!</b>\n"
            f"💰 Вы проиграли {game['bet_amount']}$",
            reply_markup=get_mines_field_keyboard(opened, game_over=True),
            parse_mode="HTML"
        )
        await state.clear()
    else:
        new_opened = opened + [cell]
        new_multiplier = 1.0 + (len(new_opened) * 0.25)
        
        async with db.pool.acquire() as conn:
            await conn.execute("""
                UPDATE mines_games 
                SET opened_cells = $1, multiplier = $2
                WHERE game_id = $3
            """, new_opened, new_multiplier, game_id)
        
        await callback.message.edit_text(
            f"💣 <b>Майнс</b>\n\n"
            f"💰 Ставка: {game['bet_amount']}$\n"
            f"💣 Мин: {game['bombs_count']}\n"
            f"📈 Текущий множитель: x{new_multiplier:.2f}\n"
            f"💎 Потенциальный выигрыш: {game['bet_amount'] * new_multiplier:.2f}$\n\n"
            f"Выбирайте клетки:",
            reply_markup=get_mines_field_keyboard(new_opened),
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "mines_cashout", BotStates.waiting_for_mines_move)
async def mines_cashout(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    game_id = data.get('mines_game_id')
    
    async with db.pool.acquire() as conn:
        game = await conn.fetchrow("SELECT * FROM mines_games WHERE game_id = $1", game_id)
    
    if not game or not game['is_active']:
        await callback.answer("❌ Игра уже закончена!", show_alert=True)
        return
    
    win_amount = game['bet_amount'] * game['multiplier']
    
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE mines_games SET is_active = FALSE WHERE game_id = $1", game_id)
    
    # Начисляем выигрыш
    await db.update_balance(game['user_id'], win_amount)
    
    await db.add_bet(
        user_id=game['user_id'],
        game_type="mines",
        bet_amount=float(game['bet_amount']),
        outcome=f"{game['bombs_count']} bombs",
        result="win",
        win_amount=win_amount,
        multiplier=float(game['multiplier'])
    )
    
    await callback.message.edit_text(
        f"💣 <b>Майнс</b>\n\n"
        f"✅ <b>ВЫ ЗАБРАЛИ ВЫИГРЫШ!</b>\n"
        f"💰 Множитель: x{game['multiplier']:.2f}\n"
        f"💵 Выигрыш: {win_amount:.2f}$",
        reply_markup=get_mines_field_keyboard(game['opened_cells'], game_over=True),
        parse_mode="HTML"
    )
    await state.clear()

# Обработчик профиля
@dp.message(F.text == "👤 Профиль")
async def profile_menu(message: Message):
    user_id = message.from_user.id
    balance = await db.get_balance(user_id)
    
    # Получаем статистику
    async with db.pool.acquire() as conn:
        games_played = await conn.fetchval("SELECT COUNT(*) FROM bets WHERE user_id = $1", user_id) or 0
        total_won = await conn.fetchval("SELECT COALESCE(SUM(win_amount), 0) FROM bets WHERE user_id = $1 AND win_amount > 0", user_id) or 0
        biggest_win = await conn.fetchval("SELECT COALESCE(MAX(win_amount), 0) FROM bets WHERE user_id = $1 AND win_amount > 0", user_id) or 0
    
    bots_count = await db.get_user_bots_count(user_id)
    
    await message.answer(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📝 Username: @{message.from_user.username or 'None'}\n"
        f"💰 Баланс: <b>{balance:.2f} USDT</b>\n"
        f"🎮 Сыграно игр: {games_played}\n"
        f"🏆 Выиграно: {total_won:.2f} USDT\n"
        f"💎 Лучший выигрыш: {biggest_win:.2f} USDT\n"
        f"🤖 Мои боты: {bots_count}",
        reply_markup=get_profile_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "deposit")
async def deposit_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "💰 <b>Пополнение баланса</b>\n\n"
        "Выберите способ пополнения:",
        reply_markup=get_deposit_currency_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("deposit_"))
async def deposit_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.replace("deposit_", "")
    await state.update_data(deposit_currency=currency)
    
    if currency == "usdt":
        await callback.message.edit_text(
            f"💰 <b>Пополнение USDT</b>\n\n"
            f"Введите сумму в USDT (минимум 1):",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"💰 <b>Пополнение TON</b>\n\n"
            f"1 TON = 1.8 USDT\n"
            f"Введите сумму в TON (минимум 1):",
            parse_mode="HTML"
        )
    await state.set_state(BotStates.waiting_for_deposit_amount)

@dp.message(BotStates.waiting_for_deposit_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 1:
            await message.answer("❌ Минимальная сумма 1")
            return
        
        data = await state.get_data()
        currency = data.get('deposit_currency', 'usdt')
        user_id = message.from_user.id
        
        # Конвертируем TON в USDT для отображения
        display_amount = amount
        if currency == "ton":
            display_amount = amount * TON_TO_USDT_RATE
        
        pay_url = await create_crypto_invoice(amount, currency.upper(), user_id)
        
        if pay_url:
            await message.answer(
                f"💰 <b>Счет на {amount} {currency.upper()} создан!</b>\n"
                f"💵 Это эквивалентно {display_amount:.2f} USDT\n\n"
                f"Для пополнения перейдите по ссылке и оплатите счет:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
                        [InlineKeyboardButton(text="🔙 В профиль", callback_data="back_to_profile")]
                    ]
                ),
                parse_mode="HTML"
            )
            
            # Сохраняем транзакцию
            async with db.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO transactions (user_id, type, currency, amount, amount_usdt, status, external_id)
                    VALUES ($1, 'deposit', $2, $3, $4, 'pending', $5)
                """, user_id, currency.upper(), amount, display_amount, pay_url.split('/')[-1])
        else:
            await message.answer("❌ Ошибка создания счета. Попробуйте позже.")
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

@dp.callback_query(F.data == "withdraw")
async def withdraw_menu(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💸 <b>Вывод средств</b>\n\n"
        "Введите сумму для вывода в USDT (минимум 5 USDT):",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_withdraw_amount)

@dp.message(BotStates.waiting_for_withdraw_amount)
async def process_withdraw(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 5:
            await message.answer("❌ Минимальная сумма вывода 5 USDT")
            return
        
        user_id = message.from_user.id
        
        balance = await db.get_balance(user_id)
        
        if balance < amount:
            await message.answer(f"❌ Недостаточно средств на балансе")
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
                    INSERT INTO transactions (user_id, type, currency, amount, amount_usdt, status, external_id)
                    VALUES ($1, 'withdraw', 'USDT', $2, $2, 'completed', $3)
                """, user_id, amount, transfer_id)
            
            await message.answer(
                f"✅ <b>Заявка на вывод создана!</b>\n"
                f"Сумма: {amount} USDT\n"
                f"ID транзакции: <code>{transfer_id}</code>",
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
    balance = await db.get_balance(user_id)
    bots_count = await db.get_user_bots_count(user_id)
    
    await callback.message.edit_text(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📝 Username: @{callback.from_user.username or 'None'}\n"
        f"💰 Баланс: <b>{balance:.2f} USDT</b>\n"
        f"🤖 Мои боты: {bots_count}",
        reply_markup=get_profile_keyboard(),
        parse_mode="HTML"
    )

# Обработчик истории
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
            "📊 У вас пока нет истории игр.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
            )
        )
        return
    
    text = "📊 <b>Последние игры:</b>\n\n"
    for bet in bets:
        emoji = "🎲" if bet['game_type'] == 'dice' else "💣" if bet['game_type'] == 'mines' else "🏀" if bet['game_type'] == 'basketball' else "⚽"
        result_emoji = "✅" if bet['result'] == 'win' else "❌"
        time_str = bet['created_at'].strftime('%H:%M %d.%m')
        text += f"{emoji} {time_str} | {result_emoji} | {float(bet['bet_amount']):.2f}$ → {float(bet['win_amount']):.2f}$ (x{float(bet['multiplier']):.2f})\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
        ),
        parse_mode="HTML"
    )

# Обработчик статистики
@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    async with db.pool.acquire() as conn:
        stats = await conn.fetch("""
            SELECT game_type, COUNT(*) as games, 
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins,
                   SUM(win_amount) as total_won,
                   SUM(bet_amount) as total_bet
            FROM bets 
            WHERE user_id = $1 
            GROUP BY game_type
        """, user_id)
        
        daily = await conn.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as games, 
                   SUM(bet_amount) as volume
            FROM bets 
            WHERE user_id = $1 
            GROUP BY DATE(created_at)
            ORDER BY date DESC
            LIMIT 7
        """, user_id)
    
    text = "📊 <b>Детальная статистика</b>\n\n"
    
    if stats:
        text += "<b>По играм:</b>\n"
        for stat in stats:
            emoji = "🎲" if stat['game_type'] == 'dice' else "💣" if stat['game_type'] == 'mines' else "🏀" if stat['game_type'] == 'basketball' else "⚽"
            win_rate = (stat['wins'] / stat['games'] * 100) if stat['games'] > 0 else 0
            profit = float(stat['total_won']) - float(stat['total_bet'])
            profit_emoji = "✅" if profit >= 0 else "❌"
            text += f"{emoji} {stat['game_type']}: {stat['games']} игр, {win_rate:.1f}% побед, {profit_emoji} {profit:+.2f}$\n"
    else:
        text += "У вас пока нет игр.\n"
    
    if daily:
        text += "\n<b>Последние 7 дней:</b>\n"
        for day in daily:
            text += f"📅 {day['date'].strftime('%d.%m')}: {day['games']} игр, {float(day['volume']):.2f}$\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
        ),
        parse_mode="HTML"
    )

# Обработчик бонусов
@dp.message(F.text == "🎁 Бонусы")
async def bonus_menu(message: Message):
    await message.answer(
        "🎁 <b>Бонусы и акции</b>\n\n"
        "📝 Активируйте бонус-коды и получайте награды!",
        reply_markup=get_bonus_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "activate_code")
async def activate_code(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📝 <b>Активация бонус-кода</b>\n\n"
        "Введите бонус-код:",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_bonus_code)

@dp.message(BotStates.waiting_for_bonus_code)
async def process_bonus_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    user_id = message.from_user.id
    
    result = await db.use_bonus_code(user_id, code)
    
    if result is None:
        await message.answer("❌ Код не найден!")
    elif result == -1:
        await message.answer("❌ Срок действия кода истек!")
    elif result == -2:
        await message.answer("❌ Лимит использований кода исчерпан!")
    elif result == -3:
        await message.answer("❌ Вы уже использовали этот код!")
    else:
        await message.answer(
            f"✅ <b>Код успешно активирован!</b>\n\n"
            f"💰 +{result:.2f} USDT зачислено на ваш счет!",
            parse_mode="HTML"
        )
    
    await state.clear()

# Реферальная система
@dp.message(F.text == "👥 Рефералы")
async def referral_menu(message: Message):
    user_id = message.from_user.id
    bots_count = await db.get_user_bots_count(user_id)
    bots = await db.get_user_bots(user_id)
    
    # Получаем общий заработок с рефералов
    earnings = await db.get_referral_earnings(user_id)
    
    # Получаем количество рефералов
    async with db.pool.acquire() as conn:
        referrals_count = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE referrer_id = $1", user_id) or 0
    
    text = (
        f"👥 <b>Реферальная система</b>\n\n"
        f"📊 Создано ботов: {bots_count}\n"
        f"👥 Рефералов: {referrals_count}\n"
        f"💰 Заработано: {earnings:.2f} USDT\n"
        f"💎 Отчисления: 10% от проигрышей\n\n"
    )
    
    if bots:
        text += "📋 <b>Ваши боты:</b>\n"
        for bot in bots[:3]:
            bot_earnings = await db.get_referral_earnings(user_id, bot['token'])
            text += f"• @{bot['bot_username']} — {bot_earnings:.2f} USDT\n"
    
    text += f"\n🔗 <b>Ваша реферальная ссылка:</b>\n"
    text += f"https://t.me/{(await bot.me()).username}?start={user_id}"
    
    await message.answer(text, reply_markup=get_referral_keyboard(), parse_mode="HTML")

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
    await state.set_state(BotStates.waiting_for_bot_token)

@dp.message(BotStates.waiting_for_bot_token)
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
            f"• Можно делать рассылки своим игрокам\n"
            f"• Можно изменить приветствие\n\n"
            f"🔗 <b>Ссылка для приглашения игроков:</b>\n"
            f"{bot_link}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nПроверьте токен и попробуйте снова.")
    
    await state.clear()

@dp.callback_query(F.data == "my_bots")
async def my_bots(callback: CallbackQuery):
    user_id = callback.from_user.id
    bots = await db.get_user_bots(user_id)
    
    if not bots:
        await callback.message.edit_text(
            "📋 <b>У вас пока нет созданных ботов</b>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Создать бота", callback_data="create_bot")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_referral")]
                ]
            ),
            parse_mode="HTML"
        )
        return
    
    # Если только один бот, показываем сразу его настройки
    if len(bots) == 1:
        bot = bots[0]
        await show_bot_owner_menu(callback.message, bot)
        return
    
    # Если несколько ботов, показываем список
    text = "📋 <b>Ваши боты:</b>\n\n"
    
    for bot in bots:
        earnings = await db.get_referral_earnings(user_id, bot['token'])
        async with db.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE bot_token = $1", bot['token']) or 0
        
        text += f"🤖 @{bot['bot_username']}\n"
        text += f"   👥 Игроков: {users_count}\n"
        text += f"   💰 Заработано: {earnings:.2f} USDT\n"
        text += f"   ⚙️ <a href='https://t.me/{bot['bot_username']}?start=settings'>Управление</a>\n\n"
    
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

async def show_bot_owner_menu(message: Message, bot_info: Dict):
    """Показывает меню управления ботом для владельца"""
    earnings = await db.get_referral_earnings(bot_info['owner_id'], bot_info['token'])
    async with db.pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE bot_token = $1", bot_info['token']) or 0
    
    text = (
        f"🤖 <b>Управление ботом @{bot_info['bot_username']}</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• 👥 Игроков: {users_count}\n"
        f"• 💰 Заработано: {earnings:.2f} USDT\n"
        f"• 📅 Создан: {bot_info['created_at'].strftime('%d.%m.%Y')}\n\n"
        f"📝 <b>Текущее приветствие:</b>\n"
        f"{bot_info['welcome_message']}\n\n"
        f"Выберите действие:"
    )
    
    await message.edit_text(
        text,
        reply_markup=get_bot_owner_keyboard(bot_info['token']),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("bot_owner_"))
async def bot_owner_menu(callback: CallbackQuery):
    token = callback.data.replace("bot_owner_", "")
    bot_info = await db.get_bot(token)
    
    if bot_info and bot_info['owner_id'] == callback.from_user.id:
        await show_bot_owner_menu(callback.message, bot_info)

@dp.callback_query(F.data.startswith("bot_broadcast_"))
async def bot_broadcast(callback: CallbackQuery, state: FSMContext):
    token = callback.data.replace("bot_broadcast_", "")
    bot_info = await db.get_bot(token)
    
    if not bot_info or bot_info['owner_id'] != callback.from_user.id:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await state.update_data(broadcast_bot_token=token)
    await callback.message.edit_text(
        f"📢 <b>Рассылка в боте @{bot_info['bot_username']}</b>\n\n"
        f"Введите сообщение для рассылки всем игрокам вашего бота:",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_bot_broadcast)

@dp.message(BotStates.waiting_for_bot_broadcast)
async def process_bot_broadcast(message: Message, state: FSMContext):
    data = await state.get_data()
    token = data.get('broadcast_bot_token')
    bot_info = await db.get_bot(token)
    
    if not bot_info or bot_info['owner_id'] != message.from_user.id:
        await message.answer("❌ Ошибка доступа!")
        await state.clear()
        return
    
    broadcast_text = message.text
    
    # Получаем всех пользователей бота
    users = await db.get_bot_users(token)
    
    sent = 0
    failed = 0
    
    status_msg = await message.answer(f"📢 Начинаю рассылку в боте @{bot_info['bot_username']}...\nВсего пользователей: {len(users)}")
    
    for user in users:
        try:
            await bot.send_message(
                user['user_id'], 
                f"📢 <b>Рассылка от @{bot_info['bot_username']}</b>\n\n{broadcast_text}",
                parse_mode="HTML"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n"
        f"📊 Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("bot_welcome_"))
async def bot_welcome(callback: CallbackQuery, state: FSMContext):
    token = callback.data.replace("bot_welcome_", "")
    bot_info = await db.get_bot(token)
    
    if not bot_info or bot_info['owner_id'] != callback.from_user.id:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await state.update_data(welcome_bot_token=token)
    await callback.message.edit_text(
        f"✏️ <b>Изменение приветствия бота @{bot_info['bot_username']}</b>\n\n"
        f"Текущее приветствие:\n{bot_info['welcome_message']}\n\n"
        f"Введите новое приветственное сообщение:",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_welcome_message)

@dp.message(BotStates.waiting_for_welcome_message)
async def process_welcome_message(message: Message, state: FSMContext):
    data = await state.get_data()
    token = data.get('welcome_bot_token')
    bot_info = await db.get_bot(token)
    
    if not bot_info or bot_info['owner_id'] != message.from_user.id:
        await message.answer("❌ Ошибка доступа!")
        await state.clear()
        return
    
    new_welcome = message.text
    
    # Обновляем приветствие
    await db.update_bot_welcome(token, new_welcome)
    
    await message.answer(
        f"✅ <b>Приветствие бота @{bot_info['bot_username']} обновлено!</b>\n\n"
        f"Новое приветствие:\n{new_welcome}",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("bot_settings_"))
async def bot_settings(callback: CallbackQuery):
    token = callback.data.replace("bot_settings_", "")
    bot_info = await db.get_bot(token)
    
    if bot_info and bot_info['owner_id'] == callback.from_user.id:
        await callback.message.edit_text(
            f"⚙️ <b>Настройки бота @{bot_info['bot_username']}</b>\n\n"
            f"Управление уведомлениями о новых игроках:",
            reply_markup=get_bot_settings_keyboard(token, bot_info['notifications_enabled']),
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("toggle_notif_"))
async def toggle_notifications(callback: CallbackQuery):
    token = callback.data.replace("toggle_notif_", "")
    bot_info = await db.get_bot(token)
    
    if bot_info and bot_info['owner_id'] == callback.from_user.id:
        new_value = not bot_info['notifications_enabled']
        await db.update_bot_notifications(token, new_value)
        
        await callback.message.edit_text(
            f"⚙️ <b>Настройки бота @{bot_info['bot_username']}</b>\n\n"
            f"✅ Настройки сохранены!",
            reply_markup=get_bot_settings_keyboard(token, new_value),
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "referral_stats")
async def referral_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    async with db.pool.acquire() as conn:
        # Общая статистика
        total_earnings = await db.get_referral_earnings(user_id)
        referrals_count = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE referrer_id = $1", user_id) or 0
        
        # Статистика по дням
        daily = await conn.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as new_refs, SUM(earnings) as earned
            FROM referrals 
            WHERE referrer_id = $1 
            GROUP BY DATE(created_at)
            ORDER BY date DESC
            LIMIT 7
        """, user_id)
    
    text = f"📊 <b>Реферальная статистика</b>\n\n"
    text += f"👥 Всего рефералов: {referrals_count}\n"
    text += f"💰 Заработано всего: {total_earnings:.2f} USDT\n\n"
    
    if daily:
        text += "<b>Последние 7 дней:</b>\n"
        for day in daily:
            text += f"📅 {day['date'].strftime('%d.%m')}: +{day['new_refs']} реф, +{float(day['earned']):.2f} USDT\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_referral")]]
        ),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "back_to_referral")
async def back_to_referral(callback: CallbackQuery):
    user_id = callback.from_user.id
    bots_count = await db.get_user_bots_count(user_id)
    earnings = await db.get_referral_earnings(user_id)
    
    await callback.message.edit_text(
        f"👥 <b>Реферальная система</b>\n\n"
        f"📊 Создано ботов: {bots_count}\n"
        f"💰 Заработано: {earnings:.2f} USDT\n"
        f"💎 Отчисления: 10% от проигрышей\n\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n"
        f"https://t.me/{(await bot.me()).username}?start={user_id}",
        reply_markup=get_referral_keyboard(),
        parse_mode="HTML"
    )

# Админ-панель
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
        f"💰 Общий оборот: <b>{stats['volume']:.2f} USDT</b>\n"
        f"📈 Прибыль: <b>{stats['profit']:.2f} USDT</b>\n"
        f"📊 Сегодня: {stats['today_bets']} ставок, {stats['today_volume']:.2f} USDT",
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
        text += f"  👥 Игроков: {bot['users_count']}\n"
        text += f"  💰 Заработано: {float(bot['total_earnings']):.2f} USDT\n"
        text += f"  🔔 Уведомления: {'✅' if bot['notifications_enabled'] else '❌'}\n"
        text += f"  📅 Создан: {bot['created_at'].strftime('%d.%m.%Y')}\n\n"
    
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
        "📢 <b>Рассылка по всем ботам</b>\n\n"
        "Введите сообщение для рассылки ВСЕМ пользователям всех ботов:",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_broadcast)

@dp.message(BotStates.waiting_for_broadcast)
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
    
    status_msg = await message.answer(f"📢 Начинаю рассылку... Всего пользователей: {len(users)}")
    
    for user in users:
        try:
            await bot.send_message(
                user['user_id'], 
                f"📢 <b>Рассылка от администрации Tvist Casino</b>\n\n{broadcast_text}",
                parse_mode="HTML"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    
    await status_msg.edit_text(
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
    await state.set_state(BotStates.waiting_for_user_id_balance)

@dp.message(BotStates.waiting_for_user_id_balance)
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
        
        balance = await db.get_balance(user_id)
        
        await state.update_data(target_user_id=user_id)
        await message.answer(
            f"👤 Пользователь: <code>{user_id}</code>\n"
            f"💰 Текущий баланс: <b>{balance:.2f} USDT</b>\n\n"
            f"Введите новую сумму баланса (можно с минусом):",
            parse_mode="HTML"
        )
        await state.set_state(BotStates.waiting_for_new_balance)
        
    except ValueError:
        await message.answer("❌ Введите корректный ID")

@dp.message(BotStates.waiting_for_new_balance)
async def process_new_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    
    try:
        new_balance = float(message.text)
        data = await state.get_data()
        user_id = data['target_user_id']
        
        # Получаем текущий баланс
        current = await db.get_balance(user_id)
        
        # Устанавливаем новый баланс
        diff = new_balance - current
        await db.update_balance(user_id, diff)
        
        # Проверяем новый баланс
        updated = await db.get_balance(user_id)
        
        await message.answer(
            f"✅ <b>Баланс пользователя изменен!</b>\n"
            f"👤 ID: <code>{user_id}</code>\n"
            f"📊 Старый баланс: {current:.2f} USDT\n"
            f"📊 Новый баланс: {updated:.2f} USDT",
            parse_mode="HTML"
        )
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")
    
    await state.clear()

@dp.callback_query(F.data == "admin_create_bonus")
async def admin_create_bonus(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    
    await callback.message.edit_text(
        "🎁 <b>Создание бонус-кода</b>\n\n"
        "Введите данные в формате:\n"
        "<code>КОД СУММА КОЛ-ВО ДНЕЙ МАКС_ИСПОЛЬЗОВАНИЙ</code>\n\n"
        "Например: <code>WELCOME 10 30 100</code>\n"
        "(код WELCOME на 10 USDT, 30 дней, 100 использований)",
        parse_mode="HTML"
    )
    # Здесь можно добавить состояние для создания бонуса

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
                                f"📝 Username: @{username}",
                                parse_mode="HTML"
                            )
                        except:
                            pass
            
            welcome_msg = bot_info['welcome_message'] if bot_info else "👋 Добро пожаловать в игрового бота!\n\n🎰 Играй и выигрывай!"
            
            await message.answer(
                f"{welcome_msg}",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
        
        # Копируем основные обработчики
        @referral_dp.message(F.text == "🎲 Играть")
        async def referral_games(message: Message):
            await message.answer(
                "🎮 Выберите игру:",
                reply_markup=get_games_keyboard()
            )
        
        @referral_dp.message(F.text == "👤 Профиль")
        async def referral_profile(message: Message):
            user_id = message.from_user.id
            balance = await db.get_balance(user_id)
            
            # Получаем статистику
            async with db.pool.acquire() as conn:
                games_played = await conn.fetchval("SELECT COUNT(*) FROM bets WHERE user_id = $1", user_id) or 0
                total_won = await conn.fetchval("SELECT COALESCE(SUM(win_amount), 0) FROM bets WHERE user_id = $1 AND win_amount > 0", user_id) or 0
            
            await message.answer(
                f"👤 <b>Ваш профиль</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"📝 Username: @{message.from_user.username or 'None'}\n"
                f"💰 Баланс: <b>{balance:.2f} USDT</b>\n"
                f"🎮 Сыграно игр: {games_played}\n"
                f"🏆 Выиграно: {total_won:.2f} USDT",
                reply_markup=get_profile_keyboard(),
                parse_mode="HTML"
            )
        
        @referral_dp.message(F.text == "👥 Рефералы")
        async def referral_referral(message: Message):
            await message.answer(
                "👥 В этом боте реферальная система отключена."
            )
        
        @referral_dp.message(F.text == "🎁 Бонусы")
        async def referral_bonus(message: Message):
            await message.answer(
                "🎁 <b>Бонусы и акции</b>\n\n"
                "📝 Активируйте бонус-коды и получайте награды!",
                reply_markup=get_bonus_keyboard(),
                parse_mode="HTML"
            )
        
        # Копируем игровые обработчики
        @referral_dp.callback_query(F.data.startswith("game_"))
        async def referral_game_selection(callback: CallbackQuery, state: FSMContext):
            game_type = callback.data.split("_")[1]
            await state.update_data(game_type=game_type)
            
            if game_type == "dice":
                await callback.message.edit_text(
                    "🎲 <b>Выберите сторону:</b>",
                    reply_markup=get_dice_bet_keyboard(),
                    parse_mode="HTML"
                )
            elif game_type == "quick":
                await callback.message.edit_text(
                    "🎯 <b>Быстрая игра</b>\n\nВыберите сумму ставки:",
                    reply_markup=get_quick_bet_keyboard(),
                    parse_mode="HTML"
                )
            elif game_type == "mines":
                await callback.message.edit_text(
                    "💣 <b>Майнс</b>\n\nВыберите количество мин:",
                    reply_markup=get_mines_bombs_keyboard(),
                    parse_mode="HTML"
                )
            else:
                sport_emoji = "🏀" if game_type == "basketball" else "⚽"
                await callback.message.edit_text(
                    f"{sport_emoji} <b>Выберите исход:</b>",
                    reply_markup=get_sport_bet_keyboard(),
                    parse_mode="HTML"
                )
        
        @referral_dp.callback_query(F.data.startswith("quick_"))
        async def referral_quick_bet(callback: CallbackQuery, state: FSMContext):
            if callback.data == "quick_custom":
                await callback.message.edit_text(
                    "💰 Введите сумму ставки (минимум 0.01$):"
                )
                await state.set_state(BotStates.waiting_for_bet_amount)
                await state.update_data(game_type="quick", bet_type="quick", bet_choice="quick")
                return
            
            try:
                bet_amount = float(callback.data.replace("quick_", ""))
                
                # Списываем ставку сразу
                user_id = callback.from_user.id
                balance = await db.get_balance(user_id)
                
                if balance < bet_amount:
                    await callback.answer("❌ Недостаточно средств!", show_alert=True)
                    return
                
                await db.update_balance(user_id, -bet_amount)
                
                # Генерируем результат
                win = random.choice([True, False])
                multiplier = 2.0
                
                if win:
                    win_amount = bet_amount * multiplier
                    await db.update_balance(user_id, win_amount)
                    result_text = f"✅ <b>ВЫ ВЫИГРАЛИ!</b> +{win_amount:.2f}$"
                else:
                    win_amount = 0
                    result_text = f"❌ <b>ВЫ ПРОИГРАЛИ!</b> -{bet_amount:.2f}$"
                    
                    # Отчисление владельцу бота
                    async with db.pool.acquire() as conn:
                        bot_info = await conn.fetchrow("SELECT owner_id FROM bots WHERE token = $1", bot_token)
                        if bot_info:
                            owner_share = bet_amount * 0.1
                            await db.update_balance(bot_info['owner_id'], owner_share)
                            await db.add_referral_earnings(bot_info['owner_id'], owner_share, bot_token)
                
                # Сохраняем ставку
                await db.add_bet(
                    user_id=user_id,
                    game_type="quick",
                    bet_amount=bet_amount,
                    outcome="quick",
                    result="win" if win else "lose",
                    win_amount=win_amount,
                    multiplier=multiplier
                )
                
                # Отправляем результат
                await callback.message.answer_dice(emoji="🎲")
                await asyncio.sleep(2)
                await callback.message.answer(
                    f"🎯 <b>Быстрая игра</b>\n\n"
                    f"{result_text}\n\n"
                    f"💰 <b>Текущий баланс:</b> {await db.get_balance(user_id):.2f}$",
                    parse_mode="HTML"
                )
                
                await callback.message.delete()
                
            except Exception as e:
                logger.error(f"Error in referral quick bet: {e}")
                await callback.answer("❌ Ошибка при создании ставки", show_alert=True)
        
        @referral_dp.callback_query(F.data.startswith("bet_"))
        async def referral_bet_selection(callback: CallbackQuery, state: FSMContext):
            bet_data = callback.data.split("_")
            bet_type = bet_data[1]
            bet_choice = bet_data[2]
            
            await state.update_data(bet_type=bet_type, bet_choice=bet_choice)
            await callback.message.edit_text("💰 Введите сумму ставки (минимум 0.01$):")
            await state.set_state(BotStates.waiting_for_bet_amount)
        
        @referral_dp.message(BotStates.waiting_for_bet_amount)
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
                
                # Списываем ставку
                await db.update_balance(user_id, -bet_amount)
                
                # Генерируем результат
                if game_type == "dice":
                    result = random.randint(1, 6)
                    if bet_choice == "less":
                        win = result <= 3
                        multiplier = 1.5
                    elif bet_choice == "more":
                        win = result >= 4
                        multiplier = 1.7
                    elif bet_choice == "even":
                        win = result % 2 == 0
                        multiplier = 1.6
                    else:  # odd
                        win = result % 2 == 1
                        multiplier = 1.6
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
                    result_text = f"❌ <b>ВЫ ПРОИГРАЛИ!</b> -{bet_amount:.2f}$"
                    
                    # Отчисление владельцу бота
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
                    win_amount=win_amount,
                    multiplier=multiplier
                )
                
                if game_type == "dice":
                    await message.answer_dice(emoji="🎲")
                    await asyncio.sleep(2)
                    result_display = result
                    if bet_choice in ["even", "odd"]:
                        result_display = "четное" if result % 2 == 0 else "нечетное"
                    await message.answer(
                        f"🎲 <b>Выпало: {result} ({result_display})</b>\n\n"
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
            except Exception as e:
                logger.error(f"Error in referral process_bet: {e}")
                await message.answer("❌ Произошла ошибка")
                await state.clear()
        
        # Копируем обработчики Mines
        @referral_dp.callback_query(F.data.startswith("mines_bombs_"))
        async def referral_mines_bombs(callback: CallbackQuery, state: FSMContext):
            bombs = int(callback.data.replace("mines_bombs_", ""))
            await state.update_data(mines_bombs=bombs)
            
            await callback.message.edit_text(
                f"💣 <b>Майнс</b> (💣 {bombs} мин)\n\n"
                f"Введите сумму ставки:",
                parse_mode="HTML"
            )
            await state.set_state(BotStates.waiting_for_mines_bet)
        
        @referral_dp.message(BotStates.waiting_for_mines_bet)
        async def referral_mines_bet(message: Message, state: FSMContext):
            try:
                bet_amount = float(message.text)
                if bet_amount < 0.01:
                    await message.answer("❌ Минимальная ставка 0.01$")
                    return
                
                data = await state.get_data()
                bombs = data.get('mines_bombs', 3)
                user_id = message.from_user.id
                
                balance = await db.get_balance(user_id)
                
                if balance < bet_amount:
                    await message.answer("❌ Недостаточно средств на балансе")
                    await state.clear()
                    return
                
                # Списываем ставку
                await db.update_balance(user_id, -bet_amount)
                
                # Генерируем уникальный ID игры
                game_id = hashlib.md5(f"{user_id}_{datetime.now()}_{random.randint(1,999999)}".encode()).hexdigest()[:8]
                
                # Создаем поле
                field = []
                for i in range(25):
                    field.append("bomb" if i < bombs else "gem")
                random.shuffle(field)
                field_str = ",".join(field)
                
                # Сохраняем игру
                async with db.pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO mines_games (user_id, game_id, bet_amount, bombs_count, field, opened_cells, multiplier, is_active)
                        VALUES ($1, $2, $3, $4, $5, ARRAY[]::TEXT[], 1.0, TRUE)
                    """, user_id, game_id, bet_amount, bombs, field_str)
                
                # Отправляем поле
                await message.answer(
                    f"💣 <b>Майнс</b>\n\n"
                    f"💰 Ставка: {bet_amount}$\n"
                    f"💣 Мин: {bombs}\n"
                    f"📈 Текущий множитель: x1.00\n\n"
                    f"Выбирайте клетки:",
                    reply_markup=get_mines_field_keyboard([]),
                    parse_mode="HTML"
                )
                
                await state.update_data(mines_game_id=game_id, mines_bet=bet_amount)
                await state.set_state(BotStates.waiting_for_mines_move)
                
            except ValueError:
                await message.answer("❌ Пожалуйста, введите число")
        
        @referral_dp.callback_query(F.data.startswith("mines_cell_"), BotStates.waiting_for_mines_move)
        async def referral_mines_cell(callback: CallbackQuery, state: FSMContext):
            cell = callback.data.replace("mines_cell_", "")
            data = await state.get_data()
            game_id = data.get('mines_game_id')
            
            async with db.pool.acquire() as conn:
                game = await conn.fetchrow("SELECT * FROM mines_games WHERE game_id = $1", game_id)
            
            if not game or not game['is_active']:
                await callback.answer("❌ Игра уже закончена!", show_alert=True)
                return
            
            field = game['field'].split(',')
            opened = game['opened_cells']
            
            if cell in opened:
                await callback.answer("❌ Клетка уже открыта!", show_alert=True)
                return
            
            cell_index = int(cell)
            if field[cell_index] == "bomb":
                # Проигрыш
                async with db.pool.acquire() as conn:
                    await conn.execute("UPDATE mines_games SET is_active = FALSE WHERE game_id = $1", game_id)
                
                await callback.message.edit_text(
                    f"💣 <b>Майнс</b>\n\n"
                    f"💥 <b>ВЗОРВАЛОСЬ!</b>\n"
                    f"💰 Вы проиграли {game['bet_amount']}$",
                    reply_markup=get_mines_field_keyboard(opened, game_over=True),
                    parse_mode="HTML"
                )
                await state.clear()
            else:
                new_opened = opened + [cell]
                new_multiplier = 1.0 + (len(new_opened) * 0.25)
                
                async with db.pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE mines_games 
                        SET opened_cells = $1, multiplier = $2
                        WHERE game_id = $3
                    """, new_opened, new_multiplier, game_id)
                
                await callback.message.edit_text(
                    f"💣 <b>Майнс</b>\n\n"
                    f"💰 Ставка: {game['bet_amount']}$\n"
                    f"💣 Мин: {game['bombs_count']}\n"
                    f"📈 Текущий множитель: x{new_multiplier:.2f}\n"
                    f"💎 Потенциальный выигрыш: {game['bet_amount'] * new_multiplier:.2f}$\n\n"
                    f"Выбирайте клетки:",
                    reply_markup=get_mines_field_keyboard(new_opened),
                    parse_mode="HTML"
                )
        
        @referral_dp.callback_query(F.data == "mines_cashout", BotStates.waiting_for_mines_move)
        async def referral_mines_cashout(callback: CallbackQuery, state: FSMContext):
            data = await state.get_data()
            game_id = data.get('mines_game_id')
            
            async with db.pool.acquire() as conn:
                game = await conn.fetchrow("SELECT * FROM mines_games WHERE game_id = $1", game_id)
            
            if not game or not game['is_active']:
                await callback.answer("❌ Игра уже закончена!", show_alert=True)
                return
            
            win_amount = game['bet_amount'] * game['multiplier']
            
            async with db.pool.acquire() as conn:
                await conn.execute("UPDATE mines_games SET is_active = FALSE WHERE game_id = $1", game_id)
            
            # Начисляем выигрыш
            await db.update_balance(game['user_id'], win_amount)
            
            await db.add_bet(
                user_id=game['user_id'],
                game_type="mines",
                bet_amount=float(game['bet_amount']),
                outcome=f"{game['bombs_count']} bombs",
                result="win",
                win_amount=win_amount,
                multiplier=float(game['multiplier'])
            )
            
            await callback.message.edit_text(
                f"💣 <b>Майнс</b>\n\n"
                f"✅ <b>ВЫ ЗАБРАЛИ ВЫИГРЫШ!</b>\n"
                f"💰 Множитель: x{game['multiplier']:.2f}\n"
                f"💵 Выигрыш: {win_amount:.2f}$",
                reply_markup=get_mines_field_keyboard(game['opened_cells'], game_over=True),
                parse_mode="HTML"
            )
            await state.clear()
        
        # Копируем обработчики пополнения и вывода
        @referral_dp.callback_query(F.data == "deposit")
        async def referral_deposit(callback: CallbackQuery, state: FSMContext):
            await callback.message.edit_text(
                "💰 <b>Пополнение баланса</b>\n\n"
                "Выберите способ пополнения:",
                reply_markup=get_deposit_currency_keyboard(),
                parse_mode="HTML"
            )
        
        @referral_dp.callback_query(F.data.startswith("deposit_"))
        async def referral_deposit_currency(callback: CallbackQuery, state: FSMContext):
            currency = callback.data.replace("deposit_", "")
            await state.update_data(deposit_currency=currency)
            
            if currency == "usdt":
                await callback.message.edit_text(
                    f"💰 <b>Пополнение USDT</b>\n\n"
                    f"Введите сумму в USDT (минимум 1):",
                    parse_mode="HTML"
                )
            else:
                await callback.message.edit_text(
                    f"💰 <b>Пополнение TON</b>\n\n"
                    f"1 TON = 1.8 USDT\n"
                    f"Введите сумму в TON (минимум 1):",
                    parse_mode="HTML"
                )
            await state.set_state(BotStates.waiting_for_deposit_amount)
        
        @referral_dp.message(BotStates.waiting_for_deposit_amount)
        async def referral_process_deposit(message: Message, state: FSMContext):
            try:
                amount = float(message.text)
                if amount < 1:
                    await message.answer("❌ Минимальная сумма 1")
                    return
                
                data = await state.get_data()
                currency = data.get('deposit_currency', 'usdt')
                user_id = message.from_user.id
                
                display_amount = amount
                if currency == "ton":
                    display_amount = amount * TON_TO_USDT_RATE
                
                pay_url = await create_crypto_invoice(amount, currency.upper(), user_id)
                
                if pay_url:
                    await message.answer(
                        f"💰 <b>Счет на {amount} {currency.upper()} создан!</b>\n"
                        f"💵 Это эквивалентно {display_amount:.2f} USDT\n\n"
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
                "💸 <b>Вывод средств</b>\n\n"
                "Введите сумму для вывода в USDT (минимум 5 USDT):",
                parse_mode="HTML"
            )
            await state.set_state(BotStates.waiting_for_withdraw_amount)
        
        @referral_dp.message(BotStates.waiting_for_withdraw_amount)
        async def referral_process_withdraw(message: Message, state: FSMContext):
            try:
                amount = float(message.text)
                if amount < 5:
                    await message.answer("❌ Минимальная сумма вывода 5 USDT")
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
                        f"Сумма: {amount} USDT\n"
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
            balance = await db.get_balance(user_id)
            
            await callback.message.edit_text(
                f"👤 <b>Ваш профиль</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"📝 Username: @{callback.from_user.username or 'None'}\n"
                f"💰 Баланс: <b>{balance:.2f} USDT</b>",
                reply_markup=get_profile_keyboard(),
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
                    "📊 У вас пока нет истории игр.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
                    )
                )
                return
            
            text = "📊 <b>Последние игры:</b>\n\n"
            for bet in bets:
                emoji = "🎲" if bet['game_type'] == 'dice' else "💣" if bet['game_type'] == 'mines' else "🏀" if bet['game_type'] == 'basketball' else "⚽"
                result_emoji = "✅" if bet['result'] == 'win' else "❌"
                time_str = bet['created_at'].strftime('%H:%M %d.%m')
                text += f"{emoji} {time_str} | {result_emoji} | {float(bet['bet_amount']):.2f}$ → {float(bet['win_amount']):.2f}$ (x{float(bet['multiplier']):.2f})\n"
            
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
                ),
                parse_mode="HTML"
            )
        
        @referral_dp.callback_query(F.data == "stats")
        async def referral_stats(callback: CallbackQuery):
            user_id = callback.from_user.id
            
            async with db.pool.acquire() as conn:
                stats = await conn.fetch("""
                    SELECT game_type, COUNT(*) as games, 
                           SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins,
                           SUM(win_amount) as total_won,
                           SUM(bet_amount) as total_bet
                    FROM bets 
                    WHERE user_id = $1 
                    GROUP BY game_type
                """, user_id)
            
            text = "📊 <b>Детальная статистика</b>\n\n"
            
            if stats:
                for stat in stats:
                    emoji = "🎲" if stat['game_type'] == 'dice' else "💣" if stat['game_type'] == 'mines' else "🏀" if stat['game_type'] == 'basketball' else "⚽"
                    win_rate = (stat['wins'] / stat['games'] * 100) if stat['games'] > 0 else 0
                    profit = float(stat['total_won']) - float(stat['total_bet'])
                    profit_emoji = "✅" if profit >= 0 else "❌"
                    text += f"{emoji} {stat['game_type']}: {stat['games']} игр, {win_rate:.1f}% побед, {profit_emoji} {profit:+.2f}$\n"
            else:
                text += "У вас пока нет игр.\n"
            
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
                ),
                parse_mode="HTML"
            )
        
        @referral_dp.callback_query(F.data == "activate_code")
        async def referral_activate_code(callback: CallbackQuery, state: FSMContext):
            await callback.message.edit_text(
                "📝 <b>Активация бонус-кода</b>\n\n"
                "Введите бонус-код:",
                parse_mode="HTML"
            )
            await state.set_state(BotStates.waiting_for_bonus_code)
        
        @referral_dp.message(BotStates.waiting_for_bonus_code)
        async def referral_process_code(message: Message, state: FSMContext):
            code = message.text.strip().upper()
            user_id = message.from_user.id
            
            result = await db.use_bonus_code(user_id, code)
            
            if result is None:
                await message.answer("❌ Код не найден!")
            elif result == -1:
                await message.answer("❌ Срок действия кода истек!")
            elif result == -2:
                await message.answer("❌ Лимит использований кода исчерпан!")
            elif result == -3:
                await message.answer("❌ Вы уже использовали этот код!")
            else:
                await message.answer(
                    f"✅ <b>Код успешно активирован!</b>\n\n"
                    f"💰 +{result:.2f} USDT зачислено на ваш счет!",
                    parse_mode="HTML"
                )
            
            await state.clear()
        
        # Запускаем реферального бота
        logger.info(f"✅ Запуск реферального бота {bot_token[:10]}...")
        await referral_dp.start_polling(referral_bot)
        
    except Exception as e:
        logger.error(f"❌ Ошибка реферального бота: {e}")

# Запуск основного бота
async def main():
    # Подключаемся к БД
    await db.connect()
    logger.info("✅ Подключение к БД установлено")
    
    # Запускаем реферальных ботов
    async with db.pool.acquire() as conn:
        bots = await conn.fetch("SELECT token FROM bots")
        for bot_record in bots:
            asyncio.create_task(run_referral_bot(bot_record['token']))
            logger.info(f"🔄 Запуск реферального бота {bot_record['token'][:10]}...")
    
    # Запускаем основного бота
    logger.info("🚀 Запуск основного бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
