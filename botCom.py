# --- Импорты ---
import os
import json
import logging
import sqlite3
import re
import time
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
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

ADMIN_IDS = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip().isdigit()]

# --- Пути к данным ---
ORDERS_FILE = "orders.json"   # старый формат — на случай миграции
DB_PATH = "orders.db"         # новый SQLite

# --- Антиспам: кулдаун на отправку заявки (в секундах) ---
ORDER_COOLDOWN_SEC = 30
LAST_ORDER_AT: dict[int, float] = {}  # user_id -> ts последней успешной заявки


# ---------- БАЗА ДАННЫХ (SQLite) ----------
def init_db():
    """Создаёт таблицу orders при необходимости."""
    conn = sqlite3.connect(DB_PATH)
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
        conn.commit()
    finally:
        conn.close()


def save_order_sql(order: dict) -> str:
    """
    Сохраняет заявку в SQLite.
    Возвращает красивый код заказа вида #001.
    """
    conn = sqlite3.connect(DB_PATH)
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
        conn.commit()
        row_id = c.lastrowid
        return f"#{row_id:03}"
    finally:
        conn.close()


def fetch_last_orders(limit: int = 10):
    """Возвращает последние N заявок (списком кортежей)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, user_id, username, oil, volume, price, currency, contact, created_at
            FROM orders
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return c.fetchall()
    finally:
        conn.close()


def fetch_orders_page(page: int, page_size: int = 10):
    """
    Возвращает (rows, total_count) для пагинации.
    rows — список кортежей как в fetch_last_orders (но в прямом порядке по id DESC).
    """
    page = max(1, page)
    offset = (page - 1) * page_size
    conn = sqlite3.connect(DB_PATH)
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
    """Пуста ли таблица orders (или её нет)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
        if not c.fetchone():
            return True
        c.execute("SELECT COUNT(*) FROM orders")
        count = c.fetchone()[0]
        return count == 0
    finally:
        conn.close()


def migrate_json_to_sql():
    """
    Разовая миграция из orders.json в SQLite (если таблица пуста и файл существует).
    Ставит цену/валюту в '—'/'₽', если в JSON их нет.
    """
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
    conn = sqlite3.connect(DB_PATH)
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
        conn.commit()
        logger.info("Миграция завершена, импортировано: %d", inserted)
    finally:
        conn.close()


# ---------- ВАЛИДАЦИЯ КОНТАКТА ----------
PHONE_RE = re.compile(
    r"""^\s*
        (?:
            (\+?\d[\d\-\s\(\)]{8,}\d)      # международный/российский телефон
          |
            (@[A-Za-z0-9_]{5,})            # Telegram username
          |
            (https?://t\.me/[A-Za-z0-9_]+) # ссылка на t.me
        )
        \s*$""",
    re.VERBOSE,
)

def validate_contact(text: str) -> tuple[bool, str | None]:
    """
    Простейшая проверка контакта.
    Возвращает (ok, normalized_or_error).
    """
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
    # нормализация — просто обрезаем пробелы
    return True, text.strip()


# ---------- УТИЛИТЫ ОТПРАВКИ ----------
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
    try:
        return await target.reply_text(text, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        logger.warning("reply_text упал (%s). Пробуем без parse_mode…", e)
        try:
            return await target.reply_text(text, **{k: v for k, v in kwargs.items() if k != "parse_mode"})
        except Exception:
            logger.exception("reply_text повторно упал")
    return None


# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "Привет! 👋\n"
        "Я бот-магазин масел для электромобилей и гибридов.\n\n"
        "📌 Команды:\n"
        "/catalog — открыть каталог\n"
        "/about — о компании\n"
        "/contacts — контакты\n"
        "/orders [страница] — заявки (для админов)\n"
        "/cancel — отменить оформление заявки\n"
        "/start — показать это сообщение",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "ordering" in context.user_data:
        del context.user_data["ordering"]
        await safe_reply_text(update.message, "❌ Оформление заявки отменено. Напишите /catalog чтобы выбрать масло снова.")
    else:
        await safe_reply_text(update.message, "Нечего отменять. Напишите /catalog чтобы открыть каталог.")


# ---------- КАТАЛОГ / КНОПКИ ----------
async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        oil = oils[oil_id]
        text = (
            f"🛒 Вы выбрали:\n"
            f"{oil['name']} ({oil['volume']}) — {oil.get('price', 'цена не указана')} {oil.get('currency', '₽')}\n\n"
            "Напишите, пожалуйста, свои контактные данные (телефон или Telegram), "
            "и я передам заявку администратору.\n\n"
            "Можно отменить командой /cancel"
        )
        await safe_reply_text(query.message, text)
        context.user_data["ordering"] = oil_id
        return

    if data.isdigit():
        oil_id = int(data)
        if oil_id not in oils:
            await safe_reply_text(query.message, "❌ Ошибка: товар не найден.")
            return

        oil = oils[oil_id]
        caption = (
            f"🔹 *{oil['name']}* ({oil['volume']})\n\n"
            f"{oil['description']}\n\n"
            f"💰 Цена: {oil.get('price', 'не указана')} {oil.get('currency', '₽')}\n\n"
            "Характеристики:\n"
            + "\n".join([f"• {f}" for f in oil["features"]])
            + f"\n\nПодходит: {oil['compatible']}"
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
            caption=caption,
            parse_mode="Markdown",  # caption безопасен (без пользовательского ввода)
            reply_markup=reply_markup,
        )


# ---------- ОБРАБОТКА ЗАЯВОК ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if "ordering" not in context.user_data:
        await update.message.reply_text("Используйте /catalog чтобы выбрать масло.")
        return

    # 1) Валидация контакта
    ok, norm = validate_contact(text)
    if not ok:
        await update.message.reply_text(norm)  # норм здесь — текст ошибки
        return
    contact = norm

    # 2) Антиспам: проверяем кулдаун (по последней УСПЕШНОЙ заявке)
    now = time.time()
    last = LAST_ORDER_AT.get(user.id)
    if last is not None:
        remain = ORDER_COOLDOWN_SEC - int(now - last)
        if remain > 0:
            await update.message.reply_text(
                f"⏳ Слишком часто. Повторите через {remain} сек."
            )
            return

    # 3) Сохраняем заявку
    oil_id = context.user_data["ordering"]
    oil = oils[oil_id]
    username_visible = f"@{user.username}" if user.username else f"ID:{user.id}"

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

    await update.message.reply_text(
        f"✅ Спасибо! Ваша заявка {order_id} на {oil['name']} ({oil['volume']}) "
        f"— {oil.get('price', '—')} {oil.get('currency', '₽')} принята.\n"
        f"Контакты: {contact}"
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

    # Обновляем кулдаун только после успеха
    LAST_ORDER_AT[user.id] = now

    # Сбрасываем состояние
    del context.user_data["ordering"]


# ---------- /orders (только админы) с пагинацией ----------
async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await safe_reply_text(update.message, f"⛔ У вас нет доступа. Ваш ID: {user.id}")
        return

    # формат: /orders [page]
    args = context.args if hasattr(context, "args") else []
    try:
        page = int(args[0]) if args else 1
    except ValueError:
        page = 1
    page = max(1, page)
    page_size = 10

    rows, total = fetch_orders_page(page=page, page_size=page_size)
    if total == 0:
        await safe_reply_text(update.message, "📭 Заявок пока нет.")
        return

    total_pages = (total + page_size - 1) // page_size
    if not rows:
        await safe_reply_text(update.message, f"Страница {page}/{total_pages} пуста.")
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
        hints.append(f"/orders {page-1} ← предыдущая")
    if page < total_pages:
        hints.append(f"/orders {page+1} → следующая")

    msg = "\n".join(lines + (["\n" + " | ".join(hints)] if hints else []))
    await safe_reply_text(update.message, msg)


# ---------- О нас / Контакты ----------
async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "🏪 О нас\n\n"
        "Мы занимаемся продажей оригинальных масел для электромобилей и гибридных автомобилей.\n"
        "🔧 Только проверенные бренды.\n\n"
        "📍 Адрес: Екатеринбург, ул. Серафимы Дерябиной, д. 18а\n"
        "🕘 Время работы: 9:00 — 21:00",
    )

async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "📞 Наши контакты:\n\n"
        "Телефон: +7 (999) 559-39-17 - Андрей, +7 (953) 046-36-54 - Влад\n"
        "Telegram: @shaba_v, @andrey_matveev\n"
        "Авито: https://m.avito.ru/brands/2c07f021e144d3169204cd556d312cdf/items/all",
    )


# ---------- Главная ----------
def main():
    # подготовим БД
    init_db()
    # разовая миграция из JSON, если таблица ещё пустая
    try:
        migrate_json_to_sql()
    except Exception as e:
        logger.warning("Миграция пропущена/ошибка: %s", e)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", show_catalog))
    app.add_handler(CommandHandler("orders", show_orders))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("contacts", contacts))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(show_oil))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен... 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()
