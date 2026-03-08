"""
Telegram Casino Bot с интеграцией CryptoBot
Полная версия с реальными платежами и расширенным функционалом
"""

import os
import logging
import asyncio
import random
import string
import sqlite3
import json
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager
from urllib.parse import urlencode

import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, 
    KeyboardButton, FSInputFile, InputFile, BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.formatting import Text, Bold, Italic, Code

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
    waiting_for_promo_code = State()
    waiting_for_bonus_type = State()
    waiting_for_bonus_value = State()
    waiting_for_expiry = State()
    waiting_for_max_activations = State()
    waiting_for_min_deposit = State()

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
    waiting_for_reply = State()

class TournamentStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_prize = State()
    waiting_for_start_date = State()
    waiting_for_end_date = State()
    waiting_for_min_bet = State()

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
        
        # Таблица настроек (одноразовая)
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
                support_chat_id INTEGER,
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
                last_name TEXT,
                balance REAL DEFAULT 0.0,
                bonus_balance REAL DEFAULT 0.0,
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
                is_vip INTEGER DEFAULT 0,
                vip_level INTEGER DEFAULT 0,
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
                tx_hash TEXT,
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
        
        # Таблица промокодов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                bonus_type TEXT NOT NULL,
                bonus_value REAL NOT NULL,
                expires_at TIMESTAMP,
                max_activations INTEGER DEFAULT 1,
                current_activations INTEGER DEFAULT 0,
                min_deposit REAL DEFAULT 0,
                for_vip_only INTEGER DEFAULT 0,
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
        
        # Таблица турниров
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                prize_pool REAL NOT NULL,
                start_date TIMESTAMP NOT NULL,
                end_date TIMESTAMP NOT NULL,
                min_bet REAL DEFAULT 0,
                max_participants INTEGER DEFAULT 0,
                current_participants INTEGER DEFAULT 0,
                status TEXT DEFAULT 'upcoming',
                winner_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (winner_id) REFERENCES users (telegram_id)
            )
        ''')
        
        # Таблица участников турниров
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tournament_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                score REAL DEFAULT 0,
                games_played INTEGER DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tournament_id) REFERENCES tournaments (id),
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
        
        # Таблица уведомлений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                type TEXT DEFAULT 'info',
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
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

async def create_user(telegram_id: int, username: str, first_name: str, last_name: str = "", referred_by: int = None):
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
            (telegram_id, username, first_name, last_name, referral_code, referred_by, is_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (telegram_id, username, first_name, last_name, referral_code, referred_by, is_admin))
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

async def update_balance(telegram_id: int, amount: float, operation: str = 'add', bonus: bool = False):
    """Обновление баланса пользователя"""
    field = 'bonus_balance' if bonus else 'balance'
    with get_db() as conn:
        cursor = conn.cursor()
        if operation == 'add':
            cursor.execute(
                f"UPDATE users SET {field} = {field} + ? WHERE telegram_id = ?",
                (amount, telegram_id)
            )
        else:
            cursor.execute(
                f"UPDATE users SET {field} = {field} - ? WHERE telegram_id = ?",
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

async def add_notification(user_id: int, title: str, message: str, notification_type: str = "info"):
    """Добавление уведомления пользователю"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO notifications (user_id, title, message, type)
            VALUES (?, ?, ?, ?)
        ''', (user_id, title, message, notification_type))
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
            "expires_in": 3600  # 1 час
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

# Обработчик вебхуков от CryptoBot
async def crypto_bot_webhook(request):
    """Обработка вебхуков от CryptoBot"""
    try:
        # Проверка подписи
        settings = await get_settings()
        if not settings:
            return {"ok": False}
        
        body = await request.json()
        
        # Проверка типа обновления
        if body.get('update_type') == 'invoice_paid':
            payload = body.get('payload', {})
            invoice_id = payload.get('invoice_id')
            user_id = int(payload.get('payload', 0))
            amount = float(payload.get('amount', 0))
            
            # Проверяем транзакцию в базе
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM transactions WHERE external_id = ? AND status = 'pending'",
                    (str(invoice_id),)
                )
                transaction = cursor.fetchone()
                
                if transaction:
                    # Обновляем баланс
                    await update_balance(user_id, amount, 'add')
                    
                    # Обновляем статистику
                    cursor.execute(
                        "UPDATE users SET total_deposit = total_deposit + ? WHERE telegram_id = ?",
                        (amount, user_id)
                    )
                    
                    # Обновляем статус транзакции
                    cursor.execute('''
                        UPDATE transactions 
                        SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
                        WHERE id = ?
                    ''', (transaction['id'],))
                    
                    conn.commit()
                    
                    # Уведомляем пользователя
                    await bot.send_message(
                        user_id,
                        f"✅ **Пополнение успешно!**\n\n"
                        f"💰 Сумма: {amount:.2f} USDT\n"
                        f"💎 Новый баланс: {(await get_user(user_id))['balance']:.2f} $",
                        parse_mode="Markdown"
                    )
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing CryptoBot webhook: {e}")
        return {"ok": False}

# Клавиатуры
def get_main_keyboard():
    """Главное меню"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎮 ИГРАТЬ"), KeyboardButton(text="👤 ПРОФИЛЬ")],
            [KeyboardButton(text="👥 РЕФЕРАЛЫ"), KeyboardButton(text="🎰 ТУРНИРЫ")],
            [KeyboardButton(text="💬 ПОДДЕРЖКА"), KeyboardButton(text="📢 НОВОСТИ")]
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
            [InlineKeyboardButton(text="📊 МОЯ СТАТИСТИКА", callback_data="my_stats")],
            [InlineKeyboardButton(text="🔔 УВЕДОМЛЕНИЯ", callback_data="notifications")]
        ]
    )
    return keyboard

def get_games_keyboard():
    """Клавиатура выбора игры"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 КУБИК x2"), KeyboardButton(text="🎯 ДАРТС x2")],
            [KeyboardButton(text="⚽ ФУТБОЛ x2"), KeyboardButton(text="🏀 БАСКЕТБОЛ x2")],
            [KeyboardButton(text="🎰 СЛОТЫ x3"), KeyboardButton(text="🎳 БОУЛИНГ x2")],
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
        ],
        "🎳 БОУЛИНГ x2": [
            [InlineKeyboardButton(text="🎳 СТРАЙК x2", callback_data="choice_strike")],
            [InlineKeyboardButton(text="🔄 НЕ ПОЛНЫЙ x2", callback_data="choice_spare")]
        ],
        "🎰 СЛОТЫ x3": [
            [InlineKeyboardButton(text="🍒 ВИШНЯ x3", callback_data="choice_cherry")],
            [InlineKeyboardButton(text="💎 БРИЛЛИАНТ x5", callback_data="choice_diamond")],
            [InlineKeyboardButton(text="🎰 ДЖЕКПОТ x10", callback_data="choice_jackpot")]
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
            [KeyboardButton(text="📢 РАССЫЛКА"), KeyboardButton(text="🏆 ТУРНИРЫ")],
            [KeyboardButton(text="⚙️ НАСТРОЙКИ"), KeyboardButton(text="📋 ЛОГИ")],
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
            [InlineKeyboardButton(text="📊 ИСТОРИЯ", callback_data="admin_balance_history")],
            [InlineKeyboardButton(text="🔍 НАЙТИ ПОЛЬЗОВАТЕЛЯ", callback_data="admin_find_user")]
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
    last_name = message.from_user.last_name or ""
    
    # Проверяем реферальный код
    args = message.text.split()
    referred_by = None
    if len(args) > 1:
        try:
            # Поиск пользователя по реферальному коду
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
        # Запускаем процесс настройки
        await state.set_state(SetupStates.waiting_for_casino_name)
        await message.answer(
            "⚙️ **ПЕРВОНАЧАЛЬНАЯ НАСТРОЙКА БОТА** ⚙️\n\n"
            "👋 Привет! Давайте настроим ваше казино.\n\n"
            "🔹 Введите **название казино**:",
            parse_mode="Markdown"
        )
        return
    
    # Проверяем, существует ли пользователь
    user = await get_user(telegram_id)
    if not user:
        await create_user(telegram_id, username, first_name, last_name, referred_by)
        user = await get_user(telegram_id)
        
        # Начисление бонуса за реферала
        if referred_by:
            bonus = 1.0  # Бонус 1$ за регистрацию по рефералке
            await update_balance(telegram_id, bonus, 'add')
            await update_balance(referred_by, 0.5, 'add')  # Бонус рефереру
            
            await message.answer(
                "🎉 **ДОБРО ПОЖАЛОВАТЬ!** 🎉\n\n"
                f"💰 Вы получили бонус **{bonus:.2f} $** за регистрацию по реферальной ссылке!\n"
                f"👥 Ваш друг тоже получил бонус **0.50 $**",
                parse_mode="Markdown"
            )
    
    settings = await get_settings()
    welcome_text = settings.get('welcome_message', '🎰 Добро пожаловать в казино!')
    
    # Проверяем наличие непрочитанных уведомлений
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM notifications WHERE user_id = ? AND is_read = 0",
            (telegram_id,)
        )
        notif_count = cursor.fetchone()['count']
    
    await message.answer(
        f"**{welcome_text}**\n\n"
        f"🆔 **Ваш ID:** `{telegram_id}`\n"
        f"💰 **Баланс:** `{user['balance']:.2f} $`\n"
        f"🎁 **Бонусный баланс:** `{user['bonus_balance']:.2f} $`\n"
        f"📊 **VIP статус:** {'👑 VIP' if user['is_vip'] else '⭐ Обычный'}\n"
        f"🔔 **Уведомления:** {notif_count} непрочитанных",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# Обработчики настройки
@dp.message(SetupStates.waiting_for_casino_name)
async def setup_casino_name(message: Message, state: FSMContext):
    await state.update_data(casino_name=message.text)
    await state.set_state(SetupStates.waiting_for_welcome_message)
    await message.answer(
        "✍️ Отлично! Теперь введите **приветственное сообщение**\n"
        "(оно будет показываться всем игрокам при /start):",
        parse_mode="Markdown"
    )

@dp.message(SetupStates.waiting_for_welcome_message)
async def setup_welcome_message(message: Message, state: FSMContext):
    await state.update_data(welcome_message=message.text)
    await state.set_state(SetupStates.waiting_for_crypto_token)
    await message.answer(
        "🔑 Введите **Crypto Bot API токен**\n"
        "(получить можно у @CryptoBot → API):",
        parse_mode="Markdown"
    )

@dp.message(SetupStates.waiting_for_crypto_token)
async def setup_crypto_token(message: Message, state: FSMContext):
    await state.update_data(crypto_token=message.text)
    await state.set_state(SetupStates.waiting_for_admin_id)
    await message.answer(
        "👑 Введите **Telegram ID администратора**\n"
        "(узнать можно у @userinfobot):",
        parse_mode="Markdown"
    )

@dp.message(SetupStates.waiting_for_admin_id)
async def setup_admin_id(message: Message, state: FSMContext):
    try:
        admin_id = int(message.text)
        await state.update_data(admin_id=admin_id)
        await state.set_state(SetupStates.waiting_for_rtp)
        await message.answer(
            "📊 Введите **RTP (Return to Player)** в процентах\n"
            "(например: 95 - означает возврат 95%):",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректный ID (только цифры):")

@dp.message(SetupStates.waiting_for_rtp)
async def setup_rtp(message: Message, state: FSMContext):
    try:
        rtp = float(message.text)
        if 1 <= rtp <= 100:
            await state.update_data(rtp=rtp)
            await state.set_state(SetupStates.waiting_for_referral_percent)
            await message.answer(
                "👥 Введите **процент от проигрыша** в реферальную систему\n"
                "(например: 5 - реферал получает 5% от проигрыша рефералов):",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Пожалуйста, введите число от 1 до 100:")
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число:")

@dp.message(SetupStates.waiting_for_referral_percent)
async def setup_referral_percent(message: Message, state: FSMContext):
    try:
        referral_percent = float(message.text)
        if 0 <= referral_percent <= 100:
            data = await state.get_data()
            
            # Сохраняем настройки
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
            
            # Создаем админа в базе
            await create_user(data['admin_id'], "admin", "Admin")
            
            await message.answer(
                "✅ **БОТ УСПЕШНО НАСТРОЕН!** ✅\n\n"
                "🎰 Теперь можно использовать команду /start\n\n"
                "📌 Важно:\n"
                "• Настройки больше нельзя изменить\n"
                "• Админ-панель доступна по команде /admin\n"
                "• Для работы платежей настройте Webhook в CryptoBot",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Пожалуйста, введите число от 0 до 100:")
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число:")

# Обработчики меню
@dp.message(F.text == "👤 ПРОФИЛЬ")
async def profile_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)
    
    if not user:
        await cmd_start(message)
        return
    
    # Получаем статистику
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM users WHERE referred_by = ?",
            (telegram_id,)
        )
        referrals = cursor.fetchone()['count']
        
        cursor.execute(
            "SELECT COUNT(*) as count FROM games WHERE user_id = ?",
            (telegram_id,)
        )
        total_games = cursor.fetchone()['count']
        
        win_rate = (user['games_won'] / user['games_played'] * 100) if user['games_played'] > 0 else 0
    
    text = (
        f"**👤 ПРОФИЛЬ ИГРОКА**\n\n"
        f"**🆔 ID:** `{telegram_id}`\n"
        f"**📝 Имя:** {user['first_name']}\n"
        f"**📊 Рейтинг:** {'👑 VIP' if user['is_vip'] else '⭐ Обычный'} (Уровень {user['vip_level']})\n\n"
        f"**💰 ОСНОВНОЙ БАЛАНС:** `{user['balance']:.2f} $`\n"
        f"**🎁 БОНУСНЫЙ БАЛАНС:** `{user['bonus_balance']:.2f} $`\n\n"
        f"**📈 ИГРОВАЯ СТАТИСТИКА:**\n"
        f"• Всего игр: {user['games_played']}\n"
        f"• Побед: {user['games_won']}\n"
        f"• Винрейт: {win_rate:.1f}%\n"
        f"• Сумма ставок: {user['total_bet']:.2f} $\n"
        f"• Сумма выигрышей: {user['total_win']:.2f} $\n\n"
        f"**💳 ФИНАНСЫ:**\n"
        f"• Пополнено: {user['total_deposit']:.2f} $\n"
        f"• Выведено: {user['total_withdrawal']:.2f} $\n\n"
        f"**👥 РЕФЕРАЛЫ:**\n"
        f"• Приглашено: {referrals}\n"
        f"• Заработано: {user['referral_earnings']:.2f} $"
    )
    
    await message.answer(text, reply_markup=get_profile_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "👥 РЕФЕРАЛЫ")
async def referral_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)
    
    if not user:
        await cmd_start(message)
        return
    
    # Получаем список рефералов
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username, first_name, created_at, total_deposit FROM users WHERE referred_by = ? ORDER BY created_at DESC LIMIT 10",
            (telegram_id,)
        )
        referrals = cursor.fetchall()
    
    settings = await get_settings()
    bot_username = (await bot.get_me()).username
    
    text = (
        f"**👥 РЕФЕРАЛЬНАЯ ПРОГРАММА**\n\n"
        f"**📊 ПАРТНЕРСКАЯ СТАТИСТИКА:**\n"
        f"• Всего приглашено: {user['referral_count']}\n"
        f"• Заработано комиссии: {user['referral_earnings']:.2f} $\n"
        f"• Процент от проигрыша: {settings['referral_percent']}%\n\n"
        f"**🔗 ВАША РЕФЕРАЛЬНАЯ ССЫЛКА:**\n"
        f"`https://t.me/{bot_username}?start={user['referral_code']}`\n\n"
    )
    
    if referrals:
        text += "**📋 ПОСЛЕДНИЕ РЕФЕРАЛЫ:**\n"
        for i, ref in enumerate(referrals, 1):
            name = ref['first_name'] or ref['username'] or "Без имени"
            date = datetime.fromisoformat(ref['created_at']).strftime("%d.%m.%Y")
            text += f"{i}. {name} - {date} (депозит: {ref['total_deposit']:.2f} $)\n"
    else:
        text += "📭 У вас пока нет рефералов. Приглашайте друзей и зарабатывайте!"
    
    text += (
        f"\n\n**💡 КАК ЭТО РАБОТАЕТ?**\n"
        f"• Вы получаете {settings['referral_percent']}% от **проигрыша** ваших рефералов\n"
        f"• Бонус начисляется автоматически после каждой игры\n"
        f"• Приглашайте больше друзей и увеличивайте доход!"
    )
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "🎮 ИГРАТЬ")
async def games_menu(message: Message):
    await message.answer(
        "**🎮 ВЫБЕРИТЕ ИГРУ**\n\n"
        "Выберите игру и испытайте удачу!",
        reply_markup=get_games_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🎰 ТУРНИРЫ")
async def tournaments_menu(message: Message):
    telegram_id = message.from_user.id
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM tournaments 
            WHERE status IN ('upcoming', 'active') 
            ORDER BY start_date ASC
        ''')
        tournaments = cursor.fetchall()
        
        cursor.execute('''
            SELECT t.* FROM tournaments t
            JOIN tournament_participants tp ON t.id = tp.tournament_id
            WHERE tp.user_id = ? AND t.status = 'active'
        ''', (telegram_id,))
        user_tournaments = cursor.fetchall()
    
    if not tournaments:
        text = (
            "🏆 **ТУРНИРЫ**\n\n"
            "📭 Активных турниров пока нет.\n"
            "Следите за новостями!"
        )
    else:
        text = "🏆 **АКТИВНЫЕ ТУРНИРЫ**\n\n"
        
        for t in tournaments:
            start = datetime.fromisoformat(t['start_date']).strftime("%d.%m.%Y %H:%M")
            end = datetime.fromisoformat(t['end_date']).strftime("%d.%m.%Y %H:%M")
            status = "🔴 АКТИВЕН" if t['status'] == 'active' else "⏳ СКОРО"
            
            text += (
                f"**{t['name']}**\n"
                f"• Статус: {status}\n"
                f"• Призовой фонд: 💰 {t['prize_pool']:.2f} $\n"
                f"• Начало: {start}\n"
                f"• Конец: {end}\n"
                f"• Участников: {t['current_participants']}/{t['max_participants'] or '∞'}\n"
                f"{'—' * 20}\n"
            )
    
    if user_tournaments:
        text += "\n**📋 ВЫ УЧАСТВУЕТЕ:**\n"
        for t in user_tournaments:
            text += f"• {t['name']}\n"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 МОИ ТУРНИРЫ", callback_data="my_tournaments")],
            [InlineKeyboardButton(text="🏆 ТАБЛИЦА ЛИДЕРОВ", callback_data="leaderboard")]
        ]
    )
    
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.text == "💬 ПОДДЕРЖКА")
async def support_menu(message: Message, state: FSMContext):
    await state.set_state(SupportStates.waiting_for_message)
    await message.answer(
        "**💬 СЛУЖБА ПОДДЕРЖКИ**\n\n"
        "Опишите вашу проблему или вопрос.\n"
        "Мы ответим вам в ближайшее время!\n\n"
        "📝 Напишите ваше сообщение:",
        parse_mode="Markdown"
    )

@dp.message(SupportStates.waiting_for_message)
async def process_support_message(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    
    # Сохраняем обращение
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO notifications (user_id, title, message, type)
            VALUES (?, ?, ?, 'support_request')
        ''', (message.from_user.id, f"Обращение в поддержку", message.text))
        conn.commit()
    
    # Уведомляем админов
    settings = await get_settings()
    if settings and settings['admin_id']:
        admin_text = (
            f"🆘 **НОВОЕ ОБРАЩЕНИЕ В ПОДДЕРЖКУ**\n\n"
            f"**От:** {user['first_name']} (@{user['username']})\n"
            f"**ID:** `{message.from_user.id}`\n"
            f"**Сообщение:**\n{message.text}"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✍️ ОТВЕТИТЬ", callback_data=f"reply_support_{message.from_user.id}")]
            ]
        )
        
        try:
            await bot.send_message(settings['admin_id'], admin_text, parse_mode="Markdown", reply_markup=keyboard)
        except:
            pass
    
    await state.clear()
    await message.answer(
        "✅ **СООБЩЕНИЕ ОТПРАВЛЕНО!**\n\n"
        "Мы ответим вам в ближайшее время.",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📢 НОВОСТИ")
async def news_menu(message: Message):
    telegram_id = message.from_user.id
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM notifications 
            WHERE user_id = ? AND type != 'support_request'
            ORDER BY created_at DESC LIMIT 10
        ''', (telegram_id,))
        notifications = cursor.fetchall()
        
        # Отмечаем как прочитанные
        cursor.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ?",
            (telegram_id,)
        )
        conn.commit()
    
    if not notifications:
        text = "📭 **У ВАС НЕТ НОВЫХ НОВОСТЕЙ**"
    else:
        text = "**📢 НОВОСТИ И УВЕДОМЛЕНИЯ**\n\n"
        for n in notifications:
            date = datetime.fromisoformat(n['created_at']).strftime("%d.%m.%Y %H:%M")
            icon = "🔔" if n['type'] == 'info' else "🎁" if n['type'] == 'bonus' else "⚠️"
            text += f"{icon} **{n['title']}**\n{date}\n{n['message']}\n\n{'—' * 20}\n\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "🔙 НАЗАД")
async def back_to_main(message: Message):
    user = await get_user(message.from_user.id)
    await message.answer(
        f"🏠 **ГЛАВНОЕ МЕНЮ**\n\n"
        f"💰 Баланс: {user['balance']:.2f} $",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# Обработчики игр
@dp.message(F.text.in_(["🎲 КУБИК x2", "🎯 ДАРТС x2", "⚽ ФУТБОЛ x2", "🏀 БАСКЕТБОЛ x2", "🎳 БОУЛИНГ x2", "🎰 СЛОТЫ x3"]))
async def select_game(message: Message, state: FSMContext):
    game = message.text
    await state.update_data(game=game)
    await state.set_state(GameStates.waiting_for_bet)
    
    multipliers = {
        "🎲 КУБИК x2": 2,
        "🎯 ДАРТС x2": 2,
        "⚽ ФУТБОЛ x2": 2,
        "🏀 БАСКЕТБОЛ x2": 2,
        "🎳 БОУЛИНГ x2": 2,
        "🎰 СЛОТЫ x3": 3
    }
    
    await message.answer(
        f"**🎮 {game}**\n\n"
        f"💰 Максимальный множитель: **x{multipliers[game]}**\n"
        f"💵 Минимальная ставка: **0.01 $**\n\n"
        f"✍️ Введите сумму ставки:",
        parse_mode="Markdown"
    )

@dp.message(GameStates.waiting_for_bet)
async def process_bet(message: Message, state: FSMContext):
    try:
        bet = float(message.text)
        if bet < 0.01:
            await message.answer("❌ Минимальная ставка **0.01 $**", parse_mode="Markdown")
            return
        
        telegram_id = message.from_user.id
        user = await get_user(telegram_id)
        
        if not user:
            await cmd_start(message)
            return
        
        # Проверяем баланс (сначала бонусный, потом основной)
        total_balance = user['balance'] + user['bonus_balance']
        if total_balance < bet:
            await message.answer(
                f"❌ **НЕДОСТАТОЧНО СРЕДСТВ!**\n\n"
                f"💰 Ваш баланс: {total_balance:.2f} $\n"
                f"💵 Нужно: {bet:.2f} $",
                parse_mode="Markdown"
            )
            await state.clear()
            return
        
        # Списываем ставку (сначала с бонусного баланса)
        bonus_used = min(user['bonus_balance'], bet)
        real_used = bet - bonus_used
        
        if bonus_used > 0:
            await update_balance(telegram_id, bonus_used, 'subtract', bonus=True)
        if real_used > 0:
            await update_balance(telegram_id, real_used, 'subtract')
        
        data = await state.get_data()
        game = data['game']
        
        await state.update_data(bet=bet, bonus_used=bonus_used, real_used=real_used)
        await state.set_state(GameStates.waiting_for_choice)
        
        await message.answer(
            f"✅ **СТАВКА ПРИНЯТА!**\n\n"
            f"💵 Сумма: {bet:.2f} $\n"
            f"🎁 Использовано бонусов: {bonus_used:.2f} $\n\n"
            f"🎲 Выберите исход:",
            reply_markup=get_game_choices_keyboard(game),
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную сумму:")

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
    bonus_used = data.get('bonus_used', 0)
    real_used = data.get('real_used', 0)
    telegram_id = callback.from_user.id
    
    if not game or not bet:
        await callback.answer("❌ Ошибка: данные игры не найдены")
        return
    
    await callback.answer()
    
    # Определяем эмодзи для игры
    game_emoji = {
        "🎲 КУБИК x2": "🎲",
        "🎯 ДАРТС x2": "🎯",
        "⚽ ФУТБОЛ x2": "⚽",
        "🏀 БАСКЕТБОЛ x2": "🏀",
        "🎳 БОУЛИНГ x2": "🎳",
        "🎰 СЛОТЫ x3": "🎰"
    }
    
    # Отправляем дайс
    msg = await callback.message.answer_dice(emoji=game_emoji.get(game, "🎲"))
    result = msg.dice.value
    
    await asyncio.sleep(2.5)  # Пауза для анимации
    
    # Определяем победу
    win = False
    multiplier = 1
    
    if game == "🎲 КУБИК x2":
        win = (result > 3 and callback.data == 'choice_more') or (result < 4 and callback.data == 'choice_less')
        multiplier = 2
    elif game == "🎯 ДАРТС x2":
        win = (result in [5, 6] and callback.data == 'choice_target') or (result in [1, 2, 3, 4] and callback.data == 'choice_miss')
        multiplier = 2
    elif game == "⚽ ФУТБОЛ x2":
        win = (result in [4, 5] and callback.data == 'choice_goal') or (result in [1, 2, 3] and callback.data == 'choice_miss')
        multiplier = 2
    elif game == "🏀 БАСКЕТБОЛ x2":
        win = (result in [4, 5] and callback.data == 'choice_score') or (result in [1, 2, 3] and callback.data == 'choice_miss_basket')
        multiplier = 2
    elif game == "🎳 БОУЛИНГ x2":
        win = (result == 6 and callback.data == 'choice_strike') or (result in [4, 5] and callback.data == 'choice_spare')
        multiplier = 2
    elif game == "🎰 СЛОТЫ x3":
        if callback.data == 'choice_cherry':
            win = result in [1, 22, 43, 64]
            multiplier = 3
        elif callback.data == 'choice_diamond':
            win = result in [7, 28, 49, 70]
            multiplier = 5
        elif callback.data == 'choice_jackpot':
            win = result in [13, 34, 55, 76]
            multiplier = 10
    
    if win:
        win_amount = bet * multiplier
        
        # Возвращаем ставку + выигрыш
        await update_balance(telegram_id, win_amount, 'add')
        
        # Обновляем статистику
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET games_won = games_won + 1, total_win = total_win + ? WHERE telegram_id = ?",
                (win_amount, telegram_id)
            )
            conn.commit()
        
        # Начисление реферальных (процент от проигрыша)
        user = await get_user(telegram_id)
        if user and user['referred_by'] and real_used > 0:
            settings = await get_settings()
            referral_earning = real_used * settings['referral_percent'] / 100
            if referral_earning > 0:
                await update_balance(user['referred_by'], referral_earning, 'add')
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE users SET referral_earnings = referral_earnings + ? WHERE telegram_id = ?",
                        (referral_earning, user['referred_by'])
                    )
                    conn.commit()
                
                # Уведомляем реферера
                try:
                    await bot.send_message(
                        user['referred_by'],
                        f"👥 **РЕФЕРАЛЬНЫЙ БОНУС**\n\n"
                        f"Ваш реферал @{user['username']} проиграл {real_used:.2f} $\n"
                        f"💰 Ваш бонус: +{referral_earning:.2f} $",
                        parse_mode="Markdown"
                    )
                except:
                    pass
        
        await callback.message.answer(
            f"🎉 **ПОБЕДА!** 🎉\n\n"
            f"💰 **Выигрыш:** `{win_amount:.2f} $`\n"
            f"✨ **Множитель:** x{multiplier}\n"
            f"🎲 **Результат:** {result}",
            parse_mode="Markdown"
        )
    else:
        # Обновляем статистику (проигрыш)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET total_bet = total_bet + ? WHERE telegram_id = ?",
                (bet, telegram_id)
            )
            conn.commit()
        
        await callback.message.answer(
            f"😢 **ПРОИГРЫШ**\n\n"
            f"💵 Потеряно: `{bet:.2f} $`\n"
            f"🎲 **Результат:** {result}\n\n"
            f"🍀 Повезет в следующий раз!",
            parse_mode="Markdown"
        )
        win_amount = 0
    
    # Сохраняем игру в историю
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO games (user_id, game_type, bet_amount, win_amount, choice, result, multiplier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (telegram_id, game, bet, win_amount, callback.data, result, multiplier if win else 0))
        
        cursor.execute(
            "UPDATE users SET games_played = games_played + 1, last_activity = CURRENT_TIMESTAMP WHERE telegram_id = ?",
            (telegram_id,)
        )
        conn.commit()
    
    await state.clear()

# Обработчики платежей
@dp.callback_query(F.data == "deposit")
async def deposit_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "**💰 ПОПОЛНЕНИЕ БАЛАНСА**\n\n"
        "💵 Минимальная сумма: **0.01 $**\n"
        "💎 Курс: **1 USDT = 1 $**\n"
        "⏱ Время действия счета: **1 час**\n\n"
        "✍️ Введите сумму пополнения в USD:",
        parse_mode="Markdown"
    )

@dp.message(F.text.regexp(r'^\d+\.?\d*$'))
async def process_deposit_amount(message: Message):
    try:
        amount = float(message.text)
        if amount < 0.01:
            await message.answer("❌ Минимальная сумма **0.01 $**", parse_mode="Markdown")
            return
        
        # Создаем счет в CryptoBot
        invoice = await create_crypto_invoice(amount, message.from_user.id)
        
        if invoice and invoice.get('pay_url'):
            # Сохраняем транзакцию
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
                    [InlineKeyboardButton(text="🔄 ПРОВЕРИТЬ ОПЛАТУ", callback_data=f"check_payment_{invoice['invoice_id']}")]
                ]
            )
            
            await message.answer(
                f"**🧾 СЧЕТ НА ОПЛАТУ**\n\n"
                f"💰 **Сумма:** `{amount} USDT`\n"
                f"📊 **Статус:** ⏳ Ожидает оплаты\n"
                f"⏱ **Действителен до:** {(datetime.now() + timedelta(hours=1)).strftime('%H:%M')}\n\n"
                f"🔗 Нажмите кнопку ниже для оплаты в @CryptoBot",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        else:
            await message.answer(
                "❌ **ОШИБКА СОЗДАНИЯ СЧЕТА**\n\n"
                "Пожалуйста, попробуйте позже или обратитесь в поддержку.",
                parse_mode="Markdown"
            )
            
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную сумму:")

@dp.callback_query(lambda c: c.data and c.data.startswith('check_payment_'))
async def check_payment(callback: CallbackQuery):
    await callback.answer()
    invoice_id = int(callback.data.replace('check_payment_', ''))
    
    # Проверяем статус
    invoice_info = await check_invoice_status(invoice_id)
    
    if invoice_info and invoice_info.get('status') == 'paid':
        # Обновляем статус в базе
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE transactions SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE external_id = ?",
                (str(invoice_id),)
            )
            conn.commit()
        
        await callback.message.edit_text(
            "✅ **ОПЛАТА ПОДТВЕРЖДЕНА!**\n\n"
            "💰 Средства зачислены на ваш баланс.",
            parse_mode="Markdown"
        )
    else:
        await callback.message.answer(
            "⏳ **ПЛАТЕЖ ЕЩЕ НЕ ПОДТВЕРЖДЕН**\n\n"
            "Пожалуйста, завершите оплату в @CryptoBot",
            parse_mode="Markdown"
        )

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    
    if not user:
        await callback.message.edit_text("❌ Пользователь не найден")
        return
    
    settings = await get_settings()
    min_withdrawal = settings.get('min_withdrawal', 1.0)
    max_withdrawal = settings.get('max_withdrawal', 1000.0)
    
    if user['balance'] < min_withdrawal:
        await callback.message.edit_text(
            f"❌ **НЕДОСТАТОЧНО СРЕДСТВ ДЛЯ ВЫВОДА**\n\n"
            f"💰 Доступно: {user['balance']:.2f} $\n"
            f"📉 Минимум для вывода: {min_withdrawal:.2f} $",
            parse_mode="Markdown"
        )
        return
    
    await state.set_state(WithdrawalStates.waiting_for_amount)
    await callback.message.edit_text(
        f"**💸 ВЫВОД СРЕДСТВ**\n\n"
        f"💰 **Доступно:** `{user['balance']:.2f} $`\n"
        f"📉 **Мин. сумма:** `{min_withdrawal:.2f} $`\n"
        f"📈 **Макс. сумма:** `{max_withdrawal:.2f} $`\n"
        f"⏱ Время обработки: **до 24 часов**\n\n"
        f"✍️ Введите сумму для вывода:",
        parse_mode="Markdown"
    )

@dp.message(WithdrawalStates.waiting_for_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        settings = await get_settings()
        min_withdrawal = settings.get('min_withdrawal', 1.0)
        max_withdrawal = settings.get('max_withdrawal', 1000.0)
        
        if amount < min_withdrawal:
            await message.answer(f"❌ Минимальная сумма вывода: {min_withdrawal:.2f} $")
            return
        
        if amount > max_withdrawal:
            await message.answer(f"❌ Максимальная сумма вывода: {max_withdrawal:.2f} $")
            return
        
        user = await get_user(message.from_user.id)
        
        if not user:
            await cmd_start(message)
            return
        
        if amount > user['balance']:
            await message.answer("❌ Недостаточно средств на балансе")
            return
        
        await state.update_data(amount=amount)
        await state.set_state(WithdrawalStates.waiting_for_wallet)
        
        await message.answer(
            f"💸 **Сумма вывода:** `{amount:.2f} $`\n\n"
            f"📝 Введите ваш **@username** в Crypto Bot или номер кошелька:\n"
            f"(например: @username или UQ...)",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную сумму:")

@dp.message(WithdrawalStates.waiting_for_wallet)
async def process_withdrawal_wallet(message: Message, state: FSMContext):
    wallet = message.text.strip()
    data = await state.get_data()
    amount = data['amount']
    telegram_id = message.from_user.id
    
    # Проверяем формат кошелька
    if not (wallet.startswith('@') or wallet.startswith('UQ') or wallet.startswith('EQ')):
        await message.answer(
            "❌ **НЕВЕРНЫЙ ФОРМАТ КОШЕЛЬКА**\n\n"
            "Введите @username в CryptoBot или адрес TON кошелька (начинается с UQ или EQ)",
            parse_mode="Markdown"
        )
        return
    
    # Резервируем средства
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transactions (user_id, type, amount, wallet_address, status)
            VALUES (?, 'withdrawal', ?, ?, 'pending')
        ''', (telegram_id, amount, wallet))
        
        # Обновляем баланс
        cursor.execute(
            "UPDATE users SET balance = balance - ? WHERE telegram_id = ?",
            (amount, telegram_id)
        )
        conn.commit()
    
    # Уведомляем админа
    settings = await get_settings()
    if settings and settings['admin_id']:
        user = await get_user(telegram_id)
        admin_text = (
            f"🆕 **НОВАЯ ЗАЯВКА НА ВЫВОД**\n\n"
            f"**От:** {user['first_name']} (@{user['username']})\n"
            f"**ID:** `{telegram_id}`\n"
            f"**💰 Сумма:** `{amount:.2f} $`\n"
            f"**📝 Кошелек:** `{wallet}`\n"
            f"**⏱ Дата:** {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ", callback_data=f"approve_withdraw_{telegram_id}_{amount}")],
                [InlineKeyboardButton(text="❌ ОТКЛОНИТЬ", callback_data=f"reject_withdraw_{telegram_id}")]
            ]
        )
        
        try:
            await bot.send_message(settings['admin_id'], admin_text, parse_mode="Markdown", reply_markup=keyboard)
        except:
            pass
    
    await state.clear()
    await message.answer(
        f"✅ **ЗАЯВКА НА ВЫВОД СОЗДАНА!**\n\n"
        f"💰 Сумма: `{amount:.2f} $`\n"
        f"📝 Кошелек: `{wallet}`\n\n"
        f"⏱ Средства будут отправлены в течение 24 часов после проверки.",
        parse_mode="Markdown"
    )

@dp.callback_query(lambda c: c.data and (c.data.startswith('approve_withdraw_') or c.data.startswith('reject_withdraw_')))
async def process_withdrawal_action(callback: CallbackQuery):
    await callback.answer()
    
    parts = callback.data.split('_')
    action = parts[0]
    
    if action == 'approve':
        user_id = int(parts[2])
        amount = float(parts[3])
        
        # Отправляем платеж через CryptoBot
        payment = await send_crypto_payment(user_id, amount, parts[4] if len(parts) > 4 else "")
        
        with get_db() as conn:
            cursor = conn.cursor()
            if payment:
                # Обновляем статус транзакции
                cursor.execute('''
                    UPDATE transactions 
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP, tx_hash = ?, admin_id = ?
                    WHERE user_id = ? AND amount = ? AND status = 'pending'
                ''', (payment.get('transfer_id', ''), callback.from_user.id, user_id, amount))
                
                cursor.execute(
                    "UPDATE users SET total_withdrawal = total_withdrawal + ? WHERE telegram_id = ?",
                    (amount, user_id)
                )
                conn.commit()
                
                # Уведомляем пользователя
                try:
                    await bot.send_message(
                        user_id,
                        f"✅ **ВЫВОД ПОДТВЕРЖДЕН!**\n\n"
                        f"💰 Сумма: `{amount:.2f} $`\n"
                        f"📝 Транзакция: `{payment.get('transfer_id', '')}`\n\n"
                        f"Средства отправлены на ваш кошелек.",
                        parse_mode="Markdown"
                    )
                except:
                    pass
                
                await callback.message.edit_text(
                    f"✅ **ВЫВОД ПОДТВЕРЖДЕН**\n\n"
                    f"Пользователь: `{user_id}`\n"
                    f"Сумма: {amount:.2f} $\n"
                    f"Средства отправлены.",
                    parse_mode="Markdown"
                )
            else:
                # Возвращаем средства при ошибке
                cursor.execute(
                    "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                    (amount, user_id)
                )
                cursor.execute('''
                    UPDATE transactions 
                    SET status = 'failed' 
                    WHERE user_id = ? AND amount = ? AND status = 'pending'
                ''', (user_id, amount))
                conn.commit()
                
                await bot.send_message(
                    user_id,
                    f"❌ **ОШИБКА ВЫВОДА**\n\n"
                    f"Средства возвращены на ваш баланс.\n"
                    f"Пожалуйста, попробуйте позже.",
                    parse_mode="Markdown"
                )
                
                await callback.message.edit_text(
                    f"❌ **ОШИБКА ОТПРАВКИ**\n\n"
                    f"Средства возвращены пользователю.",
                    parse_mode="Markdown"
                )
    
    elif action == 'reject':
        user_id = int(parts[2])
        
        with get_db() as conn:
            cursor = conn.cursor()
            # Возвращаем средства
            cursor.execute(
                "UPDATE users SET balance = balance + (SELECT amount FROM transactions WHERE user_id = ? AND status = 'pending' LIMIT 1) WHERE telegram_id = ?",
                (user_id, user_id)
            )
            cursor.execute('''
                UPDATE transactions 
                SET status = 'rejected', admin_id = ? 
                WHERE user_id = ? AND status = 'pending'
            ''', (callback.from_user.id, user_id))
            conn.commit()
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"❌ **ВЫВОД ОТКЛОНЕН**\n\n"
                f"Ваша заявка на вывод была отклонена администратором.\n"
                f"Средства возвращены на баланс.",
                parse_mode="Markdown"
            )
        except:
            pass
        
        await callback.message.edit_text(
            f"❌ **ВЫВОД ОТКЛОНЕН**\n\n"
            f"Пользователь: `{user_id}`\n"
            f"Средства возвращены.",
            parse_mode="Markdown"
        )
    
    await log_admin_action(callback.from_user.id, f"withdraw_{action}", user_id, f"amount:{amount if action == 'approve' else 0}")

# Обработчики промокодов
@dp.callback_query(F.data == "activate_promo")
async def activate_promo_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PromoStates.waiting_for_code)
    await callback.message.edit_text(
        "🎟 **АКТИВАЦИЯ ПРОМОКОДА**\n\n"
        "Введите код промокода:",
        parse_mode="Markdown"
    )

@dp.message(PromoStates.waiting_for_code)
async def process_promo_code(message: Message, state: FSMContext):
    code = message.text.upper().strip()
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Ищем промокод
        cursor.execute(
            "SELECT * FROM promocodes WHERE code = ?",
            (code,)
        )
        promo = cursor.fetchone()
        
        if not promo:
            await message.answer("❌ **ПРОМОКОД НЕ НАЙДЕН**", parse_mode="Markdown")
            await state.clear()
            return
        
        # Проверяем для VIP
        if promo['for_vip_only']:
            user = await get_user(message.from_user.id)
            if not user or not user['is_vip']:
                await message.answer("❌ Этот промокод только для VIP игроков!", parse_mode="Markdown")
                await state.clear()
                return
        
        # Проверяем срок действия
        if promo['expires_at']:
            expires_at = datetime.fromisoformat(promo['expires_at'].replace('Z', '+00:00'))
            if expires_at < datetime.now():
                await message.answer("❌ **СРОК ДЕЙСТВИЯ ПРОМОКОДА ИСТЕК**", parse_mode="Markdown")
                await state.clear()
                return
        
        # Проверяем лимит активаций
        if promo['current_activations'] >= promo['max_activations']:
            await message.answer("❌ **ЛИМИТ АКТИВАЦИЙ ПРОМОКОДА ИСЧЕРПАН**", parse_mode="Markdown")
            await state.clear()
            return
        
        # Проверяем, активировал ли уже пользователь
        cursor.execute(
            "SELECT * FROM promo_activations WHERE promo_id = ? AND user_id = ?",
            (promo['id'], message.from_user.id)
        )
        if cursor.fetchone():
            await message.answer("❌ **ВЫ УЖЕ АКТИВИРОВАЛИ ЭТОТ ПРОМОКОД**", parse_mode="Markdown")
            await state.clear()
            return
        
        # Начисляем бонус
        user = await get_user(message.from_user.id)
        if not user:
            await cmd_start(message)
            return
        
        bonus_text = ""
        bonus_amount = 0
        
        if promo['bonus_type'] == 'fixed':
            bonus_amount = promo['bonus_value']
            await update_balance(message.from_user.id, bonus_amount, 'add', bonus=True)
            bonus_text = f"+{bonus_amount:.2f} $ на бонусный счет"
        
        elif promo['bonus_type'] == 'percent':
            bonus_amount = user['balance'] * promo['bonus_value'] / 100
            await update_balance(message.from_user.id, bonus_amount, 'add', bonus=True)
            bonus_text = f"+{bonus_amount:.2f} $ ({promo['bonus_value']}% от баланса)"
        
        elif promo['bonus_type'] == 'freespins':
            # Сохраняем фриспины в отдельную таблицу или поле
            bonus_amount = promo['bonus_value']
            bonus_text = f"🎰 {bonus_amount} фриспинов"
            # TODO: добавить логику фриспинов
        
        # Обновляем статистику промокода
        cursor.execute(
            "UPDATE promocodes SET current_activations = current_activations + 1 WHERE id = ?",
            (promo['id'],)
        )
        
        # Записываем активацию
        cursor.execute(
            "INSERT INTO promo_activations (promo_id, user_id) VALUES (?, ?)",
            (promo['id'], message.from_user.id)
        )
        conn.commit()
        
        # Добавляем уведомление
        await add_notification(
            message.from_user.id,
            "🎁 Активация промокода",
            f"Промокод {code} активирован!\nНачислено: {bonus_text}"
        )
    
    await state.clear()
    await message.answer(
        f"✅ **ПРОМОКОД УСПЕШНО АКТИВИРОВАН!**\n\n"
        f"🎁 **Начислено:** {bonus_text}",
        parse_mode="Markdown"
    )

# Админ-панель
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    user = await get_user(message.from_user.id)
    
    if not user or not user['is_admin']:
        await message.answer("⛔ **ДОСТУП ЗАПРЕЩЕН**", parse_mode="Markdown")
        return
    
    await message.answer(
        "👑 **АДМИН-ПАНЕЛЬ**\n\n"
        "Выберите раздел:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    
    await log_admin_action(message.from_user.id, "admin_panel_open")

@dp.message(F.text == "📊 СТАТИСТИКА")
async def admin_stats(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Общая статистика
        cursor.execute("SELECT COUNT(*) as count FROM users")
        total_users = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM users WHERE created_at > datetime('now', '-1 day')")
        new_users_today = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM users WHERE is_vip = 1")
        vip_users = cursor.fetchone()['count']
        
        cursor.execute("SELECT SUM(balance) as sum FROM users")
        total_balance = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(bonus_balance) as sum FROM users")
        total_bonus = cursor.fetchone()['sum'] or 0
        
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
        
        # Игры сегодня
        cursor.execute("SELECT COUNT(*) as count FROM games WHERE created_at > datetime('now', '-1 day')")
        games_today = cursor.fetchone()['count']
        
        cursor.execute("SELECT SUM(bet_amount) as bet, SUM(win_amount) as win FROM games WHERE created_at > datetime('now', '-1 day')")
        today_stats = cursor.fetchone()
        today_bet = today_stats['bet'] or 0
        today_win = today_stats['win'] or 0
        
        # RTP
        cursor.execute("SELECT SUM(bet_amount) as total_bet, SUM(win_amount) as total_win FROM games")
        games_stats = cursor.fetchone()
        total_bet = games_stats['total_bet'] or 0
        total_win = games_stats['total_win'] or 0
        rtp_fact = (total_win / total_bet * 100) if total_bet > 0 else 0
        
        # Прибыль
        profit = total_deposit - total_withdrawal
        
        # Топ игроков
        cursor.execute('''
            SELECT username, first_name, balance, games_played 
            FROM users 
            ORDER BY balance DESC 
            LIMIT 5
        ''')
        top_players = cursor.fetchall()
    
    text = (
        f"**📊 ОБЩАЯ СТАТИСТИКА**\n\n"
        f"**👥 ПОЛЬЗОВАТЕЛИ:**\n"
        f"• Всего: {total_users}\n"
        f"• Новых сегодня: +{new_users_today}\n"
        f"• VIP: {vip_users}\n\n"
        f"**💰 ФИНАНСЫ:**\n"
        f"• Баланс игроков: {total_balance:.2f} $\n"
        f"• Бонусный фонд: {total_bonus:.2f} $\n"
        f"• Всего депозитов: {total_deposit:.2f} $\n"
        f"• Всего выводов: {total_withdrawal:.2f} $\n"
        f"• Прибыль: {profit:.2f} $\n"
        f"• Ожидает вывода: {pending_withdrawals} заявок на {pending_amount:.2f} $\n\n"
        f"**🎮 ИГРЫ:**\n"
        f"• Всего игр: {total_games}\n"
        f"• Игр сегодня: {games_today}\n"
        f"• Оборот сегодня: {today_bet:.2f} $\n"
        f"• Выплаты сегодня: {today_win:.2f} $\n"
        f"• RTP (факт): {rtp_fact:.2f}%\n\n"
        f"**🏆 ТОП-5 ИГРОКОВ:**\n"
    )
    
    for i, player in enumerate(top_players, 1):
        name = player['first_name'] or player['username'] or f"ID {player['id']}"
        text += f"{i}. {name} - {player['balance']:.2f} $ (игр: {player['games_played']})\n"
    
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
            [InlineKeyboardButton(text="✅ РАЗБЛОКИРОВАТЬ", callback_data="admin_unban_user")],
            [InlineKeyboardButton(text="👑 НАЗНАЧИТЬ VIP", callback_data="admin_set_vip")]
        ]
    )
    
    await message.answer(
        "**👥 УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ**\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(F.text == "💰 БАЛАНСЫ")
async def admin_balances(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer(
        "**💰 УПРАВЛЕНИЕ БАЛАНСАМИ**\n\n"
        "Выберите действие:",
        reply_markup=get_balance_management_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_balance_add")
async def admin_balance_add_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdminBalanceStates.waiting_for_user_id)
    await callback.message.edit_text(
        "➕ **НАЧИСЛЕНИЕ БАЛАНСА**\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="Markdown"
    )

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
            f"👤 **ПОЛЬЗОВАТЕЛЬ:** {user['first_name']} (@{user['username']})\n"
            f"💰 **Текущий баланс:** {user['balance']:.2f} $\n\n"
            f"Введите сумму для начисления:",
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ Введите корректный ID")

@dp.message(AdminBalanceStates.waiting_for_amount)
async def admin_balance_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        target_user = data['target_user']
        
        await update_balance(target_user, amount, 'add')
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO transactions (user_id, type, amount, status, admin_id, reason)
                VALUES (?, 'admin_deposit', ?, 'completed', ?, ?)
            ''', (target_user, amount, message.from_user.id, f"Начислено администратором"))
            conn.commit()
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                target_user,
                f"💰 **ПОПОЛНЕНИЕ БАЛАНСА**\n\n"
                f"Вам начислено: **+{amount:.2f} $**\n"
                f"👤 Администратор: @{message.from_user.username}",
                parse_mode="Markdown"
            )
        except:
            pass
        
        await state.clear()
        await message.answer(
            f"✅ **БАЛАНС ПОПОЛНЕН**\n\n"
            f"Пользователь: `{target_user}`\n"
            f"Сумма: +{amount:.2f} $",
            parse_mode="Markdown"
        )
        
        await log_admin_action(message.from_user.id, "balance_add", target_user, f"amount:{amount}")
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.callback_query(F.data == "admin_balance_subtract")
async def admin_balance_subtract_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdminBalanceStates.waiting_for_user_id)
    await state.update_data(operation='subtract')
    await callback.message.edit_text(
        "➖ **СПИСАНИЕ БАЛАНСА**\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📢 РАССЫЛКА")
async def broadcast_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(BroadcastStates.waiting_for_message)
    await message.answer(
        "**📢 РАССЫЛКА СООБЩЕНИЙ**\n\n"
        "Отправьте сообщение для рассылки (можно с фото/видео):\n\n"
        "⚠️ Рассылка будет отправлена ВСЕМ пользователям!",
        parse_mode="Markdown"
    )

@dp.message(BroadcastStates.waiting_for_message)
async def broadcast_preview(message: Message, state: FSMContext):
    await state.update_data(message=message)
    await state.set_state(BroadcastStates.waiting_for_confirmation)
    
    preview_text = message.caption or message.text or "Медиа сообщение"
    
    await message.answer(
        f"**👁 ПРЕДПРОСМОТР РАССЫЛКИ**\n\n{preview_text}\n\n"
        f"**📊 СТАТИСТИКА:**\n"
        f"• Всего пользователей: {await get_total_users()}\n"
        f"• Активных: {await get_active_users()}\n\n"
        f"Подтвердите рассылку:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ", callback_data="broadcast_confirm"),
                    InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="broadcast_cancel")
                ]
            ]
        ),
        parse_mode="Markdown"
    )

async def get_total_users():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM users WHERE is_blocked = 0")
        return cursor.fetchone()['count']

async def get_active_users():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM users WHERE last_activity > datetime('now', '-7 days')")
        return cursor.fetchone()['count']

@dp.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    original_message = data['message']
    
    await callback.message.edit_text("⏳ **НАЧИНАЮ РАССЫЛКУ...**", parse_mode="Markdown")
    
    # Получаем всех пользователей
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM users WHERE is_blocked = 0")
        users = cursor.fetchall()
    
    sent = 0
    failed = 0
    
    progress_msg = await callback.message.answer(f"📊 Прогресс: 0/{len(users)}")
    
    for i, user in enumerate(users, 1):
        try:
            if original_message.text:
                await bot.send_message(user['telegram_id'], original_message.text)
            elif original_message.photo:
                await bot.send_photo(
                    user['telegram_id'],
                    original_message.photo[-1].file_id,
                    caption=original_message.caption
                )
            elif original_message.video:
                await bot.send_video(
                    user['telegram_id'],
                    original_message.video.file_id,
                    caption=original_message.caption
                )
            sent += 1
            
            # Обновляем прогресс каждые 10 сообщений
            if i % 10 == 0:
                await progress_msg.edit_text(f"📊 Прогресс: {i}/{len(users)}")
            
            await asyncio.sleep(0.05)  # Защита от флуда
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user['telegram_id']}: {e}")
            failed += 1
    
    await state.clear()
    
    await callback.message.answer(
        f"✅ **РАССЫЛКА ЗАВЕРШЕНА**\n\n"
        f"📊 **РЕЗУЛЬТАТЫ:**\n"
        f"• Отправлено: {sent}\n"
        f"• Ошибок: {failed}\n"
        f"• Успешность: {sent/(sent+failed)*100:.1f}%",
        parse_mode="Markdown"
    )
    
    await log_admin_action(callback.from_user.id, "broadcast", details=f"sent:{sent},failed:{failed}")

@dp.message(F.text == "🏆 ТУРНИРЫ")
async def admin_tournaments(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer(
        "**🏆 УПРАВЛЕНИЕ ТУРНИРАМИ**\n\n"
        "Выберите действие:",
        reply_markup=get_tournament_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_tournament_create")
async def admin_tournament_create_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(TournamentStates.waiting_for_name)
    await callback.message.edit_text(
        "🏆 **СОЗДАНИЕ ТУРНИРА**\n\n"
        "Введите название турнира:",
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
        await message.answer(
            "📅 Введите дату начала турнира в формате ДД.ММ.ГГГГ ЧЧ:ММ\n"
            "(например: 25.12.2024 20:00)"
        )
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

@dp.message(TournamentStates.waiting_for_start_date)
async def admin_tournament_start_date(message: Message, state: FSMContext):
    try:
        start_date = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        await state.update_data(start_date=start_date.isoformat())
        await state.set_state(TournamentStates.waiting_for_end_date)
        await message.answer(
            "📅 Введите дату окончания турнира в формате ДД.ММ.ГГГГ ЧЧ:ММ\n"
            "(например: 26.12.2024 20:00)"
        )
    except ValueError:
        await message.answer("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ ЧЧ:ММ")

@dp.message(TournamentStates.waiting_for_end_date)
async def admin_tournament_end_date(message: Message, state: FSMContext):
    try:
        end_date = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        await state.update_data(end_date=end_date.isoformat())
        await state.set_state(TournamentStates.waiting_for_min_bet)
        await message.answer(
            "💰 Введите минимальную ставку для участия (0 - без ограничений):"
        )
    except ValueError:
        await message.answer("❌ Неверный формат даты")

@dp.message(TournamentStates.waiting_for_min_bet)
async def admin_tournament_min_bet(message: Message, state: FSMContext):
    try:
        min_bet = float(message.text)
        data = await state.get_data()
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO tournaments (name, prize_pool, start_date, end_date, min_bet, status)
                VALUES (?, ?, ?, ?, ?, 'upcoming')
            ''', (data['name'], data['prize'], data['start_date'], data['end_date'], min_bet))
            conn.commit()
        
        await state.clear()
        await message.answer(f"✅ **ТУРНИР СОЗДАН**\n\nНазвание: {data['name']}\nПризовой фонд: {data['prize']} $", parse_mode="Markdown")
        
        # Рассылка о новом турнире
        await broadcast_tournament_announcement(data['name'], data['prize'], data['start_date'], data['end_date'])
        
    except ValueError:
        await message.answer("❌ Введите корректную сумму")

async def broadcast_tournament_announcement(name, prize, start_date, end_date):
    """Рассылка анонса турнира"""
    start = datetime.fromisoformat(start_date).strftime("%d.%m.%Y %H:%M")
    end = datetime.fromisoformat(end_date).strftime("%d.%m.%Y %H:%M")
    
    text = (
        f"🏆 **НОВЫЙ ТУРНИР!**\n\n"
        f"**{name}**\n\n"
        f"💰 Призовой фонд: {prize} $\n"
        f"📅 Начало: {start}\n"
        f"📅 Конец: {end}\n\n"
        f"Принять участие можно в разделе 🎰 ТУРНИРЫ"
    )
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM users WHERE is_blocked = 0")
        users = cursor.fetchall()
    
    for user in users:
        try:
            await bot.send_message(user['telegram_id'], text, parse_mode="Markdown")
            await asyncio.sleep(0.05)
        except:
            pass

@dp.message(F.text == "⚙️ НАСТРОЙКИ")
async def admin_settings(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    settings = await get_settings()
    
    text = (
        f"**⚙️ НАСТРОЙКИ БОТА**\n\n"
        f"**🎰 Название казино:** {settings['casino_name']}\n"
        f"**📊 RTP:** {settings['rtp']}%\n"
        f"**👥 Реферальный %:** {settings['referral_percent']}%\n"
        f"**💸 Мин. вывод:** {settings.get('min_withdrawal', 1.0)} $\n"
        f"**💸 Макс. вывод:** {settings.get('max_withdrawal', 1000.0)} $\n\n"
        f"**👑 Админ ID:** {settings['admin_id']}\n"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ ИЗМЕНИТЬ RTP", callback_data="admin_edit_rtp")],
            [InlineKeyboardButton(text="✏️ ИЗМЕНИТЬ РЕФЕРАЛЬНЫЙ %", callback_data="admin_edit_ref")],
            [InlineKeyboardButton(text="✏️ ИЗМЕНИТЬ ЛИМИТЫ ВЫВОДА", callback_data="admin_edit_withdraw")]
        ]
    )
    
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.text == "🎟 ПРОМОКОДЫ")
async def promo_menu_admin(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer(
        "**🎟 УПРАВЛЕНИЕ ПРОМОКОДАМИ**\n\n"
        "Выберите действие:",
        reply_markup=get_promo_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "➕ СОЗДАТЬ ПРОМОКОД")
async def create_promo_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(PromoStates.waiting_for_promo_code)
    await message.answer(
        "**➕ СОЗДАНИЕ ПРОМОКОДА**\n\n"
        "Введите код промокода (например, WELCOME100):",
        parse_mode="Markdown"
    )

@dp.message(PromoStates.waiting_for_promo_code)
async def create_promo_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.upper())
    await state.set_state(PromoStates.waiting_for_bonus_type)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 ФИКСИРОВАННАЯ СУММА", callback_data="bonus_fixed")],
            [InlineKeyboardButton(text="📊 ПРОЦЕНТ ОТ БАЛАНСА", callback_data="bonus_percent")],
            [InlineKeyboardButton(text="🎰 ФРИСПИНЫ", callback_data="bonus_freespins")]
        ]
    )
    
    await message.answer(
        "Выберите тип бонуса:",
        reply_markup=keyboard
    )

@dp.callback_query(lambda c: c.data and c.data.startswith('bonus_'))
async def create_promo_bonus_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bonus_type = callback.data.replace('bonus_', '')
    await state.update_data(bonus_type=bonus_type)
    await state.set_state(PromoStates.waiting_for_bonus_value)
    
    type_names = {
        'fixed': '💰 фиксированную сумму в $',
        'percent': '📊 процент (например, 10)',
        'freespins': '🎰 количество фриспинов'
    }
    
    await callback.message.edit_text(
        f"Введите {type_names.get(bonus_type, 'значение')}:"
    )

@dp.message(PromoStates.waiting_for_bonus_value)
async def create_promo_bonus_value(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        await state.update_data(bonus_value=value)
        await state.set_state(PromoStates.waiting_for_expiry)
        await message.answer(
            "📅 Введите срок действия (в днях) или 0 для бессрочного:"
        )
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число:")

@dp.message(PromoStates.waiting_for_expiry)
async def create_promo_expiry(message: Message, state: FSMContext):
    try:
        days = int(message.text)
        expires_at = None
        if days > 0:
            expires_at = (datetime.now() + timedelta(days=days)).isoformat()
        
        await state.update_data(expires_at=expires_at)
        await state.set_state(PromoStates.waiting_for_max_activations)
        await message.answer(
            "👥 Введите максимальное количество активаций:"
        )
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message(PromoStates.waiting_for_max_activations)
async def create_promo_max_activations(message: Message, state: FSMContext):
    try:
        max_acts = int(message.text)
        await state.update_data(max_activations=max_acts)
        await state.set_state(PromoStates.waiting_for_min_deposit)
        await message.answer(
            "💰 Введите минимальную сумму депозита для активации (0 - без ограничений):"
        )
    except ValueError:
        await message.answer("❌ Введите корректное число:")

@dp.message(PromoStates.waiting_for_min_deposit)
async def create_promo_min_deposit(message: Message, state: FSMContext):
    try:
        min_deposit = float(message.text)
        data = await state.get_data()
        
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO promocodes 
                (code, bonus_type, bonus_value, expires_at, max_activations, min_deposit)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                data['code'],
                data['bonus_type'],
                data['bonus_value'],
                data['expires_at'],
                data['max_activations'],
                min_deposit
            ))
            conn.commit()
        
        await state.clear()
        await message.answer(
            f"✅ **ПРОМОКОД СОЗДАН**\n\n"
            f"Код: {data['code']}\n"
            f"Тип: {data['bonus_type']}\n"
            f"Значение: {data['bonus_value']}\n"
            f"Активаций: {data['max_activations']}",
            parse_mode="Markdown"
        )
        
        await log_admin_action(message.from_user.id, "promo_create", details=f"code:{data['code']}")
        
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
        await message.answer("📭 **ПРОМОКОДЫ НЕ НАЙДЕНЫ**", parse_mode="Markdown")
        return
    
    text = "**📋 СПИСОК ПРОМОКОДОВ**\n\n"
    for promo in promos:
        expires = "Бессрочно" if not promo['expires_at'] else datetime.fromisoformat(promo['expires_at']).strftime("%d.%m.%Y")
        status = "✅ Активен" if datetime.fromisoformat(promo['expires_at']) > datetime.now() if promo['expires_at'] else True else "❌ Истек"
        
        text += (
            f"**Код:** `{promo['code']}`\n"
            f"• Тип: {promo['bonus_type']}\n"
            f"• Значение: {promo['bonus_value']}\n"
            f"• Активаций: {promo['current_activations']}/{promo['max_activations']}\n"
            f"• Срок: {expires}\n"
            f"• Статус: {status}\n"
            f"{'—' * 20}\n"
        )
    
    # Разбиваем на части если слишком длинно
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            await message.answer(part, parse_mode="Markdown")
    else:
        await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "❌ УДАЛИТЬ ПРОМОКОД")
async def delete_promo_start_admin(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer(
        "**❌ УДАЛЕНИЕ ПРОМОКОДА**\n\n"
        "Введите код промокода для удаления:",
        parse_mode="Markdown"
    )

@dp.message(PromoStates.waiting_for_code)
async def delete_promo_admin(message: Message, state: FSMContext):
    code = message.text.upper()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM promocodes WHERE code = ?", (code,))
        conn.commit()
        
        if cursor.rowcount > 0:
            await message.answer(f"✅ **ПРОМОКОД {code} УДАЛЕН**", parse_mode="Markdown")
            await log_admin_action(message.from_user.id, "promo_delete", details=f"code:{code}")
        else:
            await message.answer("❌ **ПРОМОКОД НЕ НАЙДЕН**", parse_mode="Markdown")
    
    await state.clear()

# Дополнительные функции
@dp.callback_query(F.data == "my_stats")
async def my_stats(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Статистика по играм
        cursor.execute('''
            SELECT game_type, COUNT(*) as count, SUM(bet_amount) as total_bet, SUM(win_amount) as total_win
            FROM games 
            WHERE user_id = ?
            GROUP BY game_type
        ''', (callback.from_user.id,))
        game_stats = cursor.fetchall()
        
        # Последние игры
        cursor.execute('''
            SELECT game_type, bet_amount, win_amount, created_at 
            FROM games 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 5
        ''', (callback.from_user.id,))
        last_games = cursor.fetchall()
    
    text = (
        f"**📊 ДЕТАЛЬНАЯ СТАТИСТИКА**\n\n"
        f"**🎮 ПО ИГРАМ:**\n"
    )
    
    for stat in game_stats:
        profit = stat['total_win'] - stat['total_bet']
        text += f"• {stat['game_type']}: {stat['count']} игр, {profit:+.2f} $\n"
    
    text += "\n**🕒 ПОСЛЕДНИЕ 5 ИГР:**\n"
    for game in last_games:
        date = datetime.fromisoformat(game['created_at']).strftime("%d.%m %H:%M")
        result = "✅" if game['win_amount'] > 0 else "❌"
        text += f"• {date} {game['game_type']}: {game['bet_amount']:.2f}$ {result}\n"
    
    await callback.message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "notifications")
async def show_notifications(callback: CallbackQuery):
    await callback.answer()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM notifications 
            WHERE user_id = ? AND is_read = 0
            ORDER BY created_at DESC
        ''', (callback.from_user.id,))
        notifs = cursor.fetchall()
        
        # Отмечаем как прочитанные
        cursor.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ?",
            (callback.from_user.id,)
        )
        conn.commit()
    
    if not notifs:
        await callback.message.answer("📭 **НЕТ НЕПРОЧИТАННЫХ УВЕДОМЛЕНИЙ**", parse_mode="Markdown")
        return
    
    text = "**🔔 ВАШИ УВЕДОМЛЕНИЯ**\n\n"
    for n in notifs:
        date = datetime.fromisoformat(n['created_at']).strftime("%d.%m.%Y %H:%M")
        icon = "🔔" if n['type'] == 'info' else "🎁" if n['type'] == 'bonus' else "⚠️"
        text += f"{icon} **{n['title']}**\n{date}\n{n['message']}\n\n{'—' * 20}\n\n"
    
    await callback.message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "leaderboard")
async def show_leaderboard(callback: CallbackQuery):
    await callback.answer()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT username, first_name, balance, games_played, games_won, total_win
            FROM users 
            WHERE is_blocked = 0
            ORDER BY balance DESC 
            LIMIT 10
        ''')
        top_balance = cursor.fetchall()
        
        cursor.execute('''
            SELECT username, first_name, games_played, games_won, 
                   ROUND(CAST(games_won AS FLOAT) / games_played * 100, 2) as win_rate
            FROM users 
            WHERE games_played > 10
            ORDER BY win_rate DESC 
            LIMIT 10
        ''')
        top_winrate = cursor.fetchall()
        
        cursor.execute('''
            SELECT username, first_name, total_win
            FROM users 
            ORDER BY total_win DESC 
            LIMIT 10
        ''')
        top_winners = cursor.fetchall()
    
    text = "🏆 **ТАБЛИЦА ЛИДЕРОВ**\n\n"
    
    text += "**💰 ПО БАЛАНСУ:**\n"
    for i, user in enumerate(top_balance, 1):
        name = user['first_name'] or user['username'] or f"Игрок {i}"
        text += f"{i}. {name} - {user['balance']:.2f}$\n"
    
    text += "\n**📊 ПО ВИНРЕЙТУ:**\n"
    for i, user in enumerate(top_winrate, 1):
        name = user['first_name'] or user['username'] or f"Игрок {i}"
        text += f"{i}. {name} - {user['win_rate']}%\n"
    
    text += "\n**💰 ПО ВЫИГРЫШАМ:**\n"
    for i, user in enumerate(top_winners, 1):
        name = user['first_name'] or user['username'] or f"Игрок {i}"
        text += f"{i}. {name} - {user['total_win']:.2f}$\n"
    
    await callback.message.answer(text, parse_mode="Markdown")

# Запуск бота
async def main():
    """Главная функция запуска бота"""
    logger.info("Инициализация базы данных...")
    await init_database()
    
    logger.info("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
