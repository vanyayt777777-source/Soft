#!/usr/bin/env python3
"""
Telegram Bot для анонимных сообщений и донатов с выводом через Crypto Bot
Один файл - всё включено
Токены загружаются из переменных окружения
"""

import os
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
import random
import string
from decimal import Decimal
import json

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.markdown import hbold, hitalic, hcode, hlink
import aiohttp

# ===================== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ =====================

# Получаем токены из переменных окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CRYPTO_PAY_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "545818:AAvQLMQHJbxqEou37HutdklFOJEO1agzhLp")

# ID администратора из переменных окружения (или значение по умолчанию)
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "8112176415"))
except ValueError:
    ADMIN_ID = 8112176415

# Параметры базы данных из переменных окружения
DB_CONFIG = {
    'user': os.environ.get("DB_USER", "bothost_db_c3b58a3f7c5d"),
    'password': os.environ.get("DB_PASSWORD", "F6qlOUcjcJJA6W5X7LhSh79TvckxtWi7A7-XI5dfHbk"),
    'database': os.environ.get("DB_NAME", "bothost_db_c3b58a3f7c5d"),
    'host': os.environ.get("DB_HOST", "node1.pghost.ru"),
    'port': int(os.environ.get("DB_PORT", "32802"))
}

# Настройки приложения
MIN_WITHDRAW = float(os.environ.get("MIN_WITHDRAW", "0.01"))
CHECK_LIFETIME = int(os.environ.get("CHECK_LIFETIME", "30"))
INVOICE_LIFETIME = int(os.environ.get("INVOICE_LIFETIME", "30"))

# Проверка наличия обязательных переменных
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена!")

# ===================== НАСТРОЙКА ЛОГИРОВАНИЯ =====================

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO")),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===================== СОСТОЯНИЯ FSM =====================

class WithdrawStates(StatesGroup):
    waiting_for_amount = State()

class DonateStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_message = State()
    waiting_for_payment = State()

class ReplyStates(StatesGroup):
    waiting_for_reply = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()

# ===================== РАБОТА С БАЗОЙ ДАННЫХ =====================

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Подключение к базе данных"""
        try:
            self.pool = await asyncpg.create_pool(**DB_CONFIG)
            logger.info("Подключение к БД установлено")
            await self.create_tables()
        except Exception as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise

    async def create_tables(self):
        """Создание таблиц, если их нет"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    balance DECIMAL(20,2) DEFAULT 0,
                    registered_at TIMESTAMP DEFAULT NOW(),
                    messages_received INT DEFAULT 0,
                    donations_received INT DEFAULT 0,
                    donations_sum DECIMAL(20,2) DEFAULT 0,
                    is_admin BOOLEAN DEFAULT FALSE,
                    last_activity TIMESTAMP DEFAULT NOW()
                )
            """)

            # Таблица сообщений
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    sender_id BIGINT,
                    receiver_id BIGINT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    is_reply BOOLEAN DEFAULT FALSE,
                    parent_id INT,
                    is_read BOOLEAN DEFAULT FALSE,
                    INDEX idx_receiver (receiver_id),
                    INDEX idx_created (created_at)
                )
            """)

            # Таблица донатов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS donations (
                    id SERIAL PRIMARY KEY,
                    sender_id BIGINT,
                    receiver_id BIGINT NOT NULL,
                    amount DECIMAL(20,2) NOT NULL,
                    message TEXT,
                    invoice_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    paid_at TIMESTAMP,
                    INDEX idx_receiver (receiver_id),
                    INDEX idx_status (status)
                )
            """)

            # Таблица чеков (вывод средств)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount DECIMAL(20,2) NOT NULL,
                    check_id TEXT,
                    check_url TEXT,
                    status TEXT DEFAULT 'created',
                    created_at TIMESTAMP DEFAULT NOW(),
                    activated_at TIMESTAMP,
                    INDEX idx_user (user_id),
                    INDEX idx_status (status)
                )
            """)

            # Таблица ссылок
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS referral_links (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    code TEXT UNIQUE NOT NULL,
                    type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    clicks INT DEFAULT 0,
                    INDEX idx_user (user_id),
                    INDEX idx_code (code)
                )
            """)

            # Таблица инвойсов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id SERIAL PRIMARY KEY,
                    invoice_id TEXT UNIQUE,
                    user_id BIGINT,
                    receiver_id BIGINT,
                    amount DECIMAL(20,2),
                    pay_url TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT NOW(),
                    expires_at TIMESTAMP,
                    INDEX idx_invoice (invoice_id),
                    INDEX idx_status (status)
                )
            """)

            logger.info("Таблицы созданы/проверены")

            # Устанавливаем админа
            await conn.execute(
                "UPDATE users SET is_admin = TRUE WHERE user_id = $1",
                ADMIN_ID
            )

    async def get_user(self, user_id: int) -> Optional[Dict]:
        """Получение пользователя по ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1",
                user_id
            )
            return dict(row) if row else None

    async def create_user(self, user_id: int, username: str = None, 
                         first_name: str = None, last_name: str = None) -> Dict:
        """Создание нового пользователя"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    last_activity = NOW()
            """, user_id, username, first_name, last_name)
            
            return await self.get_user(user_id)

    async def update_balance(self, user_id: int, amount: Decimal) -> bool:
        """Обновление баланса пользователя"""
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE users 
                SET balance = balance + $1,
                    last_activity = NOW()
                WHERE user_id = $2
            """, amount, user_id)
            return result == "UPDATE 1"

    async def add_donation(self, sender_id: Optional[int], receiver_id: int, 
                          amount: Decimal, message: str = None, invoice_id: str = None) -> int:
        """Добавление доната"""
        async with self.pool.acquire() as conn:
            # Добавляем запись о донате
            donation_id = await conn.fetchval("""
                INSERT INTO donations (sender_id, receiver_id, amount, message, invoice_id, status, paid_at)
                VALUES ($1, $2, $3, $4, $5, 'completed', NOW())
                RETURNING id
            """, sender_id, receiver_id, amount, message, invoice_id)

            # Обновляем статистику получателя
            await conn.execute("""
                UPDATE users 
                SET donations_received = donations_received + 1,
                    donations_sum = donations_sum + $1,
                    balance = balance + $1,
                    last_activity = NOW()
                WHERE user_id = $2
            """, amount, receiver_id)

            return donation_id

    async def add_message(self, sender_id: Optional[int], receiver_id: int, 
                         text: str, parent_id: int = None) -> int:
        """Добавление сообщения"""
        async with self.pool.acquire() as conn:
            msg_id = await conn.fetchval("""
                INSERT INTO messages (sender_id, receiver_id, text, parent_id, is_reply)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """, sender_id, receiver_id, text, parent_id, parent_id is not None)

            # Обновляем статистику
            await conn.execute("""
                UPDATE users 
                SET messages_received = messages_received + 1,
                    last_activity = NOW()
                WHERE user_id = $1
            """, receiver_id)

            return msg_id

    async def get_user_stats(self, user_id: int) -> Dict:
        """Получение статистики пользователя"""
        async with self.pool.acquire() as conn:
            # Основная статистика
            user = await self.get_user(user_id)
            
            # Последние сообщения
            last_messages = await conn.fetch("""
                SELECT text, created_at 
                FROM messages 
                WHERE receiver_id = $1 
                ORDER BY created_at DESC 
                LIMIT 3
            """, user_id)

            # Последние донаты
            last_donations = await conn.fetch("""
                SELECT amount, message, created_at 
                FROM donations 
                WHERE receiver_id = $1 
                ORDER BY created_at DESC 
                LIMIT 3
            """, user_id)

            return {
                "user": user,
                "last_messages": [dict(m) for m in last_messages],
                "last_donations": [dict(d) for d in last_donations]
            }

    async def create_withdrawal(self, user_id: int, amount: Decimal, 
                               check_id: str, check_url: str) -> int:
        """Создание записи о выводе"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO withdrawals (user_id, amount, check_id, check_url)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, user_id, amount, check_id, check_url)

    async def get_referral_link(self, user_id: int, link_type: str) -> Optional[Dict]:
        """Получение реферальной ссылки пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM referral_links 
                WHERE user_id = $1 AND type = $2
            """, user_id, link_type)
            return dict(row) if row else None

    async def create_referral_link(self, user_id: int, link_type: str) -> str:
        """Создание реферальной ссылки"""
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO referral_links (user_id, code, type)
                VALUES ($1, $2, $3)
                ON CONFLICT (code) DO NOTHING
            """, user_id, code, link_type)
            return code

    async def get_receiver_by_code(self, code: str) -> Optional[int]:
        """Получение ID получателя по коду ссылки"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT user_id FROM referral_links WHERE code = $1
            """, code)
            return row['user_id'] if row else None

    async def increment_link_clicks(self, code: str):
        """Увеличение счетчика кликов по ссылке"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE referral_links SET clicks = clicks + 1 WHERE code = $1
            """, code)

    async def get_all_users(self) -> List[Dict]:
        """Получение всех пользователей (для админа)"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM users ORDER BY registered_at DESC")
            return [dict(row) for row in rows]

    async def get_total_stats(self) -> Dict:
        """Получение общей статистики (для админа)"""
        async with self.pool.acquire() as conn:
            # Количество пользователей
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            active_today = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_activity > NOW() - INTERVAL '1 day'"
            )
            active_week = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_activity > NOW() - INTERVAL '7 days'"
            )

            # Сумма донатов
            total_donations = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM donations WHERE status = 'completed'"
            )
            
            # Выведено средств
            total_withdrawn = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE status = 'activated'"
            )

            # Количество чеков
            checks_count = await conn.fetchval("SELECT COUNT(*) FROM withdrawals")
            
            # Количество сообщений
            messages_count = await conn.fetchval("SELECT COUNT(*) FROM messages")

            return {
                "users_count": users_count,
                "active_today": active_today,
                "active_week": active_week,
                "total_donations": float(total_donations),
                "total_withdrawn": float(total_withdrawn),
                "checks_count": checks_count,
                "messages_count": messages_count
            }

# ===================== ИНТЕГРАЦИЯ С CRYPTO BOT =====================

class CryptoPayAPI:
    def __init__(self, token: str):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Выполнение запроса к API"""
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "Crypto-Pay-API-Token": self.token,
            "Content-Type": "application/json"
        }
        
        try:
            async with self.session.request(method, url, json=data, headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("ok"):
                        return result["result"]
                    else:
                        raise Exception(f"API Error: {result.get('error')}")
                else:
                    text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {text}")
        except Exception as e:
            logger.error(f"Ошибка запроса к Crypto Pay API: {e}")
            raise

    async def create_invoice(self, asset: str, amount: float, 
                            description: str = None, payload: str = None) -> dict:
        """Создание инвойса для оплаты"""
        data = {
            "asset": asset,
            "amount": str(amount),
            "description": description,
            "payload": payload,
            "expires_in": INVOICE_LIFETIME * 60  # в секундах
        }
        return await self._request("POST", "createInvoice", data)

    async def create_check(self, asset: str, amount: float, pin: str = None) -> dict:
        """Создание чека для вывода средств"""
        data = {
            "asset": asset,
            "amount": str(amount)
        }
        if pin:
            data["pin"] = pin
            
        return await self._request("POST", "createCheck", data)

    async def get_invoice_status(self, invoice_id: int) -> dict:
        """Получение статуса инвойса"""
        data = {"invoice_id": invoice_id}
        return await self._request("GET", "getInvoices", data)

    async def get_check_status(self, check_id: int) -> dict:
        """Получение статуса чека"""
        data = {"check_id": check_id}
        return await self._request("GET", "getChecks", data)

# ===================== КЛАВИАТУРЫ =====================

class Keyboards:
    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        """Главное меню"""
        buttons = [
            [KeyboardButton(text="📋 Мой профиль"), KeyboardButton(text="🔗 Мои ссылки")],
            [KeyboardButton(text="❓ Помощь")]
        ]
        return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

    @staticmethod
    def profile_inline(user_id: int) -> InlineKeyboardMarkup:
        """Инлайн кнопки в профиле"""
        buttons = [
            [InlineKeyboardButton(text="💸 ВЫВЕСТИ СРЕДСТВА", callback_data=f"withdraw_{user_id}")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_{user_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def links_inline(user_id: int, msg_code: str, donate_code: str) -> InlineKeyboardMarkup:
        """Инлайн кнопки для ссылок"""
        buttons = [
            [
                InlineKeyboardButton(text="🔗 Копировать сообщения", callback_data=f"copy_msg_{msg_code}"),
                InlineKeyboardButton(text="📤 Поделиться сообщения", url=f"https://t.me/share/url?url=t.me/{(await bot.get_me()).username}?start=msg_{msg_code}")
            ],
            [
                InlineKeyboardButton(text="🔗 Копировать донаты", callback_data=f"copy_donate_{donate_code}"),
                InlineKeyboardButton(text="📤 Поделиться донаты", url=f"https://t.me/share/url?url=t.me/{(await bot.get_me()).username}?start=donate_{donate_code}")
            ],
            [InlineKeyboardButton(text="🔄 Сгенерировать новые", callback_data=f"regenerate_{user_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def message_actions(message_id: int) -> InlineKeyboardMarkup:
        """Действия с сообщением"""
        buttons = [
            [
                InlineKeyboardButton(text="✍️ ОТВЕТИТЬ", callback_data=f"reply_{message_id}"),
                InlineKeyboardButton(text="🗑 УДАЛИТЬ", callback_data=f"delete_msg_{message_id}")
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def donation_actions(donation_id: int) -> InlineKeyboardMarkup:
        """Действия с донатом"""
        buttons = [
            [
                InlineKeyboardButton(text="💸 ВЫВЕСТИ", callback_data="withdraw_menu"),
                InlineKeyboardButton(text="✍️ ОТВЕТИТЬ", callback_data=f"reply_donate_{donation_id}")
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def payment_actions(invoice_id: str) -> InlineKeyboardMarkup:
        """Действия с платежом"""
        buttons = [
            [InlineKeyboardButton(text="✅ ПРОВЕРИТЬ ОПЛАТУ", callback_data=f"check_payment_{invoice_id}")],
            [InlineKeyboardButton(text="✍️ НАПИСАТЬ СООБЩЕНИЕ", callback_data=f"write_msg_{invoice_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def admin_menu() -> InlineKeyboardMarkup:
        """Админ меню"""
        buttons = [
            [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 РАССЫЛКА", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="👥 ПОЛЬЗОВАТЕЛИ", callback_data="admin_users")],
            [InlineKeyboardButton(text="💰 ВСЕ ТРАНЗАКЦИИ", callback_data="admin_transactions")],
            [InlineKeyboardButton(text="🎫 ВСЕ ЧЕКИ", callback_data="admin_checks")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

# ===================== ОСНОВНОЙ КЛАСС БОТА =====================

class AnonDonateBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.db = Database()
        self.keyboards = Keyboards()
        self.crypto = None
        self._register_handlers()

    def _register_handlers(self):
        """Регистрация всех обработчиков"""
        
        # Команды
        self.dp.message.register(self.cmd_start, CommandStart())
        self.dp.message.register(self.cmd_admin, Command("admin"))
        
        # Главное меню
        self.dp.message.register(self.profile_handler, F.text == "📋 Мой профиль")
        self.dp.message.register(self.links_handler, F.text == "🔗 Мои ссылки")
        self.dp.message.register(self.help_handler, F.text == "❓ Помощь")
        
        # Callback-запросы
        self.dp.callback_query.register(self.process_callback)
        
        # FSM состояния
        self.dp.message.register(self.process_withdraw_amount, WithdrawStates.waiting_for_amount)
        self.dp.message.register(self.process_donate_amount, DonateStates.waiting_for_amount)
        self.dp.message.register(self.process_donate_message, DonateStates.waiting_for_message)
        self.dp.message.register(self.process_reply, ReplyStates.waiting_for_reply)
        self.dp.message.register(self.process_broadcast, AdminStates.waiting_for_broadcast)
        
        # Обработка оплаты
        self.dp.callback_query.register(self.check_payment, F.data.startswith("check_payment_"))
        
        # Обработка ссылок
        self.dp.message.register(self.handle_start_param, CommandStart(deep_link=True))

    async def start(self):
        """Запуск бота"""
        await self.db.connect()
        self.crypto = CryptoPayAPI(CRYPTO_PAY_TOKEN)
        
        # Пропускаем накопившиеся обновления
        await self.bot.delete_webhook(drop_pending_updates=True)
        
        # Запускаем polling
        logger.info("Бот запущен!")
        await self.dp.start_polling(self.bot)

    # ===================== ОБРАБОТЧИКИ КОМАНД =====================

    async def cmd_start(self, message: Message):
        """Обработчик /start"""
        user = message.from_user
        await self.db.create_user(
            user.id, user.username, user.first_name, user.last_name
        )
        
        welcome_text = (
            f"👋 {hbold('Добро пожаловать!')}\n\n"
            f"Этот бот позволяет получать анонимные сообщения и донаты через уникальные ссылки.\n\n"
            f"📝 {hbold('Как это работает:')}\n"
            f"1️⃣ Создай ссылки в разделе {hcode('🔗 Мои ссылки')}\n"
            f"2️⃣ Поделись ссылками с друзьями\n"
            f"3️⃣ Получай анонимные сообщения и донаты\n"
            f"4️⃣ Выводи средства на Crypto Bot через чеки\n\n"
            f"Выбери действие в меню ниже 👇"
        )
        
        await message.answer(
            welcome_text,
            reply_markup=self.keyboards.main_menu(),
            parse_mode="HTML"
        )

    async def cmd_admin(self, message: Message):
        """Обработчик /admin"""
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ Доступ запрещен!")
            return
        
        await message.answer(
            f"{hbold('👑 Админ-панель')}\n\nВыберите действие:",
            reply_markup=self.keyboards.admin_menu(),
            parse_mode="HTML"
        )

    # ===================== ОБРАБОТЧИКИ ГЛАВНОГО МЕНЮ =====================

    async def profile_handler(self, message: Message):
        """Обработчик профиля"""
        user_id = message.from_user.id
        stats = await self.db.get_user_stats(user_id)
        user = stats["user"]
        
        # Формируем текст профиля
        profile_text = (
            f"{hbold('👤 Ваш профиль')}\n\n"
            f"💰 {hbold('Баланс:')} {user['balance']} USDT\n\n"
            f"{hbold('📊 Статистика:')}\n"
            f"✉️ Получено сообщений: {user['messages_received']}\n"
            f"💸 Получено донатов: {user['donations_received']} на сумму {user['donations_sum']} USDT\n\n"
        )
        
        # Последние сообщения
        if stats["last_messages"]:
            profile_text += f"{hbold('📥 Последние 3 сообщения:')}\n"
            for msg in stats["last_messages"]:
                time_ago = self._format_time_ago(msg['created_at'])
                text = msg['text'][:30] + "..." if len(msg['text']) > 30 else msg['text']
                profile_text += f"• {hitalic(f'\"{text}\"')} ({time_ago})\n"
        else:
            profile_text += f"{hitalic('📥 Нет сообщений')}\n"
        
        profile_text += "\n"
        
        # Последние донаты
        if stats["last_donations"]:
            profile_text += f"{hbold('🎁 Последние 3 доната:')}\n"
            for don in stats["last_donations"]:
                time_ago = self._format_time_ago(don['created_at'])
                msg = f" \"{don['message']}\"" if don.get('message') else ""
                profile_text += f"• +{don['amount']} USDT{msg} ({time_ago})\n"
        else:
            profile_text += f"{hitalic('🎁 Нет донатов')}"
        
        await message.answer(
            profile_text,
            reply_markup=self.keyboards.profile_inline(user_id),
            parse_mode="HTML"
        )

    async def links_handler(self, message: Message):
        """Обработчик ссылок"""
        user_id = message.from_user.id
        bot_username = (await self.bot.get_me()).username
        
        # Получаем или создаем ссылки
        msg_link = await self.db.get_referral_link(user_id, "message")
        donate_link = await self.db.get_referral_link(user_id, "donate")
        
        if not msg_link:
            code = await self.db.create_referral_link(user_id, "message")
            msg_link = {"code": code}
        if not donate_link:
            code = await self.db.create_referral_link(user_id, "donate")
            donate_link = {"code": code}
        
        # Получаем статистику
        user = await self.db.get_user(user_id)
        
        links_text = (
            f"{hbold('🔗 Твои персональные ссылки')}\n\n"
            f"{hbold('📝 Для анонимных сообщений:')}\n"
            f"{hcode(f't.me/{bot_username}?start=msg_{msg_link["code"]}')}\n\n"
            f"{hbold('💰 Для анонимных донатов:')}\n"
            f"{hcode(f't.me/{bot_username}?start=donate_{donate_link["code"]}')}\n\n"
            f"{hbold('📊 Статистика ссылок:')}\n"
            f"• Сообщений получено: {user['messages_received']}\n"
            f"• Донатов получено: {user['donations_received']} на сумму {user['donations_sum']} USDT"
        )
        
        # Временно сохраняем bot_username для использования в клавиатуре
        self._bot_username = bot_username
        
        await message.answer(
            links_text,
            reply_markup=self.keyboards.links_inline(user_id, msg_link["code"], donate_link["code"]),
            parse_mode="HTML"
        )

    async def help_handler(self, message: Message):
        """Обработчик помощи"""
        help_text = (
            f"{hbold('❓ Помощь и инструкция')}\n\n"
            f"{hbold('📝 Как получать анонимные сообщения:')}\n"
            f"1️⃣ Перейди в раздел {hcode('🔗 Мои ссылки')}\n"
            f"2️⃣ Скопируй ссылку для сообщений\n"
            f"3️⃣ Отправь её друзьям\n"
            f"4️⃣ Когда кто-то напишет, ты получишь уведомление\n\n"
            f"{hbold('💰 Как получать донаты:')}\n"
            f"1️⃣ Используй ссылку для донатов\n"
            f"2️⃣ Отправитель оплачивает через Crypto Bot\n"
            f"3️⃣ Средства зачисляются на твой баланс\n\n"
            f"{hbold('💸 Как вывести средства:')}\n"
            f"1️⃣ Зайди в {hcode('📋 Мой профиль')}\n"
            f"2️⃣ Нажми {hcode('💸 ВЫВЕСТИ СРЕДСТВА')}\n"
            f"3️⃣ Введи сумму\n"
            f"4️⃣ Получи чек и активируй в @CryptoBot\n\n"
            f"{hbold('📞 Поддержка:')}\n"
            f"По всем вопросам: @support_username"
        )
        
        await message.answer(help_text, parse_mode="HTML")

    # ===================== ОБРАБОТЧИКИ CALLBACK =====================

    async def process_callback(self, callback: CallbackQuery):
        """Обработка всех callback-запросов"""
        data = callback.data
        user_id = callback.from_user.id
        
        try:
            if data.startswith("withdraw_"):
                await self.start_withdraw(callback)
            elif data.startswith("refresh_"):
                await self.refresh_profile(callback)
            elif data.startswith("copy_msg_"):
                await self.copy_link(callback, "msg")
            elif data.startswith("copy_donate_"):
                await self.copy_link(callback, "donate")
            elif data.startswith("regenerate_"):
                await self.regenerate_links(callback)
            elif data.startswith("reply_"):
                await self.start_reply(callback)
            elif data.startswith("delete_msg_"):
                await self.delete_message(callback)
            elif data == "withdraw_menu":
                await self.start_withdraw(callback)
            elif data.startswith("check_payment_"):
                await self.check_payment(callback)
            elif data.startswith("write_msg_"):
                await self.start_write_message(callback)
            elif data.startswith("admin_"):
                await self.process_admin_callback(callback)
            elif data == "cancel":
                await self.cancel_action(callback)
                
            await callback.answer()
        except Exception as e:
            logger.error(f"Ошибка в callback {data}: {e}")
            await callback.answer("❌ Произошла ошибка", show_alert=True)

    async def start_withdraw(self, callback: CallbackQuery):
        """Начало процесса вывода средств"""
        user_id = callback.from_user.id
        user = await self.db.get_user(user_id)
        
        if user['balance'] < MIN_WITHDRAW:
            await callback.message.edit_text(
                f"{hbold('❌ Недостаточно средств')}\n\n"
                f"Твой баланс: {user['balance']} USDT\n"
                f"Минимальная сумма вывода: {MIN_WITHDRAW} USDT",
                parse_mode="HTML"
            )
            return
        
        await callback.message.edit_text(
            f"{hbold('💸 Вывод средств')}\n\n"
            f"Твой баланс: {hbold(f'{user['balance']} USDT')}\n"
            f"Минимальная сумма вывода: {hbold(f'{MIN_WITHDRAW} USDT')}\n\n"
            f"{hitalic('Введи сумму для вывода (например: 0.05):')}",
            parse_mode="HTML"
        )
        
        # Устанавливаем состояние
        state = FSMContext(self.storage, user_id, user_id)
        await state.set_state(WithdrawStates.waiting_for_amount)

    async def process_withdraw_amount(self, message: Message, state: FSMContext):
        """Обработка введенной суммы для вывода"""
        try:
            amount = float(message.text.replace(',', '.'))
            user_id = message.from_user.id
            user = await self.db.get_user(user_id)
            
            # Проверки
            if amount < MIN_WITHDRAW:
                await message.answer(
                    f"❌ Сумма должна быть не меньше {MIN_WITHDRAW} USDT\n"
                    f"Попробуй еще раз или отправь /cancel"
                )
                return
            
            if amount > user['balance']:
                await message.answer(
                    f"❌ Недостаточно средств. Твой баланс: {user['balance']} USDT\n"
                    f"Попробуй еще раз или отправь /cancel"
                )
                return
            
            # Создаем чек через Crypto Bot
            async with self.crypto as crypto:
                try:
                    check = await crypto.create_check("USDT", amount)
                    
                    # Сохраняем в БД
                    await self.db.create_withdrawal(
                        user_id, 
                        Decimal(str(amount)),
                        check['check_id'],
                        check['check_url']
                    )
                    
                    # Уменьшаем баланс
                    await self.db.update_balance(user_id, Decimal(str(-amount)))
                    
                    # Получаем новый баланс
                    new_user = await self.db.get_user(user_id)
                    
                    # Отправляем результат
                    await message.answer(
                        f"{hbold('✅ Чек создан!')}\n\n"
                        f"💰 {hbold('Сумма:')} {amount} USDT\n\n"
                        f"🔗 {hbold('Ссылка-чек:')}\n"
                        f"{hcode(check['check_url'])}\n\n"
                        f"{hbold('📌 Инструкция:')}\n"
                        f"1️⃣ Перейди по ссылке\n"
                        f"2️⃣ Откроется Crypto Bot\n"
                        f"3️⃣ Нажми {hcode('Забрать')}\n"
                        f"4️⃣ Средства зачислятся на твой баланс\n\n"
                        f"⚠️ Чек действителен {CHECK_LIFETIME} дней\n\n"
                        f"Твой новый баланс: {hbold(f'{new_user['balance']} USDT')}",
                        parse_mode="HTML"
                    )
                    
                except Exception as e:
                    logger.error(f"Ошибка создания чека: {e}")
                    await message.answer(
                        "❌ Ошибка при создании чека. Попробуй позже."
                    )
            
            await state.clear()
            
        except ValueError:
            await message.answer(
                "❌ Неверный формат суммы. Введи число (например: 0.05)\n"
                "Или отправь /cancel для отмены"
            )

    async def handle_start_param(self, message: Message, command: CommandStart):
        """Обработка переходов по ссылкам с параметрами"""
        args = command.args
        if not args:
            await self.cmd_start(message)
            return
        
        # Определяем тип ссылки
        if args.startswith("msg_"):
            code = args[4:]
            await self.process_message_link(message, code)
        elif args.startswith("donate_"):
            code = args[7:]
            await self.process_donate_link(message, code)
        else:
            await self.cmd_start(message)

    async def process_message_link(self, message: Message, code: str):
        """Обработка ссылки на сообщение"""
        receiver_id = await self.db.get_receiver_by_code(code)
        if not receiver_id:
            await message.answer("❌ Ссылка недействительна")
            return
        
        # Увеличиваем счетчик кликов
        await self.db.increment_link_clicks(code)
        
        # Сохраняем получателя в контексте
        state = FSMContext(self.storage, message.from_user.id, message.from_user.id)
        await state.update_data(receiver_id=receiver_id)
        
        await message.answer(
            f"{hbold('📝 Анонимное сообщение')}\n\n"
            f"Напиши текст сообщения. Оно будет доставлено анонимно.",
            parse_mode="HTML"
        )
        
        await state.set_state(ReplyStates.waiting_for_reply)

    async def process_donate_link(self, message: Message, code: str):
        """Обработка ссылки на донат"""
        receiver_id = await self.db.get_receiver_by_code(code)
        if not receiver_id:
            await message.answer("❌ Ссылка недействительна")
            return
        
        # Увеличиваем счетчик кликов
        await self.db.increment_link_clicks(code)
        
        # Сохраняем получателя в контексте
        state = FSMContext(self.storage, message.from_user.id, message.from_user.id)
        await state.update_data(receiver_id=receiver_id)
        
        await message.answer(
            f"{hbold('💰 Анонимный донат')}\n\n"
            f"Введите сумму в USDT (минимум {MIN_WITHDRAW}):",
            parse_mode="HTML"
        )
        
        await state.set_state(DonateStates.waiting_for_amount)

    async def process_donate_amount(self, message: Message, state: FSMContext):
        """Обработка суммы доната"""
        try:
            amount = float(message.text.replace(',', '.'))
            
            if amount < MIN_WITHDRAW:
                await message.answer(
                    f"❌ Сумма должна быть не меньше {MIN_WITHDRAW} USDT\n"
                    f"Попробуй еще раз или отправь /cancel"
                )
                return
            
            # Сохраняем сумму
            await state.update_data(amount=amount)
            
            # Создаем инвойс
            data = await state.get_data()
            receiver_id = data.get('receiver_id')
            
            async with self.crypto as crypto:
                try:
                    invoice = await crypto.create_invoice(
                        "USDT",
                        amount,
                        description=f"Донат пользователю {receiver_id}",
                        payload=f"donate_{receiver_id}"
                    )
                    
                    # Сохраняем инвойс в контексте
                    await state.update_data(
                        invoice_id=invoice['invoice_id'],
                        pay_url=invoice['pay_url']
                    )
                    
                    await message.answer(
                        f"{hbold('💳 Оплата доната')}\n\n"
                        f"Сумма: {hbold(f'{amount} USDT')}\n\n"
                        f"Ссылка для оплаты (действительна {INVOICE_LIFETIME} мин):\n"
                        f"{hlink('👉 Перейти к оплате', invoice['pay_url'])}\n\n"
                        f"После оплаты нажми кнопку проверки:",
                        reply_markup=self.keyboards.payment_actions(invoice['invoice_id']),
                        parse_mode="HTML"
                    )
                    
                except Exception as e:
                    logger.error(f"Ошибка создания инвойса: {e}")
                    await message.answer("❌ Ошибка при создании платежа")
                    await state.clear()
                    
        except ValueError:
            await message.answer(
                "❌ Неверный формат суммы. Введи число (например: 0.05)\n"
                "Или отправь /cancel для отмены"
            )

    async def check_payment(self, callback: CallbackQuery):
        """Проверка статуса платежа"""
        invoice_id = callback.data.replace("check_payment_", "")
        
        async with self.crypto as crypto:
            try:
                # Проверяем статус инвойса
                invoice_info = await crypto.get_invoice_status(invoice_id)
                
                if invoice_info and invoice_info.get('status') == 'paid':
                    # Получаем данные из контекста
                    state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
                    data = await state.get_data()
                    
                    receiver_id = data.get('receiver_id')
                    amount = data.get('amount')
                    
                    # Сохраняем донат в БД
                    await self.db.add_donation(
                        callback.from_user.id,
                        receiver_id,
                        Decimal(str(amount)),
                        invoice_id=invoice_id
                    )
                    
                    # Уведомляем получателя
                    await self.bot.send_message(
                        receiver_id,
                        f"{hbold('🎉 Вам зачислили донат!')}\n\n"
                        f"💰 {hbold('Сумма:')} {amount} USDT\n\n"
                        f"Текущий баланс обновлен.\n\n"
                        f"Можете ответить отправителю анонимно.",
                        reply_markup=self.keyboards.donation_actions(0),  # TODO: передать ID доната
                        parse_mode="HTML"
                    )
                    
                    await callback.message.edit_text(
                        f"{hbold('✅ Оплата получена!')}\n\n"
                        f"Спасибо за донат!",
                        parse_mode="HTML"
                    )
                    
                    # Предлагаем написать сообщение
                    await callback.message.answer(
                        "Хотите написать анонимное сообщение получателю?",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✍️ Написать", callback_data=f"write_msg_{invoice_id}")],
                            [InlineKeyboardButton(text="🚫 Нет, спасибо", callback_data="cancel")]
                        ])
                    )
                    
                    await state.clear()
                else:
                    await callback.answer("⏳ Платеж еще не получен", show_alert=True)
                    
            except Exception as e:
                logger.error(f"Ошибка проверки платежа: {e}")
                await callback.answer("❌ Ошибка проверки", show_alert=True)

    async def start_write_message(self, callback: CallbackQuery):
        """Начало написания сообщения после доната"""
        invoice_id = callback.data.replace("write_msg_", "")
        
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        await state.update_data(invoice_id=invoice_id)
        
        await callback.message.edit_text(
            f"{hbold('✍️ Напишите сообщение')}\n\n"
            f"Оно будет доставлено анонимно получателю:",
            parse_mode="HTML"
        )
        
        await state.set_state(DonateStates.waiting_for_message)

    async def process_donate_message(self, message: Message, state: FSMContext):
        """Обработка сообщения после доната"""
        data = await state.get_data()
        invoice_id = data.get('invoice_id')
        
        # Получаем информацию о донате
        async with self.pool.acquire() as conn:
            donation = await conn.fetchrow(
                "SELECT * FROM donations WHERE invoice_id = $1",
                invoice_id
            )
        
        if donation:
            # Сохраняем сообщение
            await self.db.add_message(
                message.from_user.id,
                donation['receiver_id'],
                message.text,
                None
            )
            
            # Уведомляем получателя
            await self.bot.send_message(
                donation['receiver_id'],
                f"{hbold('📩 Новое анонимное сообщение')}\n\n"
                f"\"{message.text}\"",
                reply_markup=self.keyboards.message_actions(0),  # TODO: передать ID сообщения
                parse_mode="HTML"
            )
            
            await message.answer("✅ Сообщение доставлено!")
        
        await state.clear()

    async def start_reply(self, callback: CallbackQuery):
        """Начало ответа на сообщение"""
        # Извлекаем ID сообщения из callback_data
        msg_id = int(callback.data.replace("reply_", ""))
        
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        await state.update_data(reply_to_msg=msg_id)
        
        await callback.message.edit_text(
            f"{hbold('✍️ Напишите ответ')}\n\n"
            f"Он будет доставлен анонимно:",
            parse_mode="HTML"
        )
        
        await state.set_state(ReplyStates.waiting_for_reply)

    async def process_reply(self, message: Message, state: FSMContext):
        """Обработка ответа на сообщение"""
        data = await state.get_data()
        original_msg_id = data.get('reply_to_msg')
        
        # Получаем оригинальное сообщение
        async with self.pool.acquire() as conn:
            original = await conn.fetchrow(
                "SELECT * FROM messages WHERE id = $1",
                original_msg_id
            )
        
        if original:
            # Сохраняем ответ
            await self.db.add_message(
                message.from_user.id,
                original['sender_id'] if original['sender_id'] else None,
                message.text,
                original_msg_id
            )
            
            # Уведомляем отправителя, если он известен
            if original['sender_id']:
                await self.bot.send_message(
                    original['sender_id'],
                    f"{hbold('📩 Ответ на ваше сообщение')}\n\n"
                    f"\"{message.text}\"",
                    parse_mode="HTML"
                )
            
            await message.answer("✅ Ответ отправлен!")
        
        await state.clear()

    async def delete_message(self, callback: CallbackQuery):
        """Удаление сообщения"""
        msg_id = int(callback.data.replace("delete_msg_", ""))
        
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE id = $1",
                msg_id
            )
        
        await callback.message.edit_text("✅ Сообщение удалено")

    async def refresh_profile(self, callback: CallbackQuery):
        """Обновление профиля"""
        user_id = callback.from_user.id
        stats = await self.db.get_user_stats(user_id)
        user = stats["user"]
        
        profile_text = (
            f"{hbold('👤 Ваш профиль')}\n\n"
            f"💰 {hbold('Баланс:')} {user['balance']} USDT\n\n"
            f"{hbold('📊 Статистика:')}\n"
            f"✉️ Получено сообщений: {user['messages_received']}\n"
            f"💸 Получено донатов: {user['donations_received']} на сумму {user['donations_sum']} USDT\n\n"
        )
        
        await callback.message.edit_text(
            profile_text,
            reply_markup=self.keyboards.profile_inline(user_id),
            parse_mode="HTML"
        )

    async def copy_link(self, callback: CallbackQuery, link_type: str):
        """Копирование ссылки"""
        code = callback.data.replace(f"copy_{link_type}_", "")
        bot_username = (await self.bot.get_me()).username
        
        await callback.answer(
            f"Ссылка скопирована!",
            show_alert=False
        )
        
        # Отправляем ссылку отдельным сообщением для удобства копирования
        await callback.message.answer(
            f"{hcode(f't.me/{bot_username}?start={link_type}_{code}')}",
            parse_mode="HTML"
        )

    async def regenerate_links(self, callback: CallbackQuery):
        """Перегенерация ссылок"""
        user_id = callback.from_user.id
        
        # Удаляем старые ссылки
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM referral_links WHERE user_id = $1",
                user_id
            )
        
        # Создаем новые
        msg_code = await self.db.create_referral_link(user_id, "message")
        donate_code = await self.db.create_referral_link(user_id, "donate")
        
        bot_username = (await self.bot.get_me()).username
        
        await callback.message.edit_text(
            f"{hbold('✅ Новые ссылки созданы!')}\n\n"
            f"{hbold('📝 Для сообщений:')}\n"
            f"{hcode(f't.me/{bot_username}?start=msg_{msg_code}')}\n\n"
            f"{hbold('💰 Для донатов:')}\n"
            f"{hcode(f't.me/{bot_username}?start=donate_{donate_code}')}",
            reply_markup=self.keyboards.links_inline(user_id, msg_code, donate_code),
            parse_mode="HTML"
        )

    async def cancel_action(self, callback: CallbackQuery):
        """Отмена действия"""
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        await state.clear()
        
        await callback.message.edit_text(
            "❌ Действие отменено",
            reply_markup=self.keyboards.main_menu()
        )

    # ===================== АДМИН-ПАНЕЛЬ =====================

    async def process_admin_callback(self, callback: CallbackQuery):
        """Обработка админ-команд"""
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("⛔ Доступ запрещен", show_alert=True)
            return
        
        action = callback.data.replace("admin_", "")
        
        if action == "stats":
            await self.admin_stats(callback)
        elif action == "broadcast":
            await self.admin_broadcast(callback)
        elif action == "users":
            await self.admin_users(callback)
        elif action == "transactions":
            await self.admin_transactions(callback)
        elif action == "checks":
            await self.admin_checks(callback)

    async def admin_stats(self, callback: CallbackQuery):
        """Показ статистики для админа"""
        stats = await self.db.get_total_stats()
        
        text = (
            f"{hbold('📊 Общая статистика')}\n\n"
            f"👥 Всего пользователей: {stats['users_count']}\n"
            f"📈 Активных сегодня: {stats['active_today']}\n"
            f"📈 Активных за неделю: {stats['active_week']}\n\n"
            f"💰 Сумма всех донатов: {stats['total_donations']} USDT\n"
            f"💸 Выведено средств: {stats['total_withdrawn']} USDT\n"
            f"🎫 Создано чеков: {stats['checks_count']}\n"
            f"📝 Всего сообщений: {stats['messages_count']}"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.admin_menu(),
            parse_mode="HTML"
        )

    async def admin_broadcast(self, callback: CallbackQuery):
        """Начало рассылки"""
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        
        await callback.message.edit_text(
            f"{hbold('📢 Создание рассылки')}\n\n"
            f"Отправь сообщение, которое получат все пользователи бота.\n\n"
            f"Поддерживается форматирование и эмодзи.\n\n"
            f"Для отмены отправь /cancel",
            parse_mode="HTML"
        )
        
        await state.set_state(AdminStates.waiting_for_broadcast)

    async def process_broadcast(self, message: Message, state: FSMContext):
        """Обработка сообщения для рассылки"""
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ Доступ запрещен")
            await state.clear()
            return
        
        # Получаем всех пользователей
        users = await self.db.get_all_users()
        
        sent = 0
        failed = 0
        
        status_msg = await message.answer("📤 Начинаю рассылку...")
        
        for user in users:
            try:
                await self.bot.copy_message(
                    user['user_id'],
                    message.from_user.id,
                    message.message_id
                )
                sent += 1
                await asyncio.sleep(0.05)  # Защита от флуда
            except Exception as e:
                failed += 1
                logger.error(f"Ошибка отправки пользователю {user['user_id']}: {e}")
        
        await status_msg.edit_text(
            f"{hbold('✅ Рассылка завершена!')}\n\n"
            f"📊 Статистика:\n"
            f"✅ Успешно: {sent}\n"
            f"❌ Ошибок: {failed}",
            parse_mode="HTML"
        )
        
        await state.clear()

    async def admin_users(self, callback: CallbackQuery):
        """Показ списка пользователей"""
        users = await self.db.get_all_users()
        
        text = f"{hbold('👥 Последние 10 пользователей:')}\n\n"
        
        for user in users[:10]:
            text += (
                f"ID: {user['user_id']}\n"
                f"Username: @{user['username'] if user['username'] else 'нет'}\n"
                f"Баланс: {user['balance']} USDT\n"
                f"Регистрация: {user['registered_at'].strftime('%d.%m.%Y')}\n"
                f"---\n"
            )
        
        text += f"\nВсего пользователей: {len(users)}"
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.admin_menu(),
            parse_mode="HTML"
        )

    async def admin_transactions(self, callback: CallbackQuery):
        """Показ всех транзакций"""
        async with self.pool.acquire() as conn:
            donations = await conn.fetch("""
                SELECT * FROM donations 
                ORDER BY created_at DESC 
                LIMIT 10
            """)
        
        text = f"{hbold('💰 Последние 10 донатов:')}\n\n"
        
        for don in donations:
            text += (
                f"От: {don['sender_id'] if don['sender_id'] else 'аноним'}\n"
                f"Кому: {don['receiver_id']}\n"
                f"Сумма: {don['amount']} USDT\n"
                f"Статус: {don['status']}\n"
                f"Дата: {don['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
                f"---\n"
            )
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.admin_menu(),
            parse_mode="HTML"
        )

    async def admin_checks(self, callback: CallbackQuery):
        """Показ всех чеков"""
        async with self.pool.acquire() as conn:
            checks = await conn.fetch("""
                SELECT * FROM withdrawals 
                ORDER BY created_at DESC 
                LIMIT 10
            """)
        
        text = f"{hbold('🎫 Последние 10 чеков:')}\n\n"
        
        for check in checks:
            text += (
                f"Пользователь: {check['user_id']}\n"
                f"Сумма: {check['amount']} USDT\n"
                f"Статус: {check['status']}\n"
                f"Дата: {check['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
                f"---\n"
            )
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.admin_menu(),
            parse_mode="HTML"
        )

    # ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

    def _format_time_ago(self, dt: datetime) -> str:
        """Форматирование времени (например, '2 часа назад')"""
        now = datetime.now()
        diff = now - dt
        
        if diff.days > 0:
            return f"{diff.days} дн. назад"
        elif diff.seconds > 3600:
            hours = diff.seconds // 3600
            return f"{hours} ч. назад"
        elif diff.seconds > 60:
            minutes = diff.seconds // 60
            return f"{minutes} мин. назад"
        else:
            return "только что"

# ===================== ТОЧКА ВХОДА =====================

async def main():
    """Главная функция"""
    bot = AnonDonateBot()
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
