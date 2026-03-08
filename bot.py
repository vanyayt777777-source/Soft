"""
Telegram Casino Bot с интеграцией CryptoBot
Один файл, токен в переменных окружениях
Актуальная версия для aiogram 3.x
"""

import os
import logging
import asyncio
import random
import string
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Union
from contextlib import contextmanager

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

class PromoStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_promo_action = State()
    waiting_for_promo_code = State()
    waiting_for_bonus_type = State()
    waiting_for_bonus_value = State()
    waiting_for_expiry = State()
    waiting_for_max_activations = State()
    waiting_for_min_deposit = State()

class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()

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
                total_deposit REAL DEFAULT 0.0,
                total_withdrawal REAL DEFAULT 0.0,
                games_played INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_blocked INTEGER DEFAULT 0
            )
        ''')
        
        # Таблица транзакций
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                external_id TEXT,
                wallet_address TEXT,
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
        
        conn.commit()

# Вспомогательные функции
def generate_referral_code(telegram_id: int) -> str:
    """Генерация уникального реферального кода"""
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
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
            INSERT INTO users (telegram_id, username, first_name, referral_code, referred_by, is_admin)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (telegram_id, username, first_name, referral_code, referred_by, is_admin))
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
        conn.commit()

# Клавиатуры
def get_main_keyboard():
    """Главное меню"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎮 Играть")],
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="👥 Реферальная система")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_profile_keyboard():
    """Клавиатура профиля"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пополнить", callback_data="deposit")],
            [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
            [InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="activate_promo")]
        ]
    )
    return keyboard

def get_games_keyboard():
    """Клавиатура выбора игры"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 Кубик")],
            [KeyboardButton(text="⚽ Футбол")],
            [KeyboardButton(text="🏀 Баскетбол")],
            [KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_game_choices_keyboard(game: str):
    """Клавиатура выбора исхода игры"""
    if game == "🎲 Кубик":
        buttons = [
            [InlineKeyboardButton(text="Больше (4-6) x2", callback_data="choice_more")],
            [InlineKeyboardButton(text="Меньше (1-3) x2", callback_data="choice_less")]
        ]
    elif game == "⚽ Футбол":
        buttons = [
            [InlineKeyboardButton(text="Гол x2", callback_data="choice_goal")],
            [InlineKeyboardButton(text="Промах x2", callback_data="choice_miss")]
        ]
    else:  # Баскетбол
        buttons = [
            [InlineKeyboardButton(text="Попадание x2", callback_data="choice_score")],
            [InlineKeyboardButton(text="Промах x2", callback_data="choice_miss_basket")]
        ]
    
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_game")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard():
    """Админ-панель"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="🎟 Промокоды")],
            [KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_promo_keyboard():
    """Клавиатура управления промокодами"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Создать промокод")],
            [KeyboardButton(text="📋 Список промокодов")],
            [KeyboardButton(text="❌ Удалить промокод")],
            [KeyboardButton(text="🔙 Назад")]
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
            "🔧 Первоначальная настройка бота\n\n"
            "Введите название казино:"
        )
        return
    
    # Проверяем, существует ли пользователь
    user = await get_user(telegram_id)
    if not user:
        await create_user(telegram_id, username, first_name, referred_by)
        user = await get_user(telegram_id)
        
        # Начисление бонуса за реферала
        if referred_by:
            await message.answer("🎉 Вы зарегистрировались по реферальной ссылке!")
    
    settings = await get_settings()
    welcome_text = settings.get('welcome_message', 'Добро пожаловать в казино!')
    
    await message.answer(
        f"{welcome_text}\n\n"
        f"Ваш ID: {telegram_id}\n"
        f"Баланс: {user['balance']:.2f} $",
        reply_markup=get_main_keyboard()
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
        await message.answer("Введите RTP (Return to Player) в процентах (например, 95):")
    except ValueError:
        await message.answer("Пожалуйста, введите корректный ID (только цифры):")

@dp.message(SetupStates.waiting_for_rtp)
async def setup_rtp(message: Message, state: FSMContext):
    try:
        rtp = float(message.text)
        if 1 <= rtp <= 100:
            await state.update_data(rtp=rtp)
            await state.set_state(SetupStates.waiting_for_referral_percent)
            await message.answer("Введите процент от выигрыша в реферальную систему (например, 5):")
        else:
            await message.answer("Пожалуйста, введите число от 1 до 100:")
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число:")

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
                "✅ Бот успешно настроен!\n\n"
                "Теперь можно использовать команду /start"
            )
        else:
            await message.answer("Пожалуйста, введите число от 0 до 100:")
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число:")

# Обработчики меню
@dp.message(F.text == "👤 Профиль")
async def profile_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)
    
    if not user:
        await cmd_start(message)
        return
    
    text = (
        f"👤 Профиль\n\n"
        f"ID: {telegram_id}\n"
        f"Username: @{message.from_user.username or 'не указан'}\n"
        f"Баланс: {user['balance']:.2f} $\n"
        f"Всего игр: {user['games_played']}\n"
        f"Всего пополнено: {user['total_deposit']:.2f} $\n"
        f"Всего выведено: {user['total_withdrawal']:.2f} $"
    )
    
    await message.answer(text, reply_markup=get_profile_keyboard())

@dp.message(F.text == "👥 Реферальная система")
async def referral_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)
    
    if not user:
        await cmd_start(message)
        return
    
    # Получаем количество рефералов
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM users WHERE referred_by = ?",
            (telegram_id,)
        )
        result = cursor.fetchone()
        referrals_count = result['count'] if result else 0
    
    settings = await get_settings()
    bot_username = (await bot.get_me()).username
    
    text = (
        f"👥 Реферальная система\n\n"
        f"Приглашено друзей: {referrals_count}\n"
        f"Заработано с рефералов: {user['referral_earnings']:.2f} $\n\n"
        f"Процент от выигрыша реферала: {settings['referral_percent']}%\n\n"
        f"Ваша реферальная ссылка:\n"
        f"https://t.me/{bot_username}?start={user['referral_code']}"
    )
    
    await message.answer(text)

@dp.message(F.text == "🎮 Играть")
async def games_menu(message: Message):
    await message.answer("Выберите игру:", reply_markup=get_games_keyboard())

@dp.message(F.text == "🔙 Назад")
async def back_to_main(message: Message):
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())

# Обработчики игр
@dp.message(F.text.in_(["🎲 Кубик", "⚽ Футбол", "🏀 Баскетбол"]))
async def select_game(message: Message, state: FSMContext):
    game = message.text
    await state.update_data(game=game)
    await state.set_state(GameStates.waiting_for_bet)
    await message.answer(
        f"Выбрана игра: {game}\n"
        f"Введите сумму ставки (минимум 0.01):"
    )

@dp.message(GameStates.waiting_for_bet)
async def process_bet(message: Message, state: FSMContext):
    try:
        bet = float(message.text)
        if bet < 0.01:
            await message.answer("Минимальная ставка 0.01 $")
            return
        
        telegram_id = message.from_user.id
        user = await get_user(telegram_id)
        
        if not user:
            await cmd_start(message)
            return
        
        if user['balance'] < bet:
            await message.answer("Недостаточно средств на балансе!")
            await state.clear()
            return
        
        # Списываем ставку
        await update_balance(telegram_id, bet, 'subtract')
        
        data = await state.get_data()
        game = data['game']
        
        await state.update_data(bet=bet)
        await state.set_state(GameStates.waiting_for_choice)
        
        await message.answer(
            f"Ставка {bet:.2f} $ принята!\n"
            f"Выберите исход:",
            reply_markup=get_game_choices_keyboard(game)
        )
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму:")

@dp.callback_query(lambda c: c.data and (c.data.startswith('choice_') or c.data == 'cancel_game'))
async def process_game_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == 'cancel_game':
        await state.clear()
        await callback.message.edit_text("Игра отменена")
        await callback.answer()
        return
    
    data = await state.get_data()
    if not data:
        await callback.answer("Игра не найдена")
        return
    
    game = data.get('game')
    bet = data.get('bet')
    telegram_id = callback.from_user.id
    
    if not game or not bet:
        await callback.answer("Ошибка: данные игры не найдены")
        return
    
    await callback.answer()
    
    # Отправляем дайс
    if game == "🎲 Кубик":
        msg = await callback.message.answer_dice(emoji="🎲")
        result = msg.dice.value
        # Победа если больше 3 и выбрано more, или меньше 4 и выбрано less
        win = (result > 3 and callback.data == 'choice_more') or (result < 4 and callback.data == 'choice_less')
    elif game == "⚽ Футбол":
        msg = await callback.message.answer_dice(emoji="⚽")
        result = msg.dice.value
        # В футболе: 4-5 гол, 1-3 промах
        win = (result in [4, 5] and callback.data == 'choice_goal') or (result in [1, 2, 3] and callback.data == 'choice_miss')
    else:  # Баскетбол
        msg = await callback.message.answer_dice(emoji="🏀")
        result = msg.dice.value
        # В баскетболе: 4-5 попадание, 1-3 промах
        win = (result in [4, 5] and callback.data == 'choice_score') or (result in [1, 2, 3] and callback.data == 'choice_miss_basket')
    
    await asyncio.sleep(2)  # Пауза для анимации
    
    if win:
        win_amount = bet * 2
        await update_balance(telegram_id, win_amount, 'add')
        
        # Начисление реферальных
        user = await get_user(telegram_id)
        if user and user['referred_by']:
            settings = await get_settings()
            referral_earning = win_amount * settings['referral_percent'] / 100
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET referral_earnings = referral_earnings + ? WHERE telegram_id = ?",
                    (referral_earning, user['referred_by'])
                )
                cursor.execute(
                    "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                    (referral_earning, user['referred_by'])
                )
                conn.commit()
        
        await callback.message.answer(f"🎉 Вы выиграли {win_amount:.2f} $!")
    else:
        await callback.message.answer(f"😢 Вы проиграли {bet:.2f} $")
        win_amount = 0
    
    # Сохраняем игру в историю
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO games (user_id, game_type, bet_amount, win_amount, choice, result)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (telegram_id, game, bet, win_amount, callback.data, result))
        
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
        "💰 Пополнение баланса\n\n"
        "Минимальная сумма: 0.01 $\n"
        "Курс: 1 USDT = 1 $\n\n"
        "Введите сумму пополнения в USD:"
    )

@dp.message(F.text.regexp(r'^\d+\.?\d*$'))
async def process_deposit_amount(message: Message):
    try:
        amount = float(message.text)
        if amount < 0.01:
            await message.answer("Минимальная сумма 0.01 $")
            return
        
        # Здесь должна быть интеграция с CryptoBot API
        # Создание счета и получение ссылки на оплату
        
        # Для примера отправляем тестовую ссылку
        await message.answer(
            f"💳 Счет на оплату\n\n"
            f"Сумма: {amount} USDT\n"
            f"Статус: ожидает оплаты\n\n"
            f"Ссылка для оплаты: https://t.me/CryptoBot?start=test_payment\n\n"
            f"После оплаты баланс будет пополнен автоматически"
        )
        
        # Записываем транзакцию
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO transactions (user_id, type, amount, external_id)
                VALUES (?, 'deposit', ?, ?)
            ''', (message.from_user.id, amount, f"test_{message.from_user.id}_{int(datetime.now().timestamp())}"))
            conn.commit()
            
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму")

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    
    if not user:
        await callback.message.edit_text("Пользователь не найден")
        return
    
    if user['balance'] <= 0:
        await callback.message.edit_text("У вас нет средств для вывода")
        return
    
    await state.set_state(WithdrawalStates.waiting_for_amount)
    await callback.message.edit_text(
        f"💸 Вывод средств\n\n"
        f"Доступно: {user['balance']:.2f} $\n"
        f"Минимальная сумма: 0.01 $\n\n"
        f"Введите сумму для вывода:"
    )

@dp.message(WithdrawalStates.waiting_for_amount)
async def process_withdrawal_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0.01:
            await message.answer("Минимальная сумма 0.01 $")
            return
        
        user = await get_user(message.from_user.id)
        
        if not user:
            await cmd_start(message)
            return
        
        if amount > user['balance']:
            await message.answer("Недостаточно средств на балансе")
            return
        
        await state.update_data(amount=amount)
        await state.set_state(WithdrawalStates.waiting_for_wallet)
        
        await message.answer(
            f"Сумма вывода: {amount:.2f} $\n\n"
            f"Введите ваш @username в Crypto Bot или номер кошелька:"
        )
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму")

@dp.message(WithdrawalStates.waiting_for_wallet)
async def process_withdrawal_wallet(message: Message, state: FSMContext):
    wallet = message.text
    data = await state.get_data()
    amount = data['amount']
    telegram_id = message.from_user.id
    
    # Здесь должна быть интеграция с CryptoBot API для отправки средств
    
    # Резервируем средства
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transactions (user_id, type, amount, wallet_address)
            VALUES (?, 'withdrawal', ?, ?)
        ''', (telegram_id, amount, wallet))
        
        # Обновляем баланс
        cursor.execute(
            "UPDATE users SET balance = balance - ?, total_withdrawal = total_withdrawal + ? WHERE telegram_id = ?",
            (amount, amount, telegram_id)
        )
        conn.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Заявка на вывод создана\n\n"
        f"Сумма: {amount:.2f} $\n"
        f"Кошелек: {wallet}\n\n"
        f"Средства будут отправлены в ближайшее время"
    )

# Обработчики промокодов
@dp.callback_query(F.data == "activate_promo")
async def activate_promo_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PromoStates.waiting_for_code)
    await callback.message.edit_text("Введите промокод:")

@dp.message(PromoStates.waiting_for_code)
async def process_promo_code(message: Message, state: FSMContext):
    code = message.text.upper()
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Ищем промокод
        cursor.execute(
            "SELECT * FROM promocodes WHERE code = ?",
            (code,)
        )
        promo = cursor.fetchone()
        
        if not promo:
            await message.answer("❌ Промокод не найден")
            await state.clear()
            return
        
        # Проверяем срок действия
        if promo['expires_at']:
            expires_at = datetime.fromisoformat(promo['expires_at'].replace('Z', '+00:00'))
            if expires_at < datetime.now():
                await message.answer("❌ Срок действия промокода истек")
                await state.clear()
                return
        
        # Проверяем лимит активаций
        if promo['current_activations'] >= promo['max_activations']:
            await message.answer("❌ Лимит активаций промокода исчерпан")
            await state.clear()
            return
        
        # Проверяем, активировал ли уже пользователь
        cursor.execute(
            "SELECT * FROM promo_activations WHERE promo_id = ? AND user_id = ?",
            (promo['id'], message.from_user.id)
        )
        if cursor.fetchone():
            await message.answer("❌ Вы уже активировали этот промокод")
            await state.clear()
            return
        
        # Начисляем бонус
        user = await get_user(message.from_user.id)
        if not user:
            await cmd_start(message)
            return
        
        bonus_amount = 0
        
        if promo['bonus_type'] == 'fixed':
            bonus_amount = promo['bonus_value']
        elif promo['bonus_type'] == 'percent':
            bonus_amount = user['balance'] * promo['bonus_value'] / 100
        elif promo['bonus_type'] == 'freespins':
            # Логика для фриспинов
            bonus_amount = 0
            await message.answer(f"🎰 Вам начислено {promo['bonus_value']} фриспинов!")
        
        if bonus_amount > 0:
            await update_balance(message.from_user.id, bonus_amount, 'add')
        
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
    
    await state.clear()
    await message.answer(f"✅ Промокод успешно активирован! Начислено: {bonus_amount:.2f} $")

# Админ-панель
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    user = await get_user(message.from_user.id)
    
    if not user or not user['is_admin']:
        await message.answer("⛔ Доступ запрещен")
        return
    
    await message.answer("👑 Админ-панель", reply_markup=get_admin_keyboard())

@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Общая статистика
        cursor.execute("SELECT COUNT(*) as count FROM users")
        total_users = cursor.fetchone()['count']
        
        cursor.execute("SELECT SUM(total_deposit) as sum FROM users")
        total_deposit = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(total_withdrawal) as sum FROM users")
        total_withdrawal = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(games_played) as sum FROM users")
        total_games = cursor.fetchone()['sum'] or 0
        
        cursor.execute("SELECT SUM(balance) as sum FROM users")
        total_balance = cursor.fetchone()['sum'] or 0
        
        # Прибыль
        profit = total_deposit - total_withdrawal
        
        # RTP
        cursor.execute("SELECT SUM(bet_amount) as total_bet, SUM(win_amount) as total_win FROM games")
        games_stats = cursor.fetchone()
        total_bet = games_stats['total_bet'] or 0
        total_win = games_stats['total_win'] or 0
        rtp_fact = (total_win / total_bet * 100) if total_bet > 0 else 0
    
    text = (
        f"📊 Статистика\n\n"
        f"👥 Всего игроков: {total_users}\n"
        f"💰 Общий депозит: {total_deposit:.2f} $\n"
        f"💸 Общий вывод: {total_withdrawal:.2f} $\n"
        f"💎 Текущий баланс игроков: {total_balance:.2f} $\n"
        f"🎰 Сыграно игр: {total_games}\n"
        f"📈 Прибыль: {profit:.2f} $\n"
        f"📊 RTP (факт): {rtp_fact:.2f}%"
    )
    
    await message.answer(text)

@dp.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(BroadcastStates.waiting_for_message)
    await message.answer(
        "📢 Рассылка\n\n"
        "Отправьте сообщение для рассылки (можно с фото/видео):"
    )

@dp.message(BroadcastStates.waiting_for_message)
async def broadcast_preview(message: Message, state: FSMContext):
    await state.update_data(message=message)
    await state.set_state(BroadcastStates.waiting_for_confirmation)
    
    preview_text = message.caption or message.text or "Медиа сообщение"
    
    await message.answer(
        f"Предварительный просмотр:\n\n{preview_text}\n\n"
        "Подтвердите рассылку:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Подтвердить", callback_data="broadcast_confirm"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")
                ]
            ]
        )
    )

@dp.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    original_message = data['message']
    
    await callback.message.edit_text("⏳ Начинаю рассылку...")
    
    # Получаем всех пользователей
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
            elif original_message.video:
                await bot.send_video(
                    user['telegram_id'],
                    original_message.video.file_id,
                    caption=original_message.caption
                )
            sent += 1
            await asyncio.sleep(0.05)  # Защита от флуда
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user['telegram_id']}: {e}")
            failed += 1
    
    await state.clear()
    await callback.message.answer(
        f"✅ Рассылка завершена\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )

@dp.callback_query(F.data == "broadcast_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена")

@dp.message(F.text == "🎟 Промокоды")
async def promo_menu(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await message.answer("Управление промокодами:", reply_markup=get_promo_keyboard())

@dp.message(F.text == "➕ Создать промокод")
async def create_promo_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(PromoStates.waiting_for_promo_code)
    await message.answer("Введите код промокода (например, WELCOME100):")

@dp.message(PromoStates.waiting_for_promo_code)
async def create_promo_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.upper())
    await state.set_state(PromoStates.waiting_for_bonus_type)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Фиксированная сумма", callback_data="bonus_fixed")],
            [InlineKeyboardButton(text="📊 Процент от депозита", callback_data="bonus_percent")],
            [InlineKeyboardButton(text="🎰 Фриспины", callback_data="bonus_freespins")]
        ]
    )
    
    await message.answer("Выберите тип бонуса:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data and c.data.startswith('bonus_'))
async def create_promo_bonus_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bonus_type = callback.data.replace('bonus_', '')
    await state.update_data(bonus_type=bonus_type)
    await state.set_state(PromoStates.waiting_for_bonus_value)
    
    await callback.message.edit_text("Введите значение бонуса:")

@dp.message(PromoStates.waiting_for_bonus_value)
async def create_promo_bonus_value(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        await state.update_data(bonus_value=value)
        await state.set_state(PromoStates.waiting_for_expiry)
        await message.answer(
            "Введите срок действия (в днях) или 0 для бессрочного:"
        )
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число:")

@dp.message(PromoStates.waiting_for_expiry)
async def create_promo_expiry(message: Message, state: FSMContext):
    try:
        days = int(message.text)
        expires_at = None
        if days > 0:
            expires_at = (datetime.now() + timedelta(days=days)).isoformat()
        
        await state.update_data(expires_at=expires_at)
        await state.set_state(PromoStates.waiting_for_max_activations)
        await message.answer("Введите максимальное количество активаций:")
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число:")

@dp.message(PromoStates.waiting_for_max_activations)
async def create_promo_max_activations(message: Message, state: FSMContext):
    try:
        max_acts = int(message.text)
        await state.update_data(max_activations=max_acts)
        await state.set_state(PromoStates.waiting_for_min_deposit)
        await message.answer(
            "Введите минимальную сумму депозита для активации (0 - без ограничений):"
        )
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число:")

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
        await message.answer(f"✅ Промокод {data['code']} успешно создан!")
        
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму:")

@dp.message(F.text == "📋 Список промокодов")
async def list_promos(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM promocodes ORDER BY created_at DESC")
        promos = cursor.fetchall()
    
    if not promos:
        await message.answer("Промокоды не найдены")
        return
    
    text = "📋 Список промокодов:\n\n"
    for promo in promos:
        expires = "Бессрочно" if not promo['expires_at'] else promo['expires_at'][:10]
        text += (
            f"Код: {promo['code']}\n"
            f"Тип: {promo['bonus_type']}\n"
            f"Бонус: {promo['bonus_value']}\n"
            f"Активаций: {promo['current_activations']}/{promo['max_activations']}\n"
            f"Срок: {expires}\n"
            f"{'—' * 20}\n"
        )
    
    await message.answer(text)

@dp.message(F.text == "❌ Удалить промокод")
async def delete_promo_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or not user['is_admin']:
        return
    
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer("Введите код промокода для удаления:")

@dp.message(PromoStates.waiting_for_code)
async def delete_promo(message: Message, state: FSMContext):
    code = message.text.upper()
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM promocodes WHERE code = ?", (code,))
        conn.commit()
        
        if cursor.rowcount > 0:
            await message.answer(f"✅ Промокод {code} удален")
        else:
            await message.answer("❌ Промокод не найден")
    
    await state.clear()

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
