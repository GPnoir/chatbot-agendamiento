"""Adaptador para WhatsApp via Meta Cloud API."""
import hashlib
import hmac

import httpx
from fastapi import APIRouter, Request, Response

import chatbot
from config import WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET

router = APIRouter(prefix="/whatsapp")
META_API_URL = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify X-Hub-Signature-256 HMAC against WHATSAPP_APP_SECRET.

    Fails closed: returns False when the secret is not configured or the
    signature header is missing or malformed. Never logs secrets or payload.
    """
    import logging
    logger = logging.getLogger(__name__)

    if not WHATSAPP_APP_SECRET:
        logger.error("WHATSAPP_APP_SECRET not configured; rejecting webhook")
        return False

    # Guard against a missing or malformed header (no "sha256=" prefix).
    if not signature or not signature.startswith("sha256="):
        logger.warning("WhatsApp webhook: missing or malformed X-Hub-Signature-256 header")
        return False

    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def send_message(to: str, text: str):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient() as client:
        await client.post(META_API_URL, json=payload, headers=headers)


@router.get("/webhook")
async def verify_webhook(request: Request):
    """Verificación del webhook de Meta."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("/webhook")
async def receive_message(request: Request):
    """Recibe mensajes de WhatsApp."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(body, signature):
        return Response(status_code=403)
    try:
        import json
        data = json.loads(body)
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        if "messages" not in value:
            return {"status": "ok"}
        message = value["messages"][0]
        if message["type"] != "text":
            return {"status": "ok"}
        from_number = message["from"]
        text = message["text"]["body"]
        response = chatbot.handle_message("whatsapp", from_number, text)
        await send_message(from_number, response)
    except (KeyError, IndexError, ValueError):
        pass
    return {"status": "ok"}
