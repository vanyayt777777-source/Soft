"""
Telegram Casino Bot с интеграцией CryptoBot
Полная версия с PostgreSQL, турнирами и управлением RTP
"""

import os
import logging
import asyncio
import random
import string
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

import aiohttp
import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, 
    KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTOBOT_API_KEY = os.getenv("CRYPTOBOT_API_KEY", "")
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

# PostgreSQL конфигурация
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DATABASE = os.getenv("PG_DATABASE", "casino_bot")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD")

if not PG_PASSWORD:
    raise ValueError("PG_PASSWORD не найден в переменных окружения")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Пул соединений с БД
db_pool = None

# Состояния FSM
class SetupStates(StatesGroup):
    waiting_for_casino_name = State()
    waiting_for_welcome_message = State()
    waiting_for_crypto_token = State()
    waiting_for_admin_id = State()
    waiting_for_rtp_cube = State()
    waiting_for_rtp_darts = State()
    waiting_for_rtp_football = State()
    waiting_for_rtp_basketball = State()
    waiting_for_referral_percent = State()

class GameStates(StatesGroup):
    waiting_for_bet = State()
    waiting_for_choice = State()

class WithdrawalStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_wallet = State()

class PromoStates(StatesGroup):
    waiting_for_code = State()

class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()

class AdminBalanceStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

class TournamentStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_prize = State()
    waiting_for_start_date = State()
    waiting_for_end_date = State()
    waiting_for_min_bet = State()

# Подключение к PostgreSQL
async def init_db_pool():
    """Инициализация пула соединений с PostgreSQL"""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=PG_HOST,
            port=PG_PORT,
            database=PG_DATABASE,
            user=PG_USER,
            password=PG_PASSWORD,
            min_size=5,
            max_size=20
        )
        logger.info("PostgreSQL pool created successfully")
    except Exception as e:
        logger.error(f"Failed to create PostgreSQL pool: {e}")
        raise

@asynccontextmanager
async def get_db_connection():
    """Контекстный менеджер для получения соединения с БД"""
    async with db_pool.acquire() as conn:
        yield conn

# Инициализация базы данных
async def init_database():
    """Создание всех таблиц в PostgreSQL"""
    async with get_db_connection() as conn:
        # Таблица настроек
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                casino_name TEXT NOT NULL,
                welcome_message TEXT NOT NULL,
                crypto_token TEXT NOT NULL,
                admin_id BIGINT NOT NULL,
                rtp_cube REAL DEFAULT 95.0,
                rtp_darts REAL DEFAULT 95.0,
                rtp_football REAL DEFAULT 95.0,
                rtp_basketball REAL DEFAULT 95.0,
                referral_percent REAL DEFAULT 5.0,
                min_withdrawal REAL DEFAULT 1.0,
                max_withdrawal REAL DEFAULT 1000.0,
                is_initialized INTEGER DEFAULT 0
            )
        ''')
        
        # Таблица пользователей
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                balance DECIMAL(10,2) DEFAULT 0.0,
                referral_code TEXT UNIQUE,
                referred_by BIGINT,
                referral_earnings DECIMAL(10,2) DEFAULT 0.0,
                referral_count INTEGER DEFAULT 0,
                total_deposit DECIMAL(10,2) DEFAULT 0.0,
                total_withdrawal DECIMAL(10,2) DEFAULT 0.0,
                total_bet DECIMAL(10,2) DEFAULT 0.0,
                total_win DECIMAL(10,2) DEFAULT 0.0,
                games_played INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_blocked INTEGER DEFAULT 0,
                ban_reason TEXT
            )
        ''')
        
        # Таблица транзакций
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,
                amount DECIMAL(10,2) NOT NULL,
                currency TEXT DEFAULT 'USD',
                status TEXT DEFAULT 'pending',
                external_id TEXT UNIQUE,
                wallet_address TEXT,
                admin_id BIGINT,
                reason TEXT,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id)
            )
        ''')
        
        # Таблица игр
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS games (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                game_type TEXT NOT NULL,
                bet_amount DECIMAL(10,2) NOT NULL,
                win_amount DECIMAL(10,2) DEFAULT 0,
                choice TEXT,
                result INTEGER,
                multiplier INTEGER DEFAULT 2,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id)
            )
        ''')
        
        # Таблица промокодов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS promocodes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                amount DECIMAL(10,2) NOT NULL,
                expires_at TIMESTAMP,
                max_activations INTEGER DEFAULT 1,
                current_activations INTEGER DEFAULT 0,
                created_by BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица активаций промокодов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS promo_activations (
                id SERIAL PRIMARY KEY,
                promo_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (promo_id) REFERENCES promocodes(id),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id)
            )
        ''')
        
        # Таблица турниров
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tournaments (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                prize_pool DECIMAL(10,2) NOT NULL,
                start_date TIMESTAMP NOT NULL,
                end_date TIMESTAMP NOT NULL,
                min_bet DECIMAL(10,2) DEFAULT 0,
                max_participants INTEGER DEFAULT 0,
                current_participants INTEGER DEFAULT 0,
                status TEXT DEFAULT 'upcoming',
                winner_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (winner_id) REFERENCES users(telegram_id)
            )
        ''')
        
        # Таблица участников турниров
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tournament_participants (
                id SERIAL PRIMARY KEY,
                tournament_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                score DECIMAL(10,2) DEFAULT 0,
                games_played INTEGER DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
                FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                UNIQUE(tournament_id, user_id)
            )
        ''')
        
        # Таблица логов действий админов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                action TEXT NOT NULL,
                target_user BIGINT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (admin_id) REFERENCES users(telegram_id)
            )
        ''')
        
        logger.info("Database initialized successfully")

# Вспомогательные функции
def generate_referral_code(telegram_id: int) -> str:
    """Генерация уникального реферального кода"""
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"{telegram_id}{random_str}"

async def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Получение пользователя из БД"""
    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1",
            telegram_id
        )
        return dict(row) if row else None

async def create_user(telegram_id: int, username: str, first_name: str, referred_by: int = None):
    """Создание нового пользователя"""
    referral_code = generate_referral_code(telegram_id)
    async with get_db_connection() as conn:
        # Проверяем, является ли пользователь админом
        settings = await conn.fetchrow("SELECT admin_id FROM settings WHERE id = 1")
        is_admin = 1 if settings and settings['admin_id'] == telegram_id else 0
        
        await conn.execute('''
            INSERT INTO users 
            (telegram_id, username, first_name, referral_code, referred_by, is_admin)
            VALUES ($1, $2, $3, $4, $5, $6)
        ''', telegram_id, username, first_name, referral_code, referred_by, is_admin)
        
        # Если есть реферер, увеличиваем его счетчик
        if referred_by:
            await conn.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE telegram_id = $1",
                referred_by
            )

async def get_settings() -> Optional[Dict[str, Any]]:
    """Получение настроек бота"""
    async with get_db_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM settings WHERE id = 1")
        return dict(row) if row else None

async def is_initialized() -> bool:
    """Проверка, инициализирован ли бот"""
    settings = await get_settings()
    return settings is not None and settings.get('is_initialized') == 1

async def update_balance(telegram_id: int, amount: float, operation: str = 'add'):
    """Обновление баланса пользователя"""
    async with get_db_connection() as conn:
        if operation == 'add':
            await conn.execute(
                "UPDATE users SET balance = balance + $1, last_activity = CURRENT_TIMESTAMP WHERE telegram_id = $2",
                amount, telegram_id
            )
        else:
            await conn.execute(
                "UPDATE users SET balance = balance - $1, last_activity = CURRENT_TIMESTAMP WHERE telegram_id = $2",
                amount, telegram_id
            )

async def log_admin_action(admin_id: int, action: str, target_user: int = None, details: str = None):
    """Логирование действий администратора"""
    async with get_db_connection() as conn:
        await conn.execute('''
            INSERT INTO admin_logs (admin_id, action, target_user, details)
            VALUES ($1, $2, $3, $4)
        ''', admin_id, action, target_user, details)

# Функции для работы с CryptoBot API
async def create_crypto_invoice(amount: float, user_id: int, description: str = "Пополнение баланса") -> Optional[Dict]:
    """Создание счета в CryptoBot"""
    try:
        settings = await get_settings()
        if not settings or not settings.get('crypto_token'):
            logger.error("CryptoBot API token not configured")
            return None
        
        headers = {
            "Crypto-Pay-API-Token": settings['crypto_token'],
            "Content-Type": "application/json"
        }
        
        payload = {
            "asset": "USDT",
            "amount": str(amount),
            "description": description,
            "payload": str(user_id),
            "expires_in": 3600
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CRYPTOBOT_API_URL}/createInvoice",
                headers=headers,
                json=payload
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        return data['result']
                return None
    except Exception as e:
        logger.error(f"Error creating CryptoBot invoice: {e}")
        return None

async def check_invoice_status(invoice_id: int) -> Optional[Dict]:
    """Проверка статуса счета"""
    try:
        settings = await get_settings()
        if not settings or not settings.get('crypto_token'):
            return None
        
        headers = {
            "Crypto-Pay-API-Token": settings['crypto_token']
        }
        
        params = {
            "invoice_id": invoice_id
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CRYPTOBOT_API_URL}/getInvoices",
                headers=headers,
                params=params
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok') and data.get('result', {}).get('items'):
                        return data['result']['items'][0]
                return None
    except Exception as e:
        logger.error(f"Error checking invoice status: {e}")
        return None

async def send_crypto_payment(user_id: int, amount: float, wallet: str) -> Optional[Dict]:
    """Отправка платежа через CryptoBot"""
    try:
        settings = await get_settings()
        if not settings or not settings.get('crypto_token'):
            return None
        
        headers = {
            "Crypto-Pay-API-Token": settings['crypto_token'],
            "Content-Type": "application/json"
        }
        
        payload = {
            "asset": "USDT",
            "amount": str(amount),
            "user_id": wallet.replace("@", ""),
            "spend_id": f"withdraw_{user_id}_{int(datetime.now().timestamp())}"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CRYPTOBOT_API_URL}/transfer",
                headers=headers,
                json=payload
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        return data['result']
                return None
    except Exception as e:
        logger.error(f"Error sending crypto payment: {e}")
        return None

# Клавиатуры
def get_main_keyboard():
    """Главное меню"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎮 ИГРАТЬ"), KeyboardButton(text="👤 ПРОФИЛЬ")],
            [KeyboardButton(text="👥 РЕФЕРАЛЫ"), KeyboardButton(text="🏆 ТУРНИРЫ")],
            [KeyboardButton(text="💬 ПОДДЕРЖКА")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_profile_keyboard():
    """Клавиатура профиля"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 ПОПОЛНИТЬ", callback_data="deposit")],
            [InlineKeyboardButton(text="💸 ВЫВЕСТИ", callback_data="withdraw")],
            [InlineKeyboardButton(text="🎟 АКТИВИРОВАТЬ ПРОМОКОД", callback_data="activate_promo")]
        ]
    )
    return keyboard

def get_games_keyboard():
    """Клавиатура выбора игры"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 КУБИК x2")],
            [KeyboardButton(text="🎯 ДАРТС x2")],
            [KeyboardButton(text="⚽ ФУТБОЛ x2")],
            [KeyboardButton(text="🏀 БАСКЕТБОЛ x2")],
            [KeyboardButton(text="🔙 НАЗАД")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_game_choices_keyboard(game: str):
    """Клавиатура выбора исхода игры"""
    if game == "🎲 КУБИК x2":
        buttons = [
            [InlineKeyboardButton(text="📈 БОЛЬШЕ (4-6) x2", callback_data="choice_more")],
            [InlineKeyboardButton(text="📉 МЕНЬШЕ (1-3) x2", callback_data="choice_less")]
        ]
    elif game == "🎯 ДАРТС x2":
        buttons = [
            [InlineKeyboardButton(text="🎯 В ЦЕЛЬ x2", callback_data="choice_target")],
            [InlineKeyboardButton(text="💨 МИМО x2", callback_data="choice_miss")]
        ]
    elif game == "⚽ ФУТБОЛ x2":
        buttons = [
            [InlineKeyboardButton(text="⚽ ГОЛ x2", callback_data="choice_goal")],
            [InlineKeyboardButton(text="🥅 ПРОМАХ x2", callback_data="choice_miss")]
        ]
    else:  # БАСКЕТБОЛ
        buttons = [
            [InlineKeyboardButton(text="🏀 ПОПАДАНИЕ x2", callback_data="choice_score")],
            [InlineKeyboardButton(text="💫 ПРОМАХ x2", callback_data="choice_miss_basket")]
        ]
    
    buttons.append([InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="cancel_game")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard():
    """Админ-панель"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 СТАТИСТИКА"), KeyboardButton(text="👥 ПОЛЬЗОВАТЕЛИ")],
            [KeyboardButton(text="💰 БАЛАНСЫ"), KeyboardButton(text="🎟 ПРОМОКОДЫ")],
            [KeyboardButton(text="🏆 ТУРНИРЫ"), KeyboardButton(text="⚙️ RTP")],
            [KeyboardButton(text="📢 РАССЫЛКА"), KeyboardButton(text="📋 ЛОГИ")],
            [KeyboardButton(text="🔙 НАЗАД")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_balance_management_keyboard():
    """Клавиатура управления балансами"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ НАЧИСЛИТЬ", callback_data="admin_balance_add")],
            [InlineKeyboardButton(text="➖ СПИСАТЬ", callback_data="admin_balance_subtract")]
        ]
    )
    return keyboard

def get_tournament_keyboard():
    """Клавиатура управления турнирами"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ СОЗДАТЬ ТУРНИР", callback_data="admin_tournament_create")],
            [InlineKeyboardButton(text="📋 СПИСОК ТУРНИРОВ", callback_data="admin_tournament_list")],
            [InlineKeyboardButton(text="🏆 ЗАВЕРШИТЬ ТУРНИР", callback_data="admin_tournament_end")]
        ]
    )
    return keyboard

def get_promo_keyboard():
    """Клавиатура управления промокодами"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ СОЗДАТЬ ПРОМОКОД")],
            [KeyboardButton(text="📋 СПИСОК ПРОМОКОДОВ")],
            [KeyboardButton(text="❌ УДАЛИТЬ ПРОМОКОД")],
            [KeyboardButton(text="🔙 НАЗАД")]
        ],
        resize_keyboard=True
    )
    return keyboard

# Обработчики команд
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    telegram_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    
    # Проверяем реферальный код
    args = message.text.split()
    referred_by = None
    if len(args) > 1:
        try:
            async with get_db_connection() as conn:
                result = await conn.fetchval(
                    "SELECT telegram_id FROM users WHERE referral_code = $1",
                    args[1]
                )
                if result and result != telegram_id:
                    referred_by = result
        except:
            pass
    
    # Проверяем, инициализирован ли бот
    if not await is_initialized():
        await state.set_state(SetupStates.waiting_for_casino_name)
        await message.answer(
            "⚙️ **ПЕРВОНАЧАЛЬНАЯ НАСТРОЙКА**\n\n"
            "Введите название казино:",
            parse_mode="Markdown"
        )
        return
    
    # Проверяем, существует ли пользователь
    user = await get_user(telegram_id)
    if not user:
        await create_user(telegram_id, username, first_name, referred_by)
        user = await get_user(telegram_id)
        
        if referred_by:
            await message.answer("🎉 Вы зарегистрировались по реферальной ссылке!")
    
    settings = await get_settings()
    welcome_text = settings.get('welcome_message', 'Добро пожаловать в казино!')
    
    await message.answer(
        f"**{welcome_text}**\n\n"
        f"🆔 **ID:** `{telegram_id}`\n"
        f"💰 **Баланс:** `{user['balance']:.2f} $`\n",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# Обработчики настройки
@dp.message(SetupStates.waiting_for_casino_name)
async def setup_casino_name(message: Message, state: FSMContext):
    await state.update_data(casino_name=message.text)
    await state.set_state(SetupStates.waiting_for_welcome_message)
    await message.answer("Введите приветственное сообщение:")

@dp.message(SetupStates.waiting_for_welcome_message)
async def setup_welcome_message(message: Message, state: FSMContext):
    await state.update_data(welcome_message=message.text)
    await state.set_state(SetupStates.waiting_for_crypto_token)
    await message.answer("Введите Crypto Bot API токен:")

@dp.message(SetupStates.waiting_for_crypto_token)
async def setup_crypto_token(message: Message, state: FSMContext):
    await state.update_data(crypto_token=message.text)
    await state.set_state(SetupStates.waiting_for_admin_id)
    await message.answer("Введите Telegram ID администратора:")

@dp.message(SetupStates.waiting_for_admin_id)
async def setup_admin_id(message: Message, state: FSMContext):
    try:
        admin_id = int(message.text)
        await state.update_data(admin_id=admin_id)
        await state.set_state(SetupStates.waiting_for_rtp_cube)
        await message.answer("Введите RTP для КУБИКА (1-100):")
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректный ID:")

@dp.message(SetupStates.waiting_for_rtp_cube)
async def setup_rtp_cube(message: Message, state: FSMContext):
    try:
        rtp = float(message.text)
        if 1 <= rtp <= 100:
            await state.update_data(rtp_cube=rtp)
            await state.set_state(SetupStates.waiting_for_rtp_darts)
            await message.answer("Введите RTP для ДАРТСА (1-100):")
        else:
            await message.answer("❌ Введите число от 1 до 100:")
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message(SetupStates.waiting_for_rtp_darts)
async def setup_rtp_darts(message: Message, state: FSMContext):
    try:
        rtp = float(message.text)
        if 1 <= rtp <= 100:
            await state.update_data(rtp_darts=rtp)
            await state.set_state(SetupStates.waiting_for_rtp_football)
            await message.answer("Введите RTP для ФУТБОЛА (1-100):")
        else:
            await message.answer("❌ Введите число от 1 до 100:")
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message(SetupStates.waiting_for_rtp_football)
async def setup_rtp_football(message: Message, state: FSMContext):
    try:
        rtp = float(message.text)
        if 1 <= rtp <= 100:
            await state.update_data(rtp_football=rtp)
            await state.set_state(SetupStates.waiting_for_rtp_basketball)
            await message.answer("Введите RTP для БАСКЕТБОЛА (1-100):")
        else:
            await message.answer("❌ Введите число от 1 до 100:")
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message(SetupStates.waiting_for_rtp_basketball)
async def setup_rtp_basketball(message: Message, state: FSMContext):
    try:
        rtp = float(message.text)
        if 1 <= rtp <= 100:
            await state.update_data(rtp_basketball=rtp)
            await state.set_state(SetupStates.waiting_for_referral_percent)
            await message.answer("Введите процент от проигрыша в реферальную систему (0-100):")
        else:
            await message.answer("❌ Введите число от 1 до 100:")
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message(SetupStates.waiting_for_referral_percent)
async def setup_referral_percent(message: Message, state: FSMContext):
    try:
        referral_percent = float(message.text)
        if 0 <= referral_percent <= 100:
            data = await state.get_data()
            
            async with get_db_connection() as conn:
                await conn.execute('''
                    INSERT INTO settings 
                    (id, casino_name, welcome_message, crypto_token, admin_id, 
                     rtp_cube, rtp_darts, rtp_football, rtp_basketball, referral_percent, is_initialized)
                    VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, 1)
                    ON CONFLICT (id) DO UPDATE SET
                        casino_name = $1,
                        welcome_message = $2,
                        crypto_token = $3,
                        admin_id = $4,
                        rtp_cube = $5,
                        rtp_darts = $6,
                        rtp_football = $7,
                        rtp_basketball = $8,
                        referral_percent = $9,
                        is_initialized = 1
                ''', 
                    data['casino_name'],
                    data['welcome_message'],
                    data['crypto_token'],
                    data['admin_id'],
                    data['rtp_cube'],
                    data['rtp_darts'],
                    data['rtp_football'],
                    data['rtp_basketball'],
                    referral_percent
                )
            
            await state.clear()
            await create_user(data['admin_id'], "admin", "Admin")
            
            await message.answer(
                "✅ **БОТ НАСТРОЕН**\n\n"
                "Используйте /start для начала работы",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Введите число от 0 до 100:")
    except ValueError:
        await message.answer("❌ Введите корректное число:")

# Обработчики меню
@dp.message(F.text == "👤 ПРОФИЛЬ")
async def profile_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)
    
    if not user:
        await cmd_start(message)
        return
    
    async with get_db_connection() as conn:
        referrals = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by = $1",
            telegram_id
        )
    
    text = (
        f"**👤 ПРОФИЛЬ**\n\n"
        f"🆔 **ID:** `{telegram_id}`\n"
        f"💰 **Баланс:** `{user['balance']:.2f} $`\n"
        f"🎮 **Сыграно игр:** {user['games_played']}\n"
        f"🏆 **Побед:** {user['games_won']}\n"
        f"📥 **Пополнено:** {user['total_deposit']:.2f} $\n"
        f"📤 **Выведено:** {user['total_withdrawal']:.2f} $\n"
        f"👥 **Рефералов:** {referrals}\n"
        f"💰 **Заработано:** {user['referral_earnings']:.2f} $"
    )
    
    await message.answer(text, reply_markup=get_profile_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "👥 РЕФЕРАЛЫ")
async def referral_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)
    
    if not user:
        await cmd_start(message)
        return
    
    async with get_db_connection() as conn:
        referrals = await conn.fetch(
            "SELECT username, first_name, total_deposit FROM users WHERE referred_by = $1 ORDER BY created_at DESC LIMIT 5",
            telegram_id
        )
    
    settings = await get_settings()
    bot_username = (await bot.get_me()).username
    
    text = (
        f"**👥 РЕФЕРАЛЬНАЯ СИСТЕМА**\n\n"
        f"📊 **Статистика:**\n"
        f"• Приглашено: {user['referral_count']}\n"
        f"• Заработано: {user['referral_earnings']:.2f} $\n"
        f"• Процент от проигрыша: {settings['referral_percent']}%\n\n"
        f"**🔗 Ваша ссылка:**\n"
        f"`https://t.me/{bot_username}?start={user['referral_code']}`\n\n"
    )
    
    if referrals:
        text += "**📋 Последние рефералы:**\n"
        for ref in referrals:
            name = ref['first_name'] or ref['username'] or "Без имени"
            text += f"• {name} (депозит: {ref['total_deposit']:.2f} $)\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "🎮 ИГРАТЬ")
async def games_menu(message: Message):
    await message.answer(
        "**🎮 ВЫБЕРИТЕ ИГРУ**",
        reply_markup=get_games_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🏆 ТУРНИРЫ")
async def tournaments_menu(message: Message):
    telegram_id = message.from_user.id
    
    async with get_db_connection() as conn:
        # Активные турниры
        tournaments = await conn.fetch('''
            SELECT * FROM tournaments 
            WHERE status IN ('upcoming', 'active') 
            ORDER BY start_date ASC
        ''')
        
        # Турниры пользователя
        user_tournaments = await conn.fetch('''
            SELECT t.* FROM tournaments t
            JOIN tournament_participants tp ON t.id = tp.tournament_id
            WHERE tp.user_id = $1 AND t.status = 'active'
        ''', telegram_id)
    
    if not tournaments:
        text = "🏆 **ТУРНИРЫ**\n\n📭 Активных турниров нет"
    else:
        text = "🏆 **АКТИВНЫЕ ТУРНИРЫ**\n\n"
        
        for t in tournaments:
            start = t['start_date'].strftime("%d.%m.%Y %H:%M")
            end = t['end_date'].strftime("%d.%m.%Y %H:%M")
            status = "🔴 АКТИВЕН" if t['status'] == 'active' else "⏳ СКОРО"
            
            text += (
                f"**{t['name']}**\n"
                f"• {status}\n"
                f"• Приз: 💰 {t['prize_pool']:.2f} $\n"
                f"• Начало: {start}\n"
                f"• Конец: {end}\n"
                f"• Участников: {t['current_participants']}\n"
                f"{'—' * 20}\n"
            )
    
    if user_tournaments:
        text += "\n**📋 ВЫ УЧАСТВУЕТЕ:**\n"
        for t in user_tournaments:
            text += f"• {t['name']}\n"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏆 ТАБЛИЦА ЛИДЕРОВ", callback_data="leaderboard")]
        ]
    )
    
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.text == "💬 ПОДДЕРЖКА")
async def support_menu(message: Message, state: FSMContext):
    await state.set_state(SupportStates.waiting_for_message)
    await message.answer(
        "**💬 ПОДДЕРЖКА**\n\n"
        "Опишите вашу проблему:",
        parse_mode="Markdown"
    )

@dp.message(SupportStates.waiting_for_message)
async def process_support_message(message: Message, state: FSMContext):
    settings = await get_settings()
    if settings and settings['admin_id']:
        user = await get_user(message.from_user.id)
        await bot.send_message(
            settings['admin_id'],
            f"🆘 **Обращение от {user['first_name']} (@{user['username']}):**\n\n{message.text}"
        )
    
    await state.clear()
    await message.answer("✅ Сообщение отправлено")

@dp.message(F.text == "🔙 НАЗАД")
async def back_to_main(message: Message):
    user = await get_user(message.from_user.id)
    await message.answer(
        f"🏠 **ГЛАВНОЕ МЕНЮ**\n\n💰 Баланс: {user['balance']:.2f} $",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# Обработчики игр
@dp.message(F.text.in_(["🎲 КУБИК x2", "🎯 ДАРТС x2", "⚽ ФУТБОЛ x2", "🏀 БАСКЕТБОЛ x2"]))
async def select_game(message: Message, state: FSMContext):
    game = message.text
    await state.update_data(game=game)
    await state.set_state(GameStates.waiting_for_bet)
    
    await message.answer(
        f"**🎮 {game}**\n\n"
        f"💰 Множитель: x2\n"
        f"💵 Минимум: 0.01 $\n\n"
        f"Введите сумму ставки:",
        parse_mode="Markdown"
    )

@dp.message(GameStates.waiting_for_bet)
async def process_bet(message: Message, state: FSMContext):
    try:
        bet = float(message.text)
        if bet < 0.01:
            await message.answer("❌ Минимальная ставка 0.01 $")
            return
        
        telegram_id = message.from_user.id
        user = await get_user(telegram_id)
        
        if not user:
            await cmd_start(message)
            return
        
        if user['balance'] < bet:
            await message.answer("❌ Недостаточно средств")
            await state.clear()
            return
        
        await update_balance(telegram_id, bet, 'subtract')
        
        data = await state.get_data()
        game = data['game']
        
        await state.update_data(bet=bet)
        await state.set_state(GameStates.waiting_for_choice)
        
        await message.answer(
            f"✅ Ставка {bet:.2f} $ принята!\n\nВыберите исход:",
            reply_markup=get_game_choices_keyboard(game)
        )
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.callback_query(lambda c: c.data and (c.data.startswith('choice_') or c.data == 'cancel_game'))
async def process_game_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == 'cancel_game':
        await state.clear()
        await callback.message.edit_text("❌ Игра отменена")
        await callback.answer()
        return
    
    data = await state.get_data()
    if not data:
        await callback.answer("⚠️ Игра не найдена")
        return
    
    game = data.get('game')
    bet = data.get('bet')
    telegram_id = callback.from_user.id
    
    if not game or not bet:
        await callback.answer("❌ Ошибка данных")
        return
    
    await callback.answer()
    
    # Определяем эмодзи для игры
    if "КУБИК" in game:
        emoji = "🎲"
        game_type = "cube"
    elif "ДАРТС" in game:
        emoji = "🎯"
        game_type = "darts"
    elif "ФУТБОЛ" in game:
        emoji = "⚽"
        game_type = "football"
    else:
        emoji = "🏀"
        game_type = "basketball"
    
    # Отправляем дайс
    msg = await callback.message.answer_dice(emoji=emoji)
    result = msg.dice.value
    
    await asyncio.sleep(2)
    
    # Получаем настройки RTP для игры
    settings = await get_settings()
    rtp_key = f"rtp_{game_type}"
    game_rtp = settings.get(rtp_key, 95.0)
    
    # Определяем победу на основе RTP
    win = False
    if "КУБИК" in game:
        if callback.data == 'choice_more':
            win = result > 3 and random.random() * 100 < game_rtp
        else:
            win = result < 4 and random.random() * 100 < game_rtp
    elif "ДАРТС" in game:
        if callback.data == 'choice_target':
            win = result in [5, 6] and random.random() * 100 < game_rtp
        else:
            win = result in [1, 2, 3, 4] and random.random() * 100 < game_rtp
    elif "ФУТБОЛ" in game:
        if callback.data == 'choice_goal':
            win = result in [4, 5] and random.random() * 100 < game_rtp
        else:
            win = result in [1, 2, 3] and random.random() * 100 < game_rtp
    else:  # БАСКЕТБОЛ
        if callback.data == 'choice_score':
            win = result in [4, 5] and random.random() * 100 < game_rtp
        else:
            win = result in [1, 2, 3] and random.random() * 100 < game_rtp
    
    if win:
        win_amount = bet * 2
        await update_balance(telegram_id, win_amount, 'add')
        
        # Начисление реферальных
        user = await get_user(telegram_id)
        if user and user['referred_by']:
            referral_earning = bet * settings['referral_percent'] / 100
            if referral_earning > 0:
                await update_balance(user['referred_by'], referral_earning, 'add')
                async with get_db_connection() as conn:
                    await conn.execute(
                        "UPDATE users SET referral_earnings = referral_earnings + $1 WHERE telegram_id = $2",
                        referral_earning, user['referred_by']
                    )
        
        await callback.message.answer(f"🎉 **ВЫ ВЫИГРАЛИ {win_amount:.2f} $!**")
        
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE users SET games_won = games_won + 1, total_win = total_win + $1 WHERE telegram_id = $2",
                win_amount, telegram_id
            )
            
            # Обновляем счет в турнире
            await conn.execute('''
                UPDATE tournament_participants 
                SET score = score + $1 
                WHERE user_id = $2 AND tournament_id IN (
                    SELECT id FROM tournaments WHERE status = 'active'
                )
            ''', win_amount, telegram_id)
    else:
        await callback.message.answer(f"😢 **ВЫ ПРОИГРАЛИ {bet:.2f} $**")
        
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE users SET total_bet = total_bet + $1 WHERE telegram_id = $2",
                bet, telegram_id
            )
        win_amount = 0
    
    # Сохраняем игру
    async with get_db_connection() as conn:
        await conn.execute('''
            INSERT INTO games (user_id, game_type, bet_amount, win_amount, choice, result, multiplier)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        ''', telegram_id, game, bet, win_amount, callback.data, result, 2 if win else 0)
        
        await conn.execute(
            "UPDATE users SET games_played = games_played + 1, last_activity = CURRENT_TIMESTAMP WHERE telegram_id = $1",
            telegram_id
        )
    
    await state.clear()

# Обработчики платежей
@dp.callback_query(F.data == "deposit")
async def deposit_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "**💰 ПОПОЛНЕНИЕ**\n\n"
        "💵 Минимум: 0.01 $\n"
        "💎 Курс: 1 USDT = 1 $\n\n"
        "Введите сумму:",
        parse_mode="Markdown"
    )

@dp.message(F.text.regexp(r'^\d+\.?\d*$'))
async def process_deposit_amount(message: Message):
    try:
        amount = float(message.text)
        if amount < 0.01:
            await message.answer("❌ Минимальная сумма 0.01 $")
            return
        
        invoice = await create_crypto_invoice(amount, message.from_user.id)
        
        if invoice and invoice.get('pay_url'):
            async with get_db_connection() as conn:
                await conn.execute('''
                    INSERT INTO transactions (user_id, type, amount, external_id, status)
                    VALUES ($1, 'deposit', $2, $3, 'pending')
                ''', message.from_user.id, amount, str(invoice['invoice_id']))
            
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 ОПЛАТИТЬ", url=invoice['pay_url'])],
                    [InlineKeyboardButton(text="🔄 ПРОВЕРИТЬ", callback_data=f"check_payment_{invoice['invoice_id']}")]
                ]
            )
            
            await message.answer(
                f"**🧾 СЧЕТ СОЗДАН**\n\n"
                f"💰 Сумма: {amount} USDT\n"
                f"⏱ Действителен 1 час",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Ошибка создания счета")
            
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.callback_query(lambda c: c.data and c.data.startswith('check_payment_'))
async def check_payment(callback: CallbackQuery):
    await callback.answer()
    invoice_id = int(callback.data.replace('check_payment_', ''))
    
    invoice_info = await check_invoice_status(invoice_id)
    
    if invoice_info and invoice_info.get('status') == 'paid':
        async with get_db_connection() as conn:
            # Получаем сумму транзакции
            amount = await conn.fetchval(
                "SELECT amount FROM transactions WHERE external_id = $1",
                str(invoice_id)
            )
            
            await conn.execute(
                "UPDATE transactions SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE external_id = $1",
                str(invoice_id)
            )
            
            await conn.execute(
                "UPDATE users SET balance = balance + $1, total_deposit = total_deposit + $1 WHERE telegram_id = $2",
                amount, callback.from_user.id
            )
        
        await callback.message.edit_text("✅ **ОПЛАЧЕНО!** Средства зачислены")
    else:
        await callback.message.answer("⏳ Платеж еще не подтвержден")

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    
    if not user:
        return
    
    settings = await get_settings()
    min_withdrawal = settings.get('min_withdrawal', 1.0)
    
    if user['balance'] < min_withdrawal:
        await callback.message.edit_text(f"❌ Минимум для вывода: {min_withdrawal:.2f} $")
        return
    
    await state.set_state(WithdrawalStates.waiting_for_amount)
    await callback.message.edit_text(
        f"**💸 ВЫВОД**\n\n"
        f"💰 Доступно: {user['balance']:.2f} $\n"
        f"📉 Мин: {min_withdrawal:.2f} $\n\n"
        f"Введите сумму:",
        parse_mode="Markdown"
    )

@dp.message(WithdrawalStates.waiting_for_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        settings = await get_settings()
        min_withdrawal = settings.get('min_withdrawal', 1.0)
        max_withdrawal = settings.get('max_withdrawal', 1000.0)
        
        if amount < min_withdrawal or amount > max_withdrawal:
            await message.answer(f"❌ Сумма должна быть от {min_withdrawal} до {max_withdrawal} $")
            return
        
        user = await get_user(message.from_user.id)
        
        if amount > user['balance']:
            await message.answer("❌ Недостаточно средств")
            return
        
        await state.update_data(amount=amount)
        await state.set_state(WithdrawalStates.waiting_for_wallet)
        
        await message.answer("📝 Введите @username в CryptoBot или адрес кошелька:")
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.message(WithdrawalStates.waiting_for_wallet)
async def process_withdrawal_wallet(message: Message, state: FSMContext):
    wallet = message.text.strip()
    data = await state.get_data()
    amount = data['amount']
    telegram_id = message.from_user.id
    
    if not (wallet.startswith('@') or wallet.startswith('UQ') or wallet.startswith('EQ')):
        await message.answer("❌ Неверный формат кошелька")
        return
    
    async with get_db_connection() as conn:
        await conn.execute('''
            INSERT INTO transactions (user_id, type, amount, wallet_address, status)
            VALUES ($1, 'withdrawal', $2, $3, 'pending')
        ''', telegram_id, amount, wallet)
        
        await conn.execute(
            "UPDATE users SET balance = balance - $1 WHERE telegram_id = $2",
            amount, telegram_id
        )
    
    # Уведомляем админа
    settings = await get_settings()
    if settings and settings['admin_id']:
        user = await get_user(telegram_id)
        await bot.send_message(
            settings['admin_id'],
            f"🆕 **ЗАЯВКА НА ВЫВОД**\n\n"
            f"От: {user['first_name']} (@{user['username']})\n"
            f"💰 Сумма: {amount:.2f} $\n"
            f"📝 Кошелек: {wallet}"
        )
    
    await state.clear()
    await message.answer("✅ Заявка создана. Ожидайте подтверждения")

# Обработчики промокодов
@dp.callback_query(F.data == "activate_promo")
async def activate_promo_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PromoStates.waiting_for_code)
    await callback.message.edit_text(
        "🎟 **АКТИВАЦИЯ ПРОМОКОДА**\n\nВведите код:",
        parse_mode="Markdown"
    )

@dp.message(PromoStates.waiting_for_code)
async def process_promo_code(message: Message, state: FSMContext):
    code = message.text.upper().strip()
    
    async with get_db_connection() as conn:
        promo = await conn.fetchrow("SELECT * FROM promocodes WHERE code = $1", code)
        
        if not promo:
            await message.answer("❌ **ПРОМОКОД НЕ НАЙДЕН**")
            await state.clear()
            return
        
        if promo['expires_at'] and promo['expires_at'] < datetime.now():
            await message.answer("❌ **СРОК ДЕЙСТВИЯ ИСТЕК**")
            await state.clear()
            return
        
        if promo['current_activations'] >= promo['max_activations']:
            await message.answer("❌ **ЛИМИТ АКТИВАЦИЙ ИСЧЕРПАН**")
            await state.clear()
            return
        
        activated = await conn.fetchval(
            "SELECT 1 FROM promo_activations WHERE promo_id = $1 AND user_id = $2",
            promo['id'], message.from_user.id
        )
        if activated:
            await message.answer("❌ **ПРОМОКОД УЖЕ АКТИВИРОВАН**")
            await state.clear()
            return
        
        # Начисляем сумму на баланс
        await update_balance(message.from_user.id, promo['amount'], 'add')
        
        await conn.execute(
            "UPDATE promocodes SET current_activations = current_activations + 1 WHERE id = $1",
            promo['id']
        )
        
        await conn.execute(
            "INSERT INTO promo_activations (promo_id, user_id) VALUES ($1, $2)",
            promo['id'], message.from_user.id
        )
    
    await state.clear()
    await message.answer(f"✅ **ПРОМОКОД АКТИВИРОВАН!**\n💰 Начислено: +{promo['amount']:.2f} $")

# Админ-панель
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    user = await get_user(message.from_user.id)
    
    if not user or not user['is_admin']:
        await message.answer("⛔ **ДОСТУП ЗАПРЕЩЕН**")
        return
    
    await message.answer(
        "👑 **АДМИН-ПАНЕЛЬ**",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "📊 СТАТИСТИКА")
async def admin_stats(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    async with get_db_connection() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users")
        total_deposit = await conn.fetchval("SELECT COALESCE(SUM(total_deposit), 0) FROM users")
        total_withdrawal = await conn.fetchval("SELECT COALESCE(SUM(total_withdrawal), 0) FROM users")
        total_games = await conn.fetchval("SELECT COALESCE(SUM(games_played), 0) FROM users")
        
        pending_withdrawals = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE status = 'pending' AND type = 'withdrawal'"
        )
        pending_amount = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE status = 'pending' AND type = 'withdrawal'"
        )
        
        total_bet = await conn.fetchval("SELECT COALESCE(SUM(bet_amount), 0) FROM games")
        total_win = await conn.fetchval("SELECT COALESCE(SUM(win_amount), 0) FROM games")
        rtp_fact = (total_win / total_bet * 100) if total_bet > 0 else 0
    
    text = (
        f"**📊 СТАТИСТИКА**\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💰 Общий баланс: {total_balance:.2f} $\n"
        f"📥 Депозитов: {total_deposit:.2f} $\n"
        f"📤 Выводов: {total_withdrawal:.2f} $\n"
        f"🎮 Всего игр: {total_games}\n"
        f"⏳ Ожидает вывода: {pending_withdrawals} на {pending_amount:.2f} $\n"
        f"📊 RTP факт: {rtp_fact:.2f}%"
    )
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "⚙️ RTP")
async def admin_rtp(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    settings = await get_settings()
    
    text = (
        f"**⚙️ ТЕКУЩИЕ RTP**\n\n"
        f"🎲 КУБИК: {settings['rtp_cube']}%\n"
        f"🎯 ДАРТС: {settings['rtp_darts']}%\n"
        f"⚽ ФУТБОЛ: {settings['rtp_football']}%\n"
        f"🏀 БАСКЕТБОЛ: {settings['rtp_basketball']}%\n\n"
        f"Для изменения используйте команду:\n"
        f"/setrtp [игра] [процент]"
    )
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("setrtp"))
async def set_rtp_command(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    args = message.text.split()
    if len(args) != 3:
        await message.answer("❌ Использование: /setrtp [игра] [процент]\nИгры: cube, darts, football, basketball")
        return
    
    game = args[1].lower()
    try:
        rtp = float(args[2])
        if rtp < 1 or rtp > 100:
            await message.answer("❌ RTP должен быть от 1 до 100")
            return
        
        valid_games = ['cube', 'darts', 'football', 'basketball']
        if game not in valid_games:
            await message.answer("❌ Неверная игра. Доступны: cube, darts, football, basketball")
            return
        
        async with get_db_connection() as conn:
            await conn.execute(
                f"UPDATE settings SET rtp_{game} = $1 WHERE id = 1",
                rtp
            )
        
        await message.answer(f"✅ RTP для {game} установлен на {rtp}%")
        await log_admin_action(message.from_user.id, "set_rtp", details=f"{game}:{rtp}")
        
    except ValueError:
        await message.answer("❌ Введите корректный процент")

@dp.message(F.text == "👥 ПОЛЬЗОВАТЕЛИ")
async def admin_users(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer(
        "**👥 ПОИСК ПОЛЬЗОВАТЕЛЯ**\n\n"
        "Введите ID или username пользователя:"
    )

@dp.message(F.text.regexp(r'^\d+$'))
async def admin_find_user_by_id(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    try:
        user_id = int(message.text)
        target = await get_user(user_id)
        
        if not target:
            await message.answer("❌ Пользователь не найден")
            return
        
        text = (
            f"**👤 ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ**\n\n"
            f"🆔 ID: `{target['telegram_id']}`\n"
            f"📝 Имя: {target['first_name']}\n"
            f"📱 Username: @{target['username'] or 'нет'}\n"
            f"💰 Баланс: {target['balance']:.2f} $\n"
            f"🎮 Игр: {target['games_played']}\n"
            f"🏆 Побед: {target['games_won']}\n"
            f"📥 Депозит: {target['total_deposit']:.2f} $\n"
            f"📤 Вывод: {target['total_withdrawal']:.2f} $\n"
            f"👥 Рефералов: {target['referral_count']}\n"
            f"🚫 Заблокирован: {'Да' if target['is_blocked'] else 'Нет'}"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💰 ИЗМЕНИТЬ БАЛАНС", callback_data=f"admin_balance_user_{target['telegram_id']}")],
                [InlineKeyboardButton(text="🚫 ЗАБЛОКИРОВАТЬ" if not target['is_blocked'] else "✅ РАЗБЛОКИРОВАТЬ", 
                                    callback_data=f"admin_toggle_ban_{target['telegram_id']}")]
            ]
        )
        
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
        
    except ValueError:
        await message.answer("❌ Введите корректный ID")

@dp.message(F.text == "💰 БАЛАНСЫ")
async def admin_balances(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer(
        "**💰 УПРАВЛЕНИЕ БАЛАНСАМИ**",
        reply_markup=get_balance_management_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_balance_add")
async def admin_balance_add_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdminBalanceStates.waiting_for_user_id)
    await state.update_data(operation='add')
    await callback.message.edit_text("➕ Введите ID пользователя:")

@dp.callback_query(F.data == "admin_balance_subtract")
async def admin_balance_subtract_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdminBalanceStates.waiting_for_user_id)
    await state.update_data(operation='subtract')
    await callback.message.edit_text("➖ Введите ID пользователя:")

@dp.callback_query(lambda c: c.data and c.data.startswith('admin_balance_user_'))
async def admin_balance_user(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = int(callback.data.replace('admin_balance_user_', ''))
    await state.update_data(target_user=user_id)
    await state.set_state(AdminBalanceStates.waiting_for_amount)
    await callback.message.edit_text("Введите сумму:")

@dp.message(AdminBalanceStates.waiting_for_user_id)
async def admin_balance_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        user = await get_user(user_id)
        
        if not user:
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return
        
        data = await state.get_data()
        await state.update_data(target_user=user_id)
        await state.set_state(AdminBalanceStates.waiting_for_amount)
        
        operation = "начисления" if data.get('operation') == 'add' else "списания"
        await message.answer(
            f"👤 {user['first_name']}\n"
            f"💰 Текущий баланс: {user['balance']:.2f} $\n\n"
            f"Введите сумму для {operation}:"
        )
    except ValueError:
        await message.answer("❌ Введите корректный ID")

@dp.message(AdminBalanceStates.waiting_for_amount)
async def admin_balance_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        target_user = data['target_user']
        operation = data.get('operation', 'add')
        
        if operation == 'add':
            await update_balance(target_user, amount, 'add')
            action_text = "начислено"
        else:
            await update_balance(target_user, amount, 'subtract')
            action_text = "списано"
        
        async with get_db_connection() as conn:
            await conn.execute('''
                INSERT INTO transactions (user_id, type, amount, status, admin_id, reason)
                VALUES ($1, 'admin_operation', $2, 'completed', $3, $4)
            ''', target_user, amount, message.from_user.id, f"{action_text} администратором")
        
        await state.clear()
        await message.answer(f"✅ {action_text.upper()} {amount:.2f} $ пользователю {target_user}")
        await log_admin_action(message.from_user.id, f"balance_{operation}", target_user, f"amount:{amount}")
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.message(F.text == "🏆 ТУРНИРЫ")
async def admin_tournaments(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer(
        "**🏆 УПРАВЛЕНИЕ ТУРНИРАМИ**",
        reply_markup=get_tournament_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_tournament_create")
async def admin_tournament_create_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(TournamentStates.waiting_for_name)
    await callback.message.edit_text(
        "🏆 **СОЗДАНИЕ ТУРНИРА**\n\nВведите название:",
        parse_mode="Markdown"
    )

@dp.message(TournamentStates.waiting_for_name)
async def admin_tournament_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(TournamentStates.waiting_for_prize)
    await message.answer("💰 Введите призовой фонд в $:")

@dp.message(TournamentStates.waiting_for_prize)
async def admin_tournament_prize(message: Message, state: FSMContext):
    try:
        prize = float(message.text)
        await state.update_data(prize=prize)
        await state.set_state(TournamentStates.waiting_for_start_date)
        await message.answer("📅 Введите дату начала (ДД.ММ.ГГГГ ЧЧ:ММ):")
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.message(TournamentStates.waiting_for_start_date)
async def admin_tournament_start_date(message: Message, state: FSMContext):
    try:
        start_date = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        await state.update_data(start_date=start_date)
        await state.set_state(TournamentStates.waiting_for_end_date)
        await message.answer("📅 Введите дату окончания (ДД.ММ.ГГГГ ЧЧ:ММ):")
    except ValueError:
        await message.answer("❌ Неверный формат даты")

@dp.message(TournamentStates.waiting_for_end_date)
async def admin_tournament_end_date(message: Message, state: FSMContext):
    try:
        end_date = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        await state.update_data(end_date=end_date)
        await state.set_state(TournamentStates.waiting_for_min_bet)
        await message.answer("💰 Минимальная ставка (0 - без ограничений):")
    except ValueError:
        await message.answer("❌ Неверный формат даты")

@dp.message(TournamentStates.waiting_for_min_bet)
async def admin_tournament_min_bet(message: Message, state: FSMContext):
    try:
        min_bet = float(message.text)
        data = await state.get_data()
        
        async with get_db_connection() as conn:
            await conn.execute('''
                INSERT INTO tournaments (name, prize_pool, start_date, end_date, min_bet, status)
                VALUES ($1, $2, $3, $4, $5, 'upcoming')
            ''', data['name'], data['prize'], data['start_date'], data['end_date'], min_bet)
        
        await state.clear()
        await message.answer(f"✅ **ТУРНИР СОЗДАН**\n\nНазвание: {data['name']}\nПриз: {data['prize']} $")
        
        # Уведомляем пользователей
        await broadcast_tournament_announcement(data['name'], data['prize'], data['start_date'], data['end_date'])
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.callback_query(F.data == "admin_tournament_list")
async def admin_tournament_list(callback: CallbackQuery):
    await callback.answer()
    
    async with get_db_connection() as conn:
        tournaments = await conn.fetch('''
            SELECT * FROM tournaments ORDER BY created_at DESC LIMIT 10
        ''')
    
    if not tournaments:
        await callback.message.edit_text("📭 Турниры не найдены")
        return
    
    text = "**📋 СПИСОК ТУРНИРОВ**\n\n"
    for t in tournaments:
        start = t['start_date'].strftime("%d.%m.%Y")
        end = t['end_date'].strftime("%d.%m.%Y")
        status = {
            'upcoming': '⏳ Скоро',
            'active': '🔴 Активен',
            'finished': '✅ Завершен'
        }.get(t['status'], t['status'])
        
        text += (
            f"**{t['name']}**\n"
            f"• Статус: {status}\n"
            f"• Приз: {t['prize_pool']} $\n"
            f"• Даты: {start} - {end}\n"
            f"• Участников: {t['current_participants']}\n\n"
        )
    
    await callback.message.edit_text(text, parse_mode="Markdown")

async def broadcast_tournament_announcement(name, prize, start_date, end_date):
    """Рассылка анонса турнира"""
    start = start_date.strftime("%d.%m.%Y %H:%M")
    end = end_date.strftime("%d.%m.%Y %H:%M")
    
    text = (
        f"🏆 **НОВЫЙ ТУРНИР!**\n\n"
        f"**{name}**\n\n"
        f"💰 Приз: {prize} $\n"
        f"📅 Начало: {start}\n"
        f"📅 Конец: {end}\n\n"
        f"Участвуйте в разделе 🏆 ТУРНИРЫ"
    )
    
    async with get_db_connection() as conn:
        users = await conn.fetch("SELECT telegram_id FROM users WHERE is_blocked = 0")
    
    for user in users:
        try:
            await bot.send_message(user['telegram_id'], text, parse_mode="Markdown")
            await asyncio.sleep(0.05)
        except:
            pass

@dp.callback_query(F.data == "leaderboard")
async def show_leaderboard(callback: CallbackQuery):
    await callback.answer()
    
    async with get_db_connection() as conn:
        # Топ по балансу
        top_balance = await conn.fetch('''
            SELECT username, first_name, balance 
            FROM users 
            WHERE is_blocked = 0
            ORDER BY balance DESC 
            LIMIT 10
        ''')
        
        # Топ по выигрышам в турнирах
        top_tournament = await conn.fetch('''
            SELECT u.username, u.first_name, SUM(tp.score) as total_score
            FROM tournament_participants tp
            JOIN users u ON u.telegram_id = tp.user_id
            GROUP BY u.telegram_id, u.username, u.first_name
            ORDER BY total_score DESC
            LIMIT 10
        ''')
    
    text = "🏆 **ТАБЛИЦА ЛИДЕРОВ**\n\n"
    
    text += "**💰 ПО БАЛАНСУ:**\n"
    for i, user in enumerate(top_balance, 1):
        name = user['first_name'] or user['username'] or f"Игрок {i}"
        text += f"{i}. {name} - {user['balance']:.2f}$\n"
    
    if top_tournament:
        text += "\n**🏆 ПО ТУРНИРАМ:**\n"
        for i, user in enumerate(top_tournament, 1):
            name = user['first_name'] or user['username'] or f"Игрок {i}"
            text += f"{i}. {name} - {user['total_score']:.2f}$\n"
    
    await callback.message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📢 РАССЫЛКА")
async def broadcast_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(BroadcastStates.waiting_for_message)
    await message.answer(
        "**📢 РАССЫЛКА**\n\nОтправьте сообщение:",
        parse_mode="Markdown"
    )

@dp.message(BroadcastStates.waiting_for_message)
async def broadcast_preview(message: Message, state: FSMContext):
    await state.update_data(message=message)
    await state.set_state(BroadcastStates.waiting_for_confirmation)
    
    await message.answer(
        f"**👁 ПРЕДПРОСМОТР**\n\n{message.text or 'Медиа'}\n\nПодтвердите:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ ОТПРАВИТЬ", callback_data="broadcast_confirm"),
                    InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="broadcast_cancel")
                ]
            ]
        ),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    original_message = data['message']
    
    async with get_db_connection() as conn:
        users = await conn.fetch("SELECT telegram_id FROM users WHERE is_blocked = 0")
    
    sent = 0
    failed = 0
    
    for user in users:
        try:
            if original_message.text:
                await bot.send_message(user['telegram_id'], original_message.text)
            elif original_message.photo:
                await bot.send_photo(
                    user['telegram_id'],
                    original_message.photo[-1].file_id,
                    caption=original_message.caption
                )
            sent += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    
    await state.clear()
    await callback.message.answer(f"✅ Отправлено: {sent}, ошибок: {failed}")
    await log_admin_action(callback.from_user.id, "broadcast", details=f"sent:{sent},failed:{failed}")

@dp.message(F.text == "🎟 ПРОМОКОДЫ")
async def promo_menu_admin(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer(
        "**🎟 ПРОМОКОДЫ**",
        reply_markup=get_promo_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "➕ СОЗДАТЬ ПРОМОКОД")
async def create_promo_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer(
        "**➕ СОЗДАНИЕ ПРОМОКОДА**\n\n"
        "Введите код промокода:",
        parse_mode="Markdown"
    )

@dp.message(PromoStates.waiting_for_code)
async def create_promo_code_step(message: Message, state: FSMContext):
    await state.update_data(code=message.text.upper())
    await state.set_state(AdminBalanceStates.waiting_for_amount)
    await message.answer("Введите сумму для начисления:")

@dp.message(AdminBalanceStates.waiting_for_amount)
async def create_promo_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        
        async with get_db_connection() as conn:
            await conn.execute('''
                INSERT INTO promocodes (code, amount, max_activations, created_by)
                VALUES ($1, $2, 1, $3)
            ''', data['code'], amount, message.from_user.id)
        
        await state.clear()
        await message.answer(f"✅ Промокод {data['code']} создан на {amount:.2f} $")
        await log_admin_action(message.from_user.id, "promo_create", details=f"code:{data['code']},amount:{amount}")
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.message(F.text == "📋 СПИСОК ПРОМОКОДОВ")
async def list_promos_admin(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    async with get_db_connection() as conn:
        promos = await conn.fetch("SELECT * FROM promocodes ORDER BY created_at DESC")
    
    if not promos:
        await message.answer("📭 Промокоды не найдены")
        return
    
    text = "**📋 ПРОМОКОДЫ**\n\n"
    for promo in promos:
        expires = "бессрочно" if not promo['expires_at'] else promo['expires_at'].strftime("%d.%m.%Y")
        status = "✅" if not promo['expires_at'] or promo['expires_at'] > datetime.now() else "❌"
        
        text += (
            f"{status} `{promo['code']}`\n"
            f"   Сумма: {promo['amount']} $\n"
            f"   Активаций: {promo['current_activations']}/{promo['max_activations']}\n"
            f"   Срок: {expires}\n\n"
        )
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "❌ УДАЛИТЬ ПРОМОКОД")
async def delete_promo_start_admin(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer("Введите код промокода для удаления:")

@dp.message(PromoStates.waiting_for_code)
async def delete_promo_admin(message: Message, state: FSMContext):
    code = message.text.upper()
    
    async with get_db_connection() as conn:
        result = await conn.execute("DELETE FROM promocodes WHERE code = $1", code)
        
        if result.split()[1] == '1':  # PostgreSQL возвращает "DELETE 1" если удалено
            await message.answer(f"✅ Промокод {code} удален")
            await log_admin_action(message.from_user.id, "promo_delete", details=f"code:{code}")
        else:
            await message.answer("❌ Промокод не найден")
    
    await state.clear()

@dp.message(F.text == "📋 ЛОГИ")
async def admin_logs(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    async with get_db_connection() as conn:
        logs = await conn.fetch('''
            SELECT * FROM admin_logs 
            ORDER BY created_at DESC 
            LIMIT 20
        ''')
    
    if not logs:
        await message.answer("📭 Логи не найдены")
        return
    
    text = "**📋 ПОСЛЕДНИЕ ДЕЙСТВИЯ**\n\n"
    for log in logs:
        date = log['created_at'].strftime("%d.%m %H:%M")
        text += f"{date} - {log['action']}\n"
        if log['details']:
            text += f"   {log['details']}\n"
    
    await message.answer(text, parse_mode="Markdown")

# Запуск бота
async def main():
    logger.info("Инициализация пула соединений с PostgreSQL...")
    await init_db_pool()
    
    logger.info("Инициализация базы данных...")
    await init_database()
    
    logger.info("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
