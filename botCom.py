# --- Импорты ---
import os
import json
import logging
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

ORDERS_FILE = "orders.json"
VERSION_FILE = "VERSION"  # используем без расширения .txt


# --- Работа с версиями ---
def get_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "неизвестна"
    except Exception as e:
        logger.error("Ошибка чтения VERSION: %s", e)
        return f"ошибка чтения ({e})"


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ver = get_version()
    await update.message.reply_text(f"🤖 Текущая версия бота: {ver}")


# --- Функция сохранения заявок ---
def save_order(order):
    try:
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                orders = json.load(f)
        else:
            orders = []
    except Exception as e:
        logger.exception("Не удалось прочитать orders.json: %s", e)
        orders = []

    order_id = len(orders) + 1
    order["id"] = f"#{order_id:03}"
    orders.append(order)

    try:
        with open(ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Не удалось записать orders.json: %s", e)

    return order["id"]


# --- Error handler ---
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


# --- Вспомогательная безопасная отправка ---
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


# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "Привет! 👋\n"
        "Я бот-магазин масел для электромобилей и гибридов.\n\n"
        "📌 Команды:\n"
        "/catalog — открыть каталог\n"
        "/about — о компании\n"
        "/contacts — контакты\n"
        "/orders — список заявок (для админов)\n"
        "/id — показать ваш Telegram ID\n"
        "/cancel — отменить оформление заявки\n"
        "/version — показать версию бота\n"
        "/start — показать это сообщение",
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(update.message, f"Ваш Telegram ID: {update.effective_user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "ordering" in context.user_data:
        del context.user_data["ordering"]
        await safe_reply_text(update.message, "❌ Оформление заявки отменено. Напишите /catalog чтобы выбрать масло снова.")
    else:
        await safe_reply_text(update.message, "Нечего отменять. Напишите /catalog чтобы открыть каталог.")


# --- Каталог ---
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
        if query.message.photo:
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


# --- Обработка кнопок ---
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
            "и я передам заявку администратору."
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
        text = (
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
            caption=text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )


# --- Обработка заявок ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if "ordering" in context.user_data:
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
            "contact": text,
        }

        order_id = save_order(order)

        await update.message.reply_text(
            f"✅ Спасибо! Ваша заявка {order_id} на {oil['name']} ({oil['volume']}) "
            f"— {oil.get('price', '—')} {oil.get('currency', '₽')} принята.\n"
            f"Контакты: {text}"
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
                        f"📞 Контакты: {text}"
                    ),
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить админу {admin_id}: {e}")

        del context.user_data["ordering"]
    else:
        await update.message.reply_text("Используйте /catalog чтобы выбрать масло.")


# --- Команда /orders ---
async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in ADMIN_IDS:
        await safe_reply_text(update.message, f"⛔ У вас нет доступа. Ваш ID: {user.id}")
        return

    try:
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                orders = json.load(f)
        else:
            orders = []
    except Exception as e:
        logger.exception("Ошибка чтения orders.json: %s", e)
        orders = []

    if not orders:
        await safe_reply_text(update.message, "📭 Заявок пока нет.")
        return

    lines = ["📋 Список заявок:\n"]
    for order in orders[-10:]:
        username_visible = order.get("username") or f"ID:{order.get('user_id')}"
        lines.append(
            f"{order.get('id', '?')} — {order['oil']} ({order['volume']})\n"
            f"💰 Цена: {order.get('price', '—')} {order.get('currency', '₽')}\n"
            f"👤 От: {username_visible}\n"
            f"📞 Контакты: {order['contact']}\n"
        )

    await safe_reply_text(update.message, "\n".join(lines))


# --- Команда /about ---
async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "🏪 О нас\n\n"
        "Мы занимаемся продажей оригинальных масел для электромобилей и гибридных автомобилей.\n"
        "🔧 Только проверенные бренды.\n\n"
        "📍 Адрес: Екатеринбург, ул. Серафимы Дерябиной, д. 18а\n"
        "🕘 Время работы: 9:00 — 21:00",
    )


# --- Команда /contacts ---
async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "📞 Наши контакты:\n\n"
        "Телефон: +7 (999) 559-39-17 - Андрей, +7 (953) 046-36-54 - Влад\n"
        "Telegram: @shaba_v, @andrey_matveev\n"
        "Авито: https://m.avito.ru/brands/2c07f021e144d3169204cd556d312cdf/items/all",
    )


# --- Главная функция ---
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", show_catalog))
    app.add_handler(CommandHandler("orders", show_orders))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("contacts", contacts))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(CallbackQueryHandler(show_oil))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен... 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()
