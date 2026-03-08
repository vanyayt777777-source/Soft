"""
Telegram Casino Bot с интеграцией CryptoBot
Полная версия с реальными платежами без бонусов
"""

import os
import logging
import asyncio
import random
import string
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

import aiohttp
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

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Состояния FSM
class SetupStates(StatesGroup):
    waiting_for_casino_name = State()
    waiting_for_welcome_message = State()
    waiting_for_crypto_token = State()
    waiting_for_admin_id = State()
    waiting_for_rtp = State()
    waiting_for_referral_percent = State()

class GameStates(StatesGroup):
    waiting_for_bet = State()
    waiting_for_choice = State()

class WithdrawalStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_wallet = State()
    waiting_for_confirmation = State()

class PromoStates(StatesGroup):
    waiting_for_code = State()

class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()

class AdminBalanceStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()
    waiting_for_operation = State()
    waiting_for_reason = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

# Контекстный менеджер для работы с БД
@contextmanager
def get_db():
    conn = sqlite3.connect('casino_bot.db')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# Инициализация базы данных
async def init_database():
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Таблица настроек
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                casino_name TEXT NOT NULL,
                welcome_message TEXT NOT NULL,
                crypto_token TEXT NOT NULL,
                admin_id INTEGER NOT NULL,
                rtp REAL DEFAULT 95.0,
                referral_percent REAL DEFAULT 5.0,
                min_withdrawal REAL DEFAULT 1.0,
                max_withdrawal REAL DEFAULT 1000.0,
                is_initialized INTEGER DEFAULT 0
            )
        ''')
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                balance REAL DEFAULT 0.0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                referral_earnings REAL DEFAULT 0.0,
                referral_count INTEGER DEFAULT 0,
                total_deposit REAL DEFAULT 0.0,
                total_withdrawal REAL DEFAULT 0.0,
                total_bet REAL DEFAULT 0.0,
                total_win REAL DEFAULT 0.0,
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'USD',
                status TEXT DEFAULT 'pending',
                external_id TEXT UNIQUE,
                wallet_address TEXT,
                admin_id INTEGER,
                reason TEXT,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
            )
        ''')
        
        # Таблица игр
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game_type TEXT NOT NULL,
                bet_amount REAL NOT NULL,
                win_amount REAL DEFAULT 0,
                choice TEXT,
                result INTEGER,
                multiplier REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
            )
        ''')
        
        # Таблица промокодов (только для начисления на баланс)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                amount REAL NOT NULL,
                expires_at TIMESTAMP,
                max_activations INTEGER DEFAULT 1,
                current_activations INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица активаций промокодов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promo_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                promo_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (promo_id) REFERENCES promocodes (id),
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
            )
        ''')
        
        # Таблица логов действий админов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_user INTEGER,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (admin_id) REFERENCES users (telegram_id)
            )
        ''')
        
        conn.commit()

# Вспомогательные функции
def generate_referral_code(telegram_id: int) -> str:
    """Генерация уникального реферального кода"""
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"{telegram_id}{random_str}"

async def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Получение пользователя из БД"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

async def create_user(telegram_id: int, username: str, first_name: str, referred_by: int = None):
    """Создание нового пользователя"""
    referral_code = generate_referral_code(telegram_id)
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Проверяем, является ли пользователь админом
        cursor.execute("SELECT admin_id FROM settings WHERE id = 1")
        settings = cursor.fetchone()
        is_admin = 1 if settings and settings['admin_id'] == telegram_id else 0
        
        cursor.execute('''
            INSERT INTO users 
            (telegram_id, username, first_name, referral_code, referred_by, is_admin)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (telegram_id, username, first_name, referral_code, referred_by, is_admin))
        conn.commit()
        
        # Если есть реферер, увеличиваем его счетчик
        if referred_by:
            cursor.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE telegram_id = ?",
                (referred_by,)
            )
            conn.commit()

async def get_settings() -> Optional[Dict[str, Any]]:
    """Получение настроек бота"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM settings WHERE id = 1")
        row = cursor.fetchone()
        return dict(row) if row else None

async def is_initialized() -> bool:
    """Проверка, инициализирован ли бот"""
    settings = await get_settings()
    return settings is not None and settings.get('is_initialized') == 1

async def update_balance(telegram_id: int, amount: float, operation: str = 'add'):
    """Обновление баланса пользователя"""
    with get_db() as conn:
        cursor = conn.cursor()
        if operation == 'add':
            cursor.execute(
                "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                (amount, telegram_id)
            )
        else:
            cursor.execute(
                "UPDATE users SET balance = balance - ? WHERE telegram_id = ?",
                (amount, telegram_id)
            )
        cursor.execute(
            "UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE telegram_id = ?",
            (telegram_id,)
        )
        conn.commit()

async def log_admin_action(admin_id: int, action: str, target_user: int = None, details: str = None):
    """Логирование действий администратора"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO admin_logs (admin_id, action, target_user, details)
            VALUES (?, ?, ?, ?)
        ''', (admin_id, action, target_user, details))
        conn.commit()

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
            [KeyboardButton(text="👥 РЕФЕРАЛЫ"), KeyboardButton(text="💬 ПОДДЕРЖКА")],
            [KeyboardButton(text="🔙 НАЗАД")]
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
            [InlineKeyboardButton(text="🎟 АКТИВИРОВАТЬ ПРОМОКОД", callback_data="activate_promo")],
            [InlineKeyboardButton(text="📊 МОЯ СТАТИСТИКА", callback_data="my_stats")]
        ]
    )
    return keyboard

def get_games_keyboard():
    """Клавиатура выбора игры"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 КУБИК x2"), KeyboardButton(text="⚽ ФУТБОЛ x2")],
            [KeyboardButton(text="🏀 БАСКЕТБОЛ x2"), KeyboardButton(text="🎯 ДАРТС x2")],
            [KeyboardButton(text="🔙 НАЗАД")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_game_choices_keyboard(game: str):
    """Клавиатура выбора исхода игры"""
    game_buttons = {
        "🎲 КУБИК x2": [
            [InlineKeyboardButton(text="📈 БОЛЬШЕ (4-6) x2", callback_data="choice_more")],
            [InlineKeyboardButton(text="📉 МЕНЬШЕ (1-3) x2", callback_data="choice_less")]
        ],
        "🎯 ДАРТС x2": [
            [InlineKeyboardButton(text="🎯 В ЦЕЛЬ x2", callback_data="choice_target")],
            [InlineKeyboardButton(text="💨 МИМО x2", callback_data="choice_miss")]
        ],
        "⚽ ФУТБОЛ x2": [
            [InlineKeyboardButton(text="⚽ ГОЛ x2", callback_data="choice_goal")],
            [InlineKeyboardButton(text="🥅 ПРОМАХ x2", callback_data="choice_miss")]
        ],
        "🏀 БАСКЕТБОЛ x2": [
            [InlineKeyboardButton(text="🏀 ПОПАДАНИЕ x2", callback_data="choice_score")],
            [InlineKeyboardButton(text="💫 ПРОМАХ x2", callback_data="choice_miss_basket")]
        ]
    }
    
    buttons = game_buttons.get(game, [])
    buttons.append([InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="cancel_game")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard():
    """Админ-панель"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 СТАТИСТИКА"), KeyboardButton(text="👥 ПОЛЬЗОВАТЕЛИ")],
            [KeyboardButton(text="💰 БАЛАНСЫ"), KeyboardButton(text="🎟 ПРОМОКОДЫ")],
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
            [InlineKeyboardButton(text="➖ СПИСАТЬ", callback_data="admin_balance_subtract")],
            [InlineKeyboardButton(text="🔍 НАЙТИ ПОЛЬЗОВАТЕЛЯ", callback_data="admin_find_user")]
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
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT telegram_id FROM users WHERE referral_code = ?",
                    (args[1],)
                )
                result = cursor.fetchone()
                if result and result['telegram_id'] != telegram_id:
                    referred_by = result['telegram_id']
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
        f"🆔 **Ваш ID:** `{telegram_id}`\n"
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
        await state.set_state(SetupStates.waiting_for_rtp)
        await message.answer("Введите RTP (Return to Player) в процентах (1-100):")
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректный ID:")

@dp.message(SetupStates.waiting_for_rtp)
async def setup_rtp(message: Message, state: FSMContext):
    try:
        rtp = float(message.text)
        if 1 <= rtp <= 100:
            await state.update_data(rtp=rtp)
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
            
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO settings 
                    (id, casino_name, welcome_message, crypto_token, admin_id, rtp, referral_percent, is_initialized)
                    VALUES (1, ?, ?, ?, ?, ?, ?, 1)
                ''', (
                    data['casino_name'],
                    data['welcome_message'],
                    data['crypto_token'],
                    data['admin_id'],
                    data['rtp'],
                    referral_percent
                ))
                conn.commit()
            
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM users WHERE referred_by = ?",
            (telegram_id,)
        )
        referrals = cursor.fetchone()['count']
    
    text = (
        f"**👤 ПРОФИЛЬ**\n\n"
        f"🆔 **ID:** `{telegram_id}`\n"
        f"💰 **Баланс:** `{user['balance']:.2f} $`\n"
        f"🎮 **Сыграно игр:** {user['games_played']}\n"
        f"🏆 **Побед:** {user['games_won']}\n"
        f"📊 **Всего пополнено:** {user['total_deposit']:.2f} $\n"
        f"💸 **Всего выведено:** {user['total_withdrawal']:.2f} $\n"
        f"👥 **Рефералов:** {referrals}\n"
        f"💰 **Заработано с рефералов:** {user['referral_earnings']:.2f} $"
    )
    
    await message.answer(text, reply_markup=get_profile_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "👥 РЕФЕРАЛЫ")
async def referral_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)
    
    if not user:
        await cmd_start(message)
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username, first_name, created_at, total_deposit FROM users WHERE referred_by = ? ORDER BY created_at DESC LIMIT 5",
            (telegram_id,)
        )
        referrals = cursor.fetchall()
    
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
    await message.answer("✅ Сообщение отправлено администратору")

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
    
    # Отправляем дайс
    emoji = "🎲" if "КУБИК" in game else "🎯" if "ДАРТС" in game else "⚽" if "ФУТБОЛ" in game else "🏀"
    msg = await callback.message.answer_dice(emoji=emoji)
    result = msg.dice.value
    
    await asyncio.sleep(2)
    
    # Определяем победу
    win = False
    if "КУБИК" in game:
        win = (result > 3 and callback.data == 'choice_more') or (result < 4 and callback.data == 'choice_less')
    elif "ДАРТС" in game:
        win = (result in [5, 6] and callback.data == 'choice_target') or (result in [1, 2, 3, 4] and callback.data == 'choice_miss')
    elif "ФУТБОЛ" in game:
        win = (result in [4, 5] and callback.data == 'choice_goal') or (result in [1, 2, 3] and callback.data == 'choice_miss')
    elif "БАСКЕТБОЛ" in game:
        win = (result in [4, 5] and callback.data == 'choice_score') or (result in [1, 2, 3] and callback.data == 'choice_miss_basket')
    
    if win:
        win_amount = bet * 2
        await update_balance(telegram_id, win_amount, 'add')
        
        # Начисление реферальных (процент от проигрыша)
        user = await get_user(telegram_id)
        if user and user['referred_by']:
            settings = await get_settings()
            referral_earning = bet * settings['referral_percent'] / 100
            if referral_earning > 0:
                await update_balance(user['referred_by'], referral_earning, 'add')
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE users SET referral_earnings = referral_earnings + ? WHERE telegram_id = ?",
                        (referral_earning, user['referred_by'])
                    )
                    conn.commit()
        
        await callback.message.answer(f"🎉 **ВЫ ВЫИГРАЛИ {win_amount:.2f} $!**")
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET games_won = games_won + 1, total_win = total_win + ? WHERE telegram_id = ?",
                (win_amount, telegram_id)
            )
            conn.commit()
    else:
        await callback.message.answer(f"😢 **ВЫ ПРОИГРАЛИ {bet:.2f} $**")
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET total_bet = total_bet + ? WHERE telegram_id = ?",
                (bet, telegram_id)
            )
            conn.commit()
        win_amount = 0
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO games (user_id, game_type, bet_amount, win_amount, choice, result, multiplier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (telegram_id, game, bet, win_amount, callback.data, result, 2 if win else 0))
        
        cursor.execute(
            "UPDATE users SET games_played = games_played + 1 WHERE telegram_id = ?",
            (telegram_id,)
        )
        conn.commit()
    
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
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO transactions (user_id, type, amount, external_id, status)
                    VALUES (?, 'deposit', ?, ?, 'pending')
                ''', (message.from_user.id, amount, str(invoice['invoice_id'])))
                conn.commit()
            
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
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE transactions SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE external_id = ?",
                (str(invoice_id),)
            )
            cursor.execute(
                "UPDATE users SET balance = balance + (SELECT amount FROM transactions WHERE external_id = ?), total_deposit = total_deposit + (SELECT amount FROM transactions WHERE external_id = ?) WHERE telegram_id = ?",
                (str(invoice_id), str(invoice_id), callback.from_user.id)
            )
            conn.commit()
        
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transactions (user_id, type, amount, wallet_address, status)
            VALUES (?, 'withdrawal', ?, ?, 'pending')
        ''', (telegram_id, amount, wallet))
        
        cursor.execute(
            "UPDATE users SET balance = balance - ? WHERE telegram_id = ?",
            (amount, telegram_id)
        )
        conn.commit()
    
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM promocodes WHERE code = ?", (code,))
        promo = cursor.fetchone()
        
        if not promo:
            await message.answer("❌ **ПРОМОКОД НЕ НАЙДЕН**")
            await state.clear()
            return
        
        if promo['expires_at']:
            expires_at = datetime.fromisoformat(promo['expires_at'].replace('Z', '+00:00'))
            if expires_at < datetime.now():
                await message.answer("❌ **СРОК ДЕЙСТВИЯ ИСТЕК**")
                await state.clear()
                return
        
        if promo['current_activations'] >= promo['max_activations']:
            await message.answer("❌ **ЛИМИТ АКТИВАЦИЙ ИСЧЕРПАН**")
            await state.clear()
            return
        
        cursor.execute(
            "SELECT * FROM promo_activations WHERE promo_id = ? AND user_id = ?",
            (promo['id'], message.from_user.id)
        )
        if cursor.fetchone():
            await message.answer("❌ **ПРОМОКОД УЖЕ АКТИВИРОВАН**")
            await state.clear()
            return
        
        # Начисляем сумму на баланс
        await update_balance(message.from_user.id, promo['amount'], 'add')
        
        cursor.execute(
            "UPDATE promocodes SET current_activations = current_activations + 1 WHERE id = ?",
            (promo['id'],)
        )
        
        cursor.execute(
            "INSERT INTO promo_activations (promo_id, user_id) VALUES (?, ?)",
            (promo['id'], message.from_user.id)
        )
        conn.commit()
    
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) as count FROM users")
        total_users = cursor.fetchone()['count']
        
        cursor.execute("SELECT SUM(balance) as sum FROM users")
        total_balance = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(total_deposit) as sum FROM users")
        total_deposit = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(total_withdrawal) as sum FROM users")
        total_withdrawal = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(games_played) as sum FROM users")
        total_games = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT COUNT(*) as count FROM transactions WHERE status = 'pending' AND type = 'withdrawal'")
        pending_withdrawals = cursor.fetchone()['count']
        
        cursor.execute("SELECT SUM(amount) as sum FROM transactions WHERE status = 'pending' AND type = 'withdrawal'")
        pending_amount = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(bet_amount) as total_bet, SUM(win_amount) as total_win FROM games")
        games_stats = cursor.fetchone()
        total_bet = games_stats['total_bet'] or 0
        total_win = games_stats['total_win'] or 0
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

@dp.message(F.text == "👥 ПОЛЬЗОВАТЕЛИ")
async def admin_users(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 НАЙТИ ПОЛЬЗОВАТЕЛЯ", callback_data="admin_find_user")],
            [InlineKeyboardButton(text="🚫 ЗАБЛОКИРОВАТЬ", callback_data="admin_ban_user")],
            [InlineKeyboardButton(text="✅ РАЗБЛОКИРОВАТЬ", callback_data="admin_unban_user")]
        ]
    )
    
    await message.answer(
        "**👥 УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

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

@dp.message(AdminBalanceStates.waiting_for_user_id)
async def admin_balance_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        user = await get_user(user_id)
        
        if not user:
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return
        
        await state.update_data(target_user=user_id)
        await state.set_state(AdminBalanceStates.waiting_for_amount)
        
        await message.answer(
            f"👤 {user['first_name']}\n"
            f"💰 Текущий баланс: {user['balance']:.2f} $\n\n"
            f"Введите сумму:"
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
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO transactions (user_id, type, amount, status, admin_id, reason)
                VALUES (?, 'admin_operation', ?, 'completed', ?, ?)
            ''', (target_user, amount, message.from_user.id, f"{action_text} администратором"))
            conn.commit()
        
        await state.clear()
        await message.answer(f"✅ {action_text.upper()} {amount:.2f} $ пользователю {target_user}")
        await log_admin_action(message.from_user.id, f"balance_{operation}", target_user, f"amount:{amount}")
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.message(F.text == "📢 РАССЫЛКА")
async def broadcast_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(BroadcastStates.waiting_for_message)
    await message.answer(
        "**📢 РАССЫЛКА**\n\n"
        "Отправьте сообщение:",
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM users WHERE is_blocked = 0")
        users = cursor.fetchall()
    
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
    
    await state.set_state(AdminBalanceStates.waiting_for_user_id)
    await message.answer(
        "**➕ СОЗДАНИЕ ПРОМОКОДА**\n\n"
        "Введите код промокода:",
        parse_mode="Markdown"
    )

@dp.message(AdminBalanceStates.waiting_for_user_id)
async def create_promo_code_step(message: Message, state: FSMContext):
    await state.update_data(code=message.text.upper())
    await state.set_state(AdminBalanceStates.waiting_for_amount)
    await message.answer("Введите сумму для начисления:")

@dp.message(AdminBalanceStates.waiting_for_amount)
async def create_promo_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO promocodes (code, amount, max_activations)
                VALUES (?, ?, 1)
            ''', (data['code'], amount))
            conn.commit()
        
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM promocodes ORDER BY created_at DESC")
        promos = cursor.fetchall()
    
    if not promos:
        await message.answer("📭 Промокоды не найдены")
        return
    
    text = "**📋 ПРОМОКОДЫ**\n\n"
    for promo in promos:
        expires = "бессрочно" if not promo['expires_at'] else promo['expires_at'][:10]
        status = "✅" if not promo['expires_at'] or datetime.fromisoformat(promo['expires_at']) > datetime.now() else "❌"
        
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM promocodes WHERE code = ?", (code,))
        conn.commit()
        
        if cursor.rowcount > 0:
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
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM admin_logs 
            ORDER BY created_at DESC 
            LIMIT 20
        ''')
        logs = cursor.fetchall()
    
    if not logs:
        await message.answer("📭 Логи не найдены")
        return
    
    text = "**📋 ПОСЛЕДНИЕ ДЕЙСТВИЯ**\n\n"
    for log in logs:
        date = datetime.fromisoformat(log['created_at']).strftime("%d.%m %H:%M")
        text += f"{date} - {log['action']}\n"
        if log['details']:
            text += f"   {log['details']}\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "my_stats")
async def my_stats(callback: CallbackQuery):
    await callback.answer()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT game_type, COUNT(*) as count, SUM(bet_amount) as total_bet, SUM(win_amount) as total_win
            FROM games 
            WHERE user_id = ?
            GROUP BY game_type
        ''', (callback.from_user.id,))
        game_stats = cursor.fetchall()
        
        cursor.execute('''
            SELECT game_type, bet_amount, win_amount, created_at 
            FROM games 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 5
        ''', (callback.from_user.id,))
        last_games = cursor.fetchall()
    
    text = "**📊 ДЕТАЛЬНАЯ СТАТИСТИКА**\n\n"
    
    if game_stats:
        text += "**По играм:**\n"
        for stat in game_stats:
            profit = stat['total_win'] - stat['total_bet']
            text += f"• {stat['game_type']}: {stat['count']} игр, {profit:+.2f} $\n"
    
    if last_games:
        text += "\n**Последние игры:**\n"
        for game in last_games:
            date = datetime.fromisoformat(game['created_at']).strftime("%d.%m %H:%M")
            result = "✅" if game['win_amount'] > 0 else "❌"
            text += f"• {date} {game['game_type']}: {game['bet_amount']:.2f}$ {result}\n"
    
    await callback.message.answer(text, parse_mode="Markdown")

# Запуск бота
async def main():
    logger.info("Инициализация базы данных...")
    await init_database()
    
    logger.info("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
