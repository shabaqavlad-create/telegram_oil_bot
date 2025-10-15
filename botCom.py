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
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.ext import filters as tg_filters
from telegram.request import HTTPXRequest
from openpyxl import Workbook

from catalog import oils  # dict: id -> {name, volume, description, features, compatible, price, currency, image}

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

# --- Пути к данным ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")
DB_PATH = os.path.join(BASE_DIR, "orders.db")

# --- Антиспам ---
ORDER_COOLDOWN_SEC = 20
LAST_ORDER_AT: dict[int, float] = {}  # user_id -> ts последней УСПЕШНОЙ заявки


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
    """Создаёт таблицы и индексы при необходимости."""
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id   ON orders(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_oil       ON orders(oil)")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS oil_overrides (
                oil_id   INTEGER PRIMARY KEY,
                price    TEXT    NULL,   -- NULL => берём цену из catalog.py
                stock    INTEGER NULL    -- NULL => безлимит; 0..N => остаток
            )
            """
        )
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


# ---------- OVERRIDES (цена/остаток) ----------
def get_override(oil_id: int):
    """Возвращает (price_override:str|None, stock:int|None) для oil_id."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT price, stock FROM oil_overrides WHERE oil_id = ?", (oil_id,))
        row = c.fetchone()
        if not row:
            return None, None
        return row[0], row[1]
    finally:
        conn.close()


def upsert_price(oil_id: int, price_text: str | None):
    """Устанавливает override-цену (или None чтобы сбросить к каталогу)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO oil_overrides(oil_id, price, stock)
            VALUES(?, ?, (SELECT stock FROM oil_overrides WHERE oil_id=?))
            ON CONFLICT(oil_id) DO UPDATE SET price=excluded.price
            """,
            (oil_id, price_text, oil_id),
        )
    finally:
        conn.close()


def upsert_stock(oil_id: int, stock_value: int | None):
    """Устанавливает override-остаток (None => безлимит)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO oil_overrides(oil_id, price, stock)
            VALUES(?, (SELECT price FROM oil_overrides WHERE oil_id=?), ?)
            ON CONFLICT(oil_id) DO UPDATE SET stock=excluded.stock
            """,
            (oil_id, oil_id, stock_value),
        )
    finally:
        conn.close()


def get_effective_oil(oil_id: int) -> dict | None:
    """Возвращает словарь масла с учётом override-цены и stock."""
    base = oils.get(oil_id)
    if not base:
        return None
    price_ovr, stock_ovr = get_override(oil_id)
    out = dict(base)  # копия
    if price_ovr is not None and str(price_ovr).strip() != "":
        out["price"] = price_ovr
    out["stock"] = stock_ovr  # None | int
    return out


# ---------- УТИЛИТЫ ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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


async def safe_reply_text(target, text: str, parse_mode: str | None = None, **kwargs):
    """
    Безопасно отвечает в тот же чат (target: Message или CallbackQuery.message).
    """
    if target is None:
        logger.warning("safe_reply_text: target=None")
        return None
    try:
        return await target.reply_text(text, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        logger.warning("reply_text упал (%s). Пробуем без parse_mode…", e)
        try:
            return await target.reply_text(text, **{k: v for k, v in kwargs.items() if k != "parse_mode"})
        except Exception:
            logger.exception("reply_text повторно упал")
    return None


async def show_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, seconds: float = 0.8):
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        return
    if seconds > 0:
        import asyncio
        await asyncio.sleep(seconds)


# ---------- СТАТИСТИКА ----------
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


# ---------- КОМАНДЫ ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
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

    await update.message.reply_text("\n".join(lines))


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    path = os.path.join(BASE_DIR, "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as f:
            await update.message.reply_text(f"Версия: {f.read().strip()}")
    except FileNotFoundError:
        await update.message.reply_text("VERSION не найден.")


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
            "/version — текущая версия\n"
            "/setprice — изменить цену\n"
            "/setstock — изменить остаток\n"
            "/stock — сводка остатков"
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

    # ✅ Админ-ряд кнопок
    if is_admin_user:
        keyboard.append([
            InlineKeyboardButton("✏️ Изменить цену", callback_data="admin_setprice_help"),
            InlineKeyboardButton("📦 Изменить остаток", callback_data="admin_setstock_help"),
        ])
        keyboard.append([
            InlineKeyboardButton("📋 Остатки", callback_data="admin_stock_summary"),
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await safe_reply_text(update.message, text, reply_markup=reply_markup)


# --- обработка кнопок стартового меню ---
async def handle_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    is_admin_user = uid in ADMIN_IDS

    if query.data == "open_catalog":
        await show_typing(context, query.message.chat.id, 0.5)
        await show_catalog(update, context)

    elif query.data == "open_search_hint":
        await query.message.reply_text("Введите запрос командой:\n/find castrol 1 л")

    elif query.data == "open_about":
        await about(update, context)

    elif query.data == "open_contacts":
        await contacts(update, context)

    elif query.data == "noop":
        await query.answer("Нет в наличии", show_alert=False)

    # ---------- АДМИН-КНОПКИ ----------
    elif query.data == "admin_setprice_help":
        if not is_admin_user:
            await query.answer("Только для админов", show_alert=True)
            return
        # небольшая подсказка + топ-10 ID для удобства
        ids_preview = "\n".join([f"{oid}: {oil['name']}" for oid, oil in list(oils.items())[:10]])
        await query.message.reply_text(
            "✏️ Как изменить цену:\n"
            "• /setprice <oil_id> <цена>\n"
            "• чтобы сбросить к каталогу: /setprice <oil_id> reset\n\n"
            "Примеры:\n"
            "• /setprice 3 1990\n"
            "• /setprice 3 reset\n\n"
            "Первые позиции (id:name):\n" + (ids_preview if ids_preview else "—")
        )

    elif query.data == "admin_setstock_help":
        if not is_admin_user:
            await query.answer("Только для админов", show_alert=True)
            return
        ids_preview = "\n".join([f"{oid}: {oil['name']}" for oid, oil in list(oils.items())[:10]])
        await query.message.reply_text(
            "📦 Как изменить остаток:\n"
            "• /setstock <oil_id> <кол-во>\n"
            "• безлимит: /setstock <oil_id> inf (или reset)\n\n"
            "Примеры:\n"
            "• /setstock 3 15\n"
            "• /setstock 3 inf\n\n"
            "Первые позиции (id:name):\n" + (ids_preview if ids_preview else "—")
        )

    elif query.data == "admin_stock_summary":
        if not is_admin_user:
            await query.answer("Только для админов", show_alert=True)
            return
        # просто вызываем сводку, но убедимся, что она умеет отвечать в callback-чате
        await stock_cmd(update, context)


# --- Поиск по каталогу ---
def _norm(s: str) -> str:
    s = s.lower()
    s = s.replace("литра", "л").replace("литр", "л").replace("л.", "л")
    s = re.sub(r"[\s\-\(\)]", "", s)
    return s

async def find_oil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args) if context.args else ""
    if not raw.strip():
        await update.message.reply_text("Использование: /find текст_поиска\nНапр.: /find castrol 1 л")
        return

    await show_typing(context, update.effective_chat.id, 0.5)

    q_tokens = [_norm(t) for t in raw.split() if t.strip()]
    results = []
    for oid, oil in oils.items():
        blob = " ".join([
            oil.get("name", ""),
            oil.get("volume", ""),
            oil.get("description", ""),
            " ".join(oil.get("features", [])),
            oil.get("compatible", "")
        ])
        blob_n = _norm(blob)
        if all(t in blob_n for t in q_tokens):
            results.append((oid, oil))

    if not results:
        await update.message.reply_text("Ничего не нашлось 🤷\nПопробуйте короче или по-другому.")
        return

    keyboard = [
        [InlineKeyboardButton(f"{oil['name']} ({oil['volume']})", callback_data=str(oid))]
        for oid, oil in results[:10]
    ]
    keyboard.append([InlineKeyboardButton("⬅ Назад в каталог", callback_data="back")])
    await update.message.reply_text(
        f"Найдено: {len(results)}. Показаны первые {min(len(results),10)}:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ---------- КАТАЛОГ / КНОПКИ ----------
async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_msg = update.callback_query.message if update.callback_query else update.message
    if not oils:
        await safe_reply_text(
            target_msg,
            "Каталог временно пуст. Попробуйте позже или напишите нам: @shaba_v"
        )
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
        await safe_reply_text(update.message, "Выберите масло:", reply_markup=reply_markup)


async def show_oil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back":
        await show_catalog(update, context)
        return

    if data.startswith("order_"):
        oil_id = int(data.split("_")[1])
        eff = get_effective_oil(oil_id)
        if not eff:
            await query.message.reply_text("Товар больше не доступен. Откройте /catalog и выберите заново.")
            return

        st = eff.get("stock")
        if st is not None and st == 0:
            await query.message.reply_text("К сожалению, товара нет в наличии. Попробуйте выбрать другой.")
            return

        await query.answer("Ок, оформим заявку. Отправьте контакт 👇", show_alert=False)
        text = (
            f"🛒 Вы выбрали:\n"
            f"{eff['name']} ({eff['volume']}) — {eff.get('price', 'цена не указана')} {eff.get('currency', '₽')}\n\n"
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

    if data.isdigit():
        oil_id = int(data)
        eff = get_effective_oil(oil_id)
        if not eff:
            await safe_reply_text(query.message, "❌ Ошибка: товар не найден.")
            return

        await show_typing(context, update.effective_chat.id, 0.6)

        price = eff.get("price", "не указана")
        currency = eff.get("currency", "₽")
        stock = eff.get("stock")  # None | int
        if stock is None:
            stock_line = "В наличии: есть"
        elif stock == 0:
            stock_line = "В наличии: нет"
        else:
            stock_line = f"В наличии: {stock} шт."

        caption = (
            f"🔹 <b>{html.escape(eff['name'])}</b> ({html.escape(eff['volume'])})\n\n"
            f"{html.escape(eff['description'])}\n\n"
            f"💰 Цена: {html.escape(str(price))} {html.escape(currency)}\n"
            f"📦 {stock_line}\n\n"
            "Характеристики:\n" +
            "\n".join([f"• {html.escape(f)}" for f in eff["features"]]) +
            f"\n\nПодходит: {html.escape(eff['compatible'])}"
        )

        buttons = [[InlineKeyboardButton("⬅ Назад в каталог", callback_data="back")]]
        if stock == 0:
            buttons.append([InlineKeyboardButton("❌ Нет в наличии", callback_data="noop")])
        else:
            buttons.append([InlineKeyboardButton("🛒 Оставить заявку", callback_data=f"order_{oil_id}")])
        buttons.append([InlineKeyboardButton("📞 Связаться", url="https://t.me/shaba_v")])
        reply_markup = InlineKeyboardMarkup(buttons)

        try:
            await query.delete_message()
        except Exception:
            pass

        await query.message.reply_photo(
            photo=eff["image"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


# ---------- ОБЩИЙ ХЕЛПЕР ОФОРМЛЕНИЯ ЗАЯВКИ ----------
async def _create_order_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE, contact: str):
    user = update.effective_user

    # антиспам
    now = time.time()
    last = LAST_ORDER_AT.get(user.id)
    if last is not None:
        remain = ORDER_COOLDOWN_SEC - int(now - last)
        if remain > 0:
            await update.message.reply_text(f"⏳ Слишком часто. Повторите через {remain} сек.")
            return

    if "ordering" not in context.user_data:
        await update.message.reply_text("Используйте /catalog чтобы выбрать масло.")
        return

    oil_id = context.user_data.get("ordering")
    eff = get_effective_oil(oil_id)
    if not eff:
        await update.message.reply_text(
            "Товар больше не доступен. Откройте /catalog и выберите заново.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.pop("ordering", None)
        return

    st = eff.get("stock")
    # не списываем остаток — только блокируем, если 0
    if st is not None and st == 0:
        await update.message.reply_text(
            "К сожалению, товара нет в наличии. Попробуйте выбрать другой.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.pop("ordering", None)
        return

    username_visible = f"@{user.username}" if user.username else f"ID:{user.id}"
    logger.info("ORDER by %s (%s): %s %s / %s", user.id, user.username, eff['name'], eff['volume'], contact)

    order = {
        "user_id": user.id,
        "username": user.username,
        "oil": eff["name"],
        "volume": eff["volume"],
        "price": eff.get("price", "—"),
        "currency": eff.get("currency", "₽"),
        "contact": contact,
    }
    order_id = save_order_sql(order)

    await update.message.reply_text(
        f"✅ Заявка {order_id} создана!\n"
        f"Товар: {eff['name']} ({eff['volume']}) — {eff.get('price','—')} {eff.get('currency','₽')}\n"
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
                    f"🛒 Товар: {eff['name']} ({eff['volume']})\n"
                    f"💰 Цена: {eff.get('price', '—')} {eff.get('currency', '₽')}\n"
                    f"👤 От: {username_visible}\n"
                    f"📞 Контакты: {contact}"
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить админу {admin_id}: {e}")

    LAST_ORDER_AT[user.id] = now
    context.user_data.pop("ordering", None)


# ---------- ОБРАБОТКА ЗАЯВОК ----------
async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Путь 1: пользователь нажал «Отправить телефон»."""
    if not update.message.contact or not update.message.contact.phone_number:
        await update.message.reply_text("Не получил номер. Можно отправить телефон кнопкой или написать контакт вручную.")
        return
    await _create_order_for_user(update, context, update.message.contact.phone_number.strip())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Путь 2: пользователь ввёл контакт текстом."""
    ok, norm = validate_contact(update.message.text)
    if not ok:
        await update.message.reply_text(norm)
        return
    await _create_order_for_user(update, context, norm)


# ---------- /orders (только админы) с пагинацией ----------

async def _render_orders_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    """Рендерит список заявок с инлайн-кнопками пагинации.
       Работает как из команды /orders, так и из callback-кнопок."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        # безопасно игнорируем не-админа
        if getattr(update, "callback_query", None):
            await update.callback_query.answer("Нет доступа", show_alert=False)
        else:
            await safe_reply_text(update.message, f"⛔ У вас нет доступа. Ваш ID: {user.id}")
        return

    page = max(1, page)
    page_size = 10
    rows, total = fetch_orders_page(page=page, page_size=page_size)

    if total == 0:
        text = "📭 Заявок пока нет."
        markup = None
    else:
        total_pages = (total + page_size - 1) // page_size
        # если вылезли за пределы — нормализуем
        if page > total_pages:
            page = total_pages
            rows, total = fetch_orders_page(page=page, page_size=page_size)

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
        text = "\n".join(lines)

        # инлайн-кнопки пагинации
        buttons = []
        row = []
        if page > 1:
            row.append(InlineKeyboardButton("⬅ Назад", callback_data=f"orders_page_{page-1}"))
        if page < total_pages:
            row.append(InlineKeyboardButton("Вперёд ➡", callback_data=f"orders_page_{page+1}"))
        if row:
            buttons.append(row)
        markup = InlineKeyboardMarkup(buttons) if buttons else None

    if getattr(update, "callback_query", None):
        q = update.callback_query
        try:
            await q.answer()
        except Exception:
            pass
        # пробуем редактировать, если не получится — отправим новое сообщение
        try:
            await q.edit_message_text(text=text, reply_markup=markup)
        except Exception:
            await safe_reply_text(q.message, text, reply_markup=markup)
    else:
        await safe_reply_text(update.message, text, reply_markup=markup)

async def orders_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        page = int(update.callback_query.data.split("_")[-1])
    except Exception:
        page = 1
    await _render_orders_page(update, context, page)
async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /orders [page] и алиас /orders_N — рендер страницы заявок."""
    page = 1
    # /orders 3
    if context.args:
        try:
            page = int(context.args[0])
        except ValueError:
            page = 1
    else:
        # /orders_3
        txt = (update.message.text or "").strip() if update.message else ""
        if "_" in txt:
            try:
                page = int(txt.split("_", 1)[1])
            except ValueError:
                page = 1

    await _render_orders_page(update, context, page)

# ---------- ЭКСПОРТЫ (только админы) ----------
async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
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
        await update.message.reply_text("📭 Заявок пока нет.")
        return

    txt = io.StringIO()
    writer = csv.writer(txt)
    writer.writerow(["id","created_at","user_id","username","oil","volume","price","currency","contact"])
    writer.writerows(rows)

    bio = io.BytesIO(("\ufeff" + txt.getvalue()).encode("utf-8"))  # UTF-8 BOM для Excel
    bio.seek(0)

    filename = f"orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=bio,
        filename=filename,
        caption="Экспорт заявок (CSV)",
    )


async def export_xlsx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Команда доступна только администраторам.")
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
        await update.message.reply_text("📭 Заявок пока нет.")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "orders"
    headers = ["id","created_at","user_id","username","oil","volume","price","currency","contact"]
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
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=bio,
        filename=filename,
        caption="Экспорт заявок (XLSX)",
    )


# ---------- О нас / Контакты ----------
async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_msg = update.callback_query.message if update.callback_query else update.message
    await safe_reply_text(
        target_msg,
        "🏪 О нас\n\n"
        "Мы занимаемся продажей оригинальных масел для электромобилей и гибридных автомобилей.\n"
        "🔧 Только проверенные бренды.\n\n"
        "📍 Адрес: Екатеринбург, ул. Серафимы Дерябиной, д. 18а\n"
        "🕘 Время работы: 9:00 — 21:00",
    )

async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_msg = update.callback_query.message if update.callback_query else update.message
    await safe_reply_text(
        target_msg,
        "📞 Наши контакты:\n\n"
        "Телефон: +7 (999) 559-39-17 — Андрей, +7 (953) 046-36-54 — Влад\n"
        "Telegram: @shaba_v, @andrey_matveev\n"
        "Авито: https://m.avito.ru/brands/2c07f021e144d3169204cd556d312cdf/items/all",
    )


# ---------- CANCEL ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "ordering" in context.user_data:
        context.user_data.pop("ordering", None)
        await update.message.reply_text(
            "❌ Оформление заявки отменено. Напишите /catalog чтобы выбрать масло снова.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await update.message.reply_text(
            "Нечего отменять. Напишите /catalog чтобы открыть каталог.",
            reply_markup=ReplyKeyboardRemove(),
        )


# ---------- UNKNOWN COMMAND ----------
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я не знаю эту команду. Попробуйте /start или откройте каталог кнопкой.")


# ---------- ADMIN: SETPRICE / SETSTOCK / STOCK ----------
async def setprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /setprice <oil_id> <цена|reset>\nПримеры: /setprice 3 1990  |  /setprice 3 reset")
        return
    try:
        oil_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("oil_id должен быть числом.")
        return
    if oil_id not in oils:
        await update.message.reply_text("Товар с таким id не найден в catalog.py")
        return

    price_arg = " ".join(context.args[1:]).strip()
    if price_arg.lower() in ("reset", "none", "null", "-"):
        upsert_price(oil_id, None)
        await update.message.reply_text(f"✅ Цена для {oils[oil_id]['name']} сброшена к каталогу.")
    else:
        upsert_price(oil_id, price_arg)
        await update.message.reply_text(f"✅ Цена для {oils[oil_id]['name']} установлена: {price_arg}")


async def setstock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /setstock <oil_id> <кол-во|inf|reset>\nПримеры: /setstock 3 15  |  /setstock 3 inf")
        return
    try:
        oil_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("oil_id должен быть числом.")
        return
    if oil_id not in oils:
        await update.message.reply_text("Товар с таким id не найден в catalog.py")
        return

    qty_arg = context.args[1].strip().lower()
    if qty_arg in ("inf", "reset", "none", "null", "-1"):
        upsert_stock(oil_id, None)  # безлимит
        await update.message.reply_text(f"✅ Остаток для {oils[oil_id]['name']} установлен как безлимит.")
    else:
        try:
            qty = int(qty_arg)
            if qty < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Количество должно быть целым числом ≥ 0 или 'inf' для безлимита.")
            return
        upsert_stock(oil_id, qty)
        await update.message.reply_text(f"✅ Остаток для {oils[oil_id]['name']} установлен: {qty}")


async def stock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сводка остатков по всем позициям (для админов). Работает и из callback, и из /stock."""
    if update.effective_user.id not in ADMIN_IDS:
        return

    target_msg = update.callback_query.message if update.callback_query else update.message

    lines = ["📦 Остатки:"]
    for oid, base in oils.items():
        eff = get_effective_oil(oid)
        stock = eff.get("stock") if eff else None
        stock_text = "∞" if stock is None else str(stock)
        price_text = eff.get("price") if eff else base.get("price")
        lines.append(f"{oid:>2}: {base['name']} — остаток: {stock_text}; цена: {price_text} {base.get('currency','₽')}")

    await safe_reply_text(target_msg, "\n".join(lines))

async def set_bot_commands(application):
    """Заполняет кнопку 'Меню' (команды) по-разному: всем и отдельно админам."""
    base_commands = [
        BotCommand("start", "Показать меню"),
        BotCommand("catalog", "Открыть каталог"),
        BotCommand("find", "Поиск по каталогу"),
        BotCommand("about", "О компании"),
        BotCommand("contacts", "Контакты"),
        BotCommand("cancel", "Отменить оформление заявки"),
    ]

    admin_only = [
        BotCommand("orders", "Просмотр заявок"),
        BotCommand("exportxlsx", "Экспорт XLSX"),
        BotCommand("exportcsv", "Экспорт CSV"),
        BotCommand("stats", "Статистика"),
        BotCommand("version", "Версия бота"),
        BotCommand("setprice", "Изменить цену"),
        BotCommand("setstock", "Изменить остаток"),
        BotCommand("stock", "Сводка остатков"),
    ]

    # 1) Команды по умолчанию — увидят все в любом чате
    await application.bot.set_my_commands(base_commands, scope=BotCommandScopeDefault())

    # 2) Для каждого админа — расширенный набор в ЛС с ботом
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(base_commands + admin_only,
                                                 scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning("Не удалось установить команды для админа %s: %s", admin_id, e)

# ---------- Главная ----------
def main():
    # подготовим БД + миграция
    init_db()
    try:
        migrate_json_to_sql()
    except Exception as e:
        logger.warning("Миграция пропущена/ошибка: %s", e)

    # httpx-клиент
    request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=60,
        write_timeout=60,
        connect_timeout=15,
        pool_timeout=15,
    )

    # post_init для установки меню-команд
    async def _post_init(app: Application):
        await set_bot_commands(app)

    app = (
        Application
        .builder()
        .token(TOKEN)
        .request(request)
        .post_init(_post_init)   # <-- Вот так правильно
        .build()
    )

    # фильтр "только админам"
    admin_filter = tg_filters.User(user_id=ADMIN_IDS)

    # --- Команды (пользовательские) ---
    app.add_handler(CommandHandler("find", find_oil))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", show_catalog))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("contacts", contacts))
    app.add_handler(CommandHandler("cancel", cancel))

    # --- Команды (админские) ---
    app.add_handler(CommandHandler("orders", show_orders, filters=admin_filter))
    app.add_handler(MessageHandler(tg_filters.Regex(r"^/orders_\d+$") & admin_filter, show_orders))
    app.add_handler(CommandHandler("exportcsv", export_csv, filters=admin_filter))
    app.add_handler(CommandHandler("exportxcsv", export_csv, filters=admin_filter))  # алиас
    app.add_handler(CommandHandler("exportxlsx", export_xlsx, filters=admin_filter))
    app.add_handler(CommandHandler("stats", stats, filters=admin_filter))
    app.add_handler(CommandHandler("version", version_cmd, filters=admin_filter))
    app.add_handler(CommandHandler("setprice", setprice_cmd, filters=admin_filter))
    app.add_handler(CommandHandler("setstock", setstock_cmd, filters=admin_filter))
    app.add_handler(CommandHandler("stock", stock_cmd, filters=admin_filter))

    # --- Кнопки (callback) ---
    app.add_handler(CallbackQueryHandler(
        handle_start_button,
        pattern=r"^(open_catalog|open_search_hint|open_about|open_contacts|noop|admin_setprice_help|admin_setstock_help|admin_stock_summary)$"
    ))
    app.add_handler(CallbackQueryHandler(show_oil, pattern=r"^(back|order_\d+|\d+)$"))
    app.add_handler(CallbackQueryHandler(orders_page_cb, pattern=r"^orders_page_\d+$"))

    # --- Сообщения ---
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))  # в самом конце

    # --- Ошибки ---
    app.add_error_handler(error_handler)

    logger.info("Бот запущен... 🚀")

    # Запуск
    app.run_polling(
        timeout=60,
        poll_interval=1.5,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
