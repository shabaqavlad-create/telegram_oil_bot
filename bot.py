import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from catalog import oils  # импортируем словарь с маслами

# Загружаем переменные из .env
load_dotenv()

# Берем токен из .env
TOKEN = os.getenv("TOKEN")

print("Мой токен:", TOKEN)  # временно для проверки, можно потом убрать

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start — приветствие пользователя."""
    await update.message.reply_text("Привет! Бот работает 🚀\n\n"
                                    "Напиши /catalog чтобы посмотреть каталог масел.")


# --- Команда /catalog ---
async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список масел в виде кнопок."""
    # Создаём список кнопок — по одной на каждый товар
    keyboard = [
        [InlineKeyboardButton(oil["name"] + " (" + oil["volume"] + ")", callback_data=str(oil_id))]
        for oil_id, oil in oils.items()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Отправляем сообщение с кнопками
    await update.message.reply_text("Выберите масло:", reply_markup=reply_markup)


# --- Обработка нажатия на кнопку ---
async def show_oil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает карточку выбранного масла после клика на кнопку."""
    query = update.callback_query   # получаем объект нажатой кнопки
    await query.answer()            # подтверждаем, что клик обработан

    oil_id = int(query.data)        # id масла из callback_data
    oil = oils[oil_id]              # достаём данные по этому маслу из словаря

    # Формируем карточку товара
    text = (
        f"🔹 *{oil['name']}* ({oil['volume']})\n\n"
        f"{oil['description']}\n\n"
        f"Характеристики:\n"
        + "\n".join([f"• {f}" for f in oil["features"]])
        + f"\n\nПодходит: {oil['compatible']}"
    )

    # Редактируем сообщение с кнопками → заменяем на карточку
    await query.edit_message_text(text, parse_mode="Markdown")


# --- Главная функция ---
def main():
    """Запуск бота и регистрация всех обработчиков."""
    app = Application.builder().token(TOKEN).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", show_catalog))

    # Обработчик кнопок
    app.add_handler(CallbackQueryHandler(show_oil))

    print("Бот запущен... 🚀")
    app.run_polling()


# Точка входа в программу
if __name__ == "__main__":
    main()
