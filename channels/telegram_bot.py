"""Adaptador para Telegram Bot API."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, CommandHandler, filters, Defaults
from telegram.request import HTTPXRequest

import chatbot
from config import TELEGRAM_BOT_TOKEN
from telegram_ui import build_reply_markup


def _to_ptb_markup(markup: dict | None) -> InlineKeyboardMarkup | None:
    """Convierte el dict de telegram_ui al tipo de python-telegram-bot."""
    if not markup:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
        for row in markup["inline_keyboard"]
    ])


async def start_command(update: Update, context):
    response = chatbot.handle_message("telegram", str(update.effective_user.id), "/start")
    await update.message.reply_text(response, reply_markup=_to_ptb_markup(build_reply_markup(response)))


async def handle_text(update: Update, context):
    user_id = str(update.effective_user.id)
    text = update.message.text
    response = chatbot.handle_message("telegram", user_id, text)
    await update.message.reply_text(response, reply_markup=_to_ptb_markup(build_reply_markup(response)))


async def handle_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    response = chatbot.handle_message("telegram", user_id, query.data or "")
    await query.message.reply_text(response, reply_markup=_to_ptb_markup(build_reply_markup(response)))


async def error_handler(update, context):
    print(f"⚠️ Error Telegram: {context.error}")


def create_telegram_app() -> Application:
    request = HTTPXRequest(connect_timeout=10.0, read_timeout=30.0, write_timeout=30.0)
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    return app
