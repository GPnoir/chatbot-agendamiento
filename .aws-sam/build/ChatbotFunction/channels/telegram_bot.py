"""Adaptador para Telegram Bot API."""
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, Defaults
from telegram.request import HTTPXRequest

import chatbot
from config import TELEGRAM_BOT_TOKEN


async def start_command(update: Update, context):
    response = chatbot.handle_message("telegram", str(update.effective_user.id), "/start")
    await update.message.reply_text(response)


async def handle_text(update: Update, context):
    user_id = str(update.effective_user.id)
    text = update.message.text
    response = chatbot.handle_message("telegram", user_id, text)
    await update.message.reply_text(response)


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
    app.add_error_handler(error_handler)
    return app
