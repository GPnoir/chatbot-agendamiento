"""Servidor FastAPI - webhooks para Telegram y WhatsApp."""
from fastapi import FastAPI, Request
from telegram import Update

import database as db
from channels.whatsapp_bot import router as whatsapp_router
from channels.telegram_bot import create_telegram_app
from config import WEBHOOK_URL, TELEGRAM_BOT_TOKEN

app = FastAPI(title="Chatbot Agendamiento")
app.include_router(whatsapp_router)

telegram_app = None


@app.on_event("startup")
async def startup():
    global telegram_app
    db.init_db()
    telegram_app = create_telegram_app()
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/telegram/webhook")
    await telegram_app.start()


@app.on_event("shutdown")
async def shutdown():
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "chatbot-agendamiento"}
