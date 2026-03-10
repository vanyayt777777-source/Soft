import os
import asyncio
import logging
import aiohttp
from datetime import datetime, date
from typing import Dict, Optional, List
import sys
import uuid

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.fsm.strategy import FSMStrategy

from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Text, Boolean, DateTime, Date, ForeignKey, func, select, and_, Index, Float
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from sqlalchemy.exc import IntegrityError

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
DATABASE_URL = "postgresql://bothost_db_755f1e005693:jrgoZgWmNe6m7DP9lKBq7fbVWmG8PonEHFbugXhxWVQ@node1.pghost.ru:32809/bothost_db_755f1e005693"
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Токен главного бота из переменных окружения

if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в переменных окружения!")
    sys.exit(1)

# Настройка базы данных
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# Модели базы данных
class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String)
    registered_at = Column(DateTime, default=datetime.now)
    is_active = Column(Boolean, default=True)
    
    created_bots = relationship("CreatedBot", back_populates="owner")

class CreatedBot(Base):
    __tablename__ = 'created_bots'
    
    id = Column(Integer, primary_key=True)
    owner_id = Column(BigInteger, ForeignKey('users.user_id'))
    bot_token = Column(String, unique=True, nullable=False)
    bot_username = Column(String)
    bot_type = Column(String)  # "autoclaims" или "shop"
    admin_id = Column(BigInteger)
    
    # Для автозаявок
    welcome_message = Column(Text, nullable=True)
    channel_id = Column(BigInteger, nullable=True)
    
    # Для магазина
    crypto_token = Column(String, nullable=True)  # Токен от Crypto Bot
    shop_name = Column(String, nullable=True)  # Название магазина
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    last_activity = Column(DateTime, default=datetime.now)
    
    owner = relationship("User", back_populates="created_bots")
    bot_users = relationship("BotUser", back_populates="bot")
    stats = relationship("BotStat", back_populates="bot")
    products = relationship("Product", back_populates="bot")
    orders = relationship("Order", back_populates="bot")

class BotUser(Base):
    __tablename__ = 'bot_users'
    
    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey('created_bots.id'))
    user_id = Column(BigInteger)
    username = Column(String, nullable=True)
    first_name = Column(String)
    balance = Column(Float, default=0.0)  # Баланс для магазина
    last_interaction = Column(DateTime, default=datetime.now)
    claims_count = Column(Integer, default=0)  # Для автозаявок
    
    bot = relationship("CreatedBot", back_populates="bot_users")
    orders = relationship("Order", back_populates="user")
    
    __table_args__ = (Index('idx_bot_user', 'bot_id', 'user_id', unique=True),)

class Product(Base):
    __tablename__ = 'products'
    
    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey('created_bots.id'))
    name = Column(String)
    description = Column(Text)
    price = Column(Float)  # Цена в USD
    photo_url = Column(String, nullable=True)
    is_available = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    
    bot = relationship("CreatedBot", back_populates="products")
    orders = relationship("Order", back_populates="product")

class Order(Base):
    __tablename__ = 'orders'
    
    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey('created_bots.id'))
    user_id = Column(Integer, ForeignKey('bot_users.id'))
    product_id = Column(Integer, ForeignKey('products.id'))
    amount = Column(Float)
    status = Column(String, default="pending")  # pending, paid, completed, cancelled
    payment_id = Column(String, unique=True)  # ID платежа в Crypto Bot
    created_at = Column(DateTime, default=datetime.now)
    paid_at = Column(DateTime, nullable=True)
    
    bot = relationship("CreatedBot", back_populates="orders")
    user = relationship("BotUser", back_populates="orders")
    product = relationship("Product", back_populates="orders")

class BotStat(Base):
    __tablename__ = 'bot_stats'
    
    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey('created_bots.id'))
    date = Column(Date, default=date.today)
    claims_processed = Column(Integer, default=0)
    messages_sent = Column(Integer, default=0)
    orders_count = Column(Integer, default=0)
    revenue = Column(Float, default=0.0)
    
    bot = relationship("CreatedBot", back_populates="stats")
    
    __table_args__ = (Index('idx_bot_stat', 'bot_id', 'date', unique=True),)

# Создание таблиц
Base.metadata.create_all(engine)

# Состояния FSM для создания бота
class CreateBotStates(StatesGroup):
    waiting_for_type = State()
    waiting_for_token = State()
    waiting_for_admin_id = State()
    waiting_for_welcome = State()
    waiting_for_channel = State()
    waiting_for_crypto_token = State()
    waiting_for_shop_name = State()

# Состояния для добавления товара
class AddProductStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_photo = State()

# Класс для управления запущенными ботами
class BotsManager:
    def __init__(self):
        self.running_bots: Dict[int, Dict] = {}  # bot_id -> {bot, task, dp}
    
    async def start_bot_instance(self, bot_data: CreatedBot):
        """Запуск отдельного экземпляра бота"""
        if bot_data.id in self.running_bots:
            logger.info(f"Бот {bot_data.id} уже запущен")
            return
        
        try:
            # Создаем экземпляр бота
            bot = Bot(token=bot_data.bot_token)
            dp = Dispatcher(storage=MemoryStorage())
            
            # Настраиваем обработчики в зависимости от типа бота
            if bot_data.bot_type == "autoclaims":
                await self._setup_autoclaims_handlers(dp, bot_data)
            elif bot_data.bot_type == "shop":
                await self._setup_shop_handlers(dp, bot_data)
            
            # Запускаем бота в фоне
            task = asyncio.create_task(self._run_bot(bot, dp, bot_data))
            self.running_bots[bot_data.id] = {
                'bot': bot,
                'dp': dp,
                'task': task,
                'data': bot_data
            }
            logger.info(f"Бот {bot_data.bot_username} (ID: {bot_data.id}) типа {bot_data.bot_type} успешно запущен")
        except Exception as e:
            logger.error(f"Ошибка при запуске бота {bot_data.id}: {e}")
    
    async def _setup_autoclaims_handlers(self, dp: Dispatcher, bot_data: CreatedBot):
        """Настройка обработчиков для бота автозаявок"""
        
        router = Router()
        
        # Команда старт
        @router.message(CommandStart())
        async def cmd_start(message: Message):
            await message.answer(
                f"👋 Добро пожаловать!\n\n"
                f"Этот бот автоматически принимает заявки из канала.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="👨‍💼 Админ-панель", callback_data="admin_panel")]
                    ]
                )
            )
        
        # Админ-панель
        @router.message(lambda message: message.text == '/admin')
        @router.callback_query(lambda c: c.data == "admin_panel")
        async def admin_panel(event: Message | CallbackQuery):
            user_id = event.from_user.id
            if user_id != bot_data.admin_id:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ У вас нет доступа", show_alert=True)
                else:
                    await event.answer("⛔ У вас нет доступа")
                return
            
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
            builder.row(InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing"))
            builder.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings"))
            
            text = "👨‍💼 **Админ-панель**\n\nВыберите действие:"
            
            if isinstance(event, CallbackQuery):
                await event.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
                await event.answer()
            else:
                await event.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
        
        # Статистика
        @router.callback_query(F.data == "admin_stats")
        async def show_stats(callback: CallbackQuery):
            if callback.from_user.id != bot_data.admin_id:
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            with SessionLocal() as db:
                total_users = db.query(BotUser).filter(BotUser.bot_id == bot_data.id).count()
                total_claims = db.query(func.sum(BotUser.claims_count)).filter(BotUser.bot_id == bot_data.id).scalar() or 0
                today_claims = db.query(func.sum(BotStat.claims_processed)).filter(
                    and_(
                        BotStat.bot_id == bot_data.id,
                        BotStat.date == date.today()
                    )
                ).scalar() or 0
                
                stats_text = (
                    f"📊 **Статистика бота**\n\n"
                    f"👥 Всего пользователей: {total_users}\n"
                    f"📝 Всего заявок: {total_claims}\n"
                    f"📅 Заявок сегодня: {today_claims}\n"
                    f"🕐 Последняя активность: {bot_data.last_activity.strftime('%d.%m.%Y %H:%M')}"
                )
                
                await callback.message.edit_text(
                    stats_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
                        ]
                    )
                )
            await callback.answer()
        
        # Настройки
        @router.callback_query(F.data == "admin_settings")
        async def show_settings(callback: CallbackQuery):
            if callback.from_user.id != bot_data.admin_id:
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="✏️ Изменить приветствие", callback_data="edit_welcome"))
            builder.row(InlineKeyboardButton(text="📢 Изменить канал", callback_data="edit_channel"))
            builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel"))
            
            settings_text = (
                f"⚙️ **Настройки бота**\n\n"
                f"📝 Приветствие: {bot_data.welcome_message[:50]}...\n"
                f"📢 Канал: `{bot_data.channel_id}`\n"
                f"👤 Админ ID: `{bot_data.admin_id}`"
            )
            
            await callback.message.edit_text(
                settings_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
            await callback.answer()
        
        # Обработка новых сообщений в канале
        @router.channel_post()
        async def handle_channel_post(message: Message):
            """Обработка новых сообщений в канале"""
            try:
                with SessionLocal() as db:
                    bot_info = db.query(CreatedBot).filter(CreatedBot.id == bot_data.id).first()
                    if not bot_info or not bot_info.is_active:
                        return
                    
                    # Определяем отправителя
                    user_id = None
                    username = None
                    first_name = None
                    
                    if message.forward_from:
                        user_id = message.forward_from.id
                        username = message.forward_from.username
                        first_name = message.forward_from.first_name
                    
                    if user_id:
                        try:
                            # Отправляем приветствие
                            await message.bot.send_message(
                                user_id,
                                bot_info.welcome_message,
                                parse_mode="HTML"
                            )
                            
                            # Сохраняем пользователя
                            bot_user = db.query(BotUser).filter(
                                and_(
                                    BotUser.bot_id == bot_info.id,
                                    BotUser.user_id == user_id
                                )
                            ).first()
                            
                            if bot_user:
                                bot_user.claims_count += 1
                                bot_user.last_interaction = datetime.now()
                            else:
                                bot_user = BotUser(
                                    bot_id=bot_info.id,
                                    user_id=user_id,
                                    username=username,
                                    first_name=first_name,
                                    claims_count=1,
                                    last_interaction=datetime.now()
                                )
                                db.add(bot_user)
                            
                            # Обновляем статистику
                            today_stat = db.query(BotStat).filter(
                                and_(
                                    BotStat.bot_id == bot_info.id,
                                    BotStat.date == date.today()
                                )
                            ).first()
                            
                            if today_stat:
                                today_stat.claims_processed += 1
                            else:
                                today_stat = BotStat(
                                    bot_id=bot_info.id,
                                    date=date.today(),
                                    claims_processed=1
                                )
                                db.add(today_stat)
                            
                            bot_info.last_activity = datetime.now()
                            db.commit()
                            
                            # Реакция на сообщение
                            await message.react([types.ReactionTypeEmoji(emoji="✅")])
                            
                        except Exception as e:
                            logger.error(f"Ошибка при отправке пользователю {user_id}: {e}")
                    
            except Exception as e:
                logger.error(f"Ошибка при обработке поста: {e}")
        
        dp.include_router(router)
    
    async def _setup_shop_handlers(self, dp: Dispatcher, bot_data: CreatedBot):
        """Настройка обработчиков для бота магазина"""
        
        router = Router()
        
        # Команда старт
        @router.message(CommandStart())
        async def cmd_start(message: Message):
            # Сохраняем пользователя
            with SessionLocal() as db:
                bot_user = db.query(BotUser).filter(
                    and_(
                        BotUser.bot_id == bot_data.id,
                        BotUser.user_id == message.from_user.id
                    )
                ).first()
                
                if not bot_user:
                    bot_user = BotUser(
                        bot_id=bot_data.id,
                        user_id=message.from_user.id,
                        username=message.from_user.username,
                        first_name=message.from_user.first_name,
                        balance=0.0
                    )
                    db.add(bot_user)
                    db.commit()
            
            await show_main_menu(message)
        
        async def show_main_menu(event: Message | CallbackQuery):
            """Показать главное меню магазина"""
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="🛍 Купить товар", callback_data="shop_products"))
            builder.row(InlineKeyboardButton(text="👤 Профиль", callback_data="shop_profile"))
            
            if event.from_user.id == bot_data.admin_id:
                builder.row(InlineKeyboardButton(text="👨‍💼 Админ-панель", callback_data="admin_panel"))
            
            text = f"🏪 **{bot_data.shop_name}**\n\nДобро пожаловать в наш магазин!"
            
            if isinstance(event, CallbackQuery):
                await event.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
                await event.answer()
            else:
                await event.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
        
        # Админ-панель
        @router.message(lambda message: message.text == '/admin')
        @router.callback_query(lambda c: c.data == "admin_panel")
        async def admin_panel(event: Message | CallbackQuery):
            if event.from_user.id != bot_data.admin_id:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ У вас нет доступа", show_alert=True)
                else:
                    await event.answer("⛔ У вас нет доступа")
                return
            
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
            builder.row(InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_product"))
            builder.row(InlineKeyboardButton(text="📦 Управление товарами", callback_data="manage_products"))
            builder.row(InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing"))
            builder.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings"))
            builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main"))
            
            text = "👨‍💼 **Админ-панель магазина**\n\nВыберите действие:"
            
            if isinstance(event, CallbackQuery):
                await event.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
                await event.answer()
            else:
                await event.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
        
        # Статистика
        @router.callback_query(F.data == "admin_stats")
        async def show_stats(callback: CallbackQuery):
            if callback.from_user.id != bot_data.admin_id:
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            with SessionLocal() as db:
                total_users = db.query(BotUser).filter(BotUser.bot_id == bot_data.id).count()
                total_products = db.query(Product).filter(Product.bot_id == bot_data.id).count()
                total_orders = db.query(Order).filter(Order.bot_id == bot_data.id).count()
                paid_orders = db.query(Order).filter(
                    and_(
                        Order.bot_id == bot_data.id,
                        Order.status == "paid"
                    )
                ).count()
                total_revenue = db.query(func.sum(Order.amount)).filter(
                    and_(
                        Order.bot_id == bot_data.id,
                        Order.status == "paid"
                    )
                ).scalar() or 0
                
                stats_text = (
                    f"📊 **Статистика магазина**\n\n"
                    f"👥 Пользователей: {total_users}\n"
                    f"📦 Товаров: {total_products}\n"
                    f"📋 Всего заказов: {total_orders}\n"
                    f"✅ Оплаченных: {paid_orders}\n"
                    f"💰 Выручка: ${total_revenue:.2f}"
                )
                
                await callback.message.edit_text(
                    stats_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
                        ]
                    )
                )
            await callback.answer()
        
        # Добавление товара
        @router.callback_query(F.data == "add_product")
        async def add_product_start(callback: CallbackQuery, state: FSMContext):
            if callback.from_user.id != bot_data.admin_id:
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "➕ **Добавление нового товара**\n\n"
                "Шаг 1 из 4:\n"
                "Введите название товара:",
                parse_mode="Markdown"
            )
            await state.set_state(AddProductStates.waiting_for_name)
            await callback.answer()
        
        @router.message(AddProductStates.waiting_for_name)
        async def process_product_name(message: Message, state: FSMContext):
            await state.update_data(product_name=message.text)
            await message.answer(
                "Шаг 2 из 4:\n"
                "Введите описание товара:"
            )
            await state.set_state(AddProductStates.waiting_for_description)
        
        @router.message(AddProductStates.waiting_for_description)
        async def process_product_description(message: Message, state: FSMContext):
            await state.update_data(product_description=message.text)
            await message.answer(
                "Шаг 3 из 4:\n"
                "Введите цену товара в USD (например: 10.99):"
            )
            await state.set_state(AddProductStates.waiting_for_price)
        
        @router.message(AddProductStates.waiting_for_price)
        async def process_product_price(message: Message, state: FSMContext):
            try:
                price = float(message.text.replace(',', '.'))
                if price <= 0:
                    raise ValueError
                
                await state.update_data(product_price=price)
                await message.answer(
                    "Шаг 4 из 4:\n"
                    "Отправьте фото товара (или отправьте /skip чтобы пропустить):"
                )
                await state.set_state(AddProductStates.waiting_for_photo)
            except ValueError:
                await message.answer(
                    "❌ Неверный формат цены. Пожалуйста, введите число (например: 10.99)"
                )
        
        @router.message(AddProductStates.waiting_for_photo)
        async def process_product_photo(message: Message, state: FSMContext):
            photo_url = None
            if message.photo:
                photo_url = message.photo[-1].file_id
            
            data = await state.get_data()
            
            with SessionLocal() as db:
                product = Product(
                    bot_id=bot_data.id,
                    name=data['product_name'],
                    description=data['product_description'],
                    price=data['product_price'],
                    photo_url=photo_url
                )
                db.add(product)
                db.commit()
            
            await message.answer(
                f"✅ **Товар успешно добавлен!**\n\n"
                f"Название: {data['product_name']}\n"
                f"Цена: ${data['product_price']:.2f}",
                parse_mode="Markdown"
            )
            await state.clear()
        
        # Список товаров
        @router.callback_query(F.data == "shop_products")
        async def show_products(callback: CallbackQuery):
            with SessionLocal() as db:
                products = db.query(Product).filter(
                    and_(
                        Product.bot_id == bot_data.id,
                        Product.is_available == True
                    )
                ).all()
                
                if not products:
                    await callback.message.edit_text(
                        "📭 **Товаров пока нет**\n\n"
                        "Загляните позже!",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
                            ]
                        )
                    )
                    await callback.answer()
                    return
                
                builder = InlineKeyboardBuilder()
                for product in products:
                    builder.row(InlineKeyboardButton(
                        text=f"{product.name} - ${product.price:.2f}",
                        callback_data=f"product_{product.id}"
                    ))
                builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main"))
                
                await callback.message.edit_text(
                    "🛍 **Наши товары:**\n\nВыберите товар для покупки:",
                    parse_mode="Markdown",
                    reply_markup=builder.as_markup()
                )
            await callback.answer()
        
        # Детали товара
        @router.callback_query(lambda c: c.data and c.data.startswith('product_'))
        async def show_product(callback: CallbackQuery):
            product_id = int(callback.data.replace('product_', ''))
            
            with SessionLocal() as db:
                product = db.query(Product).filter(Product.id == product_id).first()
                if not product:
                    await callback.answer("Товар не найден", show_alert=True)
                    return
                
                text = f"**{product.name}**\n\n"
                text += f"{product.description}\n\n"
                text += f"💰 **Цена:** ${product.price:.2f}"
                
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(
                    text="💳 Купить",
                    callback_data=f"buy_{product.id}"
                ))
                builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="shop_products"))
                
                if product.photo_url:
                    await callback.message.delete()
                    await callback.message.answer_photo(
                        photo=product.photo_url,
                        caption=text,
                        parse_mode="Markdown",
                        reply_markup=builder.as_markup()
                    )
                else:
                    await callback.message.edit_text(
                        text,
                        parse_mode="Markdown",
                        reply_markup=builder.as_markup()
                    )
            await callback.answer()
        
        # Покупка товара
        @router.callback_query(lambda c: c.data and c.data.startswith('buy_'))
        async def buy_product(callback: CallbackQuery):
            product_id = int(callback.data.replace('buy_', ''))
            
            with SessionLocal() as db:
                product = db.query(Product).filter(Product.id == product_id).first()
                bot_user = db.query(BotUser).filter(
                    and_(
                        BotUser.bot_id == bot_data.id,
                        BotUser.user_id == callback.from_user.id
                    )
                ).first()
                
                if not product or not product.is_available:
                    await callback.answer("Товар недоступен", show_alert=True)
                    return
                
                # Создаем заказ
                payment_id = str(uuid.uuid4())
                order = Order(
                    bot_id=bot_data.id,
                    user_id=bot_user.id,
                    product_id=product.id,
                    amount=product.price,
                    status="pending",
                    payment_id=payment_id
                )
                db.add(order)
                db.commit()
                
                # Здесь должна быть интеграция с Crypto Bot
                # Пока просто имитируем успешную оплату
                
                # Отправляем инструкцию по оплате
                await callback.message.answer(
                    f"🧾 **Заказ #{order.id}**\n\n"
                    f"Товар: {product.name}\n"
                    f"Сумма: ${product.price:.2f}\n\n"
                    f"Для оплаты переведите {product.price} USDT (TRC20) на адрес:\n"
                    f"`TX7HGVPqyRZnZ1qZ1qZ1qZ1qZ1qZ1qZ1qZ`\n\n"
                    f"После оплаты нажмите кнопку ниже:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_payment_{order.id}")]
                        ]
                    )
                )
            
            await callback.answer()
        
        # Проверка оплаты
        @router.callback_query(lambda c: c.data and c.data.startswith('check_payment_'))
        async def check_payment(callback: CallbackQuery):
            order_id = int(callback.data.replace('check_payment_', ''))
            
            with SessionLocal() as db:
                order = db.query(Order).filter(Order.id == order_id).first()
                if not order:
                    await callback.answer("Заказ не найден", show_alert=True)
                    return
                
                if order.status == "paid":
                    await callback.answer("Заказ уже оплачен!", show_alert=True)
                    return
                
                # Здесь должна быть реальная проверка платежа в Crypto Bot
                # Пока просто помечаем как оплаченный
                order.status = "paid"
                order.paid_at = datetime.now()
                
                # Обновляем статистику
                today_stat = db.query(BotStat).filter(
                    and_(
                        BotStat.bot_id == bot_data.id,
                        BotStat.date == date.today()
                    )
                ).first()
                
                if today_stat:
                    today_stat.orders_count += 1
                    today_stat.revenue += order.amount
                else:
                    today_stat = BotStat(
                        bot_id=bot_data.id,
                        date=date.today(),
                        orders_count=1,
                        revenue=order.amount
                    )
                    db.add(today_stat)
                
                db.commit()
                
                await callback.message.edit_text(
                    f"✅ **Оплата подтверждена!**\n\n"
                    f"Заказ #{order.id} оплачен.\n"
                    f"Спасибо за покупку!",
                    parse_mode="Markdown"
                )
            
            await callback.answer("Оплата подтверждена!", show_alert=True)
        
        # Профиль пользователя
        @router.callback_query(F.data == "shop_profile")
        async def show_profile(callback: CallbackQuery):
            with SessionLocal() as db:
                bot_user = db.query(BotUser).filter(
                    and_(
                        BotUser.bot_id == bot_data.id,
                        BotUser.user_id == callback.from_user.id
                    )
                ).first()
                
                if not bot_user:
                    await callback.answer("Ошибка профиля", show_alert=True)
                    return
                
                orders_count = db.query(Order).filter(
                    and_(
                        Order.bot_id == bot_data.id,
                        Order.user_id == bot_user.id
                    )
                ).count()
                
                paid_orders = db.query(Order).filter(
                    and_(
                        Order.bot_id == bot_data.id,
                        Order.user_id == bot_user.id,
                        Order.status == "paid"
                    )
                ).count()
                
                total_spent = db.query(func.sum(Order.amount)).filter(
                    and_(
                        Order.bot_id == bot_data.id,
                        Order.user_id == bot_user.id,
                        Order.status == "paid"
                    )
                ).scalar() or 0
                
                profile_text = (
                    f"👤 **Ваш профиль**\n\n"
                    f"🆔 ID: {bot_user.user_id}\n"
                    f"📛 Имя: {bot_user.first_name}\n"
                    f"💰 Баланс: ${bot_user.balance:.2f}\n"
                    f"📦 Всего заказов: {orders_count}\n"
                    f"✅ Оплаченных: {paid_orders}\n"
                    f"💸 Потрачено: ${total_spent:.2f}"
                )
                
                await callback.message.edit_text(
                    profile_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="📋 История заказов", callback_data="order_history")],
                            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]
                        ]
                    )
                )
            await callback.answer()
        
        # История заказов
        @router.callback_query(F.data == "order_history")
        async def order_history(callback: CallbackQuery):
            with SessionLocal() as db:
                bot_user = db.query(BotUser).filter(
                    and_(
                        BotUser.bot_id == bot_data.id,
                        BotUser.user_id == callback.from_user.id
                    )
                ).first()
                
                orders = db.query(Order).filter(
                    and_(
                        Order.bot_id == bot_data.id,
                        Order.user_id == bot_user.id
                    )
                ).order_by(Order.created_at.desc()).limit(10).all()
                
                if not orders:
                    await callback.message.edit_text(
                        "📭 **У вас пока нет заказов**",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="◀️ Назад", callback_data="shop_profile")]
                            ]
                        )
                    )
                    await callback.answer()
                    return
                
                text = "📋 **Последние заказы:**\n\n"
                for order in orders:
                    status_emoji = "✅" if order.status == "paid" else "⏳"
                    text += f"{status_emoji} Заказ #{order.id}: ${order.amount:.2f} - {order.created_at.strftime('%d.%m.%Y')}\n"
                
                await callback.message.edit_text(
                    text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="◀️ Назад", callback_data="shop_profile")]
                        ]
                    )
                )
            await callback.answer()
        
        # Назад в главное меню
        @router.callback_query(F.data == "back_to_main")
        async def back_to_main(callback: CallbackQuery):
            await show_main_menu(callback)
        
        dp.include_router(router)
    
    async def _run_bot(self, bot: Bot, dp: Dispatcher, bot_data: CreatedBot):
        """Запуск бота в бесконечном цикле"""
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Бот {bot_data.id} остановлен с ошибкой: {e}")
        finally:
            await bot.session.close()
            if bot_data.id in self.running_bots:
                del self.running_bots[bot_data.id]
    
    async def stop_bot_instance(self, bot_id: int):
        """Остановка экземпляра бота"""
        if bot_id in self.running_bots:
            self.running_bots[bot_id]['task'].cancel()
            await self.running_bots[bot_id]['bot'].session.close()
            del self.running_bots[bot_id]
            logger.info(f"Бот {bot_id} остановлен")
    
    async def load_and_start_all_bots(self):
        """Загрузка всех активных ботов из БД и их запуск"""
        with SessionLocal() as db:
            active_bots = db.query(CreatedBot).filter(CreatedBot.is_active == True).all()
            for bot_data in active_bots:
                await self.start_bot_instance(bot_data)

# Инициализация главного бота
main_bot = Bot(token=BOT_TOKEN)
main_dp = Dispatcher(storage=MemoryStorage())
main_router = Router()
bots_manager = BotsManager()

# Вспомогательные функции
async def get_or_create_user(db: Session, tg_user: types.User):
    """Получить или создать пользователя в БД"""
    user = db.query(User).filter(User.user_id == tg_user.id).first()
    if not user:
        user = User(
            user_id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

async def validate_bot_token(token: str) -> tuple[bool, Optional[str]]:
    """Проверка валидности токена бота"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('ok'):
                        return True, data['result'].get('username')
                return False, None
    except Exception:
        return False, None

async def check_bot_in_channel(token: str, channel_id: int) -> bool:
    """Проверка, добавлен ли бот в канал как администратор"""
    try:
        bot = Bot(token=token)
        chat_member = await bot.get_chat_member(chat_id=channel_id, user_id=(await bot.me()).id)
        await bot.session.close()
        return chat_member.status in ['administrator', 'creator']
    except Exception:
        return False

# Клавиатуры главного бота
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🤖 Создать бота", callback_data="create_bot"))
    builder.row(InlineKeyboardButton(text="📋 Мои боты", callback_data="my_bots"))
    builder.row(InlineKeyboardButton(text="❓ Помощь", callback_data="help"))
    return builder.as_markup()

def bot_types_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🤖 Автозаявки", callback_data="create_autoclaims"))
    builder.row(InlineKeyboardButton(text="🛍 Магазин", callback_data="create_shop"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main"))
    return builder.as_markup()

def back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main"))
    return builder.as_markup()

# Обработчики главного бота
@main_router.message(CommandStart())
async def cmd_start(message: Message):
    with SessionLocal() as db:
        await get_or_create_user(db, message.from_user)
    
    await message.answer(
        "👋 **Добро пожаловать в Vest Creator!**\n\n"
        "Я помогу вам создать собственного бота.\n"
        "Выберите действие в меню ниже:",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

@main_router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 **Добро пожаловать в Vest Creator!**\n\n"
        "Я помогу вам создать собственного бота.\n"
        "Выберите действие в меню ниже:",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

@main_router.callback_query(F.data == "create_bot")
async def create_bot_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🤖 **Выберите тип бота для создания:**",
        parse_mode="Markdown",
        reply_markup=bot_types_keyboard()
    )
    await callback.answer()

@main_router.callback_query(F.data == "create_autoclaims")
async def create_autoclaims(callback: CallbackQuery, state: FSMContext):
    await state.update_data(bot_type="autoclaims")
    await callback.message.edit_text(
        "🔄 **Создание бота \"Автозаявки\"**\n\n"
        "Шаг 1 из 4:\n"
        "Отправьте токен бота, который вы получили от @BotFather",
        parse_mode="Markdown"
    )
    await state.set_state(CreateBotStates.waiting_for_token)
    await callback.answer()

@main_router.callback_query(F.data == "create_shop")
async def create_shop(callback: CallbackQuery, state: FSMContext):
    await state.update_data(bot_type="shop")
    await callback.message.edit_text(
        "🛍 **Создание бота \"Магазин\"**\n\n"
        "Шаг 1 из 4:\n"
        "Отправьте токен бота, который вы получили от @BotFather",
        parse_mode="Markdown"
    )
    await state.set_state(CreateBotStates.waiting_for_token)
    await callback.answer()

@main_router.message(CreateBotStates.waiting_for_token)
async def process_token(message: Message, state: FSMContext):
    token = message.text.strip()
    
    # Проверка валидности токена
    is_valid, username = await validate_bot_token(token)
    
    if not is_valid:
        await message.answer(
            "❌ **Неверный токен!**\n\n"
            "Пожалуйста, проверьте токен и отправьте снова.\n"
            "Чтобы получить токен, обратитесь к @BotFather",
            parse_mode="Markdown"
        )
        return
    
    await state.update_data(bot_token=token, bot_username=username)
    await message.answer(
        "✅ **Токен принят!**\n\n"
        "Шаг 2 из 4:\n"
        "Отправьте числовой ID администратора (обычно это ваш ID).\n"
        "Чтобы узнать свой ID, отправьте любое сообщение сюда: @userinfobot",
        parse_mode="Markdown"
    )
    await state.set_state(CreateBotStates.waiting_for_admin_id)

@main_router.message(CreateBotStates.waiting_for_admin_id)
async def process_admin_id(message: Message, state: FSMContext):
    try:
        admin_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ **Ошибка!**\n\n"
            "Пожалуйста, введите числовой ID администратора.",
            parse_mode="Markdown"
        )
        return
    
    await state.update_data(admin_id=admin_id)
    
    data = await state.get_data()
    bot_type = data.get('bot_type')
    
    if bot_type == "autoclaims":
        await message.answer(
            "✅ **ID администратора сохранен!**\n\n"
            "Шаг 3 из 4:\n"
            "Отправьте текст приветственного сообщения, которое будет отправляться пользователям при принятии заявки:",
            parse_mode="Markdown"
        )
        await state.set_state(CreateBotStates.waiting_for_welcome)
    else:  # shop
        await message.answer(
            "✅ **ID администратора сохранен!**\n\n"
            "Шаг 3 из 4:\n"
            "Отправьте токен от Crypto Bot (получите в @CryptoBot):",
            parse_mode="Markdown"
        )
        await state.set_state(CreateBotStates.waiting_for_crypto_token)

@main_router.message(CreateBotStates.waiting_for_welcome)
async def process_welcome(message: Message, state: FSMContext):
    welcome_text = message.text
    
    await state.update_data(welcome_message=welcome_text)
    await message.answer(
        "✅ **Приветственное сообщение сохранено!**\n\n"
        "Шаг 4 из 4:\n"
        "Отправьте ID канала для мониторинга заявок (например, -1001234567890).\n"
        "Бот должен быть добавлен в этот канал как администратор!",
        parse_mode="Markdown"
    )
    await state.set_state(CreateBotStates.waiting_for_channel)

@main_router.message(CreateBotStates.waiting_for_crypto_token)
async def process_crypto_token(message: Message, state: FSMContext):
    crypto_token = message.text.strip()
    
    # Здесь можно добавить проверку токена Crypto Bot
    await state.update_data(crypto_token=crypto_token)
    
    await message.answer(
        "✅ **Токен Crypto Bot сохранен!**\n\n"
        "Шаг 4 из 4:\n"
        "Введите название вашего магазина:",
        parse_mode="Markdown"
    )
    await state.set_state(CreateBotStates.waiting_for_shop_name)

@main_router.message(CreateBotStates.waiting_for_shop_name)
async def process_shop_name(message: Message, state: FSMContext):
    shop_name = message.text.strip()
    await state.update_data(shop_name=shop_name)
    
    # Сохраняем магазин в БД
    await save_bot_to_db(message, state)

@main_router.message(CreateBotStates.waiting_for_channel)
async def process_channel(message: Message, state: FSMContext):
    try:
        channel_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ **Ошибка!**\n\n"
            "Пожалуйста, введите числовой ID канала (например, -1001234567890)",
            parse_mode="Markdown"
        )
        return
    
    data = await state.get_data()
    
    # Проверяем, добавлен ли бот в канал
    is_in_channel = await check_bot_in_channel(data['bot_token'], channel_id)
    
    if not is_in_channel:
        await message.answer(
            "❌ **Бот не является администратором канала!**\n\n"
            "Пожалуйста, добавьте бота в канал как администратора и повторите попытку.",
            parse_mode="Markdown"
        )
        return
    
    await state.update_data(channel_id=channel_id)
    await save_bot_to_db(message, state)

async def save_bot_to_db(message: Message, state: FSMContext):
    """Сохранение бота в БД"""
    data = await state.get_data()
    
    with SessionLocal() as db:
        user = await get_or_create_user(db, message.from_user)
        
        if data['bot_type'] == "autoclaims":
            new_bot = CreatedBot(
                owner_id=user.user_id,
                bot_token=data['bot_token'],
                bot_username=data['bot_username'],
                bot_type="autoclaims",
                admin_id=data['admin_id'],
                welcome_message=data['welcome_message'],
                channel_id=data['channel_id'],
                is_active=True,
                created_at=datetime.now(),
                last_activity=datetime.now()
            )
        else:  # shop
            new_bot = CreatedBot(
                owner_id=user.user_id,
                bot_token=data['bot_token'],
                bot_username=data['bot_username'],
                bot_type="shop",
                admin_id=data['admin_id'],
                crypto_token=data.get('crypto_token'),
                shop_name=data.get('shop_name'),
                is_active=True,
                created_at=datetime.now(),
                last_activity=datetime.now()
            )
        
        db.add(new_bot)
        db.commit()
        db.refresh(new_bot)
    
    # Запускаем бота
    await bots_manager.start_bot_instance(new_bot)
    
    success_text = (
        f"✅ **Поздравляю! Бот успешно создан и запущен!**\n\n"
        f"🤖 Имя бота: @{data['bot_username']}\n"
        f"📊 Тип: {'Автозаявки' if data['bot_type'] == 'autoclaims' else 'Магазин'}\n\n"
    )
    
    if data['bot_type'] == "autoclaims":
        success_text += "Бот автоматически отслеживает новые сообщения в канале и отправляет приветствие пользователям."
    else:
        success_text += "Бот-магазин с оплатой через Crypto Bot готов к работе!"
    
    await message.answer(success_text, parse_mode="Markdown")
    await state.clear()

@main_router.callback_query(F.data == "my_bots")
async def my_bots(callback: CallbackQuery):
    with SessionLocal() as db:
        user = db.query(User).filter(User.user_id == callback.from_user.id).first()
        if not user:
            await callback.answer("Сначала используйте /start", show_alert=True)
            return
        
        bots = db.query(CreatedBot).filter(CreatedBot.owner_id == user.user_id).all()
        
        if not bots:
            await callback.message.edit_text(
                "📋 **У вас пока нет созданных ботов**\n\n"
                "Нажмите \"🤖 Создать бота\", чтобы создать первого бота!",
                parse_mode="Markdown",
                reply_markup=back_keyboard()
            )
            await callback.answer()
            return
        
        builder = InlineKeyboardBuilder()
        for bot in bots:
            status = "🟢" if bot.is_active else "🔴"
            type_emoji = "🤖" if bot.bot_type == "autoclaims" else "🛍"
            builder.row(InlineKeyboardButton(
                text=f"{status} {type_emoji} {bot.bot_username or 'Бот'}",
                callback_data=f"bot_info_{bot.id}"
            ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main"))
        
        await callback.message.edit_text(
            "📋 **Ваши боты:**",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    await callback.answer()

@main_router.callback_query(lambda c: c.data and c.data.startswith('bot_info_'))
async def bot_info(callback: CallbackQuery):
    bot_id = int(callback.data.replace('bot_info_', ''))
    
    with SessionLocal() as db:
        bot = db.query(CreatedBot).filter(CreatedBot.id == bot_id).first()
        if not bot:
            await callback.answer("Бот не найден", show_alert=True)
            return
        
        if bot.bot_type == "autoclaims":
            total_users = db.query(BotUser).filter(BotUser.bot_id == bot.id).count()
            total_claims = db.query(func.sum(BotUser.claims_count)).filter(BotUser.bot_id == bot.id).scalar() or 0
            
            info_text = (
                f"🤖 **Информация о боте**\n\n"
                f"📛 Имя: @{bot.bot_username}\n"
                f"📋 Тип: Автозаявки\n"
                f"📢 Канал: `{bot.channel_id}`\n"
                f"👥 Пользователей: {total_users}\n"
                f"📝 Заявок принято: {total_claims}\n"
                f"🕐 Создан: {bot.created_at.strftime('%d.%m.%Y')}\n"
                f"Статус: {'🟢 Активен' if bot.is_active else '🔴 Неактивен'}"
            )
        else:  # shop
            total_users = db.query(BotUser).filter(BotUser.bot_id == bot.id).count()
            total_products = db.query(Product).filter(Product.bot_id == bot.id).count()
            total_orders = db.query(Order).filter(Order.bot_id == bot.id).count()
            total_revenue = db.query(func.sum(Order.amount)).filter(
                and_(
                    Order.bot_id == bot.id,
                    Order.status == "paid"
                )
            ).scalar() or 0
            
            info_text = (
                f"🛍 **Информация о магазине**\n\n"
                f"🏪 Название: {bot.shop_name}\n"
                f"📛 Бот: @{bot.bot_username}\n"
                f"👥 Пользователей: {total_users}\n"
                f"📦 Товаров: {total_products}\n"
                f"📋 Заказов: {total_orders}\n"
                f"💰 Выручка: ${total_revenue:.2f}\n"
                f"🕐 Создан: {bot.created_at.strftime('%d.%m.%Y')}\n"
                f"Статус: {'🟢 Активен' if bot.is_active else '🔴 Неактивен'}"
            )
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="🔗 Перейти к боту",
            url=f"https://t.me/{bot.bot_username}"
        ))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="my_bots"))
        
        await callback.message.edit_text(
            info_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    await callback.answer()

@main_router.callback_query(F.data == "help")
async def help_menu(callback: CallbackQuery):
    help_text = (
        "❓ **Помощь по созданию ботов**\n\n"
        "**1. Создание бота через @BotFather:**\n"
        "• Напишите @BotFather\n"
        "• Отправьте /newbot\n"
        "• Придумайте имя и username бота\n"
        "• Получите токен\n\n"
        "**2. Для бота Автозаявки:**\n"
        "• Добавьте бота в канал как администратора\n"
        "• Получите ID канала через @userinfobot\n\n"
        "**3. Для бота Магазин:**\n"
        "• Получите токен в @CryptoBot\n"
        "• Настройте товары в админ-панели\n\n"
        "**4. Команды созданных ботов:**\n"
        "• /admin - админ-панель"
    )
    
    await callback.message.edit_text(
        help_text,
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )
    await callback.answer()

# Запуск всех ботов при старте
@main_dp.startup()
async def on_startup():
    logger.info("Загрузка и запуск всех активных ботов...")
    await bots_manager.load_and_start_all_bots()
    logger.info("Главный бот запущен!")

@main_dp.shutdown()
async def on_shutdown():
    logger.info("Остановка всех ботов...")
    for bot_id in list(bots_manager.running_bots.keys()):
        await bots_manager.stop_bot_instance(bot_id)
    logger.info("Все боты остановлены")

# Главная функция запуска
async def main():
    main_dp.include_router(main_router)
    await main_dp.start_polling(main_bot)

if __name__ == "__main__":
    asyncio.run(main())
