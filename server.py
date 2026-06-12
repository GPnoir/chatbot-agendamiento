"""Servidor FastAPI - webhooks para Telegram y WhatsApp."""
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update

import database as db
from channels.whatsapp_bot import router as whatsapp_router
from channels.telegram_bot import create_telegram_app
from config import WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
from input_validation import add_security_middleware, validate_telegram_payload, validate_message_text, is_oversized
from observability import get_logger, log_message_handled

logger = get_logger(__name__)

app = FastAPI(title="Chatbot Agendamiento")
add_security_middleware(app)
app.include_router(whatsapp_router)

telegram_app = None


@app.on_event("startup")
async def startup():
    global telegram_app
    db.init_db()
    telegram_app = create_telegram_app()
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(
        f"{WEBHOOK_URL}/telegram/webhook",
        secret_token=TELEGRAM_WEBHOOK_SECRET,
    )
    await telegram_app.start()


@app.on_event("shutdown")
async def shutdown():
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # Empty configured secret fails closed: reject everything
    if not TELEGRAM_WEBHOOK_SECRET or secret != TELEGRAM_WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    data = await request.json()
    if not validate_telegram_payload(data):
        return {"status": "ok"}
    msg = data["message"]
    text = msg.get("text", "")
    clean = validate_message_text(text)
    if clean is None:
        if is_oversized(text):
            # Oversized message — notify user; skip processing
            import httpx
            chat_id = msg["chat"]["id"]
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(url, json={
                        "chat_id": chat_id,
                        "text": "Tu mensaje es demasiado largo (máximo 500 caracteres).",
                    })
            except Exception:
                pass
        return {"status": "ok"}
    # Build a sanitized copy so python-telegram-bot processes clean text
    import copy
    user_id = str(msg["from"]["id"])
    clean_data = copy.deepcopy(data)
    clean_data["message"]["text"] = clean
    update = Update.de_json(clean_data, telegram_app.bot)
    t0 = time.monotonic()
    await telegram_app.process_update(update)
    duration_ms = (time.monotonic() - t0) * 1000
    log_message_handled(
        logger,
        channel="telegram",
        user_id=user_id,
        action="message_handled",
        duration_ms=duration_ms,
    )
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "chatbot-agendamiento"}
