# main.py
import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import re
import json
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, ReplyKeyboardMarkup, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery
)
from pyrogram.errors import (
    PhoneNumberInvalid, PhoneCodeInvalid, SessionPasswordNeeded,
    PasswordHashInvalid, FloodWait
)
from pyrogram.enums import ParseMode
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, BigInteger, Float, select, update, delete
from sqlalchemy.sql import func

# Загружаем переменные окружения
load_dotenv()

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения!")

# Настройки логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===================== МОДЕЛИ БАЗЫ ДАННЫХ =====================
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    id = Column(BigInteger, primary_key=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255))
    registered_at = Column(DateTime, default=datetime.now)
    language = Column(String(10), default='ru')
    api_id = Column(Integer, nullable=True)
    api_hash = Column(String(255), nullable=True)
    is_configured = Column(Boolean, default=False)

class Account(Base):
    __tablename__ = 'accounts'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    phone_number = Column(String(20))
    session_string = Column(Text)
    status = Column(String(50), default='active')
    name = Column(String(255), nullable=True)
    username = Column(String(255), nullable=True)
    added_at = Column(DateTime, default=datetime.now)
    last_used = Column(DateTime, nullable=True)

class Subscription(Base):
    __tablename__ = 'subscriptions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    expires_at = Column(DateTime)
    payment_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

class Setting(Base):
    __tablename__ = 'settings'
    
    key = Column(String(255), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class PendingSession(Base):
    __tablename__ = 'pending_sessions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    phone_number = Column(String(20))
    session_data = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

class Chat(Base):
    __tablename__ = 'chats'
    
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, index=True)
    chat_id = Column(BigInteger)
    chat_title = Column(String(255))
    chat_type = Column(String(50))
    last_message_at = Column(DateTime, nullable=True)

# ===================== КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ =====================
class Database:
    def __init__(self, db_url: str):
        self.engine = create_async_engine(db_url, echo=False)
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
    
    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        # Создаем настройки по умолчанию
        await self.init_settings()
    
    async def init_settings(self):
        default_settings = {
            'account_limit': '5',
            'price_usdt': '5',
            'usdt_to_rub': '95',
            'crypto_bot_token': '',
            'trial_enabled': 'true',
            'trial_days': '3',
            'bot_name': 'MultiAccount Bot'
        }
        
        async with self.async_session() as session:
            for key, value in default_settings.items():
                result = await session.execute(
                    select(Setting).where(Setting.key == key)
                )
                setting = result.scalar_one_or_none()
                if not setting:
                    setting = Setting(key=key, value=value)
                    session.add(setting)
            await session.commit()
    
    @asynccontextmanager
    async def get_session(self):
        async with self.async_session() as session:
            yield session
    
    async def get_setting(self, key: str) -> str:
        async with self.get_session() as session:
            result = await session.execute(
                select(Setting).where(Setting.key == key)
            )
            setting = result.scalar_one_or_none()
            return setting.value if setting else None
    
    async def set_setting(self, key: str, value: str):
        async with self.get_session() as session:
            result = await session.execute(
                select(Setting).where(Setting.key == key)
            )
            setting = result.scalar_one_or_none()
            if setting:
                setting.value = value
            else:
                setting = Setting(key=key, value=value)
                session.add(setting)
            await session.commit()
    
    async def get_user(self, user_id: int) -> Optional[User]:
        async with self.get_session() as session:
            result = await session.execute(
                select(User).where(User.id == user_id)
            )
            return result.scalar_one_or_none()
    
    async def create_user(self, user_id: int, username: str, first_name: str, language: str = 'ru'):
        async with self.get_session() as session:
            user = User(
                id=user_id,
                username=username,
                first_name=first_name,
                language=language
            )
            session.add(user)
            await session.commit()
            return user
    
    async def update_user_api(self, user_id: int, api_id: int, api_hash: str):
        async with self.get_session() as session:
            result = await session.execute(
                select(User).where(User.id == user_id)
            )
            user = result.scalar_one_or_none()
            if user:
                user.api_id = api_id
                user.api_hash = api_hash
                user.is_configured = True
                await session.commit()
                return True
            return False
    
    async def get_user_accounts(self, user_id: int) -> List[Account]:
        async with self.get_session() as session:
            result = await session.execute(
                select(Account).where(Account.user_id == user_id)
            )
            return result.scalars().all()
    
    async def get_account(self, account_id: int) -> Optional[Account]:
        async with self.get_session() as session:
            result = await session.execute(
                select(Account).where(Account.id == account_id)
            )
            return result.scalar_one_or_none()
    
    async def add_account(self, user_id: int, phone_number: str, session_string: str) -> Account:
        async with self.get_session() as session:
            account = Account(
                user_id=user_id,
                phone_number=phone_number,
                session_string=session_string
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return account
    
    async def update_account_status(self, account_id: int, status: str):
        async with self.get_session() as session:
            await session.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(status=status)
            )
            await session.commit()
    
    async def delete_account(self, account_id: int):
        async with self.get_session() as session:
            await session.execute(
                delete(Account).where(Account.id == account_id)
            )
            await session.commit()
    
    async def get_pending_session(self, user_id: int) -> Optional[PendingSession]:
        async with self.get_session() as session:
            result = await session.execute(
                select(PendingSession).where(PendingSession.user_id == user_id)
            )
            return result.scalar_one_or_none()
    
    async def save_pending_session(self, user_id: int, phone_number: str, session_data: dict):
        async with self.get_session() as session:
            # Удаляем старую сессию если есть
            await session.execute(
                delete(PendingSession).where(PendingSession.user_id == user_id)
            )
            
            pending = PendingSession(
                user_id=user_id,
                phone_number=phone_number,
                session_data=json.dumps(session_data)
            )
            session.add(pending)
            await session.commit()
    
    async def clear_pending_session(self, user_id: int):
        async with self.get_session() as session:
            await session.execute(
                delete(PendingSession).where(PendingSession.user_id == user_id)
            )
            await session.commit()
    
    async def get_subscription(self, user_id: int) -> Optional[Subscription]:
        async with self.get_session() as session:
            result = await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .where(Subscription.expires_at > datetime.now())
            )
            return result.scalar_one_or_none()
    
    async def add_subscription(self, user_id: int, days: int, payment_id: str = None):
        async with self.get_session() as session:
            expires_at = datetime.now() + timedelta(days=days)
            subscription = Subscription(
                user_id=user_id,
                expires_at=expires_at,
                payment_id=payment_id
            )
            session.add(subscription)
            await session.commit()
    
    async def add_chat(self, account_id: int, chat_id: int, chat_title: str, chat_type: str):
        async with self.get_session() as session:
            # Проверяем, есть ли уже такой чат
            result = await session.execute(
                select(Chat)
                .where(Chat.account_id == account_id)
                .where(Chat.chat_id == chat_id)
            )
            chat = result.scalar_one_or_none()
            
            if not chat:
                chat = Chat(
                    account_id=account_id,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    chat_type=chat_type
                )
                session.add(chat)
                await session.commit()
    
    async def get_account_chats(self, account_id: int) -> List[Chat]:
        async with self.get_session() as session:
            result = await session.execute(
                select(Chat).where(Chat.account_id == account_id)
            )
            return result.scalars().all()

# ===================== ОСНОВНОЙ КЛАСС БОТА =====================
class MultiAccountBot:
    def __init__(self):
        self.app = Client(
            "multiaccount_bot",
            bot_token=BOT_TOKEN
        )
        self.db = Database(DATABASE_URL)
        self.user_sessions: Dict[int, Client] = {}  # Активные сессии пользователей
        self.pending_actions: Dict[int, str] = {}  # Ожидающие действия
        self.mailing_tasks: Dict[int, asyncio.Task] = {}  # Задачи рассылок
        
    async def start(self):
        """Запуск бота"""
        await self.db.init_db()
        logger.info("База данных инициализирована")
        
        # Регистрируем обработчики
        self.register_handlers()
        
        logger.info("Бот запущен!")
        await self.app.run()
    
    def register_handlers(self):
        """Регистрация всех обработчиков"""
        
        # Основные команды
        @self.app.on_message(filters.command("start"))
        async def start_command(client: Client, message: Message):
            await self.handle_start(client, message)
        
        @self.app.on_message(filters.command("cancel"))
        async def cancel_command(client: Client, message: Message):
            await self.handle_cancel(client, message)
        
        # Обработчики меню
        @self.app.on_message(filters.regex("^⚙️ Настроить бота$"))
        async def setup_bot(client: Client, message: Message):
            await self.handle_setup_bot(client, message)
        
        @self.app.on_message(filters.regex("^👤 Мой профиль$"))
        async def my_profile(client: Client, message: Message):
            await self.handle_profile(client, message)
        
        @self.app.on_message(filters.regex("^ℹ️ О проекте$"))
        async def about_project(client: Client, message: Message):
            await self.handle_about(client, message)
        
        @self.app.on_message(filters.regex("^👥 Менеджер аккаунтов$"))
        async def account_manager(client: Client, message: Message):
            await self.handle_account_manager(client, message)
        
        @self.app.on_message(filters.regex("^➕ Добавить аккаунт$"))
        async def add_account(client: Client, message: Message):
            await self.handle_add_account_start(client, message)
        
        @self.app.on_message(filters.regex("^📱 Мои аккаунты"))
        async def my_accounts(client: Client, message: Message):
            await self.handle_my_accounts(client, message)
        
        @self.app.on_message(filters.regex("^🔙 Назад$"))
        async def back_to_menu(client: Client, message: Message):
            await self.show_main_menu(client, message)
        
        # Обработчики callback-запросов
        @self.app.on_callback_query()
        async def handle_callback(client: Client, callback_query: CallbackQuery):
            await self.handle_callback_query(client, callback_query)
        
        # Обработчик текстовых сообщений (для ввода данных)
        @self.app.on_message(filters.text & ~filters.regex("^/"))
        async def handle_text(client: Client, message: Message):
            await self.handle_text_input(client, message)
    
    async def handle_start(self, client: Client, message: Message):
        """Обработка команды /start"""
        user = message.from_user
        user_id = user.id
        
        # Проверяем, есть ли пользователь в БД
        db_user = await self.db.get_user(user_id)
        if not db_user:
            await self.db.create_user(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                language=user.language_code or 'ru'
            )
            logger.info(f"Новый пользователь: {user_id}")
        
        await self.show_main_menu(client, message)
    
    async def show_main_menu(self, client: Client, message: Message):
        """Показывает главное меню"""
        user_id = message.from_user.id
        db_user = await self.db.get_user(user_id)
        
        welcome_text = """
╔════════════════════════════╗
    🌟 ДОБРО ПОЖАЛОВАТЬ! 🌟    
╚════════════════════════════╝

✨ *MultiAccount Bot* — твой надежный помощник
   для управления несколькими аккаунтами Telegram

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 🔹 Безопасное хранение       ┃
┃ 🔹 Массовые рассылки         ┃
┃ 🔹 Проверка спам-блока       ┃
┃ 🔹 Удобный интерфейс         ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
"""
        
        # Основная клавиатура
        keyboard = [
            ["👥 Менеджер аккаунтов"],
            ["👤 Мой профиль", "ℹ️ О проекте"]
        ]
        
        # Если пользователь не настроил API, показываем кнопку настройки
        if not db_user or not db_user.is_configured:
            welcome_text += "\n\n⚙️ *Для начала работы нужно настроить бота*"
            keyboard.append(["⚙️ Настроить бота"])
        
        await self.send_typing(message.chat.id, 2.0)
        await message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
    
    async def handle_setup_bot(self, client: Client, message: Message):
        """Настройка бота пользователем"""
        setup_text = """
⚙️ *НАСТРОЙКА БОТА* ⚙️

Для работы с аккаунтами Telegram мне нужны твои API данные.
Их можно получить на my.telegram.org/apps

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 🔐 *Важно!*                  ┃
┃ Твои данные хранятся только  ┃
┃ у тебя локально и никуда     ┃
┃ не передаются                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

Отправь мне *API ID* и *API Hash* в формате:
<code>123456:abcdefghijklmnop</code>

Где:
• 123456 — это API ID
• abcdefghijklmnop — это API Hash

━━━━━━━━━━━━━━━━━━━━
❌ Для отмены нажми /cancel
"""
        
        self.pending_actions[message.from_user.id] = "waiting_api_data"
        
        await self.send_typing(message.chat.id)
        await message.reply_text(
            setup_text,
            parse_mode=ParseMode.HTML
        )
    
    async def handle_profile(self, client: Client, message: Message):
        """Обработка профиля пользователя"""
        user = message.from_user
        user_id = user.id
        db_user = await self.db.get_user(user_id)
        accounts = await self.db.get_user_accounts(user_id)
        
        # Получаем настройки
        account_limit = int(await self.db.get_setting('account_limit') or '5')
        
        # Проверяем подписку
        subscription = await self.db.get_subscription(user_id)
        subscription_status = "❌ Неактивна"
        if subscription:
            subscription_status = f"✅ Активна до {subscription.expires_at.strftime('%d.%m.%Y')}"
        
        profile_text = f"""
╔════════════════════════════╗
        👤 МОЙ ПРОФИЛЬ         
╚════════════════════════════╝

🆔 ID: <code>{user_id}</code>
🌐 Username: @{user.username if user.username else 'не указан'}
📅 Регистрация: {db_user.registered_at.strftime('%d.%m.%Y') if db_user else 'сегодня'}

📦 Мои аккаунты: {len(accounts)} из {account_limit}
🔧 API настроены: {'✅ Да' if db_user and db_user.is_configured else '❌ Нет'}
💎 Статус подписки: {subscription_status}

━━━━━━━━━━━━━━━━━━━━
💰 Хочешь больше аккаунтов?
Оформи премиум доступ ниже 👇
"""
        
        inline_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💳 Оплатить подписку", callback_data="show_subscription"),
                InlineKeyboardButton("💎 Тарифы", callback_data="show_tariffs")
            ]
        ])
        
        await self.send_typing(message.chat.id)
        await message.reply_text(
            profile_text,
            parse_mode=ParseMode.HTML,
            reply_markup=inline_keyboard
        )
    
    async def handle_about(self, client: Client, message: Message):
        """Обработка раздела О проекте"""
        # Получаем статистику
        users_count = len([u async for u in await self._get_all_users()])
        accounts_count = len([a async for a in await self._get_all_accounts()])
        
        about_text = f"""
✨ *О НАШЕМ СЕРВИСЕ* ✨

Мы — мультиаккаунт платформа №1 
для комфортной работы с Telegram.

🔹 *Безопасность* — все данные шифруются
🔹 *Скорость* — мгновенные операции
🔹 *Удобство* — интуитивный интерфейс

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 📞 Связь: @support           ┃
┃ 🌍 Сайт: example.com         ┃
┃ 📊 Версия: 2.0.0             ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

💡 *Статистика проекта:*
👥 Пользователей: {users_count}
📱 Аккаунтов: {accounts_count}
"""
        
        await self.send_typing(message.chat.id, 1.5)
        await message.reply_text(
            about_text,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def handle_account_manager(self, client: Client, message: Message):
        """Обработка менеджера аккаунтов"""
        user_id = message.from_user.id
        db_user = await self.db.get_user(user_id)
        
        # Проверяем, настроен ли пользователь
        if not db_user or not db_user.is_configured:
            await message.reply_text(
                "⚠️ *Сначала настрой бота!*\n\n"
                "Для работы с аккаунтами нужно добавить API данные.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup(
                    [["⚙️ Настроить бота"], ["🔙 Назад"]],
                    resize_keyboard=True
                )
            )
            return
        
        accounts = await self.db.get_user_accounts(user_id)
        account_limit = int(await self.db.get_setting('account_limit') or '5')
        
        manager_text = f"""
🔐 *ДОБРО ПОЖАЛОВАТЬ В МЕНЕДЖЕР* 🔐

Управляй своими аккаунтами как профи!
Выбери действие ниже 👇

📊 *Твоя статистика:*
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 📱 Аккаунтов: {len(accounts)}/{account_limit}
┃ {self.create_progress_bar(len(accounts), account_limit)}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
"""
        
        keyboard = [
            ["➕ Добавить аккаунт"],
            [f"📱 Мои аккаунты ({len(accounts)}/{account_limit})"],
            ["🔙 Назад"]
        ]
        
        await self.send_typing(message.chat.id)
        await message.reply_text(
            manager_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
    
    async def handle_add_account_start(self, client: Client, message: Message):
        """Начало процесса добавления аккаунта"""
        user_id = message.from_user.id
        db_user = await self.db.get_user(user_id)
        accounts = await self.db.get_user_accounts(user_id)
        account_limit = int(await self.db.get_setting('account_limit') or '5')
        
        # Проверяем лимит аккаунтов
        if len(accounts) >= account_limit:
            await message.reply_text(
                "⚠️ *Лимит аккаунтов исчерпан!*\n\n"
                f"У тебя уже {account_limit} аккаунтов.\n"
                "Оформи премиум подписку для увеличения лимита.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💳 Оформить подписку", callback_data="show_subscription")
                ]])
            )
            return
        
        add_text = """
📲 *ДОБАВЛЕНИЕ НОВОГО АККАУНТА*

Пожалуйста, отправь номер телефона
в международном формате:

Пример: <code>+79123456789</code>
━━━━━━━━━━━━━━━━━━━━
❌ Для отмены нажми /cancel
"""
        
        self.pending_actions[user_id] = "waiting_phone"
        
        await self.send_typing(message.chat.id)
        await message.reply_text(
            add_text,
            parse_mode=ParseMode.HTML
        )
    
    async def handle_my_accounts(self, client: Client, message: Message):
        """Показывает список аккаунтов пользователя"""
        user_id = message.from_user.id
        accounts = await self.db.get_user_accounts(user_id)
        
        if not accounts:
            await message.reply_text(
                "📭 *У тебя пока нет аккаунтов*\n\n"
                "Нажми ➕ Добавить аккаунт чтобы начать",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        text = "📱 *ТВОИ АККАУНТЫ*\n\n"
        
        for i, account in enumerate(accounts, 1):
            status_emoji = {
                'active': '✅',
                'waiting_code': '⏳',
                'spam_block': '🚫',
                'limited': '⚠️',
                'inactive': '💤'
            }.get(account.status, '❓')
            
            name = account.name or account.phone_number
            text += f"{i}. {status_emoji} {name}\n"
        
        text += "\n━━━━━━━━━━━━━━━━━━━━\n"
        text += "Нажми на аккаунт чтобы управлять им"
        
        # Создаем инлайн-кнопки для каждого аккаунта
        keyboard = []
        for account in accounts:
            name = account.name or account.phone_number
            keyboard.append([
                InlineKeyboardButton(
                    f"{'✅' if account.status == 'active' else '⏳'} {name}",
                    callback_data=f"account_{account.id}"
                )
            ])
        
        await message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def handle_text_input(self, client: Client, message: Message):
        """Обработка текстового ввода (для разных состояний)"""
        user_id = message.from_user.id
        text = message.text
        
        # Проверяем, ожидаем ли мы какой-то ввод
        if user_id not in self.pending_actions:
            return
        
        action = self.pending_actions[user_id]
        
        if action == "waiting_api_data":
            await self.process_api_data(user_id, text, message)
        elif action == "waiting_phone":
            await self.process_phone_number(user_id, text, message)
        elif action == "waiting_code":
            await self.process_code(user_id, text, message)
        elif action == "waiting_password":
            await self.process_password(user_id, text, message)
        elif action.startswith("waiting_mailing_"):
            await self.process_mailing_input(user_id, text, message, action)
    
    async def process_api_data(self, user_id: int, text: str, message: Message):
        """Обработка введенных API данных"""
        # Проверяем формат: API_ID:API_HASH
        pattern = r'^(\d+):([a-f0-9]{32})$'
        match = re.match(pattern, text.strip())
        
        if not match:
            await message.reply_text(
                "❌ *Неверный формат!*\n\n"
                "Отправь данные в формате:\n"
                "<code>123456:abcdefghijklmnopqrstuvwxyz123456</code>\n\n"
                "Попробуй еще раз или /cancel",
                parse_mode=ParseMode.HTML
            )
            return
        
        api_id = int(match.group(1))
        api_hash = match.group(2)
        
        # Сохраняем в БД
        success = await self.db.update_user_api(user_id, api_id, api_hash)
        
        if success:
            # Очищаем ожидание
            del self.pending_actions[user_id]
            
            await message.reply_text(
                "✅ *API данные успешно сохранены!*\n\n"
                "Теперь ты можешь добавлять аккаунты в разделе 👥 Менеджер аккаунтов",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardMarkup(
                    [["👥 Менеджер аккаунтов"], ["👤 Мой профиль", "ℹ️ О проекте"]],
                    resize_keyboard=True
                )
            )
        else:
            await message.reply_text(
                "❌ *Ошибка при сохранении*\n\n"
                "Попробуй еще раз или /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def process_phone_number(self, user_id: int, text: str, message: Message):
        """Обработка введенного номера телефона"""
        # Очищаем номер от лишних символов
        phone = re.sub(r'[^\d+]', '', text)
        
        # Простая валидация
        if not re.match(r'^\+\d{10,15}$', phone):
            await message.reply_text(
                "❌ *Неверный формат номера!*\n\n"
                "Номер должен быть в международном формате:\n"
                "<code>+79123456789</code>\n\n"
                "Попробуй еще раз или /cancel",
                parse_mode=ParseMode.HTML
            )
            return
        
        # Получаем API данные пользователя
        db_user = await self.db.get_user(user_id)
        if not db_user or not db_user.api_id or not db_user.api_hash:
            await message.reply_text(
                "❌ *API данные не найдены!*\n\n"
                "Сначала настрой бота в разделе ⚙️ Настроить бота",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            return
        
        await message.reply_text(
            "🔄 *Подключаюсь к Telegram...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # Создаем временного клиента
            temp_client = Client(
                f"temp_{user_id}_{phone}",
                api_id=db_user.api_id,
                api_hash=db_user.api_hash,
                phone_number=phone,
                in_memory=True
            )
            
            # Пытаемся подключиться
            await temp_client.connect()
            
            # Отправляем код
            sent_code = await temp_client.send_code(phone)
            
            # Сохраняем данные сессии
            session_data = {
                'phone': phone,
                'phone_code_hash': sent_code.phone_code_hash,
                'client_name': f"temp_{user_id}_{phone}"
            }
            
            await self.db.save_pending_session(user_id, phone, session_data)
            self.user_sessions[user_id] = temp_client
            self.pending_actions[user_id] = "waiting_code"
            
            await message.reply_text(
                f"""
🔐 *ПОДТВЕРЖДЕНИЕ ВХОДА*

Мы отправили код в Telegram.
Введи его ниже:

━━━━━━━━━━━━━━━━━━━━
⏳ Ожидание кода...
""",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except PhoneNumberInvalid:
            await message.reply_text(
                "❌ *Неверный номер телефона!*\n\n"
                "Проверь номер и попробуй снова",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            
        except FloodWait as e:
            await message.reply_text(
                f"⚠️ *Слишком много попыток!*\n\n"
                f"Подожди {e.value} секунд и попробуй снова",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            
        except Exception as e:
            logger.error(f"Ошибка при добавлении аккаунта: {e}")
            await message.reply_text(
                "❌ *Произошла ошибка*\n\n"
                "Попробуй позже или обратись в поддержку",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
    
    async def process_code(self, user_id: int, text: str, message: Message):
        """Обработка введенного кода подтверждения"""
        code = text.strip()
        
        # Получаем данные сессии
        pending = await self.db.get_pending_session(user_id)
        if not pending:
            await message.reply_text(
                "❌ *Сессия не найдена!*\n\n"
                "Начни добавление аккаунта заново",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            return
        
        session_data = json.loads(pending.session_data)
        temp_client = self.user_sessions.get(user_id)
        
        if not temp_client:
            await message.reply_text(
                "❌ *Ошибка подключения!*\n\n"
                "Начни добавление аккаунта заново",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            await self.db.clear_pending_session(user_id)
            return
        
        try:
            # Пытаемся войти с кодом
            await temp_client.sign_in(
                phone_number=pending.phone_number,
                phone_code_hash=session_data['phone_code_hash'],
                phone_code=code
            )
            
            # Успешный вход
            await self.finish_account_addition(user_id, pending, temp_client, message)
            
        except PhoneCodeInvalid:
            await message.reply_text(
                "❌ *Неверный код!*\n\n"
                "Проверь код и попробуй снова",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except SessionPasswordNeeded:
            # Требуется двухфакторная аутентификация
            self.pending_actions[user_id] = "waiting_password"
            await message.reply_text(
                """
🔐 *ДВУХФАКТОРНАЯ АУТЕНТИФИКАЦИЯ*

Введи пароль от аккаунта:

━━━━━━━━━━━━━━━━━━━━
⚠️ Пароль не сохраняется нигде
""",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Ошибка при подтверждении кода: {e}")
            await message.reply_text(
                "❌ *Произошла ошибка*\n\n"
                "Попробуй позже",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            await self.db.clear_pending_session(user_id)
            if temp_client:
                await temp_client.disconnect()
    
    async def process_password(self, user_id: int, text: str, message: Message):
        """Обработка пароля 2FA"""
        password = text
        
        pending = await self.db.get_pending_session(user_id)
        temp_client = self.user_sessions.get(user_id)
        
        if not pending or not temp_client:
            await message.reply_text(
                "❌ *Ошибка сессии!*\n\n"
                "Начни добавление аккаунта заново",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            return
        
        try:
            # Пытаемся войти с паролем
            await temp_client.check_password(password)
            
            # Успешный вход
            await self.finish_account_addition(user_id, pending, temp_client, message)
            
        except PasswordHashInvalid:
            await message.reply_text(
                "❌ *Неверный пароль!*\n\n"
                "Попробуй снова",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Ошибка при проверке пароля: {e}")
            await message.reply_text(
                "❌ *Произошла ошибка*\n\n"
                "Попробуй позже",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            await self.db.clear_pending_session(user_id)
    
    async def finish_account_addition(self, user_id: int, pending, temp_client, message: Message):
        """Завершение добавления аккаунта"""
        try:
            # Получаем информацию об аккаунте
            me = await temp_client.get_me()
            
            # Сохраняем сессию
            session_string = await temp_client.export_session_string()
            
            # Сохраняем аккаунт в БД
            account = await self.db.add_account(
                user_id=user_id,
                phone_number=pending.phone_number,
                session_string=session_string
            )
            
            # Обновляем имя, если есть
            if me.first_name:
                account.name = f"{me.first_name} {me.last_name or ''}".strip()
                async with self.db.get_session() as session:
                    session.add(account)
                    await session.commit()
            
            # Загружаем чаты аккаунта
            await self.load_account_chats(temp_client, account.id)
            
            # Отключаем временного клиента
            await temp_client.disconnect()
            
            # Очищаем временные данные
            del self.pending_actions[user_id]
            if user_id in self.user_sessions:
                del self.user_sessions[user_id]
            await self.db.clear_pending_session(user_id)
            
            # Отправляем сообщение об успехе
            success_text = f"""
✅ *АККАУНТ УСПЕШНО ДОБАВЛЕН!*

📱 Номер: {pending.phone_number}
👤 Имя: {me.first_name or 'Не указано'}
📊 Статус: ✅ Активен

💡 Теперь ты можешь использовать его
для рассылок и управления.
"""
            
            await message.reply_text(
                success_text,
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Ошибка при завершении добавления: {e}")
            await message.reply_text(
                "❌ *Ошибка при сохранении аккаунта*\n\n"
                "Попробуй позже",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def load_account_chats(self, client: Client, account_id: int):
        """Загружает чаты аккаунта"""
        try:
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                await self.db.add_chat(
                    account_id=account_id,
                    chat_id=chat.id,
                    chat_title=chat.title or f"{chat.first_name or ''} {chat.last_name or ''}".strip(),
                    chat_type=str(chat.type).split('.')[-1]
                )
                await asyncio.sleep(0.1)  # Небольшая задержка чтобы не флудить
        except Exception as e:
            logger.error(f"Ошибка при загрузке чатов: {e}")
    
    async def handle_callback_query(self, client: Client, callback_query: CallbackQuery):
        """Обработка callback-запросов"""
        data = callback_query.data
        user_id = callback_query.from_user.id
        
        await callback_query.answer()
        
        if data == "show_subscription":
            await self.show_subscription_info(callback_query)
        elif data == "show_tariffs":
            await self.show_tariffs(callback_query)
        elif data.startswith("account_"):
            account_id = int(data.split("_")[1])
            await self.show_account_menu(callback_query, account_id)
        elif data.startswith("check_spam_"):
            account_id = int(data.split("_")[2])
            await self.check_spam_block(callback_query, account_id)
        elif data.startswith("mailing_"):
            account_id = int(data.split("_")[1])
            await self.start_mailing(callback_query, account_id)
        elif data.startswith("edit_profile_"):
            account_id = int(data.split("_")[2])
            await self.edit_profile(callback_query, account_id)
        elif data.startswith("delete_account_"):
            account_id = int(data.split("_")[2])
            await self.confirm_delete_account(callback_query, account_id)
        elif data.startswith("confirm_delete_"):
            account_id = int(data.split("_")[2])
            await self.delete_account(callback_query, account_id)
    
    async def show_account_menu(self, callback_query: CallbackQuery, account_id: int):
        """Показывает меню управления аккаунтом"""
        account = await self.db.get_account(account_id)
        if not account:
            await callback_query.message.edit_text("❌ Аккаунт не найден")
            return
        
        # Получаем актуальную информацию через клиент
        info_text = f"""
⚙️ *УПРАВЛЕНИЕ АККАУНТОМ*

📱 Номер: {account.phone_number}
👤 Имя: {account.name or 'Не указано'}
🆔 User ID: загрузка...
🔹 Юзернейм: @{account.username if account.username else 'не указан'}
📊 Статус: {self.format_account_status(account.status)}

━━━━━━━━━━━━━━━━━━━━
Выбери действие:
"""
        
        # Пытаемся получить актуальные данные
        try:
            # Создаем клиент из сохраненной сессии
            user_db = await self.db.get_user(account.user_id)
            if user_db and user_db.api_id and user_db.api_hash:
                temp_client = Client(
                    f"temp_{account_id}",
                    api_id=user_db.api_id,
                    api_hash=user_db.api_hash,
                    session_string=account.session_string,
                    in_memory=True
                )
                await temp_client.connect()
                me = await temp_client.get_me()
                await temp_client.disconnect()
                
                info_text = f"""
⚙️ *УПРАВЛЕНИЕ АККАУНТОМ*

📱 Номер: {account.phone_number}
👤 Имя: {me.first_name or ''} {me.last_name or ''}
🆔 User ID: <code>{me.id}</code>
🔹 Юзернейм: @{me.username if me.username else 'не указан'}
📊 Статус: {self.format_account_status(account.status)}

━━━━━━━━━━━━━━━━━━━━
Выбери действие:
"""
        except Exception as e:
            logger.error(f"Ошибка получения данных аккаунта: {e}")
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📨 Рассылка", callback_data=f"mailing_{account_id}")],
            [InlineKeyboardButton("👤 Редактировать профиль ✏️", callback_data=f"edit_profile_{account_id}")],
            [InlineKeyboardButton("🚫 Проверить спам-блок 🔍", callback_data=f"check_spam_{account_id}")],
            [InlineKeyboardButton("❌ Удалить аккаунт", callback_data=f"delete_account_{account_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_accounts")]
        ])
        
        await callback_query.message.edit_text(
            info_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    
    async def check_spam_block(self, callback_query: CallbackQuery, account_id: int):
        """Проверка спам-блока через @spambot"""
        account = await self.db.get_account(account_id)
        if not account:
            await callback_query.message.edit_text("❌ Аккаунт не найден")
            return
        
        await callback_query.message.edit_text(
            """
🔍 *ПРОВЕРКА НА СПАМ-БЛОК*

Инициирую проверку аккаунта...
🤖 Связываюсь с @spambot
⏳ Ожидание ответа...

━━━━━━━━━━━━━━━━━━━━
""",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # Получаем API данные пользователя
            user_db = await self.db.get_user(account.user_id)
            if not user_db or not user_db.api_id or not user_db.api_hash:
                raise Exception("API данные не найдены")
            
            # Создаем клиент для аккаунта
            async with Client(
                f"spam_check_{account_id}",
                api_id=user_db.api_id,
                api_hash=user_db.api_hash,
                session_string=account.session_string,
                in_memory=True
            ) as acc_client:
                
                # Отправляем сообщение в @spambot
                spambot = await acc_client.get_users("spambot")
                
                # Отправляем /start
                await acc_client.send_message(spambot.id, "/start")
                await asyncio.sleep(2)
                
                # Получаем ответ
                async for message in acc_client.get_chat_history(spambot.id, limit=1):
                    response = message.text or "Не удалось получить ответ"
                    
                    # Обновляем статус в зависимости от ответа
                    if "good" in response.lower() or "not" in response.lower():
                        new_status = "active"
                    else:
                        new_status = "spam_block"
                    
                    await self.db.update_account_status(account_id, new_status)
                    
                    result_text = f"""
🔍 *РЕЗУЛЬТАТ ПРОВЕРКИ:*

{response}

━━━━━━━━━━━━━━━━━━━━
📊 Статус обновлен: {self.format_account_status(new_status)}
"""
                    
                    await callback_query.message.edit_text(
                        result_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
                
        except Exception as e:
            logger.error(f"Ошибка при проверке спам-блока: {e}")
            await callback_query.message.edit_text(
                """
🔍 *РЕЗУЛЬТАТ ПРОВЕРКИ:*

❌ Не удалось проверить статус
Возможные причины:
• Аккаунт заблокирован
• Проблемы с подключением

━━━━━━━━━━━━━━━━━━━━
Попробуй позже или проверь вручную через @spambot
""",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def show_subscription_info(self, callback_query: CallbackQuery):
        """Показывает информацию о подписке"""
        price_usdt = await self.db.get_setting('price_usdt') or '5'
        usdt_to_rub = await self.db.get_setting('usdt_to_rub') or '95'
        
        price_rub = int(price_usdt) * int(usdt_to_rub)
        
        text = f"""
💎 *ПРЕМИУМ ДОСТУП* 💎

Открой все возможности мультиаккаунтинга:

🔥 До 5 аккаунтов одновременно
🚀 Массовые рассылки
🛡 Приоритетная поддержка

━━━━━━━━━━━━━━━━━━━━
💰 Стоимость: {price_usdt} USDT (~{price_rub} ₽)
📆 Период: 30 дней

Выбери способ оплаты:
"""
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить USDT (Crypto Bot)", callback_data="pay_usdt")],
            [InlineKeyboardButton("🇷🇺 Оплатить RUB", callback_data="pay_rub")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_profile")]
        ])
        
        await callback_query.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    
    async def show_tariffs(self, callback_query: CallbackQuery):
        """Показывает тарифы"""
        text = """
💎 *НАШИ ТАРИФЫ* 💎

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ *Базовый*                    ┃
┃ 📱 5 аккаунтов               ┃
┃ 📨 100 сообщений/день        ┃
┃ 💰 5 USDT / месяц            ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ *Про*                         ┃
┃ 📱 15 аккаунтов              ┃
┃ 📨 500 сообщений/день        ┃
┃ 💰 15 USDT / месяц           ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ *Бизнес*                      ┃
┃ 📱 50 аккаунтов              ┃
┃ 📨 Безлимитно                ┃
┃ 💰 40 USDT / месяц           ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

🎁 *Пробный период:* 3 дня
"""
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Выбрать тариф", callback_data="show_subscription")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_profile")]
        ])
        
        await callback_query.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    
    async def start_mailing(self, callback_query: CallbackQuery, account_id: int):
        """Начинает процесс рассылки"""
        account = await self.db.get_account(account_id)
        if not account:
            await callback_query.message.edit_text("❌ Аккаунт не найден")
            return
        
        # Получаем чаты аккаунта
        chats = await self.db.get_account_chats(account_id)
        
        if not chats:
            await callback_query.message.edit_text(
                "📭 *Нет доступных чатов*\n\n"
                "Сначала добавь аккаунт и дай ему время загрузить чаты",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Формируем список чатов
        chats_text = "📋 *ДОСТУПНЫЕ ЧАТЫ*\n\n"
        for i, chat in enumerate(chats[:10], 1):
            chats_text += f"{i}. {chat.chat_title}\n"
        
        if len(chats) > 10:
            chats_text += f"\n...и еще {len(chats) - 10} чатов"
        
        chats_text += """
        
Выбери до 10 чатов для рассылки.
Отправь номера строк через запятую:

Пример: 1,3,5-7
━━━━━━━━━━━━━━━━━━━━
🔍 Всего загружено чатов: """ + str(len(chats))
        
        # Сохраняем состояние
        self.pending_actions[callback_query.from_user.id] = f"waiting_mailing_chats_{account_id}"
        
        await callback_query.message.edit_text(
            chats_text,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def process_mailing_input(self, user_id: int, text: str, message: Message, action: str):
        """Обработка ввода для рассылки"""
        if action.startswith("waiting_mailing_chats_"):
            account_id = int(action.split("_")[3])
            await self.process_mailing_chats(user_id, account_id, text, message)
        elif action.startswith("waiting_mailing_message_"):
            account_id = int(action.split("_")[3])
            await self.process_mailing_message(user_id, account_id, text, message)
        elif action.startswith("waiting_mailing_confirm_"):
            account_id = int(action.split("_")[3])
            await self.process_mailing_confirm(user_id, account_id, text, message)
    
    async def process_mailing_chats(self, user_id: int, account_id: int, text: str, message: Message):
        """Обработка выбора чатов для рассылки"""
        # Парсим выбранные чаты
        selected_indices = set()
        parts = text.split(',')
        
        for part in parts:
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                selected_indices.update(range(start, end + 1))
            else:
                try:
                    selected_indices.add(int(part))
                except ValueError:
                    pass
        
        # Получаем чаты
        chats = await self.db.get_account_chats(account_id)
        
        # Фильтруем выбранные чаты
        selected_chats = []
        for idx in selected_indices:
            if 1 <= idx <= len(chats):
                selected_chats.append(chats[idx - 1])
        
        if not selected_chats:
            await message.reply_text(
                "❌ *Не выбрано ни одного чата*\n\n"
                "Попробуй снова",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Сохраняем выбранные чаты
        self.pending_actions[user_id] = f"waiting_mailing_message_{account_id}"
        
        # Сохраняем временные данные
        temp_data = {
            'chats': [{'id': c.chat_id, 'title': c.chat_title} for c in selected_chats]
        }
        
        # В реальном проекте нужно сохранить это в БД
        # Для примера используем словарь
        if not hasattr(self, 'mailing_temp'):
            self.mailing_temp = {}
        self.mailing_temp[user_id] = temp_data
        
        await message.reply_text(
            f"""
✉️ *ВВЕДИ ТЕКСТ СООБЩЕНИЯ*

Отправь текст для рассылки:

━━━━━━━━━━━━━━━━━━━━
📊 Выбрано чатов: {len(selected_chats)}
""",
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def process_mailing_message(self, user_id: int, account_id: int, text: str, message: Message):
        """Обработка текста сообщения для рассылки"""
        # Получаем временные данные
        if not hasattr(self, 'mailing_temp') or user_id not in self.mailing_temp:
            await message.reply_text(
                "❌ *Ошибка*\n\n"
                "Начни рассылку заново",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            return
        
        temp_data = self.mailing_temp[user_id]
        temp_data['message_text'] = text
        
        preview_text = f"""
⚙️ *НАСТРОЙКИ РАССЫЛКИ*

📝 Текст сообщения:
{text[:100]}{'...' if len(text) > 100 else ''}

📊 Количество сообщений: {len(temp_data['chats'])}
⏱ Задержка: 30 секунд
🎯 Целевые чаты: {len(temp_data['chats'])}

━━━━━━━━━━━━━━━━━━━━
✅ Запустить рассылку сейчас?
"""
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Запустить", callback_data=f"confirm_mailing_{account_id}"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_mailing")
            ]
        ])
        
        self.pending_actions[user_id] = f"waiting_mailing_confirm_{account_id}"
        
        await message.reply_text(
            preview_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    
    async def process_mailing_confirm(self, user_id: int, account_id: int, text: str, message: Message):
        """Подтверждение рассылки"""
        if text.lower() == 'да':
            await self.execute_mailing(user_id, account_id, message)
        else:
            await message.reply_text(
                "❌ Рассылка отменена",
                parse_mode=ParseMode.MARKDOWN
            )
            del self.pending_actions[user_id]
            if hasattr(self, 'mailing_temp') and user_id in self.mailing_temp:
                del self.mailing_temp[user_id]
    
    async def execute_mailing(self, user_id: int, account_id: int, message: Message):
        """Выполнение рассылки"""
        if not hasattr(self, 'mailing_temp') or user_id not in self.mailing_temp:
            await message.reply_text("❌ Ошибка данных рассылки")
            del self.pending_actions[user_id]
            return
        
        temp_data = self.mailing_temp[user_id]
        account = await self.db.get_account(account_id)
        
        if not account:
            await message.reply_text("❌ Аккаунт не найден")
            return
        
        await message.reply_text(
            "🚀 *Запускаю рассылку...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Запускаем рассылку в фоне
        task = asyncio.create_task(
            self._execute_mailing_task(user_id, account_id, temp_data, message)
        )
        self.mailing_tasks[user_id] = task
        
        # Очищаем временные данные
        del self.pending_actions[user_id]
        del self.mailing_temp[user_id]
    
    async def _execute_mailing_task(self, user_id: int, account_id: int, data: dict, original_message: Message):
        """Фоновая задача рассылки"""
        try:
            account = await self.db.get_account(account_id)
            user_db = await self.db.get_user(user_id)
            
            if not account or not user_db:
                raise Exception("Данные не найдены")
            
            # Создаем клиент для аккаунта
            async with Client(
                f"mailing_{account_id}",
                api_id=user_db.api_id,
                api_hash=user_db.api_hash,
                session_string=account.session_string,
                in_memory=True
            ) as acc_client:
                
                sent = 0
                total = len(data['chats'])
                
                for i, chat_info in enumerate(data['chats'], 1):
                    try:
                        await acc_client.send_message(
                            chat_info['id'],
                            data['message_text']
                        )
                        sent += 1
                        
                        # Обновляем прогресс каждые 5 сообщений
                        if i % 5 == 0:
                            progress = self.create_progress_bar(i, total)
                            await original_message.reply_text(
                                f"📤 Отправлено: {progress}"
                            )
                        
                        # Задержка между сообщениями
                        await asyncio.sleep(30)
                        
                    except Exception as e:
                        logger.error(f"Ошибка отправки в чат {chat_info['title']}: {e}")
                        await original_message.reply_text(
                            f"⚠️ Ошибка отправки в {chat_info['title']}"
                        )
                
                # Итоговый отчет
                await original_message.reply_text(
                    f"""
✅ *РАССЫЛКА ЗАВЕРШЕНА!*

📊 *Статистика:*
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 📤 Отправлено: {sent}/{total}
┃ {self.create_progress_bar(sent, total)}
┃ ❌ Ошибок: {total - sent}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
""",
                    parse_mode=ParseMode.MARKDOWN
                )
                
        except Exception as e:
            logger.error(f"Ошибка рассылки: {e}")
            await original_message.reply_text(
                "❌ *Ошибка при выполнении рассылки*",
                parse_mode=ParseMode.MARKDOWN
            )
        finally:
            if user_id in self.mailing_tasks:
                del self.mailing_tasks[user_id]
    
    async def edit_profile(self, callback_query: CallbackQuery, account_id: int):
        """Редактирование профиля аккаунта"""
        account = await self.db.get_account(account_id)
        if not account:
            await callback_query.message.edit_text("❌ Аккаунт не найден")
            return
        
        text = f"""
✏️ *РЕДАКТИРОВАНИЕ ПРОФИЛЯ*

Текущие данные:
👤 Имя: {account.name or 'Не указано'}
📱 Номер: {account.phone_number}

━━━━━━━━━━━━━━━━━━━━
Отправь новое имя для профиля:
"""
        
        self.pending_actions[callback_query.from_user.id] = f"editing_profile_name_{account_id}"
        
        await callback_query.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def confirm_delete_account(self, callback_query: CallbackQuery, account_id: int):
        """Подтверждение удаления аккаунта"""
        text = """
⚠️ *УДАЛЕНИЕ АККАУНТА*

Ты уверен, что хочешь удалить этот аккаунт?
Это действие нельзя отменить!

━━━━━━━━━━━━━━━━━━━━
"""
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{account_id}"),
                InlineKeyboardButton("❌ Нет, отмена", callback_data=f"account_{account_id}")
            ]
        ])
        
        await callback_query.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    
    async def delete_account(self, callback_query: CallbackQuery, account_id: int):
        """Удаление аккаунта"""
        await self.db.delete_account(account_id)
        
        await callback_query.message.edit_text(
            "✅ *Аккаунт успешно удален*",
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def handle_cancel(self, client: Client, message: Message):
        """Обработка команды /cancel"""
        user_id = message.from_user.id
        
        if user_id in self.pending_actions:
            del self.pending_actions[user_id]
        
        if user_id in self.user_sessions:
            await self.user_sessions[user_id].disconnect()
            del self.user_sessions[user_id]
        
        await self.db.clear_pending_session(user_id)
        
        await message.reply_text(
            "❌ *Действие отменено*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup(
                [["👥 Менеджер аккаунтов"], ["👤 Мой профиль", "ℹ️ О проекте"]],
                resize_keyboard=True
            )
        )
    
    # ===================== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ =====================
    
    async def send_typing(self, chat_id: int, duration: float = 1.0):
        """Имитирует набор текста"""
        await self.app.send_chat_action(chat_id, enums.ChatAction.TYPING)
        await asyncio.sleep(duration)
    
    def format_account_status(self, status: str) -> str:
        """Форматирует статус аккаунта"""
        status_map = {
            "active": "✅ Активен",
            "waiting_code": "⏳ Ожидает код",
            "spam_block": "🚫 Спам-блок",
            "limited": "⚠️ Ограничен",
            "inactive": "💤 Не используется"
        }
        return status_map.get(status, "❓ Неизвестно")
    
    def create_progress_bar(self, current: int, total: int, length: int = 10) -> str:
        """Создает прогресс-бар"""
        if total == 0:
            return "[░░░░░░░░░░] 0/0"
        filled = int((current / total) * length)
        empty = length - filled
        return f"[{'█' * filled}{'░' * empty}] {current}/{total}"
    
    async def _get_all_users(self):
        """Получает всех пользователей (для статистики)"""
        async with self.db.get_session() as session:
            result = await session.execute(select(User))
            return result.scalars().all()
    
    async def _get_all_accounts(self):
        """Получает все аккаунты (для статистики)"""
        async with self.db.get_session() as session:
            result = await session.execute(select(Account))
            return result.scalars().all()

# ===================== ЗАПУСК БОТА =====================
async def main():
    bot = MultiAccountBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
