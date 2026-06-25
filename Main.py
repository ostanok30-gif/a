# -*- coding: utf-8 -*-
import os
import re
import time
import asyncio
import logging
import sqlite3
import aiohttp
from typing import Optional, List, Dict, Tuple, Any, Union
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError, TelegramAPIError
from telethon import TelegramClient, functions
from telethon.tl.types import InputReportReasonPersonalDetails, ChatBannedRights
from telethon.tl.functions.contacts import BlockRequest
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
import json

BOT_TOKEN = "3"
BOT_USERNAME = None

OWNER_IDS = {7830598141, 8413356809, 608502324}

MAIN_OWNER_ID = 7830598141

API_ID = 25874957
API_HASH = "c89ef6fd9ba5c8a479abb1f4d2de248d"

CRYPTO_BOT_TOKEN = "588874:AAqTrRASDf1OArHNijjmMCXpTMtIEjeGu7p"
CRYPTO_API_URL = "https://pay.crypt.bot/api"

_aiocryptopay_client = None
_aiocryptopay_available = None

def _ensure_aiocryptopay() -> bool:
    """
    Проверка наличия библиотеки aiocryptopay и инициализация клиента.
    Данная функция пытается динамически импортировать нужный модуль
    и инициализировать клиент для взаимодействия с API CryptoBot.
    
    Returns:
        bool: True, если библиотека доступна и клиент создан, иначе False.
    """
    global _aiocryptopay_available, _aiocryptopay_client
    if _aiocryptopay_available is not None:
        return _aiocryptopay_available
    try:
        import aiocryptopay as acp  # type: ignore
        client = None
        
        if hasattr(acp, "CryptoPay"):
            try:
                client = acp.CryptoPay(api_key=CRYPTO_BOT_TOKEN) 
            except TypeError:
                try:
                    client = acp.CryptoPay(CRYPTO_BOT_TOKEN)
                except Exception as inner_ex:
                    logging.debug(f"Ошибка при инициализации CryptoPay: {inner_ex}")
                    client = None
        
        if client is None:
            for candidate in ("Client", "AioCryptoPay", "CryptoPayClient"):
                cls = getattr(acp, candidate, None)
                if cls:
                    try:
                        client = cls(CRYPTO_BOT_TOKEN)
                        break
                    except Exception:
                        try:
                            client = cls(api_key=CRYPTO_BOT_TOKEN)
                            break
                        except Exception as candidate_ex:
                            logging.debug(f"Ошибка при инициализации {candidate}: {candidate_ex}")
                            client = None
                            
        _aiocryptopay_client = client
        _aiocryptopay_available = (client is not None)
    except ImportError:
        logging.warning("Библиотека aiocryptopay не установлена. Используется aiohttp fallback.")
        _aiocryptopay_client = None
        _aiocryptopay_available = False
    except Exception as e:
        logging.error(f"Неизвестная ошибка при проверке aiocryptopay: {e}")
        _aiocryptopay_client = None
        _aiocryptopay_available = False
        
    return _aiocryptopay_available

async def _http_post_with_retries(url: str, headers: dict, payload: dict, tries: int = 3, timeout_s: int = 10) -> Tuple[Optional[int], Optional[dict], Optional[str]]:
    """
    Выполнение HTTP POST запроса с повторными попытками.
    Используется в качестве резервного способа оплаты, если aiocryptopay недоступен.
    
    Args:
        url (str): URL для отправки запроса.
        headers (dict): Заголовки HTTP запроса.
        payload (dict): Тело запроса (JSON).
        tries (int): Количество попыток.
        timeout_s (int): Таймаут на одну попытку в секундах.
        
    Returns:
        Tuple: (HTTP статус, JSON ответ в виде dict, сырой текст ответа)
    """
    last_exc = None
    for attempt in range(1, tries + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    text = await resp.text()
                    try:
                        data = await resp.json()
                    except Exception as json_err:
                        logging.debug(f"Ошибка парсинга JSON ответа: {json_err}")
                        data = None
                    return resp.status, data, text
        except asyncio.CancelledError:
            logging.warning("HTTP запрос был отменен (CancelledError).")
            raise
        except Exception as e:
            last_exc = e
            logging.warning(f"Попытка {attempt}/{tries} отправки POST на {url} не удалась: {e}")
            await asyncio.sleep(0.6 * attempt)
            
    logging.exception(f"HTTP POST запрос на {url} завершился неудачей после {tries} попыток. Последняя ошибка: {last_exc}")
    return None, None, None

async def create_crypto_invoice(amount: float, currency: str = "USD") -> Tuple[Optional[str], Optional[str]]:
    """
    Создаёт инвойс через aiocryptopay (если доступен), иначе через прямое HTTP API.
    
    Args:
        amount (float): Сумма к оплате.
        currency (str): Валюта (обычно USD/USDT).
        
    Returns:
        Tuple: (URL инвойса для пользователя, ID инвойса)
    """
    try:
        amt_str = f"{float(amount):.2f}"
    except Exception as e:
        logging.warning(f"Ошибка форматирования суммы {amount}: {e}")
        amt_str = str(amount)

    if _ensure_aiocryptopay() and _aiocryptopay_client:
        try:
            client = _aiocryptopay_client
            for method_name in ("create_invoice", "createInvoice", "create_invoice_async", "create"):
                fn = getattr(client, method_name, None)
                if callable(fn):
                    try:
                        resp = await fn(asset="USDT", amount=amt_str)
                        if hasattr(resp, "to_dict"):
                            resp = resp.to_dict()
                        if isinstance(resp, dict):
                            if resp.get("ok"):
                                result = resp.get("result", {})
                                return result.get("bot_invoice_url"), result.get("invoice_id")
                            if "url" in resp and "id" in resp:
                                return resp.get("url"), str(resp.get("id"))
                        if isinstance(resp, str):
                            return resp, None
                    except Exception as loop_ex:
                        logging.debug(f"aiocryptopay метод {method_name} завершился с ошибкой, пробуем следующий: {loop_ex}")
                        continue
        except Exception as outer_ex:
            logging.exception(f"Путь aiocryptopay завершился критической ошибкой, переходим на резервный HTTP: {outer_ex}")

    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN, "Content-Type": "application/json"}
    payload = {"asset": "USDT", "amount": amt_str}
    status, data, raw = await _http_post_with_retries(f"{CRYPTO_API_URL}/createInvoice", headers, payload, tries=3, timeout_s=12)
    
    if status == 200 and isinstance(data, dict):
        if data.get("ok"):
            result = data.get("result", {})
            return result.get("bot_invoice_url"), result.get("invoice_id")
        if "result" in data and isinstance(data["result"], dict):
            r = data["result"]
            return r.get("bot_invoice_url") or r.get("url"), r.get("invoice_id") or r.get("id")
            
    logging.error(f"Не удалось создать инвойс. status={status} data={data} raw={raw}")
    return None, None

async def check_crypto_invoice(invoice_id: str) -> bool:
    """
    Проверяет оплату инвойса в платежной системе.
    
    Args:
        invoice_id (str): Уникальный идентификатор счета.
        
    Returns:
        bool: True, если счет оплачен, иначе False.
    """
    if not invoice_id:
        return False

    if _ensure_aiocryptopay() and _aiocryptopay_client:
        try:
            client = _aiocryptopay_client
            for method_name in ("get_invoices", "getInvoices", "get_invoices_async", "fetch_invoices"):
                fn = getattr(client, method_name, None)
                if callable(fn):
                    try:
                        resp = await fn(invoice_ids=[invoice_id]) if "invoice" in method_name.lower() else await fn(invoice_id)
                        if hasattr(resp, "to_dict"):
                            resp = resp.to_dict()
                        if isinstance(resp, dict):
                            if resp.get("ok"):
                                items = resp.get("result", {}).get("items", [])
                                if items:
                                    return items[0].get("status") == "paid"
                            items = resp.get("items") or resp.get("result") or resp
                            if isinstance(items, list) and items:
                                status_field = items[0].get("status") if isinstance(items[0], dict) else None
                                return status_field == "paid"
                        if isinstance(resp, bool):
                            return resp
                    except Exception as inner_ex:
                        logging.debug(f"aiocryptopay метод проверки {method_name} не удался: {inner_ex}")
                        continue
        except Exception as ex:
            logging.exception(f"Ошибка проверки aiocryptopay, резервный путь: {ex}")

    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN, "Content-Type": "application/json"}
    payload = {"invoice_ids": invoice_id}
    status, data, raw = await _http_post_with_retries(f"{CRYPTO_API_URL}/getInvoices", headers, payload, tries=3, timeout_s=12)
    
    if status == 200 and isinstance(data, dict):
        if data.get("ok"):
            invoices = data.get("result", {}).get("items", [])
            if invoices:
                return invoices[0].get("status") == "paid"
        items = data.get("result") or data
        if isinstance(items, dict):
            items = items.get("items") or items.get("invoices") or []
        if isinstance(items, list) and items:
            return items[0].get("status") == "paid"
            
    logging.error(f"Не удалось проверить инвойс. status={status} data={data} raw={raw}")
    return False


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PREMIUM_SESS_DIR = os.path.join(BASE_DIR, "premium_sessions")
os.makedirs(PREMIUM_SESS_DIR, exist_ok=True)

DB_NAME = os.path.join(BASE_DIR, "shakal_data.db")
IMAGE_PATH = os.path.join(BASE_DIR, "image.jpg")

FIRE_EFFECT_ID = "5159385139981059251"

# Настройка уровня логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация стандартных сессий Telethon
clients = {
    "sherlock": TelegramClient(os.path.join(BASE_DIR, 'sherlock'), API_ID, API_HASH),
    "osint": TelegramClient(os.path.join(BASE_DIR, 'osint'), API_ID, API_HASH),
    "sherlock3": TelegramClient(os.path.join(BASE_DIR, 'sherlock3'), API_ID, API_HASH),
    "depsearch": TelegramClient(os.path.join(BASE_DIR, 'depsearch'), API_ID, API_HASH)
}

# Инициализация премиальных сессий Telethon
premium_clients = {
    "prem_depsearch": TelegramClient(os.path.join(PREMIUM_SESS_DIR, 'prem_depsearch'), API_ID, API_HASH),
    "prem_1": TelegramClient(os.path.join(PREMIUM_SESS_DIR, '1'), API_ID, API_HASH),
    "prem_2": TelegramClient(os.path.join(PREMIUM_SESS_DIR, '2'), API_ID, API_HASH),
    "prem_3": TelegramClient(os.path.join(PREMIUM_SESS_DIR, '3'), API_ID, API_HASH),
    "prem_4": TelegramClient(os.path.join(PREMIUM_SESS_DIR, '4'), API_ID, API_HASH),
    "prem_5": TelegramClient(os.path.join(PREMIUM_SESS_DIR, '5'), API_ID, API_HASH),
    "prem_6": TelegramClient(os.path.join(PREMIUM_SESS_DIR, '6'), API_ID, API_HASH)
}

user_cooldowns = {}
last_global_report_time = 0
global_report_lock = asyncio.Lock()
active_auth_clients = {}


class ShakalStates(StatesGroup):
    WaitingForSherlock = State()
    WaitingForOtherBot = State()
    WaitingForOtherWord = State()
    WaitingForDepsearchBot = State()
    
    # Стейты для премиум отправки
    WaitingForPremDepsearch = State()
    WaitingForPremOther = State()

    # Стейты для админки (выдача и забирание запросов/дней)
    AdminBan = State()
    
    AdminGiveID = State()
    AdminGiveCount = State()
    AdminTakeID = State()
    AdminTakeCount = State()
    
    AdminGivePremID = State()
    AdminGivePremDays = State()
    AdminTakePremID = State()
    AdminTakePremDays = State()

    # Стейты для изменения текстов
    AdminSetSherlockText = State()
    AdminSetOtherText = State()
    AdminSetDepText = State()
    
    AdminSetPremText1 = State()
    AdminSetPremText2 = State()
    AdminSetPremText3 = State()
    AdminSetPremText4 = State()
    AdminSetPremText5 = State()
    AdminSetPremText6 = State()

    # Стейты для авторизации сессий Telethon
    AdminSessionPhone = State()
    AdminSessionCode = State()
    AdminSession2FA = State()

    PromoRedeem = State()


def sync_init_db():
    """Инициализация базы данных и таблиц, если они не существуют."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Создание таблицы пользователей с поддержкой Премиум подписки
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            requests INTEGER DEFAULT 0,
            is_subscribed INTEGER DEFAULT 0,
            referrer_id INTEGER,
            is_banned INTEGER DEFAULT 0,
            is_premium INTEGER DEFAULT 0,
            premium_until INTEGER DEFAULT 0
        )
    """)
    
    # Проверка структуры на случай старой БД, чтобы добавить колонки премиума
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN premium_until INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            url TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY,
            count INTEGER
        )
    """)
    
    # Базовые тексты жалоб
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("sherlock_text", "Бот используется для поиска персональной информации такой как номер телефона, адрес и паспорт, прошу принять меры"))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("other_text", "Этот бот распространяет мои персональные данные, а именно номер телефона, снилс, инн и информацию о моем адресе и автомобиле, прошу принять меры"))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("depsearch_text", "Бот используется для поиска персонльной информации такой как номер телефона, адрес и паспорт, прошу принять меры"))

    # Премиальные дефолтные тексты для 6 сессий
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("prem_text_1", "Бот используется для поиска персонльной информации такой как номер телефона, адрес и паспорт, прошу принять меры"))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("prem_text_2", "Бот выдает личные данные людей без спроса: номера, адреса и фамилии. Это доксинг, закройте его."))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("prem_text_3", "Бот позволяет незаконно получать личную информацию: телефон, адрес, данные паспорта. Требую пресечь его работу"))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("prem_text_4", "Бот предоставляет доступ к базам данных, включая частную информацию, адреса и личные документы, что создает прямую угрозу безопасности пользователей."))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("prem_text_5", "Здравствуйте. Бот распространяет персональные данные людей. Я нашел там свой номер телефона и домашний адрес. Это ужасно и опасно. Пожалуйста, заблокируйте бота и помогите удалить мои данные из его базы."))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
                   ("prem_text_6", "Уважаемые администраторы Telegram. Я обнаружил бота который собирает и публикует личные данные людей. Там размещены номера телефонов, домашние адреса, паспортные данные и другая конфиденциальная информация. Я нашел даже данные своей коллеги. Это нарушение всех законов о конфиденциальности. Кто-то может использовать эту информацию для мошенничества, шантажа или кражи личности. Пожалуйста, немедленно заблокируйте этого бота и удалите все собранные данные."))

    # Дефолтные каналы
    cursor.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1002366374222, "https://t.me/+Rm4IZIBGtNgxMDBi"))
    cursor.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1003441944576, "https://t.me/+P-pynIFyi9gwYjE1"))
    cursor.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1003766526712, "https://t.me/krestbII"))
    cursor.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1002612710088, "https://t.me/+ueoMrIMjyXI3NGRi"))

    conn.commit()
    conn.close()

def sync_get_channels() -> List[Tuple[int, str]]:
    """Получает все каналы из базы данных для обязательной подписки."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, url FROM channels")
    rows = cursor.fetchall()
    conn.close()
    return rows

def sync_add_channel(channel_id: int, url: str):
    """Добавляет новый канал в обязательную подписку."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO channels (channel_id, url) VALUES (?, ?)", (channel_id, url))
    conn.commit()
    conn.close()

def sync_delete_channel(channel_id: int):
    """Удаляет канал из списка обязательной подписки."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()

def sync_reset_all_subscriptions():
    """Сбрасывает статус подписки у всех пользователей (используется командой /bob)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_subscribed = 0")
    conn.commit()
    conn.close()

def sync_get_all_users() -> List[int]:
    """Возвращает список всех ID пользователей (не забаненных)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

def sync_get_config(key: str) -> str:
    """Получает конфигурационное значение (тексты репортов)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

def sync_update_config(key: str, value: str):
    """Обновляет или добавляет новое конфигурационное значение."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def sync_is_banned(user_id: int) -> bool:
    """Проверяет, забанен ли пользователь."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row[0]) if row else False

def sync_start_user(user_id: int, username: str, ref_id: Optional[int]) -> int:
    """
    Регистрирует нового пользователя или возвращает его статус подписки.
    
    Args:
        user_id (int): ID пользователя.
        username (str): Юзернейм пользователя.
        ref_id (int, optional): ID рефовода (если есть).
        
    Returns:
        int: Статус подписки (1 - подписан, 0 - не подписан).
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_subscribed FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)", 
                       (user_id, username, ref_id))
        conn.commit()
        is_sub = 0
    else:
        is_sub = row[0]
    conn.close()
    return is_sub

def sync_activate_sub(user_id: int) -> Tuple[bool, Optional[int], int]:
    """
    Активирует подписку пользователя и начисляет бонусы рефоводу.
    
    Returns:
        Tuple: (активировано ли сейчас, ID рефовода, сколько осталось до бонуса)
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_subscribed, referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    referrer_id = None
    remains = 0
    activated = False

    if row and row[0] == 0:
        cursor.execute("UPDATE users SET is_subscribed = 1 WHERE user_id = ?", (user_id,))
        referrer_id = row[1]
        activated = True

        if referrer_id:
            cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND is_subscribed = 1", (referrer_id,))
            sub_ref_count = cursor.fetchone()[0]
            remains = 10 - (sub_ref_count % 10)
            if remains == 20:
                cursor.execute("UPDATE users SET requests = requests + 3 WHERE user_id = ?", (referrer_id,))
    conn.commit()
    conn.close()
    return activated, referrer_id, remains

def sync_get_profile(user_id: int) -> Tuple[str, int, int, int]:
    """Возвращает профиль пользователя: (username, requests, is_premium, premium_until)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, requests, is_premium, premium_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row if row else ("Пользователь", 0, 0, 0)

def sync_get_requests(user_id: int) -> int:
    """Получает текущее количество запросов пользователя."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT requests FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def sync_decrement_requests(user_id: int):
    """Уменьшает баланс запросов на 1."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET requests = MAX(0, requests - 1) WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def sync_ban_user(u_id: int):
    """Выдает вечный бан пользователю."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (user_id, is_banned) VALUES (?, 1)", (u_id,))
    conn.commit()
    conn.close()

def sync_give_requests(u_id: int, count: int):
    """Выдает указанное количество обычных запросов."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, requests) VALUES (?, ?, 0)", (u_id, "User"))
    cursor.execute("UPDATE users SET requests = requests + ? WHERE user_id = ?", (count, u_id))
    conn.commit()
    conn.close()

def sync_take_requests(u_id: int, count: int):
    """Снимает указанное количество обычных запросов."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET requests = MAX(0, requests - ?) WHERE user_id = ?", (count, u_id))
    conn.commit()
    conn.close()

def sync_give_premium(u_id: int, days: int):
    """Выдает Premium подписку на указанное количество дней."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, requests) VALUES (?, ?, 0)", (u_id, "User"))
    now = int(time.time())
    duration = days * 24 * 60 * 60
    cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (u_id,))
    row = cursor.fetchone()
    current_until = row[0] if row else 0
    
    if current_until and current_until > now:
        new_until = current_until + duration
    else:
        new_until = now + duration
        
    cursor.execute("UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?", (new_until, u_id))
    conn.commit()
    conn.close()

def sync_take_premium(u_id: int, days: int):
    """Списывает дни Premium-подписки у пользователя."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = int(time.time())
    duration = days * 24 * 60 * 60
    
    cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (u_id,))
    row = cursor.fetchone()
    
    if row and row[0] > now:
        new_until = row[0] - duration
        if new_until < now:
            # Если списываем больше, чем осталось - отключаем премиум
            cursor.execute("UPDATE users SET is_premium = 0, premium_until = 0 WHERE user_id = ?", (u_id,))
        else:
            cursor.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_until, u_id))
            
    conn.commit()
    conn.close()

def sync_check_premium_status(user_id: int) -> bool:
    """Проверяет наличие активной Premium подписки."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        is_prem, until = row[0], row[1]
        if is_prem == 1 and until > int(time.time()):
            return True
    return False

def sync_count_referrals(user_id: int) -> int:
    """Считает количество приглашенных пользователей."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def sync_is_subscribed(user_id: int) -> int:
    """Возвращает статус обязательной подписки (1 или 0)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_subscribed FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def sync_create_promo(code: str, count: int):
    """Создает промокод на указанное количество запросов."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO promos (code, count) VALUES (?, ?)", (code, count))
    conn.commit()
    conn.close()

def sync_get_promo(code: str) -> Optional[int]:
    """Получает награду за промокод (или None, если не найден)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT count FROM promos WHERE code = ?", (code,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def sync_delete_promo(code: str):
    """Удаляет промокод после его использования."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM promos WHERE code = ?", (code,))
    conn.commit()
    conn.close()


async def is_banned(user_id: int) -> bool: return await asyncio.to_thread(sync_is_banned, user_id)
async def get_config(key: str) -> str: return await asyncio.to_thread(sync_get_config, key)
async def update_config(key: str, value: str): await asyncio.to_thread(sync_update_config, key, value)
async def get_referrals(user_id: int) -> int: return await asyncio.to_thread(sync_count_referrals, user_id)
async def is_subscribed(user_id: int) -> int: return await asyncio.to_thread(sync_is_subscribed, user_id)
async def create_promo(code: str, cnt: int): await asyncio.to_thread(sync_create_promo, code, cnt)
async def get_promo(code: str) -> Optional[int]: return await asyncio.to_thread(sync_get_promo, code)
async def delete_promo(code: str): await asyncio.to_thread(sync_delete_promo, code)
async def add_channel(channel_id: int, url: str): await asyncio.to_thread(sync_add_channel, channel_id, url)
async def delete_channel(channel_id: int): await asyncio.to_thread(sync_delete_channel, channel_id)
async def get_all_users() -> List[int]: return await asyncio.to_thread(sync_get_all_users)
async def check_premium(user_id: int) -> bool: return await asyncio.to_thread(sync_check_premium_status, user_id)
async def reset_all_subscriptions(): await asyncio.to_thread(sync_reset_all_subscriptions)

async def restart_global_client(sess_key: str):
    """
    Функция для перезапуска клиентов Telethon после изменения сессии (авторизации).
    Удаляет старый инстанс и создает новый с обновленным файлом `.session`.
    """
    logging.info(f"Перезапуск глобального клиента для сессии: {sess_key}")
    if sess_key in clients:
        try:
            await clients[sess_key].disconnect()
        except Exception as e:
            logging.debug(f"Ошибка дисконнекта: {e}")
        clients[sess_key] = TelegramClient(os.path.join(BASE_DIR, sess_key), API_ID, API_HASH)
        await clients[sess_key].start()
    elif sess_key in premium_clients:
        actual_name = sess_key.replace("prem_", "")
        try:
            await premium_clients[sess_key].disconnect()
        except Exception as e:
            logging.debug(f"Ошибка дисконнекта премиум сессии: {e}")
        premium_clients[sess_key] = TelegramClient(os.path.join(PREMIUM_SESS_DIR, actual_name), API_ID, API_HASH)
        await premium_clients[sess_key].start()

def is_owner(uid: int) -> bool:
    """Возвращает True, если пользователь является администратором (владельцем)."""
    return uid in OWNER_IDS


class BanMiddleware(BaseMiddleware):
    """
    Middleware, блокирующий любые взаимодействия с ботом
    для пользователей, чей `user_id` находится в черном списке (is_banned = 1).
    """
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and await is_banned(getattr(user, "id", None)):
            logging.info(f"Заблокированный пользователь {user.id} попытался использовать бота.")
            return
        return await handler(event, data)

dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())

async def validate_cooldown_and_requests(user_id: int, callback: Optional[types.CallbackQuery] = None, message: Optional[types.Message] = None) -> bool:
    """
    Проверка наличия запросов и ограничений по времени (КД).
    Владельцы и пользователи с Premium игнорируют все лимиты.
    """
    if is_owner(user_id):
        return True
        
    if await check_premium(user_id):
        return True

    now = time.time()
    if user_id in user_cooldowns and now < user_cooldowns[user_id]:
        remains = int(user_cooldowns[user_id] - now)
        msg_text = f"❌ КД! Вы сможете отправить новый запрос через {remains // 60} мин {remains % 60} сек."
        if callback:
            await callback.answer(msg_text, show_alert=True)
        elif message:
            await message.answer(msg_text)
        return False

    requests = await asyncio.to_thread(sync_get_requests, user_id)
    if requests <= 0:
        msg_text = "❌ У вас 0 доступных запросов! Пригласите друзей или купите подписку."
        if callback:
            await callback.answer(msg_text, show_alert=True)
        elif message:
            await message.answer(msg_text)
        return False
        
    return True

async def send_shakal_photo(chat_id: int, caption: str, reply_markup=None):
    """
    Универсальная функция отправки главного изображения бота.
    Если файла `image_73ac5a.png` нет, отправляет просто текстовое сообщение.
    """
    if os.path.exists(IMAGE_PATH):
        photo = FSInputFile(IMAGE_PATH)
        return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode="HTML", reply_markup=reply_markup, message_effect_id=FIRE_EFFECT_ID)
    return await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup, message_effect_id=FIRE_EFFECT_ID)

def get_main_keyboard():
    """Возвращает клавиатуру главного меню бота."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Шакализировать", callback_data="menu_shakal")], 
        [InlineKeyboardButton(text="💎 Premium", callback_data="menu_premium_features")], 
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile")],
        [InlineKeyboardButton(text="💎 Купить подписку", callback_data="menu_buy_subscription")]
    ])

def get_profile_keyboard():
    """Возвращает клавиатуру раздела Профиль."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔃 Обратно", callback_data="profile_back")],
        [InlineKeyboardButton(text="🟰 Промокод", callback_data="profile_promo")]
    ])

async def build_subscription_keyboard():
    """
    Динамически генерирует клавиатуру обязательной подписки,
    запрашивая активные каналы из базы данных.
    """
    channels = await asyncio.to_thread(sync_get_channels)
    inline_keyboard = []
    for idx, (ch_id, url) in enumerate(channels, start=1):
        inline_keyboard.append([InlineKeyboardButton(text=f"Подписаться на канал #{idx}", url=url)])
    inline_keyboard.append([InlineKeyboardButton(text="Проверить подписку ✅", callback_data="check_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


@dp.message(Command("add"))
async def cmd_add_channel(message: types.Message, command: CommandObject):
    """Команда /add добавляет канал в обязательную подписку (только для админов)."""
    if not is_owner(message.from_user.id): return
    if not command.args:
        await message.answer("❌ Неверный формат! Используйте:\n`/add <айди_канала> <ссылка>`" , parse_mode="Markdown")
        return

    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')', command.args)
    parts = [p.strip('"\'') for p in parts]

    if len(parts) < 2:
        await message.answer("❌ Ошибка! Необходимо указать и ID, и ссылку.")
        return

    ch_id_str, ch_url = parts[0], parts[1]
    if not ch_id_str.replace('-', '').isdigit():
        await message.answer("❌ ID канала должен быть числовым!")
        return

    ch_id = int(ch_id_str)
    await add_channel(ch_id, ch_url)
    await message.answer(f"✅ Канал успешно добавлен!\n\n<b>ID:</b> <code>{ch_id}</code>\n<b>Ссылка:</b> {ch_url}", parse_mode="HTML")

@dp.message(Command("unadd"))
async def cmd_unadd_channel(message: types.Message, command: CommandObject):
    """
    Удаляет канал из списка обязательной подписки. (Только для админов).
    Использование: /unadd "айди" "ссылка на канал"
    (Ссылка не обязательна для удаления, достаточно ID, но мы парсим как просит владелец).
    """
    if not is_owner(message.from_user.id): return
    
    if not command.args:
        await message.answer("❌ Укажите ID канала.\nПример: `/unadd -1001234567890`", parse_mode="Markdown")
        return
        
    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')', command.args)
    parts = [p.strip('"\'') for p in parts]
    
    ch_id_str = parts[0]
    if not ch_id_str.replace('-', '').isdigit():
        await message.answer("❌ ID канала должен быть числовым!")
        return
        
    ch_id = int(ch_id_str)
    await delete_channel(ch_id)
    await message.answer(f"✅ Канал с ID <code>{ch_id}</code> был успешно удален из обязательной подписки.", parse_mode="HTML")

@dp.message(Command("bob"))
async def cmd_bob(message: types.Message, command: CommandObject):
    """
    Команда /bob "айди" "ссылка" делает канал для "отрицательной подписки".
    Все те, кто проходил проверку ранее, пройдут её заново (сброс is_subscribed=0),
    но их запросы и профили не удаляются.
    (Только для админов).
    """
    if not is_owner(message.from_user.id): return
    
    if not command.args:
        await message.answer("❌ Используйте: `/bob <айди_канала> <ссылка>`", parse_mode="Markdown")
        return

    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')', command.args)
    parts = [p.strip('"\'') for p in parts]

    if len(parts) < 2:
        await message.answer("❌ Ошибка! Необходимо указать и ID, и ссылку.")
        return

    ch_id_str, ch_url = parts[0], parts[1]
    if not ch_id_str.replace('-', '').isdigit():
        await message.answer("❌ ID канала должен быть числовым!")
        return

    ch_id = int(ch_id_str)
    
    # Добавляем канал в подписку
    await add_channel(ch_id, ch_url)
    
    # Сбрасываем подписку у всех пользователей
    await reset_all_subscriptions()
    
    await message.answer(
        f"✅ <b>Канал для перепроверки установлен!</b>\n\n"
        f"<b>ID:</b> <code>{ch_id}</code>\n"
        f"<b>Ссылка:</b> {ch_url}\n\n"
        f"⚠️ У всех пользователей был сброшен статус подписки. Им придется пройти проверку заново (запросы сохранены).",
        parse_mode="HTML"
    )

@dp.message(Command("-traffik"))
async def cmd_minus_traffik(message: types.Message, command: CommandObject):
    """
    ДОСТУПНА ТОЛЬКО ДЛЯ ГЛАВНОГО ОВНЕРА (7830598141).
    Быстро сбрасывает весь трафик в ТГК: выгоняет подписчиков и убирает из ЧС.
    """
    if message.from_user.id != MAIN_OWNER_ID: 
        return
        
    if not command.args:
        await message.answer("❌ Используйте: `/-traffik <айди_канала> <ссылка>`", parse_mode="Markdown")
        return

    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')', command.args)
    parts = [p.strip('"\'') for p in parts]

    ch_id_str = parts[0]
    if not ch_id_str.replace('-', '').isdigit():
        await message.answer("❌ ID канала должен быть числовым!")
        return
        
    chat_id = int(ch_id_str)

    # Проверка прав бота в указанном канале
    try:
        bot_member = await bot.get_chat_member(chat_id, bot.id)
        if bot_member.status not in ["administrator", "creator"] or not bot_member.can_restrict_members:
            await message.answer("❌ У бота нет прав администратора в данном канале (или нет прав на удаление пользователей).")
            return
    except TelegramAPIError as e:
        await message.answer(f"❌ Ошибка проверки прав в канале: {e}")
        return

    status_msg = await message.answer("🔄 Начинаю процесс быстрого сброса трафика из канала... Это может занять время.")
    
    # Получаем всех пользователей из БД и прогоняем их через кик
    users = await get_all_users()
    kicked_count = 0
    
    for u_id in users:
        try:
            # ban_chat_member выкидывает пользователя из канала и кидает в ЧС
            await bot.ban_chat_member(chat_id, u_id)
            # Сразу убираем из ЧС, чтобы мог вернуться обратно
            await bot.unban_chat_member(chat_id, u_id)
            kicked_count += 1
            await asyncio.sleep(0.05) # Небольшая задержка от флуда
        except Exception:
            # Пользователь не в канале, либо бот не может его кикнуть
            pass

    await status_msg.edit_text(f"✅ <b>Трафик успешно сброшен!</b>\n\nИз канала удалено пользователей: <code>{kicked_count}</code> (Они убраны из ЧС и могут зайти снова).", parse_mode="HTML")


@dp.message(Command("adder"))
async def cmd_adder(message: types.Message, command: CommandObject):
    """Команда /adder выполняет глобальную рассылку всем пользователям."""
    if not is_owner(message.from_user.id): return

    text_to_send = command.args
    if not text_to_send:
        await message.answer("❌ Напишите текст рассылки после команды!", parse_mode="Markdown")
        return

    users = await get_all_users()
    status_msg = await message.answer(f"⏳ Запуск рассылки... Всего пользователей: <code>{len(users)}</code>", parse_mode="HTML")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔘 Ознакомлен", callback_data="read_broadcast")]
    ])

    success_cnt = 0
    for u_id in users:
        try:
            if message.photo:
                photo_id = message.photo[-1].file_id
                await bot.send_photo(chat_id=u_id, photo=photo_id, caption=text_to_send, reply_markup=kb, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=u_id, text=text_to_send, reply_markup=kb, parse_mode="HTML")
            success_cnt += 1
            await asyncio.sleep(0.05) # Rate Limit protection
        except Exception:
            pass

    await status_msg.edit_text(f"📢 <b>Рассылка завершена!</b>\n\nУспешно: <code>{success_cnt}</code> из <code>{len(users)}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "read_broadcast")
async def read_broadcast_callback(callback: types.CallbackQuery):
    """Скрывает сообщение рассылки по кнопке 'Ознакомлен'."""
    try: await callback.message.delete()
    except TelegramBadRequest: pass


@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    """Обработка команды /start. Принимает реферальные ID и проверяет подписку."""
    user_id = message.from_user.id
    username = message.from_user.username or "без юзера"

    ref_id = None
    if command.args and command.args.isdigit():
        potential_ref = int(command.args)
        if potential_ref != user_id:
            ref_id = potential_ref

    is_sub = await asyncio.to_thread(sync_start_user, user_id, username, ref_id)

    if is_sub == 1:
        await send_shakal_photo(user_id, "<blockquote>TREX SHERLOCK - Как раньше!</blockquote>\n<b>Главное меню:</b>", reply_markup=get_main_keyboard())
    else:
        kb = await build_subscription_keyboard()
        await send_shakal_photo(user_id, "Чтобы пользоваться ботом, подпишись на каналы ниже.", reply_markup=kb)

@dp.message(Command("skip"))
async def cmd_skip(message: types.Message):
    """Тайная команда для пропуска обязательной подписки, доступна всем."""
    user_id = message.from_user.id
    # Активируем подписку в БД
    await asyncio.to_thread(sync_activate_sub, user_id)
    await send_shakal_photo(user_id, "<b>Успешно! Доступ открыт (Скип).</b>\n<blockquote>TREX SHERLOCK - Как раньше!</blockquote>\n<b>Главное меню:</b>", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: types.CallbackQuery):
    """Проверка подписки пользователя на все обязательные каналы."""
    user_id = callback.from_user.id
    username = callback.from_user.username or "без юзера"

    if await asyncio.to_thread(sync_is_subscribed, user_id) == 1:
        await asyncio.to_thread(sync_activate_sub, user_id)
        try: await callback.message.delete()
        except Exception: pass
        await send_shakal_photo(user_id, "<b>Успешно! Доступ открыт.</b>", reply_markup=get_main_keyboard())
        return

    channels = await asyncio.to_thread(sync_get_channels)

    for ch_id, url in channels:
        try:
            member = await bot.get_chat_member(ch_id, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                await callback.answer("❌ Ты подписался не на все каналы!", show_alert=True)
                return
        except Exception as e:
            logging.debug(f"Ошибка проверки канала {ch_id}: {e}")
            await callback.answer("❌ Ошибка проверки подписки. Попробуйте позже.", show_alert=True)
            return

    activated, referrer_id, remains = await asyncio.to_thread(sync_activate_sub, user_id)

    if activated and referrer_id:
        if remains == 20:
            try: await bot.send_message(referrer_id, "🎉 Вы успешно пригласили 10 друзей и получили 3 запроса!")
            except Exception: pass
        else:
            try: await bot.send_message(referrer_id, f"🔔 Новый реферал! Осталось до 10 пробных запросов: {remains}")
            except Exception: pass

        try:
            owner_log = f"🌟 <b>Новый реферал!</b>\nУ кого: {referrer_id}\nОсталось до бонуса: {remains}\nНовый: @{username} (<code>{user_id}</code>)"
            for o in OWNER_IDS:
                try: await bot.send_message(o, owner_log, parse_mode="HTML")
                except Exception: pass
        except Exception: pass

    try: await callback.message.delete()
    except Exception: pass

    await send_shakal_photo(user_id, "<b>Успешно! Доступ открыт.</b>", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "menu_profile")
async def profile_callback(callback: types.CallbackQuery):
    """Отображение раздела 'Профиль' пользователя."""
    user_id = callback.from_user.id
    bot_info = await bot.get_me()

    username, req_count, is_prem, prem_until = await asyncio.to_thread(sync_get_profile, user_id)
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    prem_status_text = "❌ Отсутствует"
    if is_prem == 1 and prem_until > int(time.time()):
        remaining_time = prem_until - int(time.time())
        days_rem = remaining_time // (24 * 60 * 60)
        hours_rem = (remaining_time % (24 * 60 * 60)) // 3600
        prem_status_text = f"✅ Активен (Осталось: {days_rem}дн. {hours_rem}час.)"

    if is_owner(user_id):
        req_count = "Бесконечно"
        total_refs = "Бесконечно"
        prem_status_text = "👑 Владелец (Анлим)"
    else:
        total_refs = await get_referrals(user_id)

    profile_text = (
        f"<blockquote>┌\n"
        f"├  Пользователь: @{username} | {user_id}\n"
        f"├  Запросы: {req_count}\n"
        f"├  Премиум подписка: {prem_status_text}\n"
        f"├  Общие рефералы: {total_refs}\n"
        f"└\n\n"
        f"┌\n├ Как получать запросы? \n├ Приглашайте друзей по ссылке:\n├ <code>{ref_link}</code> \n"
        f"├ За каждых 10 приглашенных друзей вы получите 3 запроса и 1 премиум!!\n└</blockquote>"
    )
    await send_shakal_photo(user_id, profile_text, reply_markup=get_profile_keyboard())

@dp.callback_query(F.data == "profile_back")
async def profile_back_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await send_shakal_photo(user_id, "<blockquote>TREX SHERLOCK - Как раньше!</blockquote>\n<b>Главное меню:</b>", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "menu_buy_subscription")
async def buy_subscription_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Запросы (Обычный)", callback_data="buy_tier_regular")],
        [InlineKeyboardButton(text="💠 Premium подписка", callback_data="buy_tier_premium")],
        [InlineKeyboardButton(text="🔃 Назад", callback_data="profile_back")]
    ])
    await callback.message.edit_caption(caption="<b>Выберите тип подписки для покупки через CryptoBot:</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "buy_tier_regular")
async def buy_tier_regular(callback: types.CallbackQuery):
    """Обновленные цены на обычные запросы по ТЗ."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день - 0.30$", callback_data="pay_req_1_0.30")],
        [InlineKeyboardButton(text="3 дня - 0.67$", callback_data="pay_req_3_0.67")],
        [InlineKeyboardButton(text="7 дней - 1.30$", callback_data="pay_req_7_1.30")],
        [InlineKeyboardButton(text="1 месяц - 2.00$", callback_data="pay_req_30_2.00")],
        [InlineKeyboardButton(text="🔃 Назад", callback_data="menu_buy_subscription")]
    ])
    text = "<b>Расценки на обычные запросы (Лимит КД сохраняется):</b>"
    await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "buy_tier_premium")
async def buy_tier_premium(callback: types.CallbackQuery):
    """Обновленные цены на Премиум по ТЗ."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день - 0.50$", callback_data="pay_prem_1_0.50")],
        [InlineKeyboardButton(text="3 дня - 1.30$", callback_data="pay_prem_3_1.30")],
        [InlineKeyboardButton(text="7 дней - 3.00$", callback_data="pay_prem_7_3.00")],
        [InlineKeyboardButton(text="1 месяц - 5.00$", callback_data="pay_prem_30_5.00")],
        [InlineKeyboardButton(text="🔃 Назад", callback_data="menu_buy_subscription")]
    ])
    text = "<b>Расценки на Premium подписку (Полное отсутствие КД + Доступ к Premium сессиям 48 репортов):</b>"
    await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("pay_"))
async def process_payment_creation(callback: types.CallbackQuery):
    """Создает платеж в CryptoBot и выдает ссылку пользователю."""
    data_parts = callback.data.split("_")
    mode = data_parts[1]      # req или prem
    days = int(data_parts[2]) # количество дней/запросов
    price = float(data_parts[3])
    
    url, invoice_id = await create_crypto_invoice(price)
    if not url or not invoice_id:
        await callback.answer("❌ Ошибка соединения с платежной системой CryptoBot. Попробуйте позже.", show_alert=True)
        return
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить счет", url=url)],
        [InlineKeyboardButton(text="Проверить оплату ✅", callback_data=f"verify_{mode}_{days}_{invoice_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_buy_subscription")]
    ])
    
    await callback.message.edit_caption(
        caption=f"📋 <b>Счет успешно создан!</b>\n\n<b>Тариф:</b> {'Премиум подписка' if mode == 'prem' else 'Дополнительные запросы'}\n<b>Срок/Кол-во:</b> {days} дн.\n<b>Сумма к оплате:</b> {price}$", 
        parse_mode="HTML", 
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("verify_"))
async def verify_payment_callback(callback: types.CallbackQuery):
    """Проверяет оплату и начисляет покупку."""
    parts = callback.data.split("_")
    mode = parts[1]
    days = int(parts[2])
    invoice_id = parts[3]
    
    is_paid = await check_crypto_invoice(invoice_id)
    if not is_paid:
        await callback.answer("❌ Счет не оплачен! Выполните платеж в CryptoBot и нажмите кнопку снова.", show_alert=True)
        return
        
    user_id = callback.from_user.id
    if mode == "prem":
        await asyncio.to_thread(sync_give_premium, user_id, days)
        await callback.answer("🎉 Оплата получена! Вам успешно активирована Premium подписка!", show_alert=True)
    else:
        # Для обычных запросов начисляем по 5 запросов за "день"
        requests_to_give = days * 5 
        await asyncio.to_thread(sync_give_requests, user_id, requests_to_give)
        await callback.answer(f"🎉 Оплата получена! Вам начислено +{requests_to_give} запросов!", show_alert=True)
        
    await send_shakal_photo(user_id, "<blockquote>TREX SHERLOCK - Доступ обновлен!</blockquote>\n<b>Главное меню:</b>", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "profile_promo")
async def profile_promo_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите название промокода для активации:")
    await state.set_state(ShakalStates.PromoRedeem)

@dp.message(ShakalStates.PromoRedeem)
async def promo_redeem(message: types.Message, state: FSMContext):
    code = message.text.strip()
    cnt = await get_promo(code)
    await state.clear()
    if cnt is None:
        await message.answer("❌ Промокод не найден или уже использован.")
        return
    await asyncio.to_thread(sync_give_requests, message.from_user.id, cnt)
    await delete_promo(code)
    await message.answer(f"✅ Промокод принят. Вам зачислено +{cnt} запрос(ов).")

@dp.message(Command("promo"))
async def cmd_create_promo(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id): return
    args = command.args
    if not args:
        await message.answer('Использование: /promo "название" "кол-во"')
        return
    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')', args)
    parts = [p.strip('"\'') for p in parts]
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer('Неверный формат. Пример: /promo SPRING2026 5')
        return
    code = parts[0]
    cnt = int(parts[1])
    await create_promo(code, cnt)
    await message.answer(f"✅ Промокод {code} создан на {cnt} запросов.")


@dp.callback_query(F.data == "menu_shakal")
async def shakal_menu_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if not await validate_cooldown_and_requests(user_id, callback=callback):
        return

    kb_buttons = [
        [InlineKeyboardButton(text="Шерлок", callback_data="shakal_sherlock")],
        [InlineKeyboardButton(text="Другой осинт бот", callback_data="shakal_other")]
    ]

    refs = await get_referrals(user_id)
    if is_owner(user_id) or refs >= 10:
        kb_buttons.append([InlineKeyboardButton(text="Depsearch", callback_data="shakal_depsearch")])
    else:
        kb_buttons.append([InlineKeyboardButton(text="Depsearch", callback_data="shakal_depsearch_locked")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await send_shakal_photo(user_id, "<b>Выберите категорию цели для уничтожения:</b>", reply_markup=kb)

@dp.callback_query(F.data == "shakal_depsearch_locked")
async def dep_locked_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    refs = await get_referrals(user_id)
    await callback.answer(f"❌ Требуется 10 рефералов, у вас {refs}", show_alert=True)

# --- СЦЕНАРИЙ 1: ШЕРЛОК ---
@dp.callback_query(F.data == "shakal_sherlock")
async def sherlock_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await validate_cooldown_and_requests(user_id, callback=callback):
        return
        
    await callback.message.answer("Введите юзернейм бота для атаки (например, @sherlock_bot):")
    await state.set_state(ShakalStates.WaitingForSherlock)

@dp.message(ShakalStates.WaitingForSherlock)
async def process_sherlock(message: types.Message, state: FSMContext):
    target = message.text.strip()
    user_id = message.from_user.id
    
    if not await validate_cooldown_and_requests(user_id, message=message):
        await state.clear()
        return

    if not target.lower().endswith("bot"):
        await message.answer("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'. Попробуйте еще раз:")
        return

    await state.clear()
    username = message.from_user.username or "Unknown"

    global last_global_report_time
    async with global_report_lock:
        now = time.time()
        if not is_owner(user_id) and now - last_global_report_time < 300:
            wait_time = 300 - (now - last_global_report_time)
            wait_msg = await message.answer(f"⏳ Очередь занята. Ваш запрос добавлен в очередь и начнется автоматически через {int(wait_time)} сек...")
            await asyncio.sleep(wait_time)
            try: await wait_msg.delete()
            except Exception: pass

        if not is_owner(user_id):
            if not await check_premium(user_id):
                user_cooldowns[user_id] = time.time() + 15 * 60
                await asyncio.to_thread(sync_decrement_requests, user_id)

        status_msg = await message.answer("🚀 [1/3] Отправка команды /start...")
        comment_text = await get_config("sherlock_text")
        
        session_stats = {
            "sherlock": {"success": 0, "fail": 0},
            "sherlock3": {"success": 0, "fail": 0}
        }

        clients_to_use = [("sherlock", clients["sherlock"]), ("sherlock3", clients["sherlock3"])]

        for name, cl in clients_to_use:
            try:
                await cl.send_message(target, "/start")
                await asyncio.sleep(1.0)
            except Exception as e: 
                logging.debug(f"Ошибка старта сессии {name}: {e}")
        await asyncio.sleep(1.5)

        await status_msg.edit_text("🚀 [2/3] Отправка первых аллахов на бота с двух аллахав...")
        for name, cl in clients_to_use:
            for _ in range(4):
                try:
                    await cl(functions.account.ReportPeerRequest(
                        peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                    ))
                    session_stats[name]["success"] += 1
                    await asyncio.sleep(1.2)
                except Exception:
                    session_stats[name]["fail"] += 1

        await status_msg.edit_text("🔍 [3/3] Поиск целевого сообщения для отправки аллахов...")
        target_msg_id = None
        
        for name, cl in clients_to_use:
            try:
                messages = await cl.get_messages(target, limit=50)
                bot_count = 0
                third_msg_id = None

                for msg in messages:
                    if not msg.out and msg.message:
                        bot_count += 1
                        if str(msg.message).startswith("ℹ️ Примеры") or str(msg.message).startswith("«Scalp»"):
                            target_msg_id = msg.id
                            break
                        if bot_count == 3:
                            third_msg_id = msg.id

                if not target_msg_id and third_msg_id:
                    target_msg_id = third_msg_id
                
                if target_msg_id:
                    break
            except Exception as e:
                logging.debug(f"Ошибка получения сообщений: {e}")

        if target_msg_id:
            for name, cl in clients_to_use:
                for _ in range(4):
                    try:
                        await cl(functions.messages.ReportRequest(
                            peer=target, id=[target_msg_id], reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        session_stats[name]["success"] += 1
                        await asyncio.sleep(1.2)
                    except Exception:
                        session_stats[name]["fail"] += 1
        else:
            await status_msg.edit_text("⚠️ Сообщение не найдено. Досылаем финальные шакализаторы на профиль...")
            for name, cl in clients_to_use:
                for _ in range(4):
                    try:
                        await cl(functions.account.ReportPeerRequest(
                            peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        session_stats[name]["success"] += 1
                        await asyncio.sleep(1.2)
                    except Exception:
                        session_stats[name]["fail"] += 1

        for name, cl in clients_to_use:
            try: await cl(BlockRequest(id=target))
            except Exception: pass

        try: await status_msg.delete()
        except Exception: pass

        await send_shakal_photo(user_id, f"✅ <b>Шакализатор успешно отправлен на {target} с двух аллахав!</b>", reply_markup=get_main_keyboard())

        total_success = session_stats["sherlock"]["success"] + session_stats["sherlock3"]["success"]
        total_fail = session_stats["sherlock"]["fail"] + session_stats["sherlock3"]["fail"]

        try:
            log_txt = (
    f"<b>💎 Обычная отправка</b>\n"
    f"━━━━━━━━━━━━━━━━\n"
    f"<b>Успешно отправлено на:</b> {target}\n"
    f"<b>От:</b> @{username}\n"
    f"<b>Тип:</b> Шерлок (sherlock + sherlock3)\n"
    f"<b>✅ {total_success} | ❌ {total_fail}</b>"
    f"Наш бот: @{BOT_USERNAME}"
        )
            for o in OWNER_IDS:
                try: await bot.send_message(o, log_txt, parse_mode="HTML")
                except Exception: pass
        except Exception: pass

        last_global_report_time = time.time()

# --- СЦЕНАРИЙ 2: ДРУГОЙ ОСИНТ БОТ ---
@dp.callback_query(F.data == "shakal_other")
async def other_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await validate_cooldown_and_requests(user_id, callback=callback):
        return
        
    await callback.message.answer("Введите юзернейм бота (например, @osint_bot):")
    await state.set_state(ShakalStates.WaitingForOtherBot)

@dp.message(ShakalStates.WaitingForOtherBot)
async def process_other_bot(message: types.Message, state: FSMContext):
    target = message.text.strip()
    user_id = message.from_user.id
    
    if not await validate_cooldown_and_requests(user_id, message=message):
        await state.clear()
        return

    if not target.lower().endswith("bot"):
        await message.answer("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'. Попробуйте еще раз:")
        return

    await state.update_data(target_bot=target)
    await message.answer("Введите с какого слова начинается главное сообщение бота (для поиска):")
    await state.set_state(ShakalStates.WaitingForOtherWord)

@dp.message(ShakalStates.WaitingForOtherWord)
async def process_other_word(message: types.Message, state: FSMContext):
    word_trigger = message.text.strip()
    user_id = message.from_user.id
    
    if not await validate_cooldown_and_requests(user_id, message=message):
        await state.clear()
        return

    data = await state.get_data()
    target = data['target_bot']
    await state.clear()

    username = message.from_user.username or "Unknown"

    global last_global_report_time
    async with global_report_lock:
        now = time.time()
        if not is_owner(user_id) and now - last_global_report_time < 300:
            wait_time = 300 - (now - last_global_report_time)
            wait_msg = await message.answer(f"⏳ Очередь занята. Ваш запрос добавлен в очередь и начнется автоматически через {int(wait_time)} сек...")
            await asyncio.sleep(wait_time)
            try: await wait_msg.delete()
            except Exception: pass

        if not is_owner(user_id):
            if not await check_premium(user_id):
                user_cooldowns[user_id] = time.time() + 15 * 60
                await asyncio.to_thread(sync_decrement_requests, user_id)

        status_msg = await message.answer("🚀 [1/3] Отправка команды /start...")
        comment_text = await get_config("other_text")
        success_cnt, fail_cnt = 0, 0

        try:
            try:
                await clients["osint"].send_message(target, "/start")
                await asyncio.sleep(2.5)
            except Exception: pass

            await status_msg.edit_text("🚀 [2/3] Отправка первых аллахов на профиль бота...")
            for _ in range(4):
                try:
                    await clients["osint"](functions.account.ReportPeerRequest(
                        peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                    ))
                    success_cnt += 1
                    await asyncio.sleep(1.2)
                except Exception: fail_cnt += 1

            await status_msg.edit_text(f"🔍 [3/3] Поиск аллаха, начинающегося на слово: '{word_trigger}'...")
            msg_reported = False
            try:
                messages = await clients["osint"].get_messages(target, limit=50)
                target_msg = None
                third_msg = None
                bot_count = 0

                for msg in messages:
                    if not msg.out and msg.message:
                        bot_count += 1
                        if msg.message.lower().startswith(word_trigger.lower()):
                            target_msg = msg
                            break
                        if bot_count == 3:
                            third_msg = msg

                if not target_msg and third_msg:
                    target_msg = third_msg

                if target_msg:
                    for _ in range(4):
                        try:
                            await clients["osint"](functions.messages.ReportRequest(
                                peer=target, id=[target_msg.id], reason=InputReportReasonPersonalDetails(), message=comment_text
                            ))
                            success_cnt += 1
                            await asyncio.sleep(1.2)
                        except Exception: fail_cnt += 1
                    msg_reported = True
            except Exception: pass

            if not msg_reported:
                await status_msg.edit_text("⚠️ Сообщение не найдено. Досылаем финальные аллахи на профиль...")
                for _ in range(4):
                    try:
                        await clients["osint"](functions.account.ReportPeerRequest(
                            peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        success_cnt += 1
                        await asyncio.sleep(1.2)
                    except Exception: fail_cnt += 1

            try: await status_msg.delete()
            except Exception: pass

            await send_shakal_photo(user_id, f"✅ <b>Шакализатор успешно отправлен на {target}!</b>", reply_markup=get_main_keyboard())

            try:
                log_txt = (
    f"<b>💎 Обычная отправка</b>\n"
    f"━━━━━━━━━━━━━━━━\n"
    f"<b>Успешно отправлено на:</b> {target}\n"
    f"<b>От:</b> @{username}\n"
    f"<b>Тип:</b> Other OSINT\n"
    f"<b>✅ {success_cnt} | ❌ {fail_cnt}</b>"
    f"Наш бот: @{BOT_USERNAME}"
            )
                for o in OWNER_IDS:
                    try: await bot.send_message(o, log_txt, parse_mode="HTML")
                    except Exception: pass
            except Exception: pass

        except Exception as e:
            await message.answer(f"❌ Произошла критическая ошибка сессии osint: {e}")

        last_global_report_time = time.time()

# --- СЦЕНАРИЙ 3: DEPSERACH ---
@dp.callback_query(F.data == "shakal_depsearch")
async def depsearch_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await validate_cooldown_and_requests(user_id, callback=callback):
        return
        
    await callback.message.answer("Укажите ссылку/юзернейм бота Depsearch (например, @DepsearchBot):")
    await state.set_state(ShakalStates.WaitingForDepsearchBot)

@dp.message(ShakalStates.WaitingForDepsearchBot)
async def process_depsearch(message: types.Message, state: FSMContext):
    target = message.text.strip()
    user_id = message.from_user.id
    
    if not await validate_cooldown_and_requests(user_id, message=message):
        await state.clear()
        return

    if not target.lower().endswith("bot"):
        await message.answer("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'. Попробуйте еще раз:")
        return

    await state.clear()
    username = message.from_user.username or "Unknown"

    refs = await get_referrals(user_id)
    if not is_owner(user_id) and refs < 10:
        await message.answer("❌ Для использования Depsearch требуется минимум 10 рефералов.")
        return

    global last_global_report_time
    async with global_report_lock:
        now = time.time()
        if not is_owner(user_id) and now - last_global_report_time < 300:
            wait_time = 300 - (now - last_global_report_time)
            wait_msg = await message.answer(f"⏳ Очередь занята. Ваш запрос начнётся через {int(wait_time)} сек...")
            await asyncio.sleep(wait_time)
            try: await wait_msg.delete()
            except Exception: pass

        if not is_owner(user_id):
            if not await check_premium(user_id):
                user_cooldowns[user_id] = time.time() + 15 * 60
                await asyncio.to_thread(sync_decrement_requests, user_id)

        status_msg = await message.answer("🚀 [1/3] Отправка команды /start в Depsearch...")
        comment_text = await get_config("depsearch_text")
        success_cnt, fail_cnt = 0, 0

        try:
            try:
                await clients["depsearch"].send_message(target, "/start")
                await asyncio.sleep(1.0)
            except Exception: pass

            await status_msg.edit_text("🔍 Поиск кнопки 'search' и нажатие...")
            clicked = False
            try:
                messages = await clients["depsearch"].get_messages(target, limit=20)
                for msg in messages:
                    if msg.buttons:
                        rows = msg.buttons if isinstance(msg.buttons[0], list) else [msg.buttons]
                        for row in rows:
                            for btn in row:
                                data = getattr(btn, 'data', None)
                                try:
                                    if data and (data == b"search" or (isinstance(data, bytes) and b"search" in data) or (isinstance(data, str) and "search" in data)):
                                        await clients["depsearch"](functions.messages.GetBotCallbackAnswerRequest(
                                            peer=target, msg_id=msg.id, data=data
                                        ))
                                        clicked = True
                                        break
                                except Exception: pass
                            if clicked: break
                    if clicked: break
            except Exception: pass

            await asyncio.sleep(0.8)

            await status_msg.edit_text("🔍 Поиск большого результата для аллахав...")
            target_msg = None
            try:
                messages = await clients["depsearch"].get_messages(target, limit=50)
                bot_count = 0
                third_msg = None
                for msg in messages:
                    if not msg.out and msg.message:
                        bot_count += 1
                        if len(str(msg.message)) > 100:
                            target_msg = msg
                            break
                        if bot_count == 3:
                            third_msg = msg
                if not target_msg and third_msg:
                    target_msg = third_msg

                if target_msg:
                    await status_msg.edit_text("🚀 Отправка 4 аллахов на сообщение (message reports)...")
                    for _ in range(4):
                        try:
                            await clients["depsearch"](functions.messages.ReportRequest(
                                peer=target, id=[target_msg.id], reason=InputReportReasonPersonalDetails(), message=comment_text
                            ))
                            success_cnt += 1
                            await asyncio.sleep(0.5)
                        except Exception: fail_cnt += 1

                await status_msg.edit_text("🚀 Отправка 4 аллахов на профиль (peer reports)...")
                for _ in range(4):
                    try:
                        await clients["depsearch"](functions.account.ReportPeerRequest(
                            peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        success_cnt += 1
                        await asyncio.sleep(0.5)
                    except Exception: fail_cnt += 1
            except Exception: pass

            try: await status_msg.delete()
            except Exception: pass

            await send_shakal_photo(user_id, f"✅ <b>Depsearch: Шакализатор успешно отправлен на {target}!</b>", reply_markup=get_main_keyboard())

            try:
                log_txt = (
    f"<b>💎 Обычная отправка</b>\n"
    f"━━━━━━━━━━━━━━━━\n"
    f"<b>Успешно отправлено на:</b> {target}\n"
    f"<b>От:</b> @{username}\n"
    f"<b>Тип:</b> Depsearch\n"
    f"<b>✅ {success_cnt} | ❌ {fail_cnt}</b>"
    f"Наш бот: @{BOT_USERNAME}"
            )
                for o in OWNER_IDS:
                    try: await bot.send_message(o, log_txt, parse_mode="HTML")
                    except Exception: pass
            except Exception: pass

        except Exception as e:
            await message.answer(f"❌ Произошла критическая ошибка сессии depsearch: {e}")

        last_global_report_time = time.time()


@dp.callback_query(F.data == "menu_premium_features")
async def premium_features_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if not is_owner(user_id) and not await check_premium(user_id):
        await callback.answer("❌ Данная функция доступна только для пользователей с Premium подпиской!", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. Depsearch ⚡️", callback_data="prem_attack_depsearch")],
        [InlineKeyboardButton(text="2. ОСТАЛЬНЫЕ ОСИНТ БОТЫ  ⚡️", callback_data="prem_attack_others")],
        [InlineKeyboardButton(text="🔃 Назад в меню", callback_data="profile_back")]
    ])
    
    await callback.message.edit_caption(
        caption="💎 <b>Добро пожаловать в панель Premium отправки!</b>\n\nЗдесь у вас полностью отсутствует КД, а жалобы рассылаются сразу со множества выделенных сессий.",
        parse_mode="HTML",
        reply_markup=kb
    )

@dp.callback_query(F.data == "prem_attack_depsearch")
async def prem_depsearch_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_owner(user_id) and not await check_premium(user_id):
        await callback.answer("❌ Ошибка доступа.", show_alert=True)
        return
        
    await callback.message.answer("💎 Введите юзернейм бота Depsearch:")
    await state.set_state(ShakalStates.WaitingForPremDepsearch)

@dp.message(ShakalStates.WaitingForPremDepsearch)
async def process_prem_depsearch(message: types.Message, state: FSMContext):
    target = message.text.strip()
    user_id = message.from_user.id
    await state.clear()
    
    if not is_owner(user_id) and not await check_premium(user_id):
        await message.answer("❌ У вас нет Premium.")
        return

    if not target.lower().endswith("bot"):
        await message.answer("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'.")
        return

    username = message.from_user.username or "Unknown"
    status_msg = await message.answer("🚀 [Premium] Инициализация сессии prem_depsearch.session...")
    
    comment_text = "Бот выдает личные данные людей без разрешения: номера, адреса и фамилии. Это доксинг, закройте его."
    success_cnt, fail_cnt = 0, 0
    cl = premium_clients["prem_depsearch"]
    
    try:
        try:
            await cl.send_message(target, "/start")
            await asyncio.sleep(1.0)
        except Exception: pass

        await status_msg.edit_text("🔍 [Premium] Поиск кнопки 'search' и нажатие...")
        clicked = False
        try:
            messages = await cl.get_messages(target, limit=20)
            for msg in messages:
                if msg.buttons:
                    rows = msg.buttons if isinstance(msg.buttons[0], list) else [msg.buttons]
                    for row in rows:
                        for btn in row:
                            data = getattr(btn, 'data', None)
                            try:
                                if data and (data == b"search" or b"search" in data):
                                    await cl(functions.messages.GetBotCallbackAnswerRequest(
                                        peer=target, msg_id=msg.id, data=data
                                    ))
                                    clicked = True
                                    break
                            except Exception: pass
                        if clicked: break
                if clicked: break
        except Exception: pass

        await asyncio.sleep(1.0)

        target_msg = None
        try:
            messages = await cl.get_messages(target, limit=50)
            bot_count = 0
            third_msg = None
            for msg in messages:
                if not msg.out and msg.message:
                    bot_count += 1
                    if len(str(msg.message)) > 100:
                        target_msg = msg
                        break
                    if bot_count == 3:
                        third_msg = msg
            if not target_msg and third_msg:
                target_msg = third_msg
        except Exception: pass

        await status_msg.edit_text("🚀 Кидаем аллахов в бота...")
        
        for _ in range(4):
            try:
                await cl(functions.account.ReportPeerRequest(
                    peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                ))
                success_cnt += 1
                await asyncio.sleep(0.4)
            except Exception: fail_cnt += 1
            
        if target_msg:
            for _ in range(4):
                try:
                    await cl(functions.messages.ReportRequest(
                        peer=target, id=[target_msg.id], reason=InputReportReasonPersonalDetails(), message=comment_text
                    ))
                    success_cnt += 1
                    await asyncio.sleep(0.4)
                except Exception: fail_cnt += 1
        else:
            for _ in range(4):
                try:
                    await cl(functions.account.ReportPeerRequest(
                        peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                    ))
                    success_cnt += 1
                    await asyncio.sleep(0.4)
                except Exception: fail_cnt += 1

        try: await cl(BlockRequest(id=target))
        except Exception: pass

        try: await status_msg.delete()
        except Exception: pass
        
        await send_shakal_photo(user_id, f"💎 <b>[Premium] Отправка законченна!</b>", reply_markup=get_main_keyboard())

        log_txt = (
    f"<b>💎 Premium отправка</b>\n"
    f"━━━━━━━━━━━━━━━━\n"
    f"<b>Успешно отправлено на:</b> {target}\n"
    f"<b>От:</b> @{username}\n"
    f"<b>Тип:</b> Depsearch\n"
    f"<b>✅ {success_cnt} | ❌ {fail_cnt}</b>"
    f"Наш бот: @{BOT_USERNAME}"
        )
        for o in OWNER_IDS:
            try: await bot.send_message(o, log_txt, parse_mode="HTML")
            except Exception: pass

    except Exception as e:
        await message.answer(f"❌ Критическая ошибка премиум-сессии depsearch: {e}")

@dp.callback_query(F.data == "prem_attack_others")
async def prem_others_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_owner(user_id) and not await check_premium(user_id):
        await callback.answer("❌ Ошибка доступа.", show_alert=True)
        return
        
    await callback.message.answer("💎 Введите @username_bot для отправки:")
    await state.set_state(ShakalStates.WaitingForPremOther)

@dp.message(ShakalStates.WaitingForPremOther)
async def process_prem_others(message: types.Message, state: FSMContext):
    target = message.text.strip()
    user_id = message.from_user.id
    await state.clear()
    
    if not is_owner(user_id) and not await check_premium(user_id):
        await message.answer("❌ У вас нет Premium.")
        return

    if not target.lower().endswith("bot"):
        await message.answer("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'.")
        return

    status_msg = await message.answer("🚀 <b>[Premium] Запуск мега-круга уничтожения со всех 6 сессий...</b>", parse_mode="HTML")
    
    trigger_words = ["Номер", "номера", "ИНН", "адрес", "паспорт", "пример", "примеры"]
    total_success = 0
    total_fail = 0

    for i in range(1, 7):
        client_key = f"prem_{i}"
        config_key = f"prem_text_{i}"
        cl = premium_clients[client_key]
        
        comment_text = await get_config(config_key)
        await status_msg.edit_text(f"🚀 [Premium] Работает сессия {i}.session из 6...")

        try:
            try:
                await cl.send_message(target, "/start")
                await asyncio.sleep(1.2)
            except Exception: pass

            for _ in range(4):
                try:
                    await cl(functions.account.ReportPeerRequest(
                        peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                    ))
                    total_success += 1
                    await asyncio.sleep(0.4)
                except Exception: total_fail += 1

            found_msg_id = None
            try:
                messages = await cl.get_messages(target, limit=50)
                for msg in messages:
                    if not msg.out and msg.message:
                        text_lower = str(msg.message).lower()
                        if any(word.lower() in text_lower for word in trigger_words):
                            found_msg_id = msg.id
                            break
            except Exception: pass

            if found_msg_id:
                for _ in range(4):
                    try:
                        await cl(functions.messages.ReportRequest(
                            peer=target, id=[found_msg_id], reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        total_success += 1
                        await asyncio.sleep(0.4)
                    except Exception: total_fail += 1
            else:
                for _ in range(4):
                    try:
                        await cl(functions.account.ReportPeerRequest(
                            peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        total_success += 1
                        await asyncio.sleep(0.4)
                    except Exception: total_fail += 1

            try:
                await cl(BlockRequest(id=target))
            except Exception: pass

        except Exception as e:
            logging.error(f"Error in premium session {i}: {e}")
            total_fail += 8 

    try: await status_msg.delete()
    except Exception: pass
    
    await send_shakal_photo(user_id, f"💎 <b>Premium отправка завершена, обработано: 48 аллахов</b>", reply_markup=get_main_keyboard())

    log_txt = (
    f"<b>💎 Premium отправка</b>\n"
    f"━━━━━━━━━━━━━━━━\n"
    f"<b>Успешно отправлено на:</b> {target}\n"
    f"<b>От:</b> @{(message.from_user.username or 'Unknown')}\n"
    f"<b>Тип:</b> other bots\n"
    f"<b>✅ {total_success} | ❌ {total_fail}</b>"
    f"Наш бот: @{BOT_USERNAME}"
    )
    for o in OWNER_IDS:
        try: await bot.send_message(o, log_txt, parse_mode="HTML")
        except Exception: pass


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_owner(message.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. Бан пользователя ⛔️", callback_data="admin_ban")],
        [InlineKeyboardButton(text="2. Выдать запросы 💎", callback_data="admin_give")],
        [InlineKeyboardButton(text="3. Забрать запросы 📉", callback_data="admin_take")],
        [InlineKeyboardButton(text="4. Выдать Premium (дни) 👑", callback_data="admin_give_prem")],
        [InlineKeyboardButton(text="5. Забрать Premium (дни) 🗑", callback_data="admin_take_prem")],
        [InlineKeyboardButton(text="6. Изменить дефолтные тексты 📝", callback_data="admin_set_default_texts")],
        [InlineKeyboardButton(text="7. 👑 Настройки текстов Premium 👑", callback_data="admin_premium_text_panel")],
        [InlineKeyboardButton(text="8. 🔄 Заменить сессию", callback_data="admin_change_session")]
    ])
    await message.answer("👑 <b>Панель управления Владельца:</b>", parse_mode="HTML", reply_markup=kb)

# --- ДОБАВЛЕННЫЕ ФУНКЦИИ ВЫДАЧИ/СНЯТИЯ ПРЕМИУМ ДНЕЙ ---

@dp.callback_query(F.data == "admin_give_prem")
async def admin_give_prem_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ID пользователя, которому выдать Premium подписку:")
    await state.set_state(ShakalStates.AdminGivePremID)

@dp.message(ShakalStates.AdminGivePremID)
async def admin_give_prem_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен быть числом.")
        await state.clear()
        return
    await state.update_data(target_user=int(message.text))
    await message.answer("Введите количество дней Premium подписки:")
    await state.set_state(ShakalStates.AdminGivePremDays)

@dp.message(ShakalStates.AdminGivePremDays)
async def admin_give_prem_days(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not message.text.isdigit():
        await message.answer("❌ Количество дней должно быть числом.")
        return

    u_id = data['target_user']
    days = int(message.text)
    await asyncio.to_thread(sync_give_premium, u_id, days)
    await message.answer(f"👑 Пользователю <code>{u_id}</code> успешно выдана Premium подписка на {days} дней.", parse_mode="HTML")

@dp.callback_query(F.data == "admin_take_prem")
async def admin_take_prem_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ID пользователя, у которого нужно забрать Premium дни:")
    await state.set_state(ShakalStates.AdminTakePremID)

@dp.message(ShakalStates.AdminTakePremID)
async def admin_take_prem_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен быть числом.")
        await state.clear()
        return
    await state.update_data(target_user=int(message.text))
    await message.answer("Введите количество дней Premium, которые нужно снять:")
    await state.set_state(ShakalStates.AdminTakePremDays)

@dp.message(ShakalStates.AdminTakePremDays)
async def admin_take_prem_days(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not message.text.isdigit():
        await message.answer("❌ Количество дней должно быть числом.")
        return

    u_id = data['target_user']
    days = int(message.text)
    await asyncio.to_thread(sync_take_premium, u_id, days)
    await message.answer(f"📉 У пользователя <code>{u_id}</code> успешно списано {days} Premium дней.", parse_mode="HTML")


@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ID пользователя для вечной блокировки в боте:")
    await state.set_state(ShakalStates.AdminBan)

@dp.message(ShakalStates.AdminBan)
async def admin_ban_proc(message: types.Message, state: FSMContext):
    await state.clear()
    if not message.text.isdigit():
        await message.answer("❌ ID должен быть числовом.")
        return
    u_id = int(message.text)
    await asyncio.to_thread(sync_ban_user, u_id)
    await message.answer(f"⛔️ Пользователь <code>{u_id}</code> забанен навсегда.", parse_mode="HTML")

@dp.callback_query(F.data == "admin_give")
async def admin_give_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ID пользователя, которому выдать запросы:")
    await state.set_state(ShakalStates.AdminGiveID)

@dp.message(ShakalStates.AdminGiveID)
async def admin_give_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен быть числом.")
        await state.clear()
        return
    await state.update_data(target_user=int(message.text))
    await message.answer("Введите количество запросов:")
    await state.set_state(ShakalStates.AdminGiveCount)

@dp.message(ShakalStates.AdminGiveCount)
async def admin_give_count(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not message.text.isdigit():
        await message.answer("❌ Количество должно быть числом.")
        return

    u_id = data['target_user']
    count = int(message.text)
    await asyncio.to_thread(sync_give_requests, u_id, count)
    await message.answer(f"💎 Пользователю <code>{u_id}</code> успешно зачислено +{count} обычных запросов.", parse_mode="HTML")

@dp.callback_query(F.data == "admin_take")
async def admin_take_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите ID пользователя, у которого нужно ЗАБРАТЬ запросы:")
    await state.set_state(ShakalStates.AdminTakeID)

@dp.message(ShakalStates.AdminTakeID)
async def admin_take_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен быть числом.")
        await state.clear()
        return
    await state.update_data(target_user=int(message.text))
    await message.answer("Введите количество запросов, которые нужно снять:")
    await state.set_state(ShakalStates.AdminTakeCount)

@dp.message(ShakalStates.AdminTakeCount)
async def admin_take_count(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not message.text.isdigit():
        await message.answer("❌ Количество должно быть числом.")
        return

    u_id = data['target_user']
    count = int(message.text)
    await asyncio.to_thread(sync_take_requests, u_id, count)
    await message.answer(f"📉 У пользователя <code>{u_id}</code> успешно списано {count} обычных запросов.", parse_mode="HTML")

# --- РЕДАКТИРОВАНИЕ ДЕФОЛТНЫХ ТЕКСТОВ ---
@dp.callback_query(F.data == "admin_set_default_texts")
async def admin_set_default_texts(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Текст Шерлока", callback_data="admin_set_sherlock")],
        [InlineKeyboardButton(text="Текст Других ботов", callback_data="admin_set_other")],
        [InlineKeyboardButton(text="Текст Depsearch", callback_data="admin_set_dep")],
        [InlineKeyboardButton(text="🔃 Назад", callback_data="profile_back")]
    ])
    await callback.message.answer("Выберите шаблон текста для редактирования:", reply_markup=kb)

@dp.callback_query(F.data == "admin_premium_text_panel")
async def admin_premium_text_panel(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сменить текст для 1 сессии", callback_data="set_prem_text_1")],
        [InlineKeyboardButton(text="Сменить текст для 2 сессии", callback_data="set_prem_text_2")],
        [InlineKeyboardButton(text="Сменить текст для 3 сессии", callback_data="set_prem_text_3")],
        [InlineKeyboardButton(text="Сменить текст для 4 сессии", callback_data="set_prem_text_4")],
        [InlineKeyboardButton(text="Сменить текст для 5 сессии", callback_data="set_prem_text_5")],
        [InlineKeyboardButton(text="Сменить текст для 6 сессии", callback_data="set_prem_text_6")],
        [InlineKeyboardButton(text="🔃 Обратно в админку", callback_data="profile_back")]
    ])
    await callback.message.answer("📝 <b>Управление кастомными текстами для 6 Premium-сессий:</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_prem_text_"))
async def admin_set_prem_text_start(callback: types.CallbackQuery, state: FSMContext):
    index = int(callback.data.replace("set_prem_text_", ""))
    current_txt = await get_config(f"prem_text_{index}")
    await callback.message.answer(f"Текущий текст для <b>{index} сессии</b>:\n<i>{current_txt}</i>\n\nВведите новый текст жалобы:", parse_mode="HTML")
    
    state_mapping = {
        1: ShakalStates.AdminSetPremText1,
        2: ShakalStates.AdminSetPremText2,
        3: ShakalStates.AdminSetPremText3,
        4: ShakalStates.AdminSetPremText4,
        5: ShakalStates.AdminSetPremText5,
        6: ShakalStates.AdminSetPremText6
    }
    await state.set_state(state_mapping[index])

@dp.message(ShakalStates.AdminSetPremText1)
async def proc_prem_text_1(message: types.Message, state: FSMContext):
    await update_config("prem_text_1", message.text.strip())
    await state.clear()
    await message.answer("✅ Шаблон текста для 1 Premium сессии успешно изменен.")

@dp.message(ShakalStates.AdminSetPremText2)
async def proc_prem_text_2(message: types.Message, state: FSMContext):
    await update_config("prem_text_2", message.text.strip())
    await state.clear()
    await message.answer("✅ Шаблон текста для 2 Premium сессии успешно изменен.")

@dp.message(ShakalStates.AdminSetPremText3)
async def proc_prem_text_3(message: types.Message, state: FSMContext):
    await update_config("prem_text_3", message.text.strip())
    await state.clear()
    await message.answer("✅ Шаблон текста для 3 Premium сессии успешно изменен.")

@dp.message(ShakalStates.AdminSetPremText4)
async def proc_prem_text_4(message: types.Message, state: FSMContext):
    await update_config("prem_text_4", message.text.strip())
    await state.clear()
    await message.answer("✅ Шаблон текста для 4 Premium сессии успешно изменен.")

@dp.message(ShakalStates.AdminSetPremText5)
async def proc_prem_text_5(message: types.Message, state: FSMContext):
    await update_config("prem_text_5", message.text.strip())
    await state.clear()
    await message.answer("✅ Шаблон текста для 5 Premium сессии успешно изменен.")

@dp.message(ShakalStates.AdminSetPremText6)
async def proc_prem_text_6(message: types.Message, state: FSMContext):
    await update_config("prem_text_6", message.text.strip())
    await state.clear()
    await message.answer("✅ Шаблон текста для 6 Premium сессии успешно изменен.")

@dp.callback_query(F.data == "admin_set_sherlock")
async def admin_sh_text_start(callback: types.CallbackQuery, state: FSMContext):
    current_txt = await get_config('sherlock_text')
    await callback.message.answer(f"Текущий текст:\n<i>{current_txt}</i>\n\nВведите новый default текст для Шерлока:", parse_mode="HTML")
    await state.set_state(ShakalStates.AdminSetSherlockText)

@dp.message(ShakalStates.AdminSetSherlockText)
async def admin_sh_text_proc(message: types.Message, state: FSMContext):
    new_t = message.text.strip()
    await update_config("sherlock_text", new_t)
    await state.clear()
    await message.answer("✅ Шаблон текста для Шерлока изменен.")

@dp.callback_query(F.data == "admin_set_other")
async def admin_oth_text_start(callback: types.CallbackQuery, state: FSMContext):
    current_txt = await get_config('other_text')
    await callback.message.answer(f"Текущий текст:\n<i>{current_txt}</i>\n\nВведите новый default текст для Других ботов:", parse_mode="HTML")
    await state.set_state(ShakalStates.AdminSetOtherText)

@dp.message(ShakalStates.AdminSetOtherText)
async def admin_oth_text_proc(message: types.Message, state: FSMContext):
    new_t = message.text.strip()
    await update_config("other_text", new_t)
    await state.clear()
    await message.answer("✅ Шаблон текста для Других OSINT ботов изменен.")

@dp.callback_query(F.data == "admin_set_dep")
async def admin_dep_text_start(callback: types.CallbackQuery, state: FSMContext):
    current_txt = await get_config('depsearch_text')
    await callback.message.answer(f"Текущий текст:\n<i>{current_txt}</i>\n\nВведите новый default текст для Depsearch:", parse_mode="HTML")
    await state.set_state(ShakalStates.AdminSetDepText)

@dp.message(ShakalStates.AdminSetDepText)
async def admin_dep_text_proc(message: types.Message, state: FSMContext):
    new_t = message.text.strip()
    await update_config("depsearch_text", new_t)
    await state.clear()
    await message.answer("✅ Шаблон текста для Depsearch изменен.")


# --- СЦЕНАРИЙ ЗАМЕНЫ СЕССИЙ (КЛАССИЧЕСКИЕ + ПРЕМИУМ) ПОЛНАЯ РЕАЛИЗАЦИЯ ---
@dp.callback_query(F.data == "admin_change_session")
async def admin_change_session_start(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="sherlock.session", callback_data="change_sess_sherlock")],
        [InlineKeyboardButton(text="osint.session", callback_data="change_sess_osint")],
        [InlineKeyboardButton(text="sherlock3.session", callback_data="change_sess_sherlock3")],
        [InlineKeyboardButton(text="depsearch.session", callback_data="change_sess_depsearch")],
        [InlineKeyboardButton(text="prem_depsearch.session 💎", callback_data="change_sess_prem_depsearch")],
        [InlineKeyboardButton(text="1.session 💎", callback_data="change_sess_prem_1")],
        [InlineKeyboardButton(text="2.session 💎", callback_data="change_sess_prem_2")],
        [InlineKeyboardButton(text="3.session 💎", callback_data="change_sess_prem_3")],
        [InlineKeyboardButton(text="4.session 💎", callback_data="change_sess_prem_4")],
        [InlineKeyboardButton(text="5.session 💎", callback_data="change_sess_prem_5")],
        [InlineKeyboardButton(text="6.session 💎", callback_data="change_sess_prem_6")]
    ])
    await callback.message.answer("Выберите сессию которую нужно заменить/авторизовать:", reply_markup=kb)

@dp.callback_query(F.data.startswith("change_sess_"))
async def process_sess_change(callback: types.CallbackQuery, state: FSMContext):
    """
    Начинает процесс авторизации новой сессии Telethon.
    """
    user_id = callback.from_user.id
    if not is_owner(user_id):
        return await callback.answer("Нет прав.", show_alert=True)
        
    sess_key = callback.data.replace("change_sess_", "")
    await state.update_data(sess_key=sess_key)

    # Определяем правильный путь в зависимости от того, премиум это сессия или нет
    if "prem_" in sess_key:
        actual_name = sess_key.replace("prem_", "")
        path = os.path.join(PREMIUM_SESS_DIR, actual_name)
    else:
        path = os.path.join(BASE_DIR, sess_key)

    # Создаем временный клиент для авторизации (создаст/перезапишет файл .session)
    temp_client = TelegramClient(path, API_ID, API_HASH)
    await temp_client.connect()
    
    # Сохраняем в память, чтобы использовать в следующих шагах
    active_auth_clients[user_id] = temp_client

    await callback.message.answer(
        f"🔑 <b>Авторизация сессии <code>{sess_key}</code></b>.\n\n"
        f"Пожалуйста, введите номер телефона аккаунта (в международном формате с кодом, например <code>+79991234567</code>):", 
        parse_mode="HTML"
    )
    await state.set_state(ShakalStates.AdminSessionPhone)

@dp.message(ShakalStates.AdminSessionPhone)
async def auth_phone(message: types.Message, state: FSMContext):
    """Принимает телефон и отправляет код подтверждения."""
    phone = message.text.strip()
    user_id = message.from_user.id
    client = active_auth_clients.get(user_id)
    
    if not client:
        await state.clear()
        return await message.answer("❌ Временная сессия потеряна. Начните заново из меню администратора.")

    try:
        sent_code = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent_code.phone_code_hash)
        await message.answer(f"✅ Код успешно отправлен на номер <code>{phone}</code>!\n\nПожалуйста, введите полученный код подтверждения:", parse_mode="HTML")
        await state.set_state(ShakalStates.AdminSessionCode)
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки кода: {e}\nНачните процесс заново.")
        await state.clear()

@dp.message(ShakalStates.AdminSessionCode)
async def auth_code(message: types.Message, state: FSMContext):
    """Принимает код и пытается войти в аккаунт."""
    code = message.text.strip()
    user_id = message.from_user.id
    data = await state.get_data()
    client = active_auth_clients.get(user_id)

    if not client:
        await state.clear()
        return await message.answer("❌ Временная сессия потеряна. Начните заново.")

    try:
        await client.sign_in(data['phone'], code, phone_code_hash=data['phone_code_hash'])
        await finish_auth_flow(message, state, client)
    except SessionPasswordNeededError:
        # Если включена двухфакторная аутентификация
        await message.answer("🔒 На аккаунте включена Двухфакторная аутентификация (2FA).\nВведите облачный пароль:")
        await state.set_state(ShakalStates.AdminSession2FA)
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуйте еще раз:")
    except PhoneCodeExpiredError:
        await message.answer("❌ Код истек. Начните авторизацию заново.")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Неизвестная ошибка авторизации: {e}")
        await state.clear()

@dp.message(ShakalStates.AdminSession2FA)
async def auth_2fa(message: types.Message, state: FSMContext):
    """Принимает 2FA пароль и завершает вход."""
    pwd = message.text.strip()
    user_id = message.from_user.id
    client = active_auth_clients.get(user_id)
    
    if not client:
        await state.clear()
        return await message.answer("❌ Временная сессия потеряна. Начните заново.")
        
    try:
        await client.sign_in(password=pwd)
        await finish_auth_flow(message, state, client)
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA пароля: {e}\nНачните процесс заново из меню.")
        await state.clear()

async def finish_auth_flow(message: types.Message, state: FSMContext, client: TelegramClient):
    """Вспомогательная функция для успешного завершения флоу авторизации."""
    data = await state.get_data()
    sess_key = data['sess_key']
    user_id = message.from_user.id
    
    # Отключаем временный клиент
    try:
        await client.disconnect()
    except Exception as e:
        logging.debug(f"Ошибка дисконнекта временного клиента: {e}")

    # Удаляем из кэша
    if user_id in active_auth_clients:
        del active_auth_clients[user_id]
        
    # Перезапускаем глобального клиента, чтобы бот начал его использовать
    await restart_global_client(sess_key)
    
    await state.clear()
    await message.answer(f"✅ <b>Отлично! Сессия <code>{sess_key}</code> успешно авторизована и запущена!</b>", parse_mode="HTML")


async def main():
    """Основной запуск поллинга и инициализация базы."""
    logging.info("Инициализация базы данных...")
    sync_init_db()
    
    global BOT_USERNAME
    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    
    logging.info("Подключение сессий Telethon...")
    for name, cl in clients.items():
        try:
            await cl.start()
            logging.info(f"Сессия {name} запущена.")
        except Exception as e:
            logging.error(f"Ошибка запуска сессии {name}: {e}")
            
    for name, cl in premium_clients.items():
        try:
            await cl.start()
            logging.info(f"Премиум-сессия {name} запущена.")
        except Exception as e:
            logging.error(f"Ошибка запуска премиум-сессии {name}: {e}")
            
    logging.info("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
