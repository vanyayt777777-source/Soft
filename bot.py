import os
import asyncio
import logging
import random
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Union

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

# Crypto Bot API (поддерживает USDT и TON)
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
class BotStates(StatesGroup):
    waiting_for_bet_amount = State()
    waiting_for_deposit_amount = State()
    waiting_for_deposit_currency = State()
    waiting_for_withdraw_amount = State()
    waiting_for_withdraw_currency = State()
    waiting_for_bot_token = State()
    waiting_for_broadcast = State()
    waiting_for_user_id_balance = State()
    waiting_for_new_balance = State()
    waiting_for_bonus_code = State()
    waiting_for_mines_bombs = State()
    waiting_for_mines_bet = State()
    waiting_for_mines_move = State()
    waiting_for_crash_bet = State()
    waiting_for_crash_auto = State()

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
                    balance_usdt DECIMAL(10,2) DEFAULT 0,
                    balance_ton DECIMAL(10,2) DEFAULT 0,
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
                    currency TEXT DEFAULT 'USDT',
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
                    earnings_usdt DECIMAL(10,2) DEFAULT 0,
                    earnings_ton DECIMAL(10,2) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица бонусных кодов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bonus_codes (
                    code TEXT PRIMARY KEY,
                    amount_usdt DECIMAL(10,2),
                    amount_ton DECIMAL(10,2),
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
                    amount_usdt DECIMAL(10,2),
                    amount_ton DECIMAL(10,2),
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
                    currency TEXT DEFAULT 'USDT',
                    bombs_count INT,
                    field TEXT,
                    opened_cells TEXT[] DEFAULT '{}',
                    multiplier DECIMAL(10,2) DEFAULT 1.0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Таблица игр Crash
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS crash_games (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    game_id TEXT UNIQUE,
                    bet_amount DECIMAL(10,2),
                    currency TEXT DEFAULT 'USDT',
                    auto_stop DECIMAL(10,2),
                    crash_point DECIMAL(10,2),
                    cashed_out BOOLEAN DEFAULT FALSE,
                    multiplier DECIMAL(10,2) DEFAULT 1.0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
    
    async def update_schema(self):
        """Обновление схемы БД"""
        async with self.pool.acquire() as conn:
            # Добавляем новые поля, если их нет
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_usdt DECIMAL(10,2) DEFAULT 0")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_ton DECIMAL(10,2) DEFAULT 0")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_token TEXT UNIQUE")
            
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
                INSERT INTO users (user_id, username, referrer_id, bot_token, balance_usdt, balance_ton, 
                                  notifications_enabled, total_bets, total_wins, auth_token)
                VALUES ($1, $2, $3, $4, 0, 0, TRUE, 0, 0, $5)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, username, referrer_id, bot_token, auth_token)
    
    async def update_balance(self, user_id: int, amount_usdt: float = 0, amount_ton: float = 0):
        async with self.pool.acquire() as conn:
            if amount_usdt != 0:
                await conn.execute("UPDATE users SET balance_usdt = balance_usdt + $1 WHERE user_id = $2", 
                                  amount_usdt, user_id)
            if amount_ton != 0:
                await conn.execute("UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id = $2", 
                                  amount_ton, user_id)
            return True
    
    async def get_balance(self, user_id: int) -> Dict[str, float]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT balance_usdt, balance_ton FROM users WHERE user_id = $1", user_id)
            if row:
                return {"usdt": float(row['balance_usdt']), "ton": float(row['balance_ton'])}
            return {"usdt": 0.0, "ton": 0.0}
    
    async def add_bet(self, user_id: int, game_type: str, currency: str, bet_amount: float, 
                      outcome: str, result: str, win_amount: float, multiplier: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bets (user_id, game_type, currency, bet_amount, outcome, result, win_amount, multiplier, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            """, user_id, game_type, currency, bet_amount, outcome, result, win_amount, multiplier)
            
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
    
    async def add_bot(self, token: str, owner_id: int, bot_username: str, bot_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bots (token, owner_id, bot_username, bot_name, notifications_enabled)
                VALUES ($1, $2, $3, $4, TRUE)
            """, token, owner_id, bot_username, bot_name)
    
    async def update_bot_notifications(self, token: str, enabled: bool):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE bots SET notifications_enabled = $1 WHERE token = $2", enabled, token)
    
    async def add_referral(self, referrer_id: int, referral_id: int, bot_token: str):
        async with self.pool.acquire() as conn:
            # Проверяем, существует ли уже такая запись
            exists = await conn.fetchval("""
                SELECT COUNT(*) FROM referrals 
                WHERE referrer_id = $1 AND referral_id = $2 AND bot_token = $3
            """, referrer_id, referral_id, bot_token)
            
            if not exists:
                await conn.execute("""
                    INSERT INTO referrals (referrer_id, referral_id, bot_token, earnings_usdt, earnings_ton)
                    VALUES ($1, $2, $3, 0, 0)
                """, referrer_id, referral_id, bot_token)
                return True
            return False
    
    async def add_referral_earnings(self, referrer_id: int, amount_usdt: float, amount_ton: float, bot_token: str):
        async with self.pool.acquire() as conn:
            if amount_usdt != 0:
                await conn.execute("""
                    UPDATE referrals SET earnings_usdt = earnings_usdt + $1 
                    WHERE referrer_id = $2 AND bot_token = $3
                """, amount_usdt, referrer_id, bot_token)
            if amount_ton != 0:
                await conn.execute("""
                    UPDATE referrals SET earnings_ton = earnings_ton + $1 
                    WHERE referrer_id = $2 AND bot_token = $3
                """, amount_ton, referrer_id, bot_token)
    
    async def get_referral_earnings(self, referrer_id: int, bot_token: str = None) -> Dict[str, float]:
        async with self.pool.acquire() as conn:
            if bot_token:
                row = await conn.fetchrow("""
                    SELECT COALESCE(earnings_usdt, 0) as usdt, COALESCE(earnings_ton, 0) as ton 
                    FROM referrals 
                    WHERE referrer_id = $1 AND bot_token = $2
                """, referrer_id, bot_token)
            else:
                row = await conn.fetchrow("""
                    SELECT COALESCE(SUM(earnings_usdt), 0) as usdt, COALESCE(SUM(earnings_ton), 0) as ton 
                    FROM referrals 
                    WHERE referrer_id = $1
                """, referrer_id)
            
            if row:
                return {"usdt": float(row['usdt']), "ton": float(row['ton'])}
            return {"usdt": 0.0, "ton": 0.0}
    
    async def is_user_exists(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id = $1", user_id) > 0
    
    async def get_all_bots(self) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.*, u.username as owner_username, 
                       (SELECT COUNT(*) FROM users WHERE bot_token = b.token) as users_count,
                       (SELECT COALESCE(SUM(earnings_usdt + earnings_ton), 0) FROM referrals WHERE bot_token = b.token) as total_earnings
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
            total_volume_usdt = await conn.fetchval("SELECT COALESCE(SUM(CASE WHEN currency='USDT' THEN bet_amount ELSE 0 END), 0) FROM bets") or 0
            total_volume_ton = await conn.fetchval("SELECT COALESCE(SUM(CASE WHEN currency='TON' THEN bet_amount ELSE 0 END), 0) FROM bets") or 0
            total_profit_usdt = await conn.fetchval("""
                SELECT COALESCE(SUM(CASE WHEN currency='USDT' THEN (bet_amount - win_amount) ELSE 0 END), 0) 
                FROM bets WHERE win_amount > 0
            """) or 0
            total_profit_ton = await conn.fetchval("""
                SELECT COALESCE(SUM(CASE WHEN currency='TON' THEN (bet_amount - win_amount) ELSE 0 END), 0) 
                FROM bets WHERE win_amount > 0
            """) or 0
            
            today = datetime.now().date()
            today_bets = await conn.fetchval("SELECT COUNT(*) FROM bets WHERE DATE(created_at) = $1", today) or 0
            
            return {
                "users": users_count,
                "bots": bots_count,
                "bets": total_bets,
                "volume_usdt": float(total_volume_usdt),
                "volume_ton": float(total_volume_ton),
                "profit_usdt": float(total_profit_usdt),
                "profit_ton": float(total_profit_ton),
                "today_bets": today_bets
            }
    
    async def create_bonus_code(self, code: str, amount_usdt: float, amount_ton: float, max_uses: int, expires_days: int, created_by: int):
        async with self.pool.acquire() as conn:
            expires_at = datetime.now() + timedelta(days=expires_days)
            await conn.execute("""
                INSERT INTO bonus_codes (code, amount_usdt, amount_ton, max_uses, used_count, expires_at, created_by)
                VALUES ($1, $2, $3, $4, 0, $5, $6)
            """, code.upper(), amount_usdt, amount_ton, max_uses, expires_at, created_by)
    
    async def use_bonus_code(self, user_id: int, code: str) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            # Проверяем код
            bonus = await conn.fetchrow("SELECT * FROM bonus_codes WHERE code = $1", code.upper())
            if not bonus:
                return None
            
            # Проверяем срок действия
            if bonus['expires_at'] < datetime.now():
                return {"error": "expired"}
            
            # Проверяем лимит использований
            if bonus['used_count'] >= bonus['max_uses']:
                return {"error": "limit"}
            
            # Проверяем, не использовал ли пользователь этот код
            used = await conn.fetchval("SELECT COUNT(*) FROM used_bonuses WHERE user_id = $1 AND code = $2", 
                                      user_id, code.upper())
            if used > 0:
                return {"error": "already_used"}
            
            # Начисляем бонус
            if bonus['amount_usdt'] > 0:
                await conn.execute("UPDATE users SET balance_usdt = balance_usdt + $1 WHERE user_id = $2", 
                                 bonus['amount_usdt'], user_id)
            if bonus['amount_ton'] > 0:
                await conn.execute("UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id = $2", 
                                 bonus['amount_ton'], user_id)
            
            # Обновляем счетчик использований
            await conn.execute("UPDATE bonus_codes SET used_count = used_count + 1 WHERE code = $1", code.upper())
            
            # Записываем использование
            await conn.execute("""
                INSERT INTO used_bonuses (user_id, code, amount_usdt, amount_ton) 
                VALUES ($1, $2, $3, $4)
            """, user_id, code.upper(), bonus['amount_usdt'], bonus['amount_ton'])
            
            return {
                "usdt": float(bonus['amount_usdt']),
                "ton": float(bonus['amount_ton'])
            }
    
    async def get_top_players(self, limit: int = 10) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, username, total_bets, total_wins, 
                       CASE WHEN total_bets > 0 THEN (total_wins::float / total_bets * 100) ELSE 0 END as win_rate,
                       (balance_usdt + balance_ton) as total_balance
                FROM users 
                WHERE total_bets > 0
                ORDER BY total_wins DESC, win_rate DESC
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]
    
    # Методы для игры Crash
    async def create_crash_game(self, user_id: int, game_id: str, bet_amount: float, currency: str, auto_stop: float):
        async with self.pool.acquire() as conn:
            crash_point = random.uniform(1.01, 10.0)
            await conn.execute("""
                INSERT INTO crash_games (user_id, game_id, bet_amount, currency, auto_stop, crash_point, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
            """, user_id, game_id, bet_amount, currency, auto_stop, crash_point)
            return crash_point
    
    async def get_crash_game(self, game_id: str) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM crash_games WHERE game_id = $1", game_id)
            return dict(row) if row else None
    
    async def cashout_crash_game(self, game_id: str, multiplier: float):
        async with self.pool.acquire() as conn:
            game = await self.get_crash_game(game_id)
            if game and game['is_active'] and not game['cashed_out']:
                win_amount = game['bet_amount'] * multiplier
                
                if game['currency'] == 'USDT':
                    await self.update_balance(game['user_id'], amount_usdt=win_amount)
                else:
                    await self.update_balance(game['user_id'], amount_ton=win_amount)
                
                await conn.execute("""
                    UPDATE crash_games SET cashed_out = TRUE, multiplier = $1, is_active = FALSE 
                    WHERE game_id = $2
                """, multiplier, game_id)
                
                await self.add_bet(
                    user_id=game['user_id'],
                    game_type="crash",
                    currency=game['currency'],
                    bet_amount=float(game['bet_amount']),
                    outcome=f"auto_{multiplier}" if game['auto_stop'] > 0 else f"manual_{multiplier}",
                    result="win",
                    win_amount=win_amount,
                    multiplier=multiplier
                )
                return True
            return False
    
    async def end_crash_game(self, game_id: str):
        async with self.pool.acquire() as conn:
            game = await self.get_crash_game(game_id)
            if game and game['is_active'] and not game['cashed_out']:
                await conn.execute("UPDATE crash_games SET is_active = FALSE WHERE game_id = $1", game_id)
                
                await self.add_bet(
                    user_id=game['user_id'],
                    game_type="crash",
                    currency=game['currency'],
                    bet_amount=float(game['bet_amount']),
                    outcome=f"crashed_at_{game['crash_point']:.2f}",
                    result="lose",
                    win_amount=0,
                    multiplier=0
                )
                return True
            return False

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
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💎 Краш")],
            [KeyboardButton(text="👥 Реферальная система"), KeyboardButton(text="🏆 Топ")]
        ],
        resize_keyboard=True
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

def get_crash_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Играть в Crash", callback_data="crash_play")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="crash_stats")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
    )

def get_crash_bet_keyboard() -> InlineKeyboardMarkup:
    amounts = [1, 5, 10, 25, 50]
    keyboard = []
    row = []
    for i, amount in enumerate(amounts):
        row.append(InlineKeyboardButton(text=f"{amount}$", callback_data=f"crash_bet_{amount}"))
        if (i + 1) % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton(text="💰 Своя сумма", callback_data="crash_bet_custom"),
        InlineKeyboardButton(text="🤖 Автостоп", callback_data="crash_auto")
    ])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="crash_menu")])
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
    
    if not game_over:
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
            [InlineKeyboardButton(text="💵 USDT", callback_data="deposit_usdt")],
            [InlineKeyboardButton(text="💎 TON", callback_data="deposit_ton")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
        ]
    )

def get_withdraw_currency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 USDT", callback_data="withdraw_usdt")],
            [InlineKeyboardButton(text="💎 TON", callback_data="withdraw_ton")],
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
            [InlineKeyboardButton(text="➕ Создать нового бота", callback_data="create_bot")],
            [InlineKeyboardButton(text="📋 Мои боты", callback_data="my_bots")],
            [InlineKeyboardButton(text="📊 Статистика рефералов", callback_data="referral_stats")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
    )

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🤖 Список ботов", callback_data="admin_bots_list")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="✏️ Изменить баланс", callback_data="admin_change_balance")],
            [InlineKeyboardButton(text="🎁 Создать бонус-код", callback_data="admin_create_bonus")]
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

async def create_crypto_withdrawal(amount: float, currency: str, user_id: int) -> Optional[str]:
    """Создание выплаты через Crypto Bot"""
    url = f"{CRYPTO_BOT_API_URL}/createTransfer"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    
    asset = "USDT" if currency == "USDT" else "TON"
    
    payload = {
        "user_id": user_id,
        "asset": asset,
        "amount": str(amount),
        "spend_id": f"withdraw_{currency}_{user_id}_{datetime.now().timestamp()}"
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                return data["result"]["transfer_id"]
    except Exception as e:
        logger.error(f"Error creating {currency} withdrawal: {e}")
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

@dp.message(F.text == "💎 Краш")
async def crash_menu(message: Message):
    user_id = message.from_user.id
    balance = await db.get_balance(user_id)
    
    # Получаем статистику игр Crash
    async with db.pool.acquire() as conn:
        crash_games = await conn.fetchval("""
            SELECT COUNT(*) FROM bets 
            WHERE user_id = $1 AND game_type = 'crash'
        """, user_id) or 0
        
        crash_wins = await conn.fetchval("""
            SELECT COUNT(*) FROM bets 
            WHERE user_id = $1 AND game_type = 'crash' AND result = 'win'
        """, user_id) or 0
        
        best_multiplier = await conn.fetchval("""
            SELECT COALESCE(MAX(multiplier), 0) FROM bets 
            WHERE user_id = $1 AND game_type = 'crash' AND result = 'win'
        """, user_id) or 0
    
    win_rate = (crash_wins / crash_games * 100) if crash_games > 0 else 0
    
    await message.answer(
        f"💎 <b>Игра Crash</b>\n\n"
        f"💰 Ваш баланс: {balance['usdt']:.2f} USDT | {balance['ton']:.2f} TON\n\n"
        f"📊 <b>Ваша статистика:</b>\n"
        f"• Игр сыграно: {crash_games}\n"
        f"• Побед: {crash_wins}\n"
        f"• Процент побед: {win_rate:.1f}%\n"
        f"• Лучший множитель: x{float(best_multiplier):.2f}",
        reply_markup=get_crash_menu_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "crash_stats")
async def crash_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    async with db.pool.acquire() as conn:
        stats = await conn.fetch("""
            SELECT currency, COUNT(*) as games, 
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins,
                   COALESCE(AVG(multiplier), 0) as avg_multiplier
            FROM bets 
            WHERE user_id = $1 AND game_type = 'crash'
            GROUP BY currency
        """, user_id)
    
    text = "📊 <b>Статистика Crash:</b>\n\n"
    
    if stats:
        for stat in stats:
            win_rate = (stat['wins'] / stat['games'] * 100) if stat['games'] > 0 else 0
            text += f"💵 {stat['currency']}:\n"
            text += f"• Игр: {stat['games']}\n"
            text += f"• Побед: {stat['wins']} ({win_rate:.1f}%)\n"
            text += f"• Средний множитель: x{float(stat['avg_multiplier']):.2f}\n\n"
    else:
        text += "У вас пока нет игр в Crash"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="crash_menu")]]
        ),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "crash_play")
async def crash_play(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💎 <b>Игра Crash</b>\n\n"
        "Выберите валюту:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💵 USDT", callback_data="crash_currency_USDT")],
                [InlineKeyboardButton(text="💎 TON", callback_data="crash_currency_TON")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="crash_menu")]
            ]
        ),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("crash_currency_"))
async def crash_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.replace("crash_currency_", "")
    await state.update_data(crash_currency=currency)
    
    await callback.message.edit_text(
        f"💎 <b>Игра Crash</b> ({currency})\n\n"
        f"Введите сумму ставки:",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_crash_bet)

@dp.message(BotStates.waiting_for_crash_bet)
async def process_crash_bet(message: Message, state: FSMContext):
    try:
        bet_amount = float(message.text)
        if bet_amount < 0.01:
            await message.answer("❌ Минимальная ставка 0.01")
            return
        
        data = await state.get_data()
        currency = data.get('crash_currency', 'USDT')
        user_id = message.from_user.id
        
        balance = await db.get_balance(user_id)
        current_balance = balance['usdt'] if currency == 'USDT' else balance['ton']
        
        if current_balance < bet_amount:
            await message.answer(f"❌ Недостаточно {currency} на балансе")
            await state.clear()
            return
        
        await state.update_data(crash_bet=bet_amount)
        
        await message.answer(
            f"💎 <b>Игра Crash</b> ({currency})\n\n"
            f"💰 Ставка: {bet_amount} {currency}\n"
            f"🤖 Введите коэффициент автостопа (например 2.5) или 0 если без автостопа:",
            parse_mode="HTML"
        )
        await state.set_state(BotStates.waiting_for_crash_auto)
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

@dp.message(BotStates.waiting_for_crash_auto)
async def process_crash_auto(message: Message, state: FSMContext):
    try:
        auto_stop = float(message.text)
        if auto_stop < 0:
            await message.answer("❌ Коэффициент должен быть больше 0")
            return
        
        data = await state.get_data()
        bet_amount = data['crash_bet']
        currency = data['crash_currency']
        user_id = message.from_user.id
        
        # Списываем ставку
        if currency == 'USDT':
            await db.update_balance(user_id, amount_usdt=-bet_amount)
        else:
            await db.update_balance(user_id, amount_ton=-bet_amount)
        
        # Генерируем уникальный ID игры
        game_id = hashlib.md5(f"{user_id}_{datetime.now()}_{random.randint(1,999999)}".encode()).hexdigest()[:8]
        
        # Создаем игру
        crash_point = await db.create_crash_game(user_id, game_id, bet_amount, currency, auto_stop)
        
        await message.answer(
            f"💎 <b>Игра Crash началась!</b>\n\n"
            f"💰 Ставка: {bet_amount} {currency}\n"
            f"🎯 Автостоп: x{auto_stop if auto_stop > 0 else 'нет'}\n"
            f"🆔 ID игры: <code>{game_id}</code>\n\n"
            f"📈 Коэффициент растет...",
            parse_mode="HTML"
        )
        
        # Запускаем игру
        asyncio.create_task(run_crash_game(message.chat.id, game_id, crash_point, auto_stop, bet_amount, currency))
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число")

async def run_crash_game(chat_id: int, game_id: str, crash_point: float, auto_stop: float, bet_amount: float, currency: str):
    """Запуск игры Crash с анимацией"""
    multiplier = 1.0
    step = 0.05
    
    # Отправляем сообщение с кнопкой для кэшаута
    msg = await bot.send_message(
        chat_id,
        f"💎 <b>Crash Game #{game_id}</b>\n\n"
        f"📈 Текущий множитель: x{multiplier:.2f}\n"
        f"⏳ Идет игра...",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"💰 Забрать x{multiplier:.2f}", callback_data=f"crash_cashout_{game_id}")]
            ]
        ),
        parse_mode="HTML"
    )
    
    # Обновляем множитель каждые 0.5 секунды
    while multiplier < crash_point:
        await asyncio.sleep(0.5)
        multiplier += step
        
        if multiplier >= crash_point:
            break
        
        # Обновляем сообщение
        try:
            await msg.edit_text(
                f"💎 <b>Crash Game #{game_id}</b>\n\n"
                f"📈 Текущий множитель: x{multiplier:.2f}\n"
                f"⏳ Идет игра...",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=f"💰 Забрать x{multiplier:.2f}", callback_data=f"crash_cashout_{game_id}")]
                    ]
                ),
                parse_mode="HTML"
            )
        except:
            pass
        
        # Проверяем автостоп
        if auto_stop > 0 and multiplier >= auto_stop:
            await db.cashout_crash_game(game_id, multiplier)
            win_amount = bet_amount * multiplier
            await msg.edit_text(
                f"💎 <b>Crash Game #{game_id}</b>\n\n"
                f"✅ <b>ВЫ ЗАБРАЛИ (автостоп)!</b>\n"
                f"💰 Множитель: x{multiplier:.2f}\n"
                f"💵 Выигрыш: {win_amount:.2f} {currency}",
                parse_mode="HTML"
            )
            return
    
    # Краш
    game = await db.get_crash_game(game_id)
    if game and game['is_active'] and not game['cashed_out']:
        await db.end_crash_game(game_id)
        await msg.edit_text(
            f"💎 <b>Crash Game #{game_id}</b>\n\n"
            f"💥 <b>КРАШ!</b>\n"
            f"📈 Множитель: x{crash_point:.2f}\n"
            f"❌ Вы проиграли {bet_amount} {currency}!",
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("crash_cashout_"))
async def crash_cashout(callback: CallbackQuery):
    game_id = callback.data.replace("crash_cashout_", "")
    
    # Получаем игру
    game = await db.get_crash_game(game_id)
    if not game or not game['is_active'] or game['cashed_out']:
        await callback.answer("❌ Игра уже закончена!", show_alert=True)
        return
    
    # Забираем выигрыш
    success = await db.cashout_crash_game(game_id, float(game['multiplier']))
    if success:
        win_amount = float(game['bet_amount']) * float(game['multiplier'])
        await callback.message.edit_text(
            f"💎 <b>Crash Game #{game_id}</b>\n\n"
            f"✅ <b>ВЫ ЗАБРАЛИ!</b>\n"
            f"💰 Множитель: x{float(game['multiplier']):.2f}\n"
            f"💵 Выигрыш: {win_amount:.2f} {game['currency']}",
            parse_mode="HTML"
        )
        await callback.answer("✅ Выигрыш зачислен!", show_alert=True)
    else:
        await callback.answer("❌ Ошибка!", show_alert=True)

# Обработчик профиля
@dp.message(F.text == "👤 Профиль")
async def profile_menu(message: Message):
    user_id = message.from_user.id
    balance = await db.get_balance(user_id)
    user = await db.get_user(user_id)
    
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
        f"💰 Баланс USDT: <b>{balance['usdt']:.2f}</b>\n"
        f"💰 Баланс TON: <b>{balance['ton']:.2f}</b>\n"
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
        "Выберите валюту для пополнения:",
        reply_markup=get_deposit_currency_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("deposit_"))
async def deposit_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.replace("deposit_", "")
    await state.update_data(deposit_currency=currency)
    
    await callback.message.edit_text(
        f"💰 <b>Пополнение {currency}</b>\n\n"
        f"Введите сумму пополнения (минимум 0.01):",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_deposit_amount)

@dp.message(BotStates.waiting_for_deposit_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0.01:
            await message.answer("❌ Минимальная сумма 0.01")
            return
        
        data = await state.get_data()
        currency = data.get('deposit_currency', 'USDT')
        
        pay_url = await create_crypto_invoice(amount, currency, message.from_user.id)
        
        if pay_url:
            await message.answer(
                f"💰 <b>Счет на {amount} {currency} создан!</b>\n\n"
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
async def withdraw_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "💸 <b>Вывод средств</b>\n\n"
        "Выберите валюту для вывода:",
        reply_markup=get_withdraw_currency_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("withdraw_"))
async def withdraw_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.replace("withdraw_", "")
    await state.update_data(withdraw_currency=currency)
    
    await callback.message.edit_text(
        f"💸 <b>Вывод {currency}</b>\n\n"
        f"Введите сумму для вывода (минимум 1):",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_withdraw_amount)

@dp.message(BotStates.waiting_for_withdraw_amount)
async def process_withdraw(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 1:
            await message.answer("❌ Минимальная сумма вывода 1")
            return
        
        data = await state.get_data()
        currency = data.get('withdraw_currency', 'USDT')
        user_id = message.from_user.id
        
        balance = await db.get_balance(user_id)
        current_balance = balance['usdt'] if currency == 'USDT' else balance['ton']
        
        if current_balance < amount:
            await message.answer(f"❌ Недостаточно {currency} на балансе")
            await state.clear()
            return
        
        # Создаем выплату
        transfer_id = await create_crypto_withdrawal(amount, currency, user_id)
        
        if transfer_id:
            # Списываем средства
            if currency == 'USDT':
                await db.update_balance(user_id, amount_usdt=-amount)
            else:
                await db.update_balance(user_id, amount_ton=-amount)
            
            await message.answer(
                f"✅ <b>Заявка на вывод создана!</b>\n"
                f"Сумма: {amount} {currency}\n"
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
        f"💰 Баланс USDT: <b>{balance['usdt']:.2f}</b>\n"
        f"💰 Баланс TON: <b>{balance['ton']:.2f}</b>\n"
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
        emoji = "🎲" if bet['game_type'] == 'dice' else "💣" if bet['game_type'] == 'mines' else "💎" if bet['game_type'] == 'crash' else "🏀" if bet['game_type'] == 'basketball' else "⚽"
        result_emoji = "✅" if bet['result'] == 'win' else "❌"
        time_str = bet['created_at'].strftime('%H:%M %d.%m')
        text += f"{emoji} {time_str} | {result_emoji} | {float(bet['bet_amount']):.2f} {bet['currency']} → {float(bet['win_amount']):.2f} (x{float(bet['multiplier']):.2f})\n"
    
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
            SELECT game_type, currency, COUNT(*) as games, 
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins,
                   SUM(win_amount) as total_won,
                   SUM(bet_amount) as total_bet
            FROM bets 
            WHERE user_id = $1 
            GROUP BY game_type, currency
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
            emoji = "🎲" if stat['game_type'] == 'dice' else "💣" if stat['game_type'] == 'mines' else "💎" if stat['game_type'] == 'crash' else "🏀" if stat['game_type'] == 'basketball' else "⚽"
            win_rate = (stat['wins'] / stat['games'] * 100) if stat['games'] > 0 else 0
            profit = float(stat['total_won']) - float(stat['total_bet'])
            profit_emoji = "✅" if profit >= 0 else "❌"
            text += f"{emoji} {stat['game_type']} ({stat['currency']}): {stat['games']} игр, {win_rate:.1f}% побед, {profit_emoji} {profit:+.2f}\n"
    else:
        text += "У вас пока нет игр.\n"
    
    if daily:
        text += "\n<b>Последние 7 дней:</b>\n"
        for day in daily:
            text += f"📅 {day['date'].strftime('%d.%m')}: {day['games']} игр, {float(day['volume']):.2f} $\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]]
        ),
        parse_mode="HTML"
    )

# Обработчик Топ
@dp.message(F.text == "🏆 Топ")
async def top_players(message: Message):
    top = await db.get_top_players(10)
    
    text = "🏆 <b>Топ 10 игроков</b>\n\n"
    
    for i, player in enumerate(top, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "👤"
        username = player['username'] or f"ID {player['user_id']}"
        win_rate = float(player['win_rate'])
        text += f"{medal} {i}. @{username}\n"
        text += f"   🎯 Побед: {player['total_wins']} | 📊 {win_rate:.1f}% | 💰 {float(player['total_balance']):.2f}$\n\n"
    
    await message.answer(text, parse_mode="HTML")

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
    elif isinstance(result, dict) and "error" in result:
        if result["error"] == "expired":
            await message.answer("❌ Срок действия кода истек!")
        elif result["error"] == "limit":
            await message.answer("❌ Лимит использований кода исчерпан!")
        elif result["error"] == "already_used":
            await message.answer("❌ Вы уже использовали этот код!")
    else:
        text = "✅ <b>Код успешно активирован!</b>\n\n"
        if result['usdt'] > 0:
            text += f"💰 +{result['usdt']:.2f} USDT зачислено!\n"
        if result['ton'] > 0:
            text += f"💰 +{result['ton']:.2f} TON зачислено!\n"
        
        await message.answer(text, parse_mode="HTML")
    
    await state.clear()

# Реферальная система
@dp.message(F.text == "👥 Реферальная система")
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
        f"💰 Заработано: {earnings['usdt']:.2f} USDT | {earnings['ton']:.2f} TON\n"
        f"💎 Отчисления: 10% от проигрышей\n\n"
    )
    
    if bots:
        text += "📋 <b>Ваши боты:</b>\n"
        for bot in bots[:3]:
            bot_earnings = await db.get_referral_earnings(user_id, bot['token'])
            text += f"• @{bot['bot_username']} — {bot_earnings['usdt']:.2f} USDT | {bot_earnings['ton']:.2f} TON\n"
    
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
            f"• Можно отключить уведомления в настройках\n\n"
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
    
    text = "📋 <b>Ваши боты:</b>\n\n"
    
    for bot in bots:
        earnings = await db.get_referral_earnings(user_id, bot['token'])
        async with db.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE bot_token = $1", bot['token']) or 0
        
        notif_status = "✅" if bot['notifications_enabled'] else "❌"
        text += f"🤖 @{bot['bot_username']}\n"
        text += f"   👥 Игроков: {users_count}\n"
        text += f"   💰 Заработано: {earnings['usdt']:.2f} USDT | {earnings['ton']:.2f} TON\n"
        text += f"   🔔 Уведомления: {notif_status}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать нового", callback_data="create_bot")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_referral")]
            ]
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
        f"💰 Заработано: {earnings['usdt']:.2f} USDT | {earnings['ton']:.2f} TON\n"
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
        f"💰 Объем USDT: <b>{stats['volume_usdt']:.2f}</b>\n"
        f"💰 Объем TON: <b>{stats['volume_ton']:.2f}</b>\n"
        f"📈 Прибыль USDT: <b>{stats['profit_usdt']:.2f}</b>\n"
        f"📈 Прибыль TON: <b>{stats['profit_ton']:.2f}</b>\n"
        f"📊 Сегодня: {stats['today_bets']} ставок",
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
        text += f"  💰 Заработано: {float(bot['total_earnings']):.2f}$\n"
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
        "📢 Введите сообщение для рассылки всем пользователям:"
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
    
    await message.answer(f"📢 Начинаю рассылку... Всего пользователей: {len(users)}")
    
    for user in users:
        try:
            await bot.send_message(
                user['user_id'], 
                f"📢 <b>Рассылка от администрации</b>\n\n{broadcast_text}",
                parse_mode="HTML"
            )
            sent += 1
            await asyncio.sleep(0.05)
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
            f"💰 Текущий баланс USDT: <b>{balance['usdt']:.2f}</b>\n"
            f"💰 Текущий баланс TON: <b>{balance['ton']:.2f}</b>\n\n"
            f"Введите новую сумму в формате: <code>USDT СУММА</code> или <code>TON СУММА</code>\n"
            f"Например: <code>USDT 100</code> или <code>TON 50</code>",
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
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте: USDT 100 или TON 50")
            return
        
        currency = parts[0].upper()
        new_balance = float(parts[1])
        
        if currency not in ["USDT", "TON"]:
            await message.answer("❌ Валюта должна быть USDT или TON")
            return
        
        data = await state.get_data()
        user_id = data['target_user_id']
        
        # Получаем текущий баланс
        current = await db.get_balance(user_id)
        
        # Устанавливаем новый баланс
        if currency == 'USDT':
            diff = new_balance - current['usdt']
            await db.update_balance(user_id, amount_usdt=diff)
        else:
            diff = new_balance - current['ton']
            await db.update_balance(user_id, amount_ton=diff)
        
        # Проверяем новый баланс
        updated = await db.get_balance(user_id)
        
        await message.answer(
            f"✅ <b>Баланс пользователя изменен!</b>\n"
            f"👤 ID: <code>{user_id}</code>\n"
            f"📊 Старый баланс: {current['usdt']:.2f} USDT | {current['ton']:.2f} TON\n"
            f"📊 Новый баланс: {updated['usdt']:.2f} USDT | {updated['ton']:.2f} TON",
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
        "<code>КОД USDT_СУММА TON_СУММА КОЛ-ВО ДНЕЙ МАКС_ИСПОЛЬЗОВАНИЙ</code>\n\n"
        "Например: <code>WELCOME 10 5 30 100</code>\n"
        "(код WELCOME на 10 USDT и 5 TON, 30 дней, 100 использований)",
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
            
            await message.answer(
                f"👋 <b>Добро пожаловать в игрового бота!</b>\n\n"
                f"🎰 Играй и выигрывай!",
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
            
            await message.answer(
                f"👤 <b>Ваш профиль</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"📝 Username: @{message.from_user.username or 'None'}\n"
                f"💰 Баланс USDT: <b>{balance['usdt']:.2f}</b>\n"
                f"💰 Баланс TON: <b>{balance['ton']:.2f}</b>",
                reply_markup=get_profile_keyboard(),
                parse_mode="HTML"
            )
        
        @referral_dp.message(F.text == "💎 Краш")
        async def referral_crash(message: Message):
            await message.answer(
                "💎 Игра Crash временно недоступна в реферальных ботах"
            )
        
        @referral_dp.message(F.text == "👥 Реферальная система")
        async def referral_referral(message: Message):
            await message.answer(
                "👥 В этом боте реферальная система отключена."
            )
        
        @referral_dp.message(F.text == "🏆 Топ")
        async def referral_top(message: Message):
            top = await db.get_top_players(10)
            
            text = "🏆 <b>Топ 10 игроков</b>\n\n"
            for i, player in enumerate(top, 1):
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "👤"
                username = player['username'] or f"ID {player['user_id']}"
                text += f"{medal} {i}. @{username}\n"
                text += f"   🎯 Побед: {player['total_wins']}\n\n"
            
            await message.answer(text, parse_mode="HTML")
        
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
