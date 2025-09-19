# --- Импорты ---
import os
import json
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
from catalog import oils  # словарь с маслами

# --- Настройка токена и админов ---
load_dotenv()
TOKEN = os.getenv("TOKEN")

# Получаем список админов из .env
ADMIN_IDS = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip().isdigit()]

ORDERS_FILE = "orders.json"


# --- Функция сохранения заявок ---
def save_order(order):
    """Сохраняем заявку в файл orders.json с уникальным ID."""
    try:
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                orders = json.load(f)
        else:
            orders = []
    except:
        orders = []

    # Генерируем ID заявки
    order_id = len(orders) + 1
    order["id"] = f"#{order_id:03}"  # например: #001, #002, #010

    orders.append(order)

    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

    return order["id"]


# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение."""
    await update.message.reply_text(
        "Привет! 👋\n"
        "Я бот-магазин масел для электромобилей и гибридов.\n\n"
        "📌 Команды:\n"
        "/catalog — открыть каталог\n"
        "/about — о компании\n"
        "/contacts — контакты\n"
        "/orders — список заявок (для админов)\n"
        "/start — показать это сообщение"
    )


# --- Каталог ---
async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает каталог масел списком кнопок."""
    keyboard = [
        [InlineKeyboardButton(f"{oil['name']} ({oil['volume']})", callback_data=str(oil_id))]
        for oil_id, oil in oils.items()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:  # вызов через кнопку "Назад"
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("Выберите масло:", reply_markup=reply_markup)
    else:  # вызов через /catalog
        await update.message.reply_text("Выберите масло:", reply_markup=reply_markup)


# --- Обработка кнопок ---
async def show_oil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает карточку масла или начинает оформление заявки."""
    query = update.callback_query
    await query.answer()
    data = query.data

    # Назад в каталог
    if data == "back":
        await show_catalog(update, context)
        return

    # Оставить заявку
    if data.startswith("order_"):
        oil_id = int(data.split("_")[1])
        oil = oils[oil_id]
        text = (
            f"🛒 Вы выбрали:\n"
            f"*{oil['name']}* ({oil['volume']})\n\n"
            f"Напишите, пожалуйста, свои контактные данные (телефон или Telegram), "
            f"и я передам заявку администратору."
        )
        await query.edit_message_text(text, parse_mode="Markdown")
        context.user_data["ordering"] = oil_id
        return

    # Показ карточки масла
    if data.isdigit():
        oil_id = int(data)
        if oil_id not in oils:
            await query.edit_message_text("❌ Ошибка: товар не найден.")
            return

        oil = oils[oil_id]
        text = (
            f"🔹 *{oil['name']}* ({oil['volume']})\n\n"
            f"{oil['description']}\n\n"
            f"Характеристики:\n"
            + "\n".join([f"• {f}" for f in oil["features"]])
            + f"\n\nПодходит: {oil['compatible']}"
        )
        keyboard = [
            [InlineKeyboardButton("⬅ Назад в каталог", callback_data="back")],
            [InlineKeyboardButton("🛒 Оставить заявку", callback_data=f"order_{oil_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.delete_message()  # удаляем старое сообщение с кнопкой
        await query.message.reply_photo(
            photo=oil["image"],
            caption=text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )


# --- Обработка заявок (текст от пользователя) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет заявку и уведомляет админов."""
    user = update.effective_user
    text = update.message.text

    if "ordering" in context.user_data:
        oil_id = context.user_data["ordering"]
        oil = oils[oil_id]
        username = f"@{user.username}" if user.username else f"ID:{user.id}"

        order = {
            "user_id": user.id,
            "username": user.username,
            "oil": oil["name"],
            "volume": oil["volume"],
            "contact": text,
        }

        order_id = save_order(order)

        await update.message.reply_text(
            f"✅ Спасибо! Ваша заявка {order_id} на {oil['name']} ({oil['volume']}) принята.\n"
            f"Контакты: {text}"
        )

        for admin_id in ADMIN_IDS:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📩 Новая заявка {order_id}\n\n"
                    f"🛒 Товар: {oil['name']} ({oil['volume']})\n"
                    f"👤 От: {username}\n"
                    f"📞 Контакты: {text}"
                )
            )

        del context.user_data["ordering"]
    else:
        await update.message.reply_text("Используйте /catalog чтобы выбрать масло.")


# --- Команда /orders (только для админов) ---
async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список заявок из файла orders.json (только админам)."""
    user = update.effective_user

    if user.id not in ADMIN_IDS:
        await update.message.reply_text(
            f"⛔ У вас нет доступа к этому разделу.\nВаш ID: {user.id}"
        )
        return

    try:
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                orders = json.load(f)
        else:
            orders = []
    except:
        orders = []

    if not orders:
        await update.message.reply_text("📭 Заявок пока нет.")
        return

    text = "📋 Список заявок:\n\n"
    for order in orders[-10:]:
        order_id = order.get("id", "❓")
        username = order.get("username") or f"ID:{order.get('user_id')}"
        text += (
            f"{order_id} — {order['oil']} ({order['volume']})\n"
            f"👤 От: {username}\n"
            f"📞 Контакты: {order['contact']}\n\n"
        )

    await update.message.reply_text(text)


# --- Команда /about ---
async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏪 *О нас*\n\n"
        "Мы занимаемся продажей оригинальных масел для электромобилей и гибридных автомобилей.\n"
        "🔧 Только проверенные бренды.\n\n"
        "📍 Адрес: Екатеринбург, ул. Серафимы Дерябиной, д. 18а\n"
        "🕘 Время работы: 9:00 — 21:00"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# --- Команда /contacts ---
async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📞 *Наши контакты:*\n\n"
        "Телефон: +7 (999) 559-39-17, +7 (953) 046-36-54\n"
        "Telegram: @shaba_v, @andrey_matveev\n"
        "Авито: https://m.avito.ru/brands/2c07f021e144d3169204cd556d312cdf/items/all"
    )
    await update.message.reply_text(text)


# --- Главная функция ---
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("contacts", contacts))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", show_catalog))
    app.add_handler(CommandHandler("orders", show_orders))
    app.add_handler(CallbackQueryHandler(show_oil))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен... 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()
