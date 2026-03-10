import os
import asyncio
import logging
import aiohttp
from datetime import datetime, date
from contextlib import asynccontextmanager
from typing import Dict, Optional
import sys

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError

from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Text, Boolean, DateTime, Date, ForeignKey, func, select, and_, Index
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
    bot_type = Column(String)  # "autoclaims"
    admin_id = Column(BigInteger)
    welcome_message = Column(Text)
    channel_id = Column(BigInteger)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    last_activity = Column(DateTime, default=datetime.now)
    
    owner = relationship("User", back_populates="created_bots")
    bot_users = relationship("BotUser", back_populates="bot")
    stats = relationship("BotStat", back_populates="bot")

class BotUser(Base):
    __tablename__ = 'bot_users'
    
    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey('created_bots.id'))
    user_id = Column(BigInteger)
    username = Column(String, nullable=True)
    first_name = Column(String)
    last_interaction = Column(DateTime, default=datetime.now)
    claims_count = Column(Integer, default=0)
    
    bot = relationship("CreatedBot", back_populates="bot_users")
    
    __table_args__ = (Index('idx_bot_user', 'bot_id', 'user_id', unique=True),)

class BotStat(Base):
    __tablename__ = 'bot_stats'
    
    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey('created_bots.id'))
    date = Column(Date, default=date.today)
    claims_processed = Column(Integer, default=0)
    messages_sent = Column(Integer, default=0)
    
    bot = relationship("CreatedBot", back_populates="stats")
    
    __table_args__ = (Index('idx_bot_stat', 'bot_id', 'date', unique=True),)

# Создание таблиц
Base.metadata.create_all(engine)

# Состояния FSM для создания бота
class CreateBotStates(StatesGroup):
    waiting_for_token = State()
    waiting_for_admin_id = State()
    waiting_for_welcome = State()
    waiting_for_channel = State()

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
            
            # Настраиваем обработчики для этого бота
            await self._setup_bot_handlers(dp, bot_data)
            
            # Запускаем бота в фоне
            task = asyncio.create_task(self._run_bot(bot, dp, bot_data))
            self.running_bots[bot_data.id] = {
                'bot': bot,
                'dp': dp,
                'task': task,
                'data': bot_data
            }
            logger.info(f"Бот {bot_data.bot_username} (ID: {bot_data.id}) успешно запущен")
        except Exception as e:
            logger.error(f"Ошибка при запуске бота {bot_data.id}: {e}")
    
    async def _setup_bot_handlers(self, dp: Dispatcher, bot_data: CreatedBot):
        """Настройка обработчиков для бота автозаявок"""
        
        # Роутер для этого бота
        router = Router()
        
        # Middleware для проверки админ-доступа
        @router.message(lambda message: message.text == '/admin')
        async def admin_panel(message: Message):
            if message.from_user.id != bot_data.admin_id:
                await message.answer("⛔ У вас нет доступа к админ-панели")
                return
            
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
            builder.row(InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing"))
            builder.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings"))
            builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
            
            await message.answer(
                "👨‍💼 **Админ-панель**\n\nВыберите действие:",
                reply_markup=builder.as_markup(),
                parse_mode="Markdown"
            )
        
        # Обработка новых сообщений в канале
        @router.channel_post()
        async def handle_channel_post(message: Message):
            """Обработка новых сообщений в канале"""
            try:
                with SessionLocal() as db:
                    # Получаем информацию о боте
                    bot_info = db.query(CreatedBot).filter(CreatedBot.id == bot_data.id).first()
                    if not bot_info or not bot_info.is_active:
                        return
                    
                    # Определяем отправителя (если есть подпись или пересланное сообщение)
                    user_id = None
                    username = None
                    first_name = None
                    
                    if message.forward_from:
                        user_id = message.forward_from.id
                        username = message.forward_from.username
                        first_name = message.forward_from.first_name
                    elif message.forward_sender_name:
                        # Анонимный канал
                        pass
                    elif message.author_signature:
                        # Подпись в канале
                        pass
                    
                    if user_id:
                        # Отправляем приветственное сообщение пользователю
                        try:
                            await message.bot.send_message(
                                user_id,
                                bot_info.welcome_message,
                                parse_mode="HTML"
                            )
                            
                            # Сохраняем или обновляем пользователя
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
                            
                            # Реакция на сообщение в канале
                            await message.react([types.ReactionTypeEmoji(emoji="✅")])
                            
                        except Exception as e:
                            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
                    
            except Exception as e:
                logger.error(f"Ошибка при обработке поста в канале: {e}")
        
        # Обработчики админ-панели
        @router.callback_query(lambda c: c.data and c.data.startswith('admin_'))
        async def admin_callbacks(callback: CallbackQuery):
            if callback.from_user.id != bot_data.admin_id:
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            action = callback.data.replace('admin_', '')
            
            with SessionLocal() as db:
                bot_info = db.query(CreatedBot).filter(CreatedBot.id == bot_data.id).first()
                
                if action == 'stats':
                    total_users = db.query(BotUser).filter(BotUser.bot_id == bot_info.id).count()
                    total_claims = db.query(func.sum(BotUser.claims_count)).filter(BotUser.bot_id == bot_info.id).scalar() or 0
                    today_claims = db.query(func.sum(BotStat.claims_processed)).filter(
                        and_(
                            BotStat.bot_id == bot_info.id,
                            BotStat.date == date.today()
                        )
                    ).scalar() or 0
                    
                    stats_text = (
                        f"📊 **Статистика бота**\n\n"
                        f"👥 Всего пользователей: {total_users}\n"
                        f"📝 Всего заявок: {total_claims}\n"
                        f"📅 Заявок сегодня: {today_claims}\n"
                        f"🕐 Последняя активность: {bot_info.last_activity.strftime('%d.%m.%Y %H:%M')}"
                    )
                    
                    await callback.message.edit_text(
                        stats_text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
                            ]
                        )
                    )
                
                elif action == 'mailing':
                    await callback.message.edit_text(
                        "📨 **Режим рассылки**\n\n"
                        "Отправьте сообщение, которое нужно разослать всем пользователям бота.\n"
                        "Для отмены отправьте /cancel",
                        parse_mode="Markdown"
                    )
                    
                    # Устанавливаем состояние для рассылки
                    # TODO: реализовать FSM для рассылки
                
                elif action == 'settings':
                    builder = InlineKeyboardBuilder()
                    builder.row(InlineKeyboardButton(text="✏️ Изменить приветствие", callback_data="edit_welcome"))
                    builder.row(InlineKeyboardButton(text="📢 Изменить канал", callback_data="edit_channel"))
                    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
                    
                    settings_text = (
                        f"⚙️ **Настройки бота**\n\n"
                        f"📝 Приветствие: {bot_info.welcome_message[:50]}...\n"
                        f"📢 Канал: `{bot_info.channel_id}`\n"
                        f"👤 Админ ID: `{bot_info.admin_id}`"
                    )
                    
                    await callback.message.edit_text(
                        settings_text,
                        parse_mode="Markdown",
                        reply_markup=builder.as_markup()
                    )
                
                elif action == 'back':
                    builder = InlineKeyboardBuilder()
                    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
                    builder.row(InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing"))
                    builder.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings"))
                    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
                    
                    await callback.message.edit_text(
                        "👨‍💼 **Админ-панель**\n\nВыберите действие:",
                        reply_markup=builder.as_markup(),
                        parse_mode="Markdown"
                    )
            
            await callback.answer()
        
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
        "Я помогу вам создать собственного бота для автоматического приема заявок.\n"
        "Выберите действие в меню ниже:",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

@main_router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 **Добро пожаловать в Vest Creator!**\n\n"
        "Я помогу вам создать собственного бота для автоматического приема заявок.\n"
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
    await callback.message.edit_text(
        "🔄 **Создание бота \"Автозаявки\"**\n\n"
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
    await message.answer(
        "✅ **ID администратора сохранен!**\n\n"
        "Шаг 3 из 4:\n"
        "Отправьте текст приветственного сообщения, которое будет отправляться пользователям при принятии заявки:",
        parse_mode="Markdown"
    )
    await state.set_state(CreateBotStates.waiting_for_welcome)

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
            "Пожалуйста, добавьте бота в канал как администратора и повторите попытку.\n"
            "ID канала должен быть правильным.",
            parse_mode="Markdown"
        )
        return
    
    # Сохраняем бота в базу данных
    with SessionLocal() as db:
        user = await get_or_create_user(db, message.from_user)
        
        new_bot = CreatedBot(
            owner_id=user.user_id,
            bot_token=data['bot_token'],
            bot_username=data['bot_username'],
            bot_type="autoclaims",
            admin_id=data['admin_id'],
            welcome_message=data['welcome_message'],
            channel_id=channel_id,
            is_active=True,
            created_at=datetime.now(),
            last_activity=datetime.now()
        )
        
        db.add(new_bot)
        db.commit()
        db.refresh(new_bot)
        
        bot_id = new_bot.id
    
    # Запускаем бота
    await bots_manager.start_bot_instance(new_bot)
    
    await message.answer(
        "✅ **Поздравляю! Бот успешно создан и запущен!**\n\n"
        f"🤖 Имя бота: @{data['bot_username']}\n"
        f"📊 Статистика: доступна в админ-панели по команде /admin\n\n"
        "Бот автоматически отслеживает новые сообщения в канале и отправляет приветствие пользователям.",
        parse_mode="Markdown"
    )
    
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
            builder.row(InlineKeyboardButton(
                text=f"{status} {bot.bot_username or 'Бот'}",
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
        "❓ **Помощь по созданию бота**\n\n"
        "**1. Создание бота через @BotFather:**\n"
        "• Напишите @BotFather\n"
        "• Отправьте /newbot\n"
        "• Придумайте имя и username бота\n"
        "• Получите токен\n\n"
        "**2. Как получить ID канала:**\n"
        "• Добавьте @userinfobot в канал\n"
        "• Отправьте любое сообщение\n"
        "• Бот покажет ID канала\n\n"
        "**3. Добавление бота в канал:**\n"
        "• Сделайте бота администратором канала\n"
        "• Дайте права на отправку сообщений\n\n"
        "**4. Команды созданного бота:**\n"
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
