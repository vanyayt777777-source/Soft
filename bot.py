#!/usr/bin/env python3
"""
Telegram Bot для анонимных сообщений и донатов с выводом через Crypto Bot
"""

import os
import asyncio
import logging
import random
import string
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, List

import asyncpg
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.markdown import hbold, hitalic, hcode

# ===================== КОНФИГУРАЦИЯ =====================

# Токены из переменных окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CRYPTO_PAY_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "545818:AAvQLMQHJbxqEou37HutdklFOJEO1agzhLp")

# ID администратора
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "8112176415"))
except ValueError:
    ADMIN_ID = 8112176415

# Подключение к базе данных
DB_CONFIG = {
    'user': os.environ.get("DB_USER", "bothost_db_c3b58a3f7c5d"),
    'password': os.environ.get("DB_PASSWORD", "F6qlOUcjcJJA6W5X7LhSh79TvckxtWi7A7-XI5dfHbk"),
    'database': os.environ.get("DB_NAME", "bothost_db_c3b58a3f7c5d"),
    'host': os.environ.get("DB_HOST", "node1.pghost.ru"),
    'port': int(os.environ.get("DB_PORT", "32802"))
}

# Настройки
MIN_WITHDRAW = float(os.environ.get("MIN_WITHDRAW", "0.01"))
CHECK_LIFETIME = int(os.environ.get("CHECK_LIFETIME", "30"))

# Проверка токена бота
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена!")

# Настройка логирования
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
            await self.create_tables()
            logger.info("✅ Подключение к БД установлено")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            raise

    async def create_tables(self):
        """Создание таблиц"""
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
                    is_read BOOLEAN DEFAULT FALSE
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
                    paid_at TIMESTAMP
                )
            """)

            # Таблица выводов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount DECIMAL(20,2) NOT NULL,
                    check_id TEXT,
                    check_url TEXT,
                    status TEXT DEFAULT 'created',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Таблица ссылок
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS links (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    code TEXT UNIQUE NOT NULL,
                    type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            logger.info("✅ Таблицы созданы/проверены")

            # Устанавливаем админа
            await conn.execute(
                "UPDATE users SET is_admin = TRUE WHERE user_id = $1",
                ADMIN_ID
            )

    async def get_user(self, user_id: int) -> Optional[Dict]:
        """Получить пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            return dict(row) if row else None

    async def create_user(self, user_id: int, username: str = None, 
                         first_name: str = None, last_name: str = None):
        """Создать или обновить пользователя"""
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

    async def update_balance(self, user_id: int, amount: Decimal):
        """Обновить баланс"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE users 
                SET balance = balance + $1, last_activity = NOW()
                WHERE user_id = $2
            """, amount, user_id)

    async def add_message(self, sender_id: Optional[int], receiver_id: int, text: str):
        """Добавить сообщение"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO messages (sender_id, receiver_id, text)
                VALUES ($1, $2, $3)
            """, sender_id, receiver_id, text)
            
            await conn.execute("""
                UPDATE users 
                SET messages_received = messages_received + 1, last_activity = NOW()
                WHERE user_id = $1
            """, receiver_id)

    async def add_donation(self, sender_id: Optional[int], receiver_id: int, 
                          amount: Decimal, message: str = None, invoice_id: str = None):
        """Добавить донат"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO donations (sender_id, receiver_id, amount, message, invoice_id, status, paid_at)
                VALUES ($1, $2, $3, $4, $5, 'completed', NOW())
            """, sender_id, receiver_id, amount, message, invoice_id)
            
            await conn.execute("""
                UPDATE users 
                SET balance = balance + $1,
                    donations_received = donations_received + 1,
                    donations_sum = donations_sum + $1,
                    last_activity = NOW()
                WHERE user_id = $2
            """, amount, receiver_id)

    async def create_withdrawal(self, user_id: int, amount: Decimal, check_id: str, check_url: str):
        """Создать вывод"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO withdrawals (user_id, amount, check_id, check_url)
                VALUES ($1, $2, $3, $4)
            """, user_id, amount, check_id, check_url)

    async def get_link(self, user_id: int, link_type: str) -> Optional[Dict]:
        """Получить ссылку пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM links WHERE user_id = $1 AND type = $2",
                user_id, link_type
            )
            return dict(row) if row else None

    async def create_link(self, user_id: int, link_type: str) -> str:
        """Создать новую ссылку"""
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO links (user_id, code, type)
                VALUES ($1, $2, $3)
            """, user_id, code, link_type)
            return code

    async def get_receiver_by_code(self, code: str) -> Optional[int]:
        """Получить получателя по коду ссылки"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT user_id FROM links WHERE code = $1", code)
            return row['user_id'] if row else None

    async def get_user_stats(self, user_id: int) -> Dict:
        """Получить статистику пользователя"""
        async with self.pool.acquire() as conn:
            user = await self.get_user(user_id)
            
            last_messages = await conn.fetch("""
                SELECT text, created_at 
                FROM messages 
                WHERE receiver_id = $1 
                ORDER BY created_at DESC 
                LIMIT 3
            """, user_id)

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

    async def get_all_users(self) -> List[Dict]:
        """Получить всех пользователей"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM users ORDER BY registered_at DESC")
            return [dict(row) for row in rows]

    async def get_total_stats(self) -> Dict:
        """Получить общую статистику"""
        async with self.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            active_today = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_activity > NOW() - INTERVAL '1 day'"
            )
            active_week = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_activity > NOW() - INTERVAL '7 days'"
            )
            total_donations = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM donations WHERE status = 'completed'"
            )
            total_withdrawn = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM withdrawals"
            )
            messages_count = await conn.fetchval("SELECT COUNT(*) FROM messages")

            return {
                "users_count": users_count,
                "active_today": active_today,
                "active_week": active_week,
                "total_donations": float(total_donations),
                "total_withdrawn": float(total_withdrawn),
                "messages_count": messages_count
            }

# ===================== CRYPTO PAY API =====================

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
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "Crypto-Pay-API-Token": self.token,
            "Content-Type": "application/json"
        }
        
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

    async def create_invoice(self, asset: str, amount: float, description: str = None, payload: str = None) -> dict:
        data = {
            "asset": asset,
            "amount": str(amount),
            "description": description,
            "payload": payload,
            "expires_in": 30 * 60
        }
        return await self._request("POST", "createInvoice", data)

    async def create_check(self, asset: str, amount: float) -> dict:
        data = {
            "asset": asset,
            "amount": str(amount)
        }
        return await self._request("POST", "createCheck", data)

    async def get_invoices(self, invoice_id: int = None) -> dict:
        data = {}
        if invoice_id:
            data["invoice_id"] = invoice_id
        return await self._request("GET", "getInvoices", data)

# ===================== КЛАВИАТУРЫ =====================

class Keyboards:
    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        buttons = [
            [KeyboardButton(text="📋 Мой профиль"), KeyboardButton(text="🔗 Мои ссылки")],
            [KeyboardButton(text="❓ Помощь")]
        ]
        return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

    @staticmethod
    def profile_inline(user_id: int) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton(text="💸 ВЫВЕСТИ", callback_data=f"withdraw_{user_id}")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_{user_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def links_inline(user_id: int, msg_code: str, donate_code: str, bot_username: str) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton(text="🔗 Копировать сообщения", callback_data=f"copy_msg_{msg_code}"),
                InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url=t.me/{bot_username}?start=msg_{msg_code}")
            ],
            [
                InlineKeyboardButton(text="🔗 Копировать донаты", callback_data=f"copy_donate_{donate_code}"),
                InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url=t.me/{bot_username}?start=donate_{donate_code}")
            ],
            [InlineKeyboardButton(text="🔄 Новые ссылки", callback_data=f"regenerate_{user_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def message_actions(message_id: int) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton(text="✍️ Ответить", callback_data=f"reply_{message_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{message_id}")
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def donation_actions(donation_id: int) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw_menu"),
                InlineKeyboardButton(text="✍️ Ответить", callback_data=f"reply_donate_{donation_id}")
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def payment_actions(invoice_id: str) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_payment_{invoice_id}")],
            [InlineKeyboardButton(text="✍️ Написать сообщение", callback_data=f"write_msg_{invoice_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def admin_menu() -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton(text="💰 Транзакции", callback_data="admin_transactions")],
            [InlineKeyboardButton(text="🎫 Чеки", callback_data="admin_checks")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def cancel() -> InlineKeyboardMarkup:
        buttons = [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

# ===================== ОСНОВНОЙ БОТ =====================

class AnonDonateBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.db = Database()
        self.keyboards = Keyboards()
        self.crypto = None
        self.bot_username = None
        self._register_handlers()

    def _register_handlers(self):
        # Команды
        self.dp.message.register(self.cmd_start, CommandStart())
        self.dp.message.register(self.cmd_admin, Command("admin"))
        
        # Главное меню
        self.dp.message.register(self.profile_handler, F.text == "📋 Мой профиль")
        self.dp.message.register(self.links_handler, F.text == "🔗 Мои ссылки")
        self.dp.message.register(self.help_handler, F.text == "❓ Помощь")
        
        # Callback запросы
        self.dp.callback_query.register(self.process_callback)
        
        # Состояния
        self.dp.message.register(self.process_withdraw_amount, WithdrawStates.waiting_for_amount)
        self.dp.message.register(self.process_donate_amount, DonateStates.waiting_for_amount)
        self.dp.message.register(self.process_donate_message, DonateStates.waiting_for_message)
        self.dp.message.register(self.process_reply, ReplyStates.waiting_for_reply)
        self.dp.message.register(self.process_broadcast, AdminStates.waiting_for_broadcast)
        
        # Глубокие ссылки
        self.dp.message.register(self.handle_start_param, CommandStart(deep_link=True))

    async def start(self):
        """Запуск бота"""
        await self.db.connect()
        self.crypto = CryptoPayAPI(CRYPTO_PAY_TOKEN)
        self.bot_username = (await self.bot.get_me()).username
        await self.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Бот запущен!")
        await self.dp.start_polling(self.bot)

    # ===================== ОБРАБОТЧИКИ КОМАНД =====================

    async def cmd_start(self, message: Message):
        """Обработчик /start"""
        user = message.from_user
        await self.db.create_user(user.id, user.username, user.first_name, user.last_name)
        
        await message.answer(
            f"{hbold('👋 Добро пожаловать!')}\n\n"
            f"Этот бот позволяет получать анонимные сообщения и донаты.\n"
            f"• Создайте ссылки в разделе {hcode('🔗 Мои ссылки')}\n"
            f"• Поделитесь ими с друзьями\n"
            f"• Получайте сообщения и донаты\n"
            f"• Выводите средства через Crypto Bot\n",
            reply_markup=self.keyboards.main_menu()
        )

    async def cmd_admin(self, message: Message):
        """Обработчик /admin"""
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ Доступ запрещен!")
            return
        
        await message.answer(
            f"{hbold('👑 Админ-панель')}",
            reply_markup=self.keyboards.admin_menu()
        )

    # ===================== ОБРАБОТЧИКИ МЕНЮ =====================

    async def profile_handler(self, message: Message):
        """Профиль пользователя"""
        user_id = message.from_user.id
        stats = await self.db.get_user_stats(user_id)
        user = stats["user"]
        
        text = (
            f"{hbold('👤 Ваш профиль')}\n\n"
            f"💰 Баланс: {user['balance']} USDT\n\n"
            f"{hbold('📊 Статистика:')}\n"
            f"✉️ Сообщений: {user['messages_received']}\n"
            f"💸 Донатов: {user['donations_received']} на сумму {user['donations_sum']} USDT\n\n"
        )
        
        if stats["last_messages"]:
            text += f"{hbold('📥 Последние сообщения:')}\n"
            for msg in stats["last_messages"]:
                time = self._format_time(msg['created_at'])
                msg_text = msg['text'][:30] + "..." if len(msg['text']) > 30 else msg['text']
                text += f'• "{msg_text}" ({time})\n'
        
        if stats["last_donations"]:
            text += f"\n{hbold('🎁 Последние донаты:')}\n"
            for don in stats["last_donations"]:
                time = self._format_time(don['created_at'])
                msg = f" \"{don['message']}\"" if don.get('message') else ""
                text += f"• +{don['amount']} USDT{msg} ({time})\n"
        
        await message.answer(
            text,
            reply_markup=self.keyboards.profile_inline(user_id)
        )

    async def links_handler(self, message: Message):
        """Управление ссылками"""
        user_id = message.from_user.id
        
        msg_link = await self.db.get_link(user_id, "message")
        donate_link = await self.db.get_link(user_id, "donate")
        
        if not msg_link:
            code = await self.db.create_link(user_id, "message")
            msg_link = {"code": code}
        if not donate_link:
            code = await self.db.create_link(user_id, "donate")
            donate_link = {"code": code}
        
        # ИСПРАВЛЕНО: используем разные кавычки для избежания синтаксической ошибки
        text = (
            f"{hbold('🔗 Ваши ссылки')}\n\n"
            f"{hbold('📝 Для сообщений:')}\n"
            f"{hcode('t.me/' + self.bot_username + '?start=msg_' + msg_link['code'])}\n\n"
            f"{hbold('💰 Для донатов:')}\n"
            f"{hcode('t.me/' + self.bot_username + '?start=donate_' + donate_link['code'])}"
        )
        
        await message.answer(
            text,
            reply_markup=self.keyboards.links_inline(user_id, msg_link["code"], donate_link["code"], self.bot_username)
        )

    async def help_handler(self, message: Message):
        """Помощь"""
        await message.answer(
            f"{hbold('❓ Помощь')}\n\n"
            f"{hbold('1. Получение сообщений:')}\n"
            f"• Создайте ссылку для сообщений\n"
            f"• Отправьте её друзьям\n"
            f"• Когда напишут - получите уведомление\n\n"
            f"{hbold('2. Получение донатов:')}\n"
            f"• Используйте ссылку для донатов\n"
            f"• Отправитель оплачивает через Crypto Bot\n"
            f"• Средства зачисляются на баланс\n\n"
            f"{hbold('3. Вывод средств:')}\n"
            f"• Зайдите в профиль\n"
            f"• Нажмите {hcode('💸 ВЫВЕСТИ')}\n"
            f"• Введите сумму\n"
            f"• Получите чек и активируйте в @CryptoBot"
        )

    # ===================== ОБРАБОТЧИКИ CALLBACK =====================

    async def process_callback(self, callback: CallbackQuery):
        """Обработка всех callback запросов"""
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
            elif data.startswith("delete_"):
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
            await callback.answer("❌ Ошибка", show_alert=True)

    async def start_withdraw(self, callback: CallbackQuery):
        """Начало вывода средств"""
        user_id = callback.from_user.id
        user = await self.db.get_user(user_id)
        
        if user['balance'] < MIN_WITHDRAW:
            await callback.message.edit_text(
                f"{hbold('❌ Недостаточно средств')}\n\n"
                f"Баланс: {user['balance']} USDT\n"
                f"Минимум: {MIN_WITHDRAW} USDT"
            )
            return
        
        await callback.message.edit_text(
            f"{hbold('💸 Вывод средств')}\n\n"
            f"Баланс: {user['balance']} USDT\n"
            f"Минимум: {MIN_WITHDRAW} USDT\n\n"
            f"Введите сумму:",
            reply_markup=self.keyboards.cancel()
        )
        
        state = FSMContext(self.storage, user_id, user_id)
        await state.set_state(WithdrawStates.waiting_for_amount)

    async def refresh_profile(self, callback: CallbackQuery):
        """Обновление профиля"""
        user_id = callback.from_user.id
        stats = await self.db.get_user_stats(user_id)
        user = stats["user"]
        
        text = (
            f"{hbold('👤 Ваш профиль')}\n\n"
            f"💰 Баланс: {user['balance']} USDT\n\n"
            f"✉️ Сообщений: {user['messages_received']}\n"
            f"💸 Донатов: {user['donations_received']} на сумму {user['donations_sum']} USDT"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.profile_inline(user_id)
        )

    async def copy_link(self, callback: CallbackQuery, link_type: str):
        """Копирование ссылки"""
        code = callback.data.replace(f"copy_{link_type}_", "")
        
        await callback.answer(f"🔗 Ссылка скопирована!")
        
        await callback.message.answer(
            f"{hcode('t.me/' + self.bot_username + '?start=' + link_type + '_' + code)}"
        )

    async def regenerate_links(self, callback: CallbackQuery):
        """Перегенерация ссылок"""
        user_id = callback.from_user.id
        
        async with self.db.pool.acquire() as conn:
            await conn.execute("DELETE FROM links WHERE user_id = $1", user_id)
        
        msg_code = await self.db.create_link(user_id, "message")
        donate_code = await self.db.create_link(user_id, "donate")
        
        # ИСПРАВЛЕНО: используем конкатенацию вместо вложенных f-строк
        text = (
            f"{hbold('✅ Новые ссылки созданы!')}\n\n"
            f"{hbold('📝 Для сообщений:')}\n"
            f"{hcode('t.me/' + self.bot_username + '?start=msg_' + msg_code)}\n\n"
            f"{hbold('💰 Для донатов:')}\n"
            f"{hcode('t.me/' + self.bot_username + '?start=donate_' + donate_code)}"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.links_inline(user_id, msg_code, donate_code, self.bot_username)
        )

    async def delete_message(self, callback: CallbackQuery):
        """Удаление сообщения"""
        msg_id = int(callback.data.replace("delete_", ""))
        
        async with self.db.pool.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE id = $1", msg_id)
        
        await callback.message.edit_text("✅ Сообщение удалено")

    async def cancel_action(self, callback: CallbackQuery):
        """Отмена действия"""
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        await state.clear()
        
        await callback.message.edit_text("❌ Действие отменено")

    # ===================== ОБРАБОТЧИКИ СОСТОЯНИЙ =====================

    async def process_withdraw_amount(self, message: Message, state: FSMContext):
        """Обработка суммы вывода"""
        try:
            amount = float(message.text.replace(',', '.'))
            user_id = message.from_user.id
            user = await self.db.get_user(user_id)
            
            if amount < MIN_WITHDRAW:
                await message.answer(f"❌ Минимум {MIN_WITHDRAW} USDT")
                return
            
            if amount > user['balance']:
                await message.answer("❌ Недостаточно средств")
                return
            
            async with self.crypto as crypto:
                check = await crypto.create_check("USDT", amount)
                
                await self.db.create_withdrawal(user_id, Decimal(str(amount)), check['check_id'], check['check_url'])
                await self.db.update_balance(user_id, Decimal(str(-amount)))
                
                new_user = await self.db.get_user(user_id)
                
                await message.answer(
                    f"{hbold('✅ Чек создан!')}\n\n"
                    f"💰 Сумма: {amount} USDT\n\n"
                    f"🔗 Ссылка:\n{hcode(check['check_url'])}\n\n"
                    f"💰 Новый баланс: {new_user['balance']} USDT"
                )
            
            await state.clear()
            
        except ValueError:
            await message.answer("❌ Введите число")

    # ===================== ОБРАБОТЧИКИ ГЛУБОКИХ ССЫЛОК =====================

    async def handle_start_param(self, message: Message, command: CommandStart):
        """Обработка переходов по ссылкам"""
        args = command.args
        if not args:
            await self.cmd_start(message)
            return
        
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
        
        state = FSMContext(self.storage, message.from_user.id, message.from_user.id)
        await state.update_data(receiver_id=receiver_id)
        
        await message.answer(
            f"{hbold('📝 Напишите анонимное сообщение:')}",
            reply_markup=self.keyboards.cancel()
        )
        await state.set_state(ReplyStates.waiting_for_reply)

    async def process_reply(self, message: Message, state: FSMContext):
        """Обработка отправки сообщения"""
        data = await state.get_data()
        receiver_id = data.get('receiver_id')
        
        await self.db.add_message(message.from_user.id, receiver_id, message.text)
        
        # Получаем ID сообщения для кнопки ответа
        async with self.db.pool.acquire() as conn:
            msg_row = await conn.fetchrow(
                "SELECT id FROM messages WHERE receiver_id = $1 ORDER BY created_at DESC LIMIT 1",
                receiver_id
            )
        
        await self.bot.send_message(
            receiver_id,
            f"{hbold('📩 Новое анонимное сообщение')}\n\n"
            f'"{message.text}"',
            reply_markup=self.keyboards.message_actions(msg_row['id'] if msg_row else 0)
        )
        
        await message.answer("✅ Сообщение отправлено!")
        await state.clear()

    async def process_donate_link(self, message: Message, code: str):
        """Обработка ссылки на донат"""
        receiver_id = await self.db.get_receiver_by_code(code)
        if not receiver_id:
            await message.answer("❌ Ссылка недействительна")
            return
        
        state = FSMContext(self.storage, message.from_user.id, message.from_user.id)
        await state.update_data(receiver_id=receiver_id)
        
        await message.answer(
            f"{hbold('💰 Введите сумму в USDT:')}\n"
            f"Минимум: {MIN_WITHDRAW} USDT",
            reply_markup=self.keyboards.cancel()
        )
        await state.set_state(DonateStates.waiting_for_amount)

    async def process_donate_amount(self, message: Message, state: FSMContext):
        """Обработка суммы доната"""
        try:
            amount = float(message.text.replace(',', '.'))
            
            if amount < MIN_WITHDRAW:
                await message.answer(f"❌ Минимум {MIN_WITHDRAW} USDT")
                return
            
            await state.update_data(amount=amount)
            data = await state.get_data()
            
            async with self.crypto as crypto:
                invoice = await crypto.create_invoice(
                    "USDT", 
                    amount, 
                    description=f"Donate to user {data['receiver_id']}",
                    payload=f"donate_{data['receiver_id']}"
                )
                
                await state.update_data(invoice_id=invoice['invoice_id'])
                
                await message.answer(
                    f"{hbold('💳 Оплата доната')}\n\n"
                    f"Сумма: {amount} USDT\n"
                    f"Ссылка для оплаты:\n{invoice['pay_url']}\n\n"
                    f"После оплаты нажмите кнопку",
                    reply_markup=self.keyboards.payment_actions(invoice['invoice_id'])
                )
                
        except ValueError:
            await message.answer("❌ Введите число")

    async def check_payment(self, callback: CallbackQuery):
        """Проверка оплаты"""
        invoice_id = callback.data.replace("check_payment_", "")
        
        async with self.crypto as crypto:
            try:
                invoices = await crypto.get_invoices(int(invoice_id))
                
                if invoices and isinstance(invoices, list) and len(invoices) > 0:
                    invoice = invoices[0]
                    if invoice.get('status') == 'paid':
                        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
                        data = await state.get_data()
                        
                        await self.db.add_donation(
                            callback.from_user.id,
                            data['receiver_id'],
                            Decimal(str(data['amount'])),
                            invoice_id=invoice_id
                        )
                        
                        # Получаем ID доната для кнопки ответа
                        async with self.db.pool.acquire() as conn:
                            don_row = await conn.fetchrow(
                                "SELECT id FROM donations WHERE receiver_id = $1 ORDER BY created_at DESC LIMIT 1",
                                data['receiver_id']
                            )
                        
                        await self.bot.send_message(
                            data['receiver_id'],
                            f"{hbold('🎉 Вам донат!')}\n\n"
                            f"💰 Сумма: {data['amount']} USDT\n\n"
                            f"Текущий баланс обновлен.",
                            reply_markup=self.keyboards.donation_actions(don_row['id'] if don_row else 0)
                        )
                        
                        await callback.message.edit_text(
                            f"{hbold('✅ Оплата получена!')}\n\n"
                            f"Спасибо за донат!"
                        )
                        
                        await callback.message.answer(
                            f"{hbold('✍️ Напишите сообщение получателю (необязательно):')}",
                            reply_markup=self.keyboards.cancel()
                        )
                        await state.set_state(DonateStates.waiting_for_message)
                        
                    else:
                        await callback.answer("⏳ Платеж еще не получен", show_alert=True)
                else:
                    await callback.answer("❌ Инвойс не найден", show_alert=True)
                    
            except Exception as e:
                logger.error(f"Ошибка проверки платежа: {e}")
                await callback.answer("❌ Ошибка проверки", show_alert=True)

    async def process_donate_message(self, message: Message, state: FSMContext):
        """Обработка сообщения после доната"""
        data = await state.get_data()
        receiver_id = data.get('receiver_id')
        
        await self.db.add_message(message.from_user.id, receiver_id, message.text)
        
        await self.bot.send_message(
            receiver_id,
            f"{hbold('📩 Сообщение с донатом')}\n\n"
            f'"{message.text}"'
        )
        
        await message.answer("✅ Сообщение отправлено!")
        await state.clear()

    async def start_reply(self, callback: CallbackQuery):
        """Начало ответа на сообщение"""
        msg_id = int(callback.data.replace("reply_", ""))
        
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        await state.update_data(reply_to_msg=msg_id)
        
        await callback.message.edit_text(
            f"{hbold('✍️ Напишите ответ:')}",
            reply_markup=self.keyboards.cancel()
        )
        
        await state.set_state(ReplyStates.waiting_for_reply)

    async def start_write_message(self, callback: CallbackQuery):
        """Начало написания сообщения после доната"""
        invoice_id = callback.data.replace("write_msg_", "")
        
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        await state.update_data(invoice_id=invoice_id)
        
        await callback.message.edit_text(
            f"{hbold('✍️ Напишите сообщение:')}",
            reply_markup=self.keyboards.cancel()
        )
        
        await state.set_state(DonateStates.waiting_for_message)

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
        """Статистика для админа"""
        stats = await self.db.get_total_stats()
        
        text = (
            f"{hbold('📊 Общая статистика')}\n\n"
            f"👥 Всего пользователей: {stats['users_count']}\n"
            f"📈 Активных сегодня: {stats['active_today']}\n"
            f"📈 Активных за неделю: {stats['active_week']}\n\n"
            f"💰 Сумма донатов: {stats['total_donations']} USDT\n"
            f"💸 Выведено: {stats['total_withdrawn']} USDT\n"
            f"📝 Сообщений: {stats['messages_count']}"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.admin_menu()
        )

    async def admin_broadcast(self, callback: CallbackQuery):
        """Начало рассылки"""
        state = FSMContext(self.storage, callback.from_user.id, callback.from_user.id)
        
        await callback.message.edit_text(
            f"{hbold('📢 Рассылка')}\n\n"
            f"Отправьте сообщение для всех пользователей:",
            reply_markup=self.keyboards.cancel()
        )
        
        await state.set_state(AdminStates.waiting_for_broadcast)

    async def process_broadcast(self, message: Message, state: FSMContext):
        """Обработка сообщения для рассылки"""
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ Доступ запрещен")
            await state.clear()
            return
        
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
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        
        await status_msg.edit_text(
            f"{hbold('✅ Рассылка завершена!')}\n\n"
            f"✅ Успешно: {sent}\n"
            f"❌ Ошибок: {failed}"
        )
        
        await state.clear()

    async def admin_users(self, callback: CallbackQuery):
        """Список пользователей"""
        users = await self.db.get_all_users()
        
        text = f"{hbold('👥 Последние 10 пользователей:')}\n\n"
        
        for user in users[:10]:
            text += (
                f"ID: {user['user_id']}\n"
                f"Username: @{user['username'] if user['username'] else 'нет'}\n"
                f"Баланс: {user['balance']} USDT\n"
                f"Сообщений: {user['messages_received']}\n"
                f"Донатов: {user['donations_received']}\n"
                f"---\n"
            )
        
        text += f"\nВсего: {len(users)}"
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.admin_menu()
        )

    async def admin_transactions(self, callback: CallbackQuery):
        """Последние транзакции"""
        async with self.db.pool.acquire() as conn:
            donations = await conn.fetch("""
                SELECT * FROM donations 
                WHERE status = 'completed'
                ORDER BY created_at DESC 
                LIMIT 10
            """)
        
        text = f"{hbold('💰 Последние донаты:')}\n\n"
        
        for don in donations:
            text += (
                f"От: {don['sender_id'] if don['sender_id'] else 'аноним'}\n"
                f"Кому: {don['receiver_id']}\n"
                f"Сумма: {don['amount']} USDT\n"
                f"Дата: {don['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
                f"---\n"
            )
        
        await callback.message.edit_text(
            text,
            reply_markup=self.keyboards.admin_menu()
        )

    async def admin_checks(self, callback: CallbackQuery):
        """Последние чеки"""
        async with self.db.pool.acquire() as conn:
            checks = await conn.fetch("""
                SELECT * FROM withdrawals 
                ORDER BY created_at DESC 
                LIMIT 10
            """)
        
        text = f"{hbold('🎫 Последние чеки:')}\n\n"
        
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
            reply_markup=self.keyboards.admin_menu()
        )

    # ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

    def _format_time(self, dt: datetime) -> str:
        """Форматирование времени"""
        diff = datetime.now() - dt
        if diff.days > 0:
            return f"{diff.days}д"
        elif diff.seconds > 3600:
            return f"{diff.seconds // 3600}ч"
        elif diff.seconds > 60:
            return f"{diff.seconds // 60}мин"
        return "только что"

# ===================== ТОЧКА ВХОДА =====================

async def main():
    """Главная функция"""
    bot = AnonDonateBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
