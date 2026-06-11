"""AWS Lambda handler - punto de entrada para API Gateway."""
import json
import hashlib
import hmac
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mangum import Mangum

import chatbot_lambda as chatbot
import database_dynamo as db
from config import (
    WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
    WHATSAPP_APP_SECRET, TELEGRAM_WEBHOOK_SECRET, ADMIN_API_KEY,
)
from input_validation import (
    add_security_middleware,
    validate_telegram_payload,
    validate_whatsapp_payload,
    validate_message_text,
    is_oversized,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Chatbot Agendamiento Lambda")
add_security_middleware(app)

META_API_URL = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"


# ── Startup ───────────────────────────────────────────────────────────
# Init on cold start (lifespan="off" means on_event startup doesn't run)
db.init_db()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "chatbot-agendamiento", "runtime": "lambda"}


def _check_admin_auth(request: Request) -> bool:
    """Verify the Authorization: Bearer <key> header for admin endpoints.

    Returns True when the key is valid. Fails closed: if ADMIN_API_KEY is
    empty or unset, always returns False regardless of the presented token.
    """
    if not ADMIN_API_KEY:
        return False
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return False
    scheme, _, credentials = auth_header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    presented = credentials.lstrip(" ")
    if not presented:
        return False
    return hmac.compare_digest(presented, ADMIN_API_KEY)


@app.get("/admin/agenda")
async def admin_agenda(
    request: Request,
    fecha: str = None,
    desde: str = None,
    hasta: str = None,
):
    """Admin JSON agenda API. Requires Authorization: Bearer <ADMIN_API_KEY> header."""
    if not _check_admin_auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    from datetime import date as d, timedelta
    from decimal import Decimal

    if desde and hasta:
        fechas = []
        current = d.fromisoformat(desde)
        end = d.fromisoformat(hasta)
        while current <= end:
            fechas.append(current.isoformat())
            current += timedelta(days=1)
    else:
        fechas = [fecha or d.today().isoformat()]

    table = db.get_table()
    all_citas = []
    for f in fechas:
        resp = table.scan(
            FilterExpression="begins_with(PK, :p) AND fecha = :f AND estado = :e",
            ExpressionAttributeValues={":p": "APPOINTMENT#", ":f": f, ":e": "confirmada"},
        )
        all_citas.extend(resp["Items"])

    # Enriquecer con datos del cliente
    clientes_cache = {}
    result = []
    for c in sorted(all_citas, key=lambda x: (x["fecha"], x["hora"])):
        cid = c.get("cliente_id", "")
        if cid and cid not in clientes_cache:
            cli_resp = table.get_item(Key={"PK": "CLIENT", "SK": cid})
            clientes_cache[cid] = cli_resp.get("Item", {})
        cli = clientes_cache.get(cid, {})
        item = {k: (int(v) if isinstance(v, Decimal) else v) for k, v in c.items()
                if k not in ("PK", "SK", "GSI1PK", "GSI1SK")}
        item["cliente_nombre"] = cli.get("nombre", "")
        item["cliente_canal"] = cli.get("canal", "")
        item["cliente_contacto"] = cli.get("canal_user_id", "")
        result.append(item)

    return {"fechas": fechas, "total": len(result), "citas": result}


@app.get("/admin/panel")
async def admin_panel():
    """Admin calendar panel — login shell only, no appointment data embedded."""
    html = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agenda - Centro de Flores de Bach</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#f0f4f0;color:#333;padding:16px}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px}
h1{color:#2d5a27;font-size:1.4em}
.nav{display:flex;gap:8px;align-items:center}
.nav button{background:#4caf50;color:#fff;border:none;border-radius:6px;padding:8px 14px;cursor:pointer;font-size:.9em}
.nav button:hover{background:#388e3c}
.nav span{font-weight:600;min-width:180px;text-align:center}
.calendar{display:grid;grid-template-columns:60px repeat(var(--days),1fr);gap:1px;background:#ddd;border-radius:8px;overflow:hidden}
.cal-header{background:#2d5a27;color:#fff;padding:8px 4px;text-align:center;font-size:.75em;font-weight:600}
.cal-hour{background:#f9f9f9;padding:4px;font-size:.7em;color:#666;text-align:center;display:flex;align-items:center;justify-content:center;min-height:48px}
.cal-cell{background:#fff;min-height:48px;padding:2px;position:relative}
.cita{background:#e8f5e9;border-left:3px solid #4caf50;border-radius:4px;padding:4px 6px;margin:1px 0;font-size:.7em;cursor:pointer;overflow:hidden}
.cita:hover{background:#c8e6c9}
.cita .nombre{font-weight:600;color:#2d5a27}
.cita .servicio{color:#555}
.cita .contacto{color:#1565c0;font-size:.9em}
.hoy{background:#f1f8e9}
.weekend{background:#fafafa}
.cerrado{background:#f5f5f5;color:#bbb;display:flex;align-items:center;justify-content:center;font-size:.7em}
#login-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:100}
#login-box{background:#fff;border-radius:12px;padding:32px;min-width:320px;box-shadow:0 4px 24px rgba(0,0,0,.2)}
#login-box h2{color:#2d5a27;margin-bottom:16px;font-size:1.2em}
#login-box input{width:100%;padding:10px;border:1px solid #ccc;border-radius:6px;font-size:1em;margin-bottom:12px}
#login-box button{width:100%;padding:10px;background:#4caf50;color:#fff;border:none;border-radius:6px;font-size:1em;cursor:pointer}
#login-box button:hover{background:#388e3c}
#login-error{color:#c62828;font-size:.85em;margin-top:8px;display:none}
@media(max-width:768px){.calendar{grid-template-columns:40px repeat(var(--days),1fr)}.cal-header,.cal-hour{font-size:.65em}.cita{font-size:.6em}}
</style></head><body>
<div id="login-overlay">
  <div id="login-box">
    <h2>Agenda — Acceso</h2>
    <input type="password" id="api-key-input" placeholder="API key" autocomplete="current-password">
    <button onclick="doLogin()">Ingresar</button>
    <div id="login-error">Clave incorrecta. Intentá de nuevo.</div>
  </div>
</div>
<div class="header" style="display:none" id="main-content">
  <h1>🌸 Agenda</h1>
  <div class="nav"><button onclick="semana(-1)">◀ Anterior</button><span id="rango"></span><button onclick="semana(1)">Siguiente ▶</button></div>
</div>
<div class="calendar" id="cal" style="--days:7;display:none"></div>
<script>
function esc(s){const d=document.createElement("div");d.appendChild(document.createTextNode(s||""));return d.innerHTML}
const HORARIOS={0:{i:9,f:18},1:{i:9,f:18},2:{i:9,f:18},3:{i:9,f:18},4:{i:9,f:17},5:{i:9,f:13}};
const DIAS=["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"];
let offset=0;
let apiKey="";

function lunes(d){const r=new Date(d);const day=r.getDay();r.setDate(r.getDate()-((day+6)%7));return r}
function fmt(d){return d.toISOString().slice(0,10)}
function semana(dir){offset+=dir;render()}

function doLogin(){
  const k=document.getElementById("api-key-input").value.trim();
  if(!k){return}
  verifyKey(k);
}
document.getElementById("api-key-input").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin()});

async function verifyKey(k){
  const base=location.pathname.replace(/[/]admin[/]panel[/]?$/,"");
  const today=new Date();
  const r=await fetch(base+"/admin/agenda?fecha="+fmt(today),{
    headers:{"Authorization":"Bearer "+k}
  });
  if(r.ok){
    apiKey=k;
    sessionStorage.setItem("admin_api_key",k);
    document.getElementById("login-overlay").style.display="none";
    document.getElementById("main-content").style.display="flex";
    document.getElementById("cal").style.display="grid";
    render();
  } else {
    sessionStorage.removeItem("admin_api_key");
    document.getElementById("login-error").style.display="block";
  }
}

// Restore from session storage on page load
(function(){
  const stored=sessionStorage.getItem("admin_api_key");
  if(stored){verifyKey(stored)}
})();

async function render(){
  const base=location.pathname.replace(/[/]admin[/]panel[/]?$/,"");
  const hoy=new Date();
  const lun=lunes(hoy);
  lun.setDate(lun.getDate()+offset*7);
  const dias=[];
  for(let i=0;i<7;i++){const d=new Date(lun);d.setDate(d.getDate()+i);dias.push(d)}
  const desde=fmt(dias[0]),hasta=fmt(dias[6]);
  document.getElementById("rango").textContent=desde.slice(5)+" → "+hasta.slice(5);

  const r=await fetch(base+"/admin/agenda?desde="+desde+"&hasta="+hasta,{
    headers:{"Authorization":"Bearer "+apiKey}
  });
  if(r.status===401||r.status===403){
    sessionStorage.removeItem("admin_api_key");
    apiKey="";
    document.getElementById("login-overlay").style.display="flex";
    document.getElementById("main-content").style.display="none";
    document.getElementById("cal").style.display="none";
    document.getElementById("login-error").style.display="block";
    return;
  }
  const data=await r.json();
  const citasMap={};
  (data.citas||[]).forEach(c=>{const k=c.fecha+"#"+c.hora;if(!citasMap[k])citasMap[k]=[];citasMap[k].push(c)});

  const minH=9,maxH=18;
  let html="<div class='cal-header'></div>";
  dias.forEach((d,i)=>{
    const dn=DIAS[d.getDay()===0?6:d.getDay()-1];
    const dd=d.getDate()+"/"+(d.getMonth()+1);
    const esHoy=fmt(d)===fmt(hoy)?" ⬤":"";
    html+="<div class='cal-header'>"+dn+"<br>"+dd+esHoy+"</div>";
  });

  for(let h=minH;h<maxH;h++){
    for(let m=0;m<60;m+=30){
      const hStr=String(h).padStart(2,"0")+":"+String(m).padStart(2,"0");
      html+="<div class='cal-hour'>"+hStr+"</div>";
      dias.forEach((d,i)=>{
        const dw=d.getDay();
        const horario=HORARIOS[dw===0?6:dw-1];
        const esHoyClass=fmt(d)===fmt(hoy)?" hoy":"";
        if(!horario||(h<horario.i)||(h>=horario.f)){
          html+="<div class='cal-cell cerrado"+esHoyClass+"'>—</div>";return;
        }
        const key=fmt(d)+"#"+hStr;
        const citas=citasMap[key]||[];
        html+="<div class='cal-cell"+esHoyClass+"'>";
        citas.forEach(c=>{
          const contactoRaw=c.cliente_canal==="telegram"?"@tg:"+c.cliente_contacto:c.cliente_canal==="whatsapp"?"+"+c.cliente_contacto:c.cliente_contacto;
          html+="<div class='cita'><div class='nombre'>"+esc(c.cliente_nombre||"Sin nombre")+"</div><div class='servicio'>"+esc(c.servicio_nombre||"Consulta")+" ("+esc(String(c.servicio_duracion||60))+"min)</div><div class='contacto'>"+esc(contactoRaw)+"</div></div>";
        });
        html+="</div>";
      });
    }
  }
  document.getElementById("cal").innerHTML=html;
}
</script></body></html>"""
    return Response(content=html, media_type="text/html")


# ── WhatsApp ──────────────────────────────────────────────────────────
def _verify_whatsapp_signature(payload: bytes, signature: str) -> bool:
    """Verify X-Hub-Signature-256 HMAC against WHATSAPP_APP_SECRET.

    Fails closed: returns False when the secret is not configured or the
    signature header is missing or malformed. Never logs the payload, the
    signature value, or any secret.
    """
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


async def _send_whatsapp(to: str, text: str):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    async with httpx.AsyncClient() as client:
        await client.post(META_API_URL, json=payload, headers=headers)


async def _send_telegram(chat_id: int, text: str):
    from config import TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


@app.get("/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    params = request.query_params
    # Empty verify token fails closed: never match an unconfigured deployment
    if WHATSAPP_VERIFY_TOKEN and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN:
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
        if not validate_whatsapp_payload(data):
            return {"status": "ok"}
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]
        if message["type"] != "text":
            return {"status": "ok"}
        from_number = message["from"]
        raw_text = message["text"]["body"]
        clean = validate_message_text(raw_text)
        if clean is None:
            if is_oversized(raw_text):
                await _send_whatsapp(
                    from_number,
                    "Tu mensaje es demasiado largo (máximo 500 caracteres).",
                )
            return {"status": "ok"}
        response = chatbot.handle_message("whatsapp", from_number, clean)
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
    # Empty configured secret fails closed: reject everything
    if not TELEGRAM_WEBHOOK_SECRET or secret != TELEGRAM_WEBHOOK_SECRET:
        logger.info(f"Telegram: secret mismatch, got='{secret[:10]}...'")
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    data = await request.json()
    if not validate_telegram_payload(data):
        logger.info("Telegram: payload failed structural validation, skipping")
        return {"status": "ok"}
    msg = data["message"]
    raw_text = msg.get("text", "")
    user_id = str(msg["from"]["id"])
    chat_id = msg["chat"]["id"]
    clean = validate_message_text(raw_text)
    if clean is None:
        if is_oversized(raw_text):
            logger.info("Telegram: message too long, sending rejection")
            await _send_telegram(
                chat_id,
                "Tu mensaje es demasiado largo (máximo 500 caracteres).",
            )
        return {"status": "ok"}
    try:
        response = chatbot.handle_message("telegram", user_id, clean)
        logger.info(f"Telegram: response='{response[:80]}...'")
        await _send_telegram(chat_id, response)
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
