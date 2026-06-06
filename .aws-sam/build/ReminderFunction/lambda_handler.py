"""AWS Lambda handler - punto de entrada para API Gateway."""
import json
import hashlib
import hmac
import os

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mangum import Mangum

import chatbot_lambda as chatbot
import database_dynamo as db
from config import (
    WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
    WHATSAPP_APP_SECRET, TELEGRAM_WEBHOOK_SECRET,
)

app = FastAPI(title="Chatbot Agendamiento Lambda")

META_API_URL = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"


# ── Startup ───────────────────────────────────────────────────────────
# Init on cold start (lifespan="off" means on_event startup doesn't run)
db.init_db()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "chatbot-agendamiento", "runtime": "lambda"}


@app.get("/admin/agenda")
async def admin_agenda(fecha: str = None, token: str = None):
    """API JSON de la agenda. Protegido por token."""
    if token != TELEGRAM_WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    from datetime import date as d
    from decimal import Decimal
    fecha = fecha or d.today().isoformat()
    table = db.get_table()
    resp = table.scan(
        FilterExpression="begins_with(PK, :p) AND fecha = :f AND estado = :e",
        ExpressionAttributeValues={":p": "APPOINTMENT#", ":f": fecha, ":e": "confirmada"},
    )
    citas = sorted(resp["Items"], key=lambda x: x["hora"])
    result = [{k: (int(v) if isinstance(v, Decimal) else v) for k, v in c.items()
               if k not in ("PK", "SK", "GSI1PK", "GSI1SK")} for c in citas]
    return {"fecha": fecha, "total": len(result), "citas": result}


@app.get("/admin/panel")
async def admin_panel():
    """Panel HTML para ver agenda."""
    html = '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agenda</title><style>body{font-family:system-ui;max-width:600px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#2d5a27}.cita{background:#fff;padding:12px;margin:8px 0;border-radius:8px;border-left:4px solid #4caf50}.hora{font-weight:bold;font-size:1.2em;color:#2d5a27}.vacia{color:#888;text-align:center;padding:40px}input[type=date]{padding:8px;border-radius:4px;border:1px solid #ccc;font-size:1em}</style></head><body><h1>🌸 Agenda</h1><input type="date" id="fecha" onchange="cargar()"><div id="citas"></div><script>const T=new URLSearchParams(location.search).get("token")||"";document.getElementById("fecha").valueAsDate=new Date();async function cargar(){const f=document.getElementById("fecha").value;const r=await fetch(location.origin+"/Prod/admin/agenda?fecha="+f+"&token="+T);const d=await r.json();if(!d.citas||d.citas.length===0){document.getElementById("citas").innerHTML="<p class=vacia>Sin citas</p>";return}document.getElementById("citas").innerHTML=d.citas.map(c=>"<div class=cita><span class=hora>"+c.hora+"</span> - "+(c.servicio_nombre||"Consulta")+"</div>").join("")}cargar()</script></body></html>'
    return Response(content=html, media_type="text/html")


# ── WhatsApp ──────────────────────────────────────────────────────────
def _verify_whatsapp_signature(payload: bytes, signature: str) -> bool:
    if not WHATSAPP_APP_SECRET:
        return True
    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _send_whatsapp(to: str, text: str):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    async with httpx.AsyncClient() as client:
        await client.post(META_API_URL, json=payload, headers=headers)


@app.get("/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/whatsapp/webhook")
async def whatsapp_message(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_whatsapp_signature(body, signature):
        return Response(status_code=403)
    try:
        data = json.loads(body)
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]
        if message["type"] != "text":
            return {"status": "ok"}
        from_number = message["from"]
        text = message["text"]["body"]
        response = chatbot.handle_message("whatsapp", from_number, text)
        await _send_whatsapp(from_number, response)
    except (KeyError, IndexError, ValueError):
        pass
    return {"status": "ok"}


# ── Telegram ──────────────────────────────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    import logging
    logger = logging.getLogger()
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != TELEGRAM_WEBHOOK_SECRET:
        logger.info(f"Telegram: secret mismatch, got='{secret[:10]}...'")
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    data = await request.json()
    try:
        msg = data.get("message", {})
        if not msg:
            logger.info("Telegram: no message in update")
            return {"status": "ok"}
        text = msg.get("text", "")
        user_id = str(msg["from"]["id"])
        chat_id = msg["chat"]["id"]
        logger.info(f"Telegram: user={user_id} text='{text}' chat={chat_id}")
        if not text:
            return {"status": "ok"}
        response = chatbot.handle_message("telegram", user_id, text)
        logger.info(f"Telegram: response='{response[:80]}...'")
        from config import TELEGRAM_BOT_TOKEN
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json={"chat_id": chat_id, "text": response})
            logger.info(f"Telegram: sendMessage status={r.status_code}")
    except (KeyError, TypeError) as e:
        logger.error(f"Telegram: error {e}")
    return {"status": "ok"}


# ── Mangum handler ────────────────────────────────────────────────────
_mangum = Mangum(app, lifespan="off")


def handler(event, context):
    """Lambda entry point - maneja tanto API Gateway como EventBridge warm-up."""
    import logging
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.info(f"Event keys: {list(event.keys())}")
    # EventBridge warm-up o evento no-HTTP
    if "httpMethod" not in event and "requestContext" not in event:
        return {"statusCode": 200, "body": "warm"}
    return _mangum(event, context)
