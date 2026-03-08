import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
    CallbackQuery, Message
)
from dotenv import load_dotenv
import aiosqlite

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# Настройки бота
BOT_NAME = "TaskCoin"  # Название можно изменить
START_COINS = 150  # Стартовые монеты
SUBSCRIBE_REWARD = 10  # Награда за подписку
MIN_REWARD = 5  # Минимальная награда за задание
REWARD_COOLDOWN_HOURS = 24  # Часов до повторной награды

# Каналы для подписки (пустой список, добавишь позже)
REQUIRED_CHANNELS = []  # Формат: [{"name": "Название", "url": "https://t.me/channel", "id": "@channel"}]

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Состояния для FSM
class CreateTaskStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_reward = State()
    waiting_for_limit = State()
    waiting_for_confirmation = State()

# Клавиатуры
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура с красивыми кнопками"""
    buttons = [
        [KeyboardButton(text="🌟 Задания"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="📋 Создать задание")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="❓ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_channels_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с каналами для подписки"""
    buttons = []
    
    if REQUIRED_CHANNELS:
        for i in range(0, len(REQUIRED_CHANNELS), 2):
            row = []
            row.append(InlineKeyboardButton(
                text=f"📺 {REQUIRED_CHANNELS[i]['name']}", 
                url=REQUIRED_CHANNELS[i]['url']
            ))
            if i + 1 < len(REQUIRED_CHANNELS):
                row.append(InlineKeyboardButton(
                    text=f"📺 {REQUIRED_CHANNELS[i+1]['name']}", 
                    url=REQUIRED_CHANNELS[i+1]['url']
                ))
            buttons.append(row)
    else:
        buttons.append([InlineKeyboardButton(
            text="📢 Каналы для подписки скоро появятся", 
            callback_data="no_channels"
        )])
    
    if REQUIRED_CHANNELS:
        buttons.append([InlineKeyboardButton(text="🔄 ПРОВЕРИТЬ ПОДПИСКУ", callback_data="check_subscription")])
    buttons.append([InlineKeyboardButton(text="❌ ЗАКРЫТЬ", callback_data="close_welcome")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_tasks_keyboard(tasks: List[Dict]) -> InlineKeyboardMarkup:
    """Клавиатура со списком заданий"""
    buttons = []
    for task in tasks:
        buttons.append([InlineKeyboardButton(
            text=f"📌 Задание #{task['id']} | {task['reward']} монет | Осталось: {task['left']}",
            callback_data=f"view_task_{task['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_action_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для конкретного задания"""
    buttons = [
        [InlineKeyboardButton(text="▶ ВЫПОЛНИТЬ ЗАДАНИЕ", callback_data=f"do_task_{task_id}")],
        [InlineKeyboardButton(text="◀ НАЗАД К ЗАДАНИЯМ", callback_data="back_to_tasks")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подтверждения создания задания"""
    buttons = [
        [
            InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ", callback_data="confirm_task"),
            InlineKeyboardButton(text="❌ ОТМЕНИТЬ", callback_data="cancel_task")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Инициализация базы данных
async def init_db():
    """Создание всех необходимых таблиц"""
    async with aiosqlite.connect('bot_database.db') as db:
        # Таблица пользователей
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                tasks_created INTEGER DEFAULT 0,
                tasks_completed INTEGER DEFAULT 0,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_reward TIMESTAMP
            )
        ''')
        
        # Таблица заданий (любой пользователь может создать)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                channel_link TEXT,
                channel_name TEXT,
                reward INTEGER,
                total_limit INTEGER,
                completed_count INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица выполненных заданий
        await db.execute('''
            CREATE TABLE IF NOT EXISTS completed_tasks (
                user_id INTEGER,
                task_id INTEGER,
                completed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, task_id)
            )
        ''')
        
        await db.commit()

# Функции для работы с пользователями
async def get_user(user_id: int) -> Optional[Dict]:
    """Получить данные пользователя"""
    async with aiosqlite.connect('bot_database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = await cursor.fetchone()
        return dict(user) if user else None

async def create_user(user_id: int, username: str = None):
    """Создать нового пользователя со стартовыми монетами"""
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute(
            'INSERT OR IGNORE INTO users (user_id, username, balance, joined_date) VALUES (?, ?, ?, ?)',
            (user_id, username, START_COINS, datetime.now())
        )
        await db.commit()

async def update_balance(user_id: int, amount: int):
    """Изменить баланс пользователя"""
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute(
            'UPDATE users SET balance = balance + ? WHERE user_id = ?',
            (amount, user_id)
        )
        await db.commit()

async def can_get_reward(user_id: int) -> bool:
    """Проверить, может ли пользователь получить награду за подписку"""
    async with aiosqlite.connect('bot_database.db') as db:
        cursor = await db.execute(
            'SELECT last_reward FROM users WHERE user_id = ?',
            (user_id,)
        )
        result = await cursor.fetchone()
        
        if not result or not result[0]:
            return True
        
        last_reward = datetime.fromisoformat(result[0])
        cooldown_end = last_reward + timedelta(hours=REWARD_COOLDOWN_HOURS)
        return datetime.now() > cooldown_end

async def update_last_reward(user_id: int):
    """Обновить время последней награды"""
    async with aiosqlite.connect('bot_database.db') as db:
        await db.execute(
            'UPDATE users SET last_reward = ? WHERE user_id = ?',
            (datetime.now().isoformat(), user_id)
        )
        await db.commit()

# Функции для работы с заданиями
async def get_active_tasks() -> List[Dict]:
    """Получить все активные задания"""
    async with aiosqlite.connect('bot_database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT *, (total_limit - completed_count) as left 
            FROM tasks 
            WHERE is_active = 1 AND completed_count < total_limit
            ORDER BY created_date DESC
        ''')
        tasks = await cursor.fetchall()
        return [dict(task) for task in tasks]

async def get_task(task_id: int) -> Optional[Dict]:
    """Получить задание по ID"""
    async with aiosqlite.connect('bot_database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            'SELECT *, (total_limit - completed_count) as left FROM tasks WHERE id = ?',
            (task_id,)
        )
        task = await cursor.fetchone()
        return dict(task) if task else None

async def create_task(creator_id: int, channel_link: str, channel_name: str, reward: int, limit: int):
    """Создать новое задание"""
    async with aiosqlite.connect('bot_database.db') as db:
        # Списываем монеты у создателя
        total_cost = reward * limit
        await db.execute(
            'UPDATE users SET balance = balance - ?, tasks_created = tasks_created + 1 WHERE user_id = ?',
            (total_cost, creator_id)
        )
        
        # Создаем задание
        await db.execute('''
            INSERT INTO tasks (creator_id, channel_link, channel_name, reward, total_limit)
            VALUES (?, ?, ?, ?, ?)
        ''', (creator_id, channel_link, channel_name, reward, limit))
        
        await db.commit()

async def complete_task(user_id: int, task_id: int):
    """Отметить задание как выполненное"""
    async with aiosqlite.connect('bot_database.db') as db:
        # Получаем информацию о задании
        task = await get_task(task_id)
        if not task:
            return False
        
        # Проверяем, не выполнял ли уже пользователь это задание
        cursor = await db.execute(
            'SELECT * FROM completed_tasks WHERE user_id = ? AND task_id = ?',
            (user_id, task_id)
        )
        if await cursor.fetchone():
            return False
        
        # Отмечаем выполнение
        await db.execute(
            'INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)',
            (user_id, task_id)
        )
        
        # Увеличиваем счетчик выполненных
        await db.execute(
            'UPDATE tasks SET completed_count = completed_count + 1 WHERE id = ?',
            (task_id,)
        )
        
        # Начисляем награду пользователю
        await db.execute(
            'UPDATE users SET balance = balance + ?, tasks_completed = tasks_completed + 1 WHERE user_id = ?',
            (task['reward'], user_id)
        )
        
        await db.commit()
        return True

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Создаем пользователя в БД
    await create_user(user_id, username)
    user = await get_user(user_id)
    
    # Красивое приветствие
    welcome_text = f"""
╔══════════════════════════════╗
     🎯 ДОБРО ПОЖАЛОВАТЬ В {BOT_NAME}     
╚══════════════════════════════╝

👤 Твой баланс: *{user['balance']} монет*

📌 *Что нужно сделать:*
1️⃣ Подпишись на каналы ниже
2️⃣ Нажми "Проверить подписку" 
3️⃣ Получи +{SUBSCRIBE_REWARD} монет

💰 *Зарабатывай монеты:*
• Выполняй задания других пользователей
• Создавай свои задания и получай подписчиков

👇 *Выбери действие в меню ниже*
    """
    
    await message.answer(
        welcome_text,
        reply_markup=get_channels_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data == "close_welcome")
async def close_welcome(callback: CallbackQuery):
    """Закрыть приветственное окно"""
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "no_channels")
async def no_channels(callback: CallbackQuery):
    """Обработка нажатия на заглушку каналов"""
    await callback.answer("Каналы для подписки появятся позже!", show_alert=True)

@dp.message(F.text == "🌟 Задания")
async def show_tasks(message: Message):
    """Показать список доступных заданий"""
    tasks = await get_active_tasks()
    
    if not tasks:
        await message.answer(
            "📭 *Пока нет активных заданий*\n\n"
            "Создай свое задание первым! Нажми 📋 Создать задание",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    text = "🔥 *Доступные задания:*\n\n"
    for task in tasks:
        text += f"• Задание #{task['id']}\n"
        text += f"  📺 Канал: {task['channel_name']}\n"
        text += f"  💰 Награда: {task['reward']} монет\n"
        text += f"  👥 Осталось мест: {task['left']}/{task['total_limit']}\n\n"
    
    await message.answer(text, reply_markup=get_tasks_keyboard(tasks), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("view_task_"))
async def view_task(callback: CallbackQuery):
    """Просмотр конкретного задания"""
    task_id = int(callback.data.split("_")[2])
    task = await get_task(task_id)
    
    if not task:
        await callback.answer("Задание не найдено!", show_alert=True)
        return
    
    text = f"""
📌 *Задание #{task['id']}*

📺 *Канал:* {task['channel_name']}
🔗 *Ссылка:* {task['channel_link']}
💰 *Награда:* {task['reward']} монет
👥 *Осталось мест:* {task['left']}/{task['total_limit']}
📅 *Создано:* {task['created_date'][:10]}

👉 Нажми кнопку ниже, чтобы выполнить задание
    """
    
    await callback.message.edit_text(text, reply_markup=get_task_action_keyboard(task_id), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@dp.callback_query(F.data == "back_to_tasks")
async def back_to_tasks(callback: CallbackQuery):
    """Вернуться к списку заданий"""
    tasks = await get_active_tasks()
    
    text = "🔥 *Доступные задания:*\n\n"
    for task in tasks:
        text += f"• Задание #{task['id']}\n"
        text += f"  📺 Канал: {task['channel_name']}\n"
        text += f"  💰 Награда: {task['reward']} монет\n"
        text += f"  👥 Осталось мест: {task['left']}/{task['total_limit']}\n\n"
    
    await callback.message.edit_text(text, reply_markup=get_tasks_keyboard(tasks), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@dp.callback_query(F.data.startswith("do_task_"))
async def do_task(callback: CallbackQuery):
    """Выполнить задание"""
    task_id = int(callback.data.split("_")[2])
    task = await get_task(task_id)
    user_id = callback.from_user.id
    
    if not task:
        await callback.answer("Задание не найдено!", show_alert=True)
        return
    
    if task['left'] <= 0:
        await callback.answer("Места закончились!", show_alert=True)
        return
    
    # Отправляем ссылку на канал
    await callback.message.answer(
        f"📺 *Канал для подписки:*\n{task['channel_link']}\n\n"
        f"После подписки нажми кнопку Я ПОДПИСАЛСЯ",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я ПОДПИСАЛСЯ", callback_data=f"check_task_{task_id}")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("check_task_"))
async def check_task_completion(callback: CallbackQuery):
    """Проверить выполнение задания"""
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    # Здесь должна быть реальная проверка подписки через API
    # Для демо просто начисляем награду
    success = await complete_task(user_id, task_id)
    
    if success:
        await callback.message.answer(
            "✅ *Задание выполнено!*\n"
            f"Монеты зачислены на твой баланс!",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await callback.message.answer(
            "❌ Ты уже выполнял это задание!",
            parse_mode=ParseMode.MARKDOWN
        )
    
    await callback.answer()

@dp.message(F.text == "📋 Создать задание")
async def create_task_start(message: Message, state: FSMContext):
    """Начать создание задания"""
    user = await get_user(message.from_user.id)
    
    await message.answer(
        "📋 *Создание нового задания*\n\n"
        "🔗 Отправь ссылку на канал (например, https://t.me/channel_name):",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(CreateTaskStates.waiting_for_link)

@dp.message(CreateTaskStates.waiting_for_link)
async def process_task_link(message: Message, state: FSMContext):
    """Обработка ссылки на канал"""
    link = message.text.strip()
    
    # Простая валидация ссылки
    if not link.startswith(('https://t.me/', 't.me/')):
        await message.answer("❌ Это не похоже на ссылку на Telegram канал. Попробуй еще раз:")
        return
    
    await state.update_data(channel_link=link)
    
    # Просим ввести название канала
    await message.answer(
        "📝 Введите название канала (как оно будет отображаться в списке заданий):",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(CreateTaskStates.waiting_for_channel_name)

@dp.message(CreateTaskStates.waiting_for_channel_name)
async def process_channel_name(message: Message, state: FSMContext):
    """Обработка названия канала"""
    channel_name = message.text.strip()
    
    if len(channel_name) > 50:
        await message.answer("❌ Слишком длинное название. Максимум 50 символов:")
        return
    
    await state.update_data(channel_name=channel_name)
    
    await message.answer(
        f"💰 Сколько монет получит исполнитель?\n"
        f"(минимум {MIN_REWARD} монет)",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(CreateTaskStates.waiting_for_reward)

@dp.message(CreateTaskStates.waiting_for_reward)
async def process_task_reward(message: Message, state: FSMContext):
    """Обработка награды"""
    try:
        reward = int(message.text)
        if reward < MIN_REWARD:
            await message.answer(f"❌ Минимальная награда - {MIN_REWARD} монет. Введи другое число:")
            return
        if reward > 1000:
            await message.answer("❌ Максимальная награда - 1000 монет. Введи другое число:")
            return
    except ValueError:
        await message.answer("❌ Введи число (например, 10):")
        return
    
    await state.update_data(reward=reward)
    
    await message.answer(
        "👥 Сколько человек могут выполнить это задание? (введи число):",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(CreateTaskStates.waiting_for_limit)

@dp.message(CreateTaskStates.waiting_for_limit)
async def process_task_limit(message: Message, state: FSMContext):
    """Обработка лимита"""
    try:
        limit = int(message.text)
        if limit < 1:
            await message.answer("❌ Минимум 1 человек. Введи другое число:")
            return
        if limit > 1000:
            await message.answer("❌ Максимум 1000 человек. Введи другое число:")
            return
    except ValueError:
        await message.answer("❌ Введи число (например, 10):")
        return
    
    data = await state.get_data()
    total_cost = data['reward'] * limit
    
    # Проверяем баланс
    user = await get_user(message.from_user.id)
    if user['balance'] < total_cost:
        await message.answer(
            f"❌ Недостаточно монет!\n"
            f"Твой баланс: {user['balance']} монет\n"
            f"Нужно: {total_cost} монет",
            parse_mode=ParseMode.MARKDOWN
        )
        await state.clear()
        return
    
    # Сохраняем лимит и показываем подтверждение
    await state.update_data(limit=limit)
    data = await state.get_data()
    
    confirm_text = f"""
📋 *Проверь данные задания:*

📺 Канал: {data['channel_name']}
🔗 Ссылка: {data['channel_link']}
💰 Награда: {data['reward']} монет
👥 Лимит: {limit} человек
💳 Общая стоимость: {total_cost} монет
💰 Твой баланс после: {user['balance'] - total_cost} монет

Всё верно?
    """
    
    await message.answer(confirm_text, reply_markup=get_confirmation_keyboard(), parse_mode=ParseMode.MARKDOWN)
    await state.set_state(CreateTaskStates.waiting_for_confirmation)

@dp.callback_query(CreateTaskStates.waiting_for_confirmation, F.data == "confirm_task")
async def confirm_task_creation(callback: CallbackQuery, state: FSMContext):
    """Подтверждение создания задания"""
    data = await state.get_data()
    
    # Создаем задание
    await create_task(
        creator_id=callback.from_user.id,
        channel_link=data['channel_link'],
        channel_name=data['channel_name'],
        reward=data['reward'],
        limit=data['limit']
    )
    
    await callback.message.edit_text(
        "✅ *Задание успешно создано!*\n\n"
        "Оно появится в списке доступных заданий.",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()
    await callback.answer()

@dp.callback_query(CreateTaskStates.waiting_for_confirmation, F.data == "cancel_task")
async def cancel_task_creation(callback: CallbackQuery, state: FSMContext):
    """Отмена создания задания"""
    await callback.message.edit_text(
        "❌ Создание задания отменено.",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()
    await callback.answer()

@dp.message(F.text == "👤 Профиль")
async def show_profile(message: Message):
    """Показать профиль пользователя"""
    user = await get_user(message.from_user.id)
    
    if not user:
        await message.answer("Профиль не найден. Нажми /start")
        return
    
    profile_text = f"""
👤 *Мой профиль*
┌─────────────────────┐
│ ID: {user['user_id']}
│ 💰 Баланс: *{user['balance']} монет*
│ 📊 Создано заданий: *{user['tasks_created']}*
│ ✅ Выполнено заданий: *{user['tasks_completed']}*
│ 📅 В системе: {user['joined_date'][:10]}
└─────────────────────┘
    """
    
    await message.answer(profile_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "💰 Баланс")
async def show_balance(message: Message):
    """Показать баланс"""
    user = await get_user(message.from_user.id)
    
    await message.answer(
        f"💰 *Твой баланс:* {user['balance']} монет",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(F.text == "❓ Помощь")
async def show_help(message: Message):
    """Показать справку"""
    help_text = f"""
❓ *Помощь по {BOT_NAME}*

🌟 *ЗАДАНИЯ*
• Нажми «Задания» чтобы увидеть доступные задания
• Выбери задание и подпишись на канал
• Получи монеты на баланс

📋 *СОЗДАТЬ ЗАДАНИЕ*
• Нажми «Создать задание»
• Укажи ссылку на канал
• Укажи награду и количество мест
• Монеты спишутся с баланса

💰 *ЗАРАБОТОК*
• {SUBSCRIBE_REWARD} монет за подписку на наши каналы
• Выполняй задания других пользователей
• Монеты можно тратить на свои задания

⚡ *СОВЕТЫ*
• Чем выше награда, тем быстрее найдутся исполнители
• Следи за балансом в профиле
• Создавай задания для продвижения своих каналов
    """
    
    await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)

@dp.message()
async def handle_unknown(message: Message):
    """Обработка неизвестных команд"""
    await message.answer(
        "Я не понимаю эту команду. Используй кнопки меню ниже 👇",
        reply_markup=get_main_keyboard()
    )

# Запуск бота
async def main():
    """Главная функция запуска"""
    # Инициализируем базу данных
    await init_db()
    
    # Запускаем бота
    logger.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
