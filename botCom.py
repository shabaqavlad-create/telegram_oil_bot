# bot.py

# --- Импорты ---
import os
import json
import logging
import sqlite3
import re
import time
import io
import csv
import html
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
)
from telegram.ext import filters as F
from telegram.request import HTTPXRequest
from openpyxl import Workbook

from catalog import oils  # словарь с маслами

# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Настройка токена и админов ---
load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден TOKEN в окружении (.env). Укажите TOKEN=<...>")

ADMIN_IDS = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    logger.warning("ADMIN_IDS пуст — админские команды будут недоступны ни для кого.")

# --- Пути к данным ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")  # на случай миграции
DB_PATH = os.path.join(BASE_DIR, "orders.db")        # SQLite

# --- Антиспам ---
ORDER_COOLDOWN_SEC = 20
LAST_ORDER_AT: dict[int, float] = {}  # user_id -> ts последней УСПЕШНОЙ заявки

# --- Константы UI ---
CAPTION_LIMIT = 1024  # безопасный лимит подписи к фото


# ---------- УТИЛИТЫ (общие) ----------

def truncate(s: str, limit: int = CAPTION_LIMIT) -> str:
    return s if len(s) <= limit else s[:limit - 1] + "…"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def target_message(update: Update):
    """Возвращает сообщение, в которое безопасно отвечать (callback или обычное)."""
    return update.callback_query.message if update.callback_query else update.message


async def safe_reply_text(target, text: str, parse_mode: str | None = None, **kwargs):
    """Безопасный reply_text с fallback без parse_mode."""
    try:
        return await target.reply_text(text, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        logger.warning("reply_text упал (%s). Пробуем без parse_mode…", e)
        try:
            return await target.reply_text(text, **{k: v for k, v in kwargs.items() if k != "parse_mode"})
        except Exception:
            logger.exception("reply_text повторно упал")
    return None


async def reply_to_update(update: Update, text: str, **kwargs):
    """Безопасно ответить в тот же контекст, откуда пришёл апдейт (callback/сообщение)."""
    msg = target_message(update)
    if msg:
        return await safe_reply_text(msg, text, **kwargs)


async def show_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, seconds: float = 0.8):
    """Ненавязчивый индикатор 'печатает…'."""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        return
    if seconds > 0:
        import asyncio
        await asyncio.sleep(seconds)


# ---------- БАЗА ДАННЫХ (SQLite) ----------

def get_conn():
    """Единая точка подключения: WAL, busy_timeout, FK и др."""
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)  # autocommit
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("PRAGMA foreign_keys=ON;")
    c.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db():
    """Создаёт таблицу orders и индексы при необходимости."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT    DEFAULT (datetime('now')),
                user_id    INTEGER,
                username   TEXT,
                oil        TEXT,
                volume     TEXT,
                price      TEXT,
                currency   TEXT,
                contact    TEXT
            )
            """
        )
        # индексы
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id   ON orders(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_oil       ON orders(oil)")
    finally:
        conn.close()


def save_order_sql(order: dict) -> str:
    """Сохраняет заявку и возвращает код вида #001."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO orders (user_id, username, oil, volume, price, currency, contact)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.get("user_id"),
                order.get("username"),
                order.get("oil"),
                order.get("volume"),
                order.get("price"),
                order.get("currency"),
                order.get("contact"),
            ),
        )
        row_id = c.lastrowid
        return f"#{row_id:03}"
    finally:
        conn.close()


def fetch_orders_page(page: int, page_size: int = 10):
    """Постраничная выборка. Возвращает (rows, total)."""
    page = max(1, page)
    offset = (page - 1) * page_size
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM orders")
        total = c.fetchone()[0] or 0

        c.execute(
            """
            SELECT id, user_id, username, oil, volume, price, currency, contact, created_at
            FROM orders
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        )
        rows = c.fetchall()
        return rows, total
    finally:
        conn.close()


def db_is_empty() -> bool:
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
        if not c.fetchone():
            return True
        c.execute("SELECT COUNT(*) FROM orders")
        return (c.fetchone()[0] or 0) == 0
    finally:
        conn.close()


def migrate_json_to_sql():
    """Разовая миграция из orders.json в SQLite (если таблица пуста и JSON есть)."""
    if not os.path.exists(ORDERS_FILE):
        return
    if not db_is_empty():
        return

    try:
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            orders = json.load(f)
    except Exception as e:
        logger.exception("Не удалось прочитать %s для миграции: %s", ORDERS_FILE, e)
        return

    if not isinstance(orders, list) or not orders:
        return

    logger.info("Начинаем миграцию %s -> %s (%d записей)", ORDERS_FILE, DB_PATH, len(orders))
    conn = get_conn()
    inserted = 0
    try:
        c = conn.cursor()
        for old in orders:
            try:
                c.execute(
                    """
                    INSERT INTO orders (user_id, username, oil, volume, price, currency, contact)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        old.get("user_id"),
                        old.get("username"),
                        old.get("oil"),
                        old.get("volume"),
                        old.get("price", "—"),
                        old.get("currency", "₽"),
                        old.get("contact"),
                    ),
                )
                inserted += 1
            except Exception as e:
                logger.warning("Пропущена запись при миграции: %s", e)
        logger.info("Миграция завершена, импортировано: %d", inserted)
    finally:
        conn.close()


# ---------- ВАЛИДАЦИЯ КОНТАКТА ----------

PHONE_RE = re.compile(
    r"""^\s*
        (?:
            (\+?\d[\d\-\s\(\)]{8,}\d)      | # телефон
            (@[A-Za-z0-9_]{5,})            | # username
            (https?://t\.me/[A-Za-z0-9_]+)   # ссылка t.me
        )
        \s*$""",
    re.VERBOSE,
)

def validate_contact(text: str) -> tuple[bool, str | None]:
    if not text:
        return False, "Пустой контакт. Укажите телефон или Telegram."
    m = PHONE_RE.match(text)
    if not m:
        return False, (
            "Некорректные контакты. Примеры:\n"
            "• +7 900 123-45-67\n"
            "• @username\n"
            "• https://t.me/username"
        )
    return True, text.strip()


# ---------- Ошибки ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ Ошибка в боте: {context.error}",
            )
        except Exception:
            logger.debug("Не удалось уведомить админа %s", admin_id)


# ---------- СТАТИСТИКА / АДМИН ----------

def fetch_stats():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM orders")
        total = c.fetchone()[0] or 0

        c.execute("""
            SELECT COUNT(*) FROM orders
            WHERE created_at >= datetime('now','-7 days')
        """)
        last7 = c.fetchone()[0] or 0

        c.execute("SELECT COUNT(DISTINCT user_id) FROM orders")
        uniq_users = c.fetchone()[0] or 0

        c.execute("""
            SELECT oil, COUNT(*) as cnt
            FROM orders
            GROUP BY oil
            ORDER BY cnt DESC
            LIMIT 5
        """)
        top = c.fetchall()
        return {"total": total, "last7": last7, "uniq_users": uniq_users, "top": top}
    finally:
        conn.close()


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await reply_to_update(update, "⛔ Команда доступна только администраторам.")
        return

    s = fetch_stats()
    lines = [
        "📊 Статистика заявок:",
        f"• Всего: {s['total']}",
        f"• За 7 дней: {s['last7']}",
        f"• Уникальных пользователей: {s['uniq_users']}",
        "",
        "🏆 Топ-5 товаров:",
    ]
    if s["top"]:
        for oil_name, cnt in s["top"]:
            lines.append(f"  — {oil_name}: {cnt}")
    else:
        lines.append("  — пока нет данных")

    await reply_to_update(update, "\n".join(lines))


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ версии — только админам."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    path = os.path.join(BASE_DIR, "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as f:
            await reply_to_update(update, f"Версия: {f.read().strip()}")
    except FileNotFoundError:
        await reply_to_update(update, "VERSION не найден.")


# ---------- START / Меню ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin_user = user_id in ADMIN_IDS

    text = (
        "Привет! 👋\n"
        "Я бот-магазин масел для электромобилей и гибридов.\n\n"
        "🛠 Используйте кнопки ниже, чтобы открыть каталог, узнать о компании или связаться с нами.\n\n"
        "📌 Дополнительно:\n"
        "/cancel — отменить оформление заявки\n"
        "/start — показать это сообщение"
    )

    if is_admin_user:
        text += (
            "\n\n👑 Команды для админов:\n"
            "/orders [страница] — заявки\n"
            "/exportxlsx — выгрузка заявок в XLSX\n"
            "/exportcsv — выгрузка заявок в CSV\n"
            "/stats — статистика\n"
            "/version — текущая версия"
        )

    keyboard = [
        [
            InlineKeyboardButton("🛒 Каталог", callback_data="open_catalog"),
            InlineKeyboardButton("ℹ️ О компании", callback_data="open_about"),
        ],
        [
            InlineKeyboardButton("📞 Контакты", callback_data="open_contacts"),
            InlineKeyboardButton("🔎 Поиск", callback_data="open_search_hint"),
        ],
    ]
    await reply_to_update(update, text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "open_catalog":
        await show_typing(context, query.message.chat.id, 0.5)
        await show_catalog(update, context)

    elif query.data == "open_search_hint":
        await reply_to_update(update, "Введите запрос командой:\n/find castrol 1 л")

    elif query.data == "open_about":
        await about(update, context)

    elif query.data == "open_contacts":
        await contacts(update, context)


# ---------- Поиск ----------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()

async def find_oil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = norm(" ".join(context.args)) if context.args else ""
    if not user_query:
        await reply_to_update(update, "Использование: /find текст_поиска\nНапр.: /find castrol 1 л")
        return

    await show_typing(context, update.effective_chat.id, 0.5)

    results = []
    for oid, oil in oils.items():
        blob = norm(" ".join([
            oil.get("name", ""),
            oil.get("volume", ""),
            oil.get("description", ""),
            " ".join(oil.get("features", [])),
            oil.get("compatible", ""),
        ]))
        # терпим к пробелам/регистру
        if all(tok.replace(" ", "") in blob.replace(" ", "") for tok in user_query.split()):
            results.append((oid, oil))

    if not results:
        await reply_to_update(update, "Ничего не нашлось 🤷\nПопробуйте короче или по-другому.")
        return

    keyboard = [
        [InlineKeyboardButton(f"{oil['name']} ({oil['volume']})", callback_data=str(oid))]
        for oid, oil in results[:10]
    ]
    keyboard.append([InlineKeyboardButton("⬅ Назад в каталог", callback_data="back")])
    await reply_to_update(
        update,
        f"Найдено: {len(results)}. Показаны первые {min(len(results),10)}:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------- Каталог ----------

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # защита на пустой каталог
    if not oils:
        await reply_to_update(update, "Каталог временно пуст. Попробуйте позже или напишите нам: @shaba_v")
        return

    await show_typing(context, update.effective_chat.id, 0.6)

    keyboard = [
        [InlineKeyboardButton(f"{oil['name']} ({oil['volume']})", callback_data=str(oil_id))]
        for oil_id, oil in oils.items()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        if getattr(query.message, "photo", None):
            try:
                await query.delete_message()
            except Exception:
                logger.debug("Не удалось удалить фото при возврате в каталог")
            await safe_reply_text(query.message, "Выберите масло:", reply_markup=reply_markup)
        else:
            try:
                await query.edit_message_text("Выберите масло:", reply_markup=reply_markup)
            except Exception:
                await safe_reply_text(query.message, "Выберите масло:", reply_markup=reply_markup)
    else:
        await reply_to_update(update, "Выберите масло:", reply_markup=reply_markup)


async def show_oil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back":
        await show_catalog(update, context)
        return

    # Оставить заявку
    if data.startswith("order_"):
        oil_id = int(data.split("_")[1])
        oil = oils.get(oil_id)
        if not oil:
            await reply_to_update(update, "Товар больше не доступен. Откройте /catalog и выберите заново.")
            return

        await query.answer("Ок, оформим заявку. Отправьте контакт 👇", show_alert=False)
        text = (
            f"🛒 Вы выбрали:\n"
            f"{oil['name']} ({oil['volume']}) — {oil.get('price', 'цена не указана')} {oil.get('currency', '₽')}\n\n"
            "Отправьте ваш телефон одной кнопкой (рекомендуется) или введите контакт вручную.\n"
            "Можно отменить командой /cancel"
        )
        kb = [
            [KeyboardButton("📱 Отправить телефон", request_contact=True)],
            [KeyboardButton("Отмена /cancel")],
        ]
        await query.message.reply_text(
            text,
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        context.user_data["ordering"] = oil_id
        return

    # Показ карточки масла
    if data.isdigit():
        oil_id = int(data)
        oil = oils.get(oil_id)
        if not oil:
            await reply_to_update(update, "❌ Ошибка: товар не найден.")
            return

        await show_typing(context, update.effective_chat.id, 0.6)

        caption = (
            f"🔹 <b>{html.escape(oil['name'])}</b> ({html.escape(oil['volume'])})\n\n"
            f"{html.escape(oil['description'])}\n\n"
            f"💰 Цена: {html.escape(str(oil.get('price', 'не указана')))} {html.escape(oil.get('currency', '₽'))}\n\n"
            "Характеристики:\n" +
            "\n".join([f"• {html.escape(f)}" for f in oil["features"]]) +
            f"\n\nПодходит: {html.escape(oil['compatible'])}"
        )
        keyboard = [
            [InlineKeyboardButton("⬅ Назад в каталог", callback_data="back")],
            [InlineKeyboardButton("🛒 Оставить заявку", callback_data=f"order_{oil_id}")],
            [InlineKeyboardButton("📞 Связаться", url="https://t.me/shaba_v")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.delete_message()
        except Exception:
            pass

        await query.message.reply_photo(
            photo=oil["image"],
            caption=truncate(caption),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


# ---------- ОБРАБОТКА ЗАЯВОК ----------

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Путь 1: пользователь нажал «Отправить телефон»."""
    if "ordering" not in context.user_data:
        await reply_to_update(update, "Используйте /catalog чтобы выбрать масло.")
        return

    user = update.effective_user
    if not update.message.contact or not update.message.contact.phone_number:
        await reply_to_update(update, "Не получил номер. Можно отправить телефон кнопкой или написать контакт вручную.")
        return

    contact = update.message.contact.phone_number.strip()

    # Антиспам
    now = time.time()
    last = LAST_ORDER_AT.get(user.id)
    if last is not None:
        remain = ORDER_COOLDOWN_SEC - int(now - last)
        if remain > 0:
            await reply_to_update(update, f"⏳ Слишком часто. Повторите через {remain} сек.")
            return

    oil_id = context.user_data.get("ordering")
    oil = oils.get(oil_id)
    if not oil:
        await reply_to_update(
            update,
            "Товар больше не доступен. Откройте /catalog и выберите заново.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.pop("ordering", None)
        return

    username_visible = f"@{user.username}" if user.username else f"ID:{user.id}"
    logger.info("ORDER by %s (%s): %s %s / %s", user.id, user.username, oil['name'], oil['volume'], contact)

    order = {
        "user_id": user.id,
        "username": user.username,
        "oil": oil["name"],
        "volume": oil["volume"],
        "price": oil.get("price", "—"),
        "currency": oil.get("currency", "₽"),
        "contact": contact,
    }
    order_id = save_order_sql(order)

    await reply_to_update(
        update,
        f"✅ Заявка {order_id} создана!\n"
        f"Товар: {oil['name']} ({oil['volume']}) — {oil.get('price','—')} {oil.get('currency','₽')}\n"
        f"Контакт: {contact}\n"
        f"⏱️ Время: {datetime.now().strftime('%H:%M:%S')}",
        reply_markup=ReplyKeyboardRemove(),
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📩 Новая заявка {order_id}\n\n"
                    f"🛒 Товар: {oil['name']} ({oil['volume']})\n"
                    f"💰 Цена: {oil.get('price', '—')} {oil.get('currency', '₽')}\n"
                    f"👤 От: {username_visible}\n"
                    f"📞 Контакты: {contact}"
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить админу {admin_id}: {e}")

    LAST_ORDER_AT[user.id] = now
    context.user_data.pop("ordering", None)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Путь 2: пользователь ввёл контакт текстом."""
    user = update.effective_user
    text = update.message.text

    if "ordering" not in context.user_data:
        await reply_to_update(update, "Используйте /catalog чтобы выбрать масло.")
        return

    ok, norm_contact = validate_contact(text)
    if not ok:
        await reply_to_update(update, norm_contact)
        return
    contact = norm_contact

    now = time.time()
    last = LAST_ORDER_AT.get(user.id)
    if last is not None:
        remain = ORDER_COOLDOWN_SEC - int(now - last)
        if remain > 0:
            await reply_to_update(update, f"⏳ Слишком часто. Повторите через {remain} сек.")
            return

    oil_id = context.user_data.get("ordering")
    oil = oils.get(oil_id)
    if not oil:
        await reply_to_update(
            update,
            "Товар больше не доступен. Откройте /catalog и выберите заново.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.pop("ordering", None)
        return

    username_visible = f"@{user.username}" if user.username else f"ID:{user.id}"
    logger.info("ORDER by %s (%s): %s %s / %s", user.id, user.username, oil['name'], oil['volume'], contact)

    order = {
        "user_id": user.id,
        "username": user.username,
        "oil": oil["name"],
        "volume": oil["volume"],
        "price": oil.get("price", "—"),
        "currency": oil.get("currency", "₽"),
        "contact": contact,
    }
    order_id = save_order_sql(order)

    await reply_to_update(
        update,
        f"✅ Заявка {order_id} создана!\n"
        f"Товар: {oil['name']} ({oil['volume']}) — {oil.get('price','—')} {oil.get('currency','₽')}\n"
        f"Контакт: {contact}\n"
        f"⏱️ Время: {datetime.now().strftime('%H:%M:%S')}",
        reply_markup=ReplyKeyboardRemove(),
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📩 Новая заявка {order_id}\n\n"
                    f"🛒 Товар: {oil['name']} ({oil['volume']})\n"
                    f"💰 Цена: {oil.get('price', '—')} {oil.get('currency', '₽')}\n"
                    f"👤 От: {username_visible}\n"
                    f"📞 Контакты: {contact}"
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить админу {admin_id}: {e}")

    LAST_ORDER_AT[user.id] = now
    context.user_data.pop("ordering", None)


# ---------- /orders (только админы) ----------

async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await reply_to_update(update, f"⛔ У вас нет доступа. Ваш ID: {update.effective_user.id}")
        return

    # извлекаем страницу из "/orders N" или "/orders_N"
    page = 1
    args = context.args if hasattr(context, "args") else []
    if args:
        try:
            page = int(args[0])
        except ValueError:
            page = 1
    else:
        txt = (update.message.text or "").strip()
        if "_" in txt:
            try:
                page = int(txt.split("_", 1)[1])
            except ValueError:
                page = 1

    page = max(1, page)
    page_size = 10

    rows, total = fetch_orders_page(page=page, page_size=page_size)
    if total == 0:
        await reply_to_update(update, "📭 Заявок пока нет.")
        return

    total_pages = (total + page_size - 1) // page_size
    if not rows:
        await reply_to_update(update, f"Страница {page}/{total_pages} пуста.")
        return

    lines = [f"📋 Список заявок — стр. {page}/{total_pages}\n"]
    for (oid, user_id, username, oil, volume, price, currency, contact, created_at) in rows:
        username_visible = f"@{username}" if username else f"ID:{user_id}"
        lines.append(
            f"#{oid:03} — {oil} ({volume})\n"
            f"💰 Цена: {price or '—'} {currency or '₽'}\n"
            f"👤 От: {username_visible}\n"
            f"📞 Контакты: {contact}\n"
            f"🕒 {created_at}\n"
        )

    hints = []
    if page > 1:
        hints.append(f"/orders_{page-1} ← предыдущая")
    if page < total_pages:
        hints.append(f"/orders_{page+1} → следующая")

    msg = "\n".join(lines + (["\n" + " | ".join(hints)] if hints else []))
    await reply_to_update(update, msg)


# ---------- ЭКСПОРТЫ (только админы) ----------

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply_to_update(update, "⛔ Команда доступна только администраторам.")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, created_at, user_id, username, oil, volume, price, currency, contact
            FROM orders
            ORDER BY id ASC
            """
        )
        rows = c.fetchall()
    finally:
        conn.close()

    if not rows:
        await reply_to_update(update, "📭 Заявок пока нет.")
        return

    txt = io.StringIO()
    writer = csv.writer(txt)
    writer.writerow(["id", "created_at", "user_id", "username", "oil", "volume", "price", "currency", "contact"])
    writer.writerows(rows)

    # UTF-8 BOM, чтобы Excel корректно открыл кириллицу
    bio = io.BytesIO(("\ufeff" + txt.getvalue()).encode("utf-8"))
    bio.seek(0)

    filename = f"orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=bio,
            filename=filename,
            caption="Экспорт заявок (CSV)",
        )
    except Exception as e:
        logger.exception("Не удалось отправить файл экспорта CSV: %s", e)
        await reply_to_update(update, "⚠️ Не удалось отправить CSV. Попробуйте ещё раз позже.")


async def export_xlsx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await reply_to_update(update, "⛔ Команда доступна только администраторам.")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, created_at, user_id, username, oil, volume, price, currency, contact
            FROM orders
            ORDER BY id ASC
            """
        )
        rows = c.fetchall()
    finally:
        conn.close()

    if not rows:
        await reply_to_update(update, "📭 Заявок пока нет.")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "orders"
    headers = ["id", "created_at", "user_id", "username", "oil", "volume", "price", "currency", "contact"]
    ws.append(headers)
    for r in rows:
        ws.append(list(r))

    # автоширина колонок
    for col in ws.columns:
        max_len = max(len(str(cell.value)) if cell.value is not None else 0 for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(10, max_len + 2), 50)

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)

    filename = f"orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=bio,
            filename=filename,
            caption="Экспорт заявок (XLSX)",
        )
    except Exception as e:
        logger.exception("Не удалось отправить файл экспорта XLSX: %s", e)
        await reply_to_update(update, "⚠️ Не удалось отправить XLSX. Попробуйте ещё раз позже.")


# ---------- О нас / Контакты ----------

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to_update(
        update,
        "🏪 О нас\n\n"
        "Мы занимаемся продажей оригинальных масел для электромобилей и гибридных автомобилей.\n"
        "🔧 Только проверенные бренды.\n\n"
        "📍 Адрес: Екатеринбург, ул. Серафимы Дерябиной, д. 18а\n"
        "🕘 Время работы: 9:00 — 21:00",
    )


async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to_update(
        update,
        "📞 Наши контакты:\n\n"
        "Телефон: +7 (999) 559-39-17 - Андрей, +7 (953) 046-36-54 - Влад\n"
        "Telegram: @shaba_v, @andrey_matveev\n"
        "Авито: https://m.avito.ru/brands/2c07f021e144d3169204cd556d312cdf/items/all",
    )


# ---------- CANCEL / UNKNOWN ----------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет оформление заявки и убирает клавиатуру."""
    if "ordering" in context.user_data:
        context.user_data.pop("ordering", None)
        await reply_to_update(
            update,
            "❌ Оформление заявки отменено. Напишите /catalog чтобы выбрать масло снова.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await reply_to_update(
            update,
            "Нечего отменять. Напишите /catalog чтобы открыть каталог.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to_update(update, "Я не знаю эту команду. Попробуйте /start или /catalog.")


# ---------- Меню команд (красота) ----------

async def set_bot_commands(app: Application):
    # для всех
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "стартовое сообщение"),
            BotCommand("catalog", "открыть каталог"),
            BotCommand("about", "о компании"),
            BotCommand("contacts", "контакты"),
            BotCommand("find", "поиск по каталогу"),
            BotCommand("cancel", "отменить оформление"),
        ], scope=BotCommandScopeDefault())
    except Exception as e:
        logger.warning("Не удалось установить общие команды: %s", e)

    # для админов — персональные команды в их чатах
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands([
                BotCommand("orders", "заявки (/orders 2 — страница 2)"),
                BotCommand("exportcsv", "выгрузка заявок (CSV)"),
                BotCommand("exportxlsx", "выгрузка заявок (XLSX)"),
                BotCommand("stats", "статистика"),
                BotCommand("version", "версия бота"),
            ], scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning("Не удалось задать команды для админа %s: %s", admin_id, e)


# ---------- Главная ----------

def main():
    # подготовим БД + миграция
    init_db()
    try:
        migrate_json_to_sql()
    except Exception as e:
        logger.warning("Миграция пропущена/ошибка: %s", e)

    # стабильный httpx-клиент
    request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=60,
        write_timeout=60,
        connect_timeout=15,
        pool_timeout=15,
    )

    app = Application.builder().token(TOKEN).request(request).build()

    # После инициализации — задать меню команд
    app.post_init = set_bot_commands

    # фильтр "только админам"
    admin_filter = F.User(user_id=ADMIN_IDS)

    # --- Команды ---
    app.add_handler(CommandHandler("find", find_oil))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", show_catalog))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("contacts", contacts))
    app.add_handler(CommandHandler("cancel", cancel))

    # Админские
    app.add_handler(CommandHandler("orders", show_orders, filters=admin_filter))
    app.add_handler(MessageHandler(F.Regex(r"^/orders_\d+$") & admin_filter, show_orders))
    app.add_handler(CommandHandler("exportcsv", export_csv, filters=admin_filter))
    app.add_handler(CommandHandler("exportxcsv", export_csv, filters=admin_filter))  # алиас
    app.add_handler(CommandHandler("exportxlsx", export_xlsx, filters=admin_filter))
    app.add_handler(CommandHandler("stats", stats, filters=admin_filter))
    app.add_handler(CommandHandler("version", version_cmd, filters=admin_filter))

    # --- Кнопки (callback) ---
    # Стартовое меню: ловим все четыре кнопки
    app.add_handler(CallbackQueryHandler(
        handle_start_button,
        pattern=r"^(open_catalog|open_search_hint|open_about|open_contacts)$"
    ))
    # Карточки каталога: back | order_<id> | <id>
    app.add_handler(CallbackQueryHandler(show_oil, pattern=r"^(back|order_\d+|\d+)$"))

    # --- Сообщения ---
    app.add_handler(MessageHandler(F.CONTACT, handle_contact))
    app.add_handler(MessageHandler(F.TEXT & ~F.COMMAND, handle_message))
    # Unknown command — в самом конце
    app.add_handler(MessageHandler(F.COMMAND, unknown_command))

    # --- Ошибки ---
    app.add_error_handler(error_handler)

    logger.info("Бот запущен... 🚀")

    # увеличенные таймауты long-polling
    app.run_polling(
        timeout=60,
        poll_interval=1.5,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
