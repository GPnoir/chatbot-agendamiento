"""AWS Lambda handler - punto de entrada para API Gateway."""
import json
import hashlib
import hmac
import os
import time

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mangum import Mangum

import admin_auth
import chatbot_lambda as chatbot
import database_dynamo as db
from config import (
    WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
    WHATSAPP_APP_SECRET, TELEGRAM_WEBHOOK_SECRET, ADMIN_API_KEY,
    ADMIN_USERNAME, ADMIN_PASSWORD_HASH, SESSION_SECRET,
)
from rate_limiter import is_rate_limited
from input_validation import (
    add_security_middleware,
    validate_telegram_callback,
    validate_telegram_payload,
    validate_whatsapp_payload,
    validate_message_text,
    is_oversized,
)
from observability import get_logger, log_message_handled
from telegram_ui import build_reply_markup

logger = get_logger(__name__)

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
    """Verify the Authorization: Bearer <credential> header for admin endpoints.

    Acepta dos credenciales:
    - un token de sesión válido emitido por POST /admin/login (firmado con
      SESSION_SECRET), o
    - la ADMIN_API_KEY cruda (break-glass para automatización y back-compat).

    Falla cerrado: si no hay credenciales configuradas o el header falta/está
    malformado, devuelve False.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return False
    scheme, _, credentials = auth_header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    presented = credentials.lstrip(" ")
    if not presented:
        return False
    # 1) Token de sesión firmado (camino normal del panel).
    if SESSION_SECRET and admin_auth.verify_session_token(presented, SESSION_SECRET) is not None:
        return True
    # 2) API key cruda (break-glass / automatización).
    if ADMIN_API_KEY and hmac.compare_digest(presented, ADMIN_API_KEY):
        return True
    return False


@app.post("/admin/login")
async def admin_login(request: Request):
    """Login del panel: {username, password} -> {token, expires_in}.

    Falla cerrado si las credenciales no están configuradas. Rate-limited por
    usuario para frenar fuerza bruta. Nunca loguea la contraseña.
    """
    expires_in = 8 * 3600
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "invalid json"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "invalid body"})
    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return JSONResponse(status_code=400, content={"error": "missing credentials"})
    username = username.strip()
    # Límite defensivo de tamaño (evita PBKDF2 sobre payloads enormes).
    if not username or not password or len(username) > 150 or len(password) > 1024:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    # Rate limit por usuario presentado (clave separada del rate limit del bot).
    if is_rate_limited(f"adminlogin:{username}"):
        return JSONResponse(status_code=429, content={"error": "too many attempts"})

    # Falla cerrado si la auth no está configurada en el entorno.
    if not (ADMIN_USERNAME and ADMIN_PASSWORD_HASH and SESSION_SECRET):
        logger.error("admin login attempted but auth is not configured")
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    user_ok = hmac.compare_digest(username, ADMIN_USERNAME)
    pass_ok = admin_auth.verify_password(password, ADMIN_PASSWORD_HASH)
    # Comparar ambos siempre (no cortocircuitar) para no filtrar por timing.
    if not (user_ok and pass_ok):
        logger.warning("admin login failed")
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    token = admin_auth.issue_session_token(ADMIN_USERNAME, SESSION_SECRET, ttl_seconds=expires_in)
    return {"token": token, "expires_in": expires_in}


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
        # Claves para referenciar la cita desde el panel (cancelar). El endpoint
        # solo es alcanzable con auth admin; valida que la PK sea una cita.
        item["pk"] = c.get("PK")
        item["sk"] = c.get("SK")
        item["cliente_nombre"] = cli.get("nombre", "")
        item["cliente_canal"] = cli.get("canal", "")
        item["cliente_contacto"] = cli.get("canal_user_id", "")
        result.append(item)

    return {"fechas": fechas, "total": len(result), "citas": result}


@app.get("/admin/reporte")
async def admin_reporte(request: Request, desde: str = None, hasta: str = None):
    """Métricas de citas en un rango (issue #15). Default: últimos 7 días.

    Requiere Authorization: Bearer <ADMIN_API_KEY>.
    """
    if not _check_admin_auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    from datetime import date as d, timedelta

    try:
        hasta_v = d.fromisoformat(hasta) if hasta else d.today()
        desde_v = d.fromisoformat(desde) if desde else hasta_v - timedelta(days=6)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid date format, use YYYY-MM-DD"})

    return db.resumen_citas_rango(desde_v.isoformat(), hasta_v.isoformat())


@app.post("/admin/cita/cancelar")
async def admin_cancelar_cita(request: Request):
    """Cancela una cita desde el panel. Body: {pk, sk}.

    Acción destructiva: requiere auth admin y valida que la clave corresponda a
    una cita (PK APPOINTMENT# / SK DATE#) para no poder mutar otros registros.
    """
    if not _check_admin_auth(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "invalid json"})
    pk = body.get("pk") if isinstance(body, dict) else None
    sk = body.get("sk") if isinstance(body, dict) else None
    if (not isinstance(pk, str) or not isinstance(sk, str)
            or not pk.startswith("APPOINTMENT#") or not sk.startswith("DATE#")):
        return JSONResponse(status_code=400, content={"error": "invalid appointment id"})

    table = db.get_table()
    existing = table.get_item(Key={"PK": pk, "SK": sk}).get("Item")
    if not existing:
        return JSONResponse(status_code=404, content={"error": "not found"})

    db.cancelar_cita(pk, sk)
    return {"status": "ok"}


@app.get("/admin/panel")
async def admin_panel():
    """Panel admin — login shell, sin datos de citas embebidos.

    Dos vistas client-side (Agenda + Reporte) que consumen /admin/agenda y
    /admin/reporte con la API key del login. El HTML no embebe datos ni
    secretos: la grilla nace oculta y los datos llegan tras autenticar.
    Diseño "papel neutro + acento botánico" (ver DESIGN.md).
    """
    html = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agenda — Centro de Flores de Bach</title>
<style>
:root{
  --bg:oklch(0.985 0.003 200);
  --surface:oklch(1 0 0);
  --surface-sunk:oklch(0.965 0.004 200);
  --ink:oklch(0.26 0.015 165);
  --ink-2:oklch(0.45 0.012 165);
  --ink-3:oklch(0.48 0.012 165);
  --line:oklch(0.91 0.004 200);
  --line-2:oklch(0.85 0.005 200);
  --accent:oklch(0.47 0.082 156);
  --accent-strong:oklch(0.40 0.075 156);
  --accent-tint:oklch(0.955 0.022 156);
  --accent-tint-2:oklch(0.91 0.035 156);
  --clay:oklch(0.62 0.10 45);
  --clay-ink:oklch(0.46 0.09 42);
  --clay-tint:oklch(0.955 0.022 50);
  --focus:oklch(0.55 0.12 156);
  --r-sm:8px;--r-md:10px;--r-lg:14px;
  --ease:cubic-bezier(0.22,1,0.36,1);
  --shadow:0 1px 2px oklch(0.25 0.02 165/.05),0 12px 28px oklch(0.25 0.02 165/.06);
  --shadow-sm:0 1px 3px oklch(0.25 0.02 165/.12);
  --font-display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --font-ui:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{font-family:var(--font-ui);background:var(--bg);color:var(--ink);min-height:100vh;line-height:1.5;-webkit-font-smoothing:antialiased}
[hidden]{display:none!important}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:3px}

/* topbar */
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px clamp(16px,4vw,32px);border-bottom:1px solid var(--line);background:var(--surface);flex-wrap:wrap;position:sticky;top:0;z-index:20}
.brand{display:flex;align-items:center;gap:10px}
.brand-text{display:flex;flex-direction:column;line-height:1.25}
.brand-name{font:600 1rem var(--font-display);color:var(--ink);letter-spacing:-.01em}
.brand-sub{font:500 .72rem var(--font-ui);color:var(--ink-3)}
.mark{flex:none;display:block}
.topbar-view{margin-left:auto;font:600 .9rem var(--font-ui);color:var(--ink-3)}

/* menú hamburguesa + drawer */
.hamburger{appearance:none;border:1px solid var(--line);background:var(--surface);border-radius:var(--r-sm);width:38px;height:38px;display:inline-flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;cursor:pointer;flex:none;transition:border-color .15s var(--ease)}
.hamburger span{display:block;width:18px;height:2px;background:var(--ink-2);border-radius:2px;transition:background .15s var(--ease)}
.hamburger:hover{border-color:var(--accent)}
.hamburger:hover span{background:var(--accent-strong)}
.nav-backdrop{position:fixed;inset:0;background:color-mix(in oklch,var(--ink) 40%,transparent);z-index:90;animation:fade .15s var(--ease)}
.nav-drawer{position:fixed;top:0;left:0;height:100%;width:min(82vw,280px);background:var(--surface);border-right:1px solid var(--line);box-shadow:var(--shadow);z-index:95;padding:18px;display:flex;flex-direction:column;gap:4px;animation:slidein .22s var(--ease)}
@keyframes slidein{from{transform:translateX(-100%)}to{transform:translateX(0)}}
.nav-head{display:flex;align-items:center;gap:8px;padding:4px 8px 16px;border-bottom:1px solid var(--line);margin-bottom:8px}
.nav-title{font:600 .95rem var(--font-display);color:var(--ink);letter-spacing:-.01em}
.nav-list{list-style:none;display:flex;flex-direction:column;gap:2px}
.nav-item{appearance:none;border:0;background:transparent;width:100%;text-align:left;font:500 .95rem var(--font-ui);color:var(--ink);padding:11px 12px;border-radius:var(--r-sm);cursor:pointer;transition:background .15s var(--ease),color .15s var(--ease)}
.nav-item:hover{background:var(--surface-sunk)}
.nav-item.is-active{background:var(--accent-tint);color:var(--accent-strong)}
.nav-logout{margin-top:auto;color:var(--clay-ink)}
.nav-logout:hover{background:var(--clay-tint)}

/* panel de detalle de cita (derecha) */
.detail-backdrop{position:fixed;inset:0;background:color-mix(in oklch,var(--ink) 35%,transparent);z-index:96;animation:fade .15s var(--ease)}
.detail-panel{position:fixed;top:0;right:0;height:100%;width:min(90vw,360px);background:var(--surface);border-left:1px solid var(--line);box-shadow:var(--shadow);z-index:97;padding:20px clamp(16px,3vw,24px);display:flex;flex-direction:column;gap:16px;overflow-y:auto;animation:slideinR .22s var(--ease)}
@keyframes slideinR{from{transform:translateX(100%)}to{transform:translateX(0)}}
.detail-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.detail-title{font:600 1.2rem var(--font-display);color:var(--ink);letter-spacing:-.01em}
.detail-close{appearance:none;border:0;background:transparent;font-size:1.6rem;line-height:1;color:var(--ink-3);cursor:pointer;padding:2px 6px;border-radius:var(--r-sm)}
.detail-close:hover{color:var(--ink)}
.detail-fields{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.detail-fields>div{display:flex;justify-content:space-between;gap:16px;padding:11px 0;border-bottom:1px solid var(--line)}
.detail-fields dt{font:500 .82rem var(--font-ui);color:var(--ink-3)}
.detail-fields dd{font:500 .9rem var(--font-ui);color:var(--ink);text-align:right}
.detail-actions{margin-top:auto;display:flex;flex-direction:column;gap:8px}
.confirm-row{display:flex;gap:8px}.confirm-row button{flex:1}
.btn-danger{appearance:none;border:1px solid color-mix(in oklch,var(--clay) 40%,transparent);background:var(--clay-tint);color:var(--clay-ink);font:600 .9rem var(--font-ui);padding:11px;border-radius:var(--r-sm);cursor:pointer;transition:background .15s var(--ease)}
.btn-danger:hover{background:color-mix(in oklch,var(--clay-tint) 65%,var(--clay))}
.btn-ghost{appearance:none;border:1px solid var(--line);background:var(--surface);color:var(--ink-2);font:600 .9rem var(--font-ui);padding:11px;border-radius:var(--r-sm);cursor:pointer;transition:border-color .15s var(--ease),color .15s var(--ease)}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent-strong)}
.detail-msg{font:500 .85rem var(--font-ui);padding:10px;border-radius:var(--r-sm);text-align:center;color:var(--ink-2)}
.detail-msg.ok{background:var(--accent-tint);color:var(--accent-strong)}
.detail-msg.err{background:var(--clay-tint);color:var(--clay-ink)}

/* segmented control */
.seg{display:inline-flex;background:var(--surface-sunk);border:1px solid var(--line);border-radius:999px;padding:3px;gap:2px}
.seg-btn{appearance:none;border:0;background:transparent;font:600 .85rem var(--font-ui);color:var(--ink-2);padding:7px 16px;border-radius:999px;cursor:pointer;transition:color .15s var(--ease),background .15s var(--ease)}
.seg-btn:hover{color:var(--ink)}
.seg-btn.is-active{background:var(--surface);color:var(--accent-strong);box-shadow:var(--shadow-sm)}
.seg-sm .seg-btn{padding:5px 12px;font-size:.8rem}

/* layout */
.wrap{max-width:1100px;margin:0 auto;padding:clamp(18px,4vw,32px)}
.view{animation:fade .18s var(--ease)}
.view-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px;flex-wrap:wrap}
.view-title{font:600 1.5rem var(--font-display);color:var(--ink);letter-spacing:-.015em}
.weeknav{display:flex;align-items:center;gap:8px}
.rango{font:600 .9rem var(--font-ui);color:var(--ink-2);min-width:120px;text-align:center;font-variant-numeric:tabular-nums}
.nav-btn{appearance:none;width:36px;height:36px;border:1px solid var(--line);background:var(--surface);border-radius:var(--r-sm);color:var(--ink-2);font-size:1.2rem;line-height:1;cursor:pointer;transition:border-color .15s var(--ease),color .15s var(--ease)}
.nav-btn:hover{border-color:var(--accent);color:var(--accent-strong)}

/* calendar */
.calendar{display:grid;grid-template-columns:58px repeat(var(--days),1fr);gap:1px;background:var(--line);border-radius:var(--r-lg);overflow:hidden;box-shadow:var(--shadow)}
.cal-corner{background:var(--surface)}
.cal-head{background:var(--surface);padding:9px 4px;text-align:center;font:600 .72rem var(--font-ui);color:var(--ink-2)}
.cal-head .dnum{display:block;margin-top:2px;color:var(--ink);font-size:.92rem;font-variant-numeric:tabular-nums}
.cal-head.is-today{background:var(--accent-tint)}
.cal-head.is-today .dnum{color:var(--accent-strong)}
.cal-hour{background:var(--surface);display:flex;align-items:center;justify-content:center;min-height:46px;font:500 .68rem var(--font-ui);color:var(--ink-3);font-variant-numeric:tabular-nums}
.cal-cell{background:var(--surface);min-height:46px;padding:3px;display:flex;flex-direction:column;gap:3px}
.cal-cell.is-today{background:color-mix(in oklch,var(--accent-tint) 45%,var(--surface))}
.cal-cell.is-closed{background:var(--surface-sunk);align-items:center;justify-content:center}
.cal-cell.is-closed span{color:var(--ink-3);opacity:.45;font-size:.8rem}
.cita{background:var(--accent-tint);border:1px solid color-mix(in oklch,var(--accent) 16%,transparent);border-radius:var(--r-sm);padding:5px 7px;display:flex;gap:6px;cursor:pointer;transition:background .15s var(--ease),box-shadow .15s var(--ease)}
.cita:hover{background:var(--accent-tint-2);box-shadow:var(--shadow-sm)}
.cita-dot{flex:none;width:7px;height:7px;border-radius:50%;background:var(--accent);margin-top:5px}
.cita-body{min-width:0}
.cita .nombre{font:600 .76rem var(--font-ui);color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cita .servicio{font:500 .7rem var(--font-ui);color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cita .contacto{font:500 .68rem var(--font-ui);color:var(--ink-3)}
.cita.cita-cancel{background:var(--clay-tint)}
.cita.cita-cancel .cita-dot{background:var(--clay)}

/* reporte */
.rep-rango{font:500 .85rem var(--font-ui);color:var(--ink-3);margin:-8px 0 18px}
.rep-summary{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:24px clamp(18px,3vw,28px);margin-bottom:20px}
.lead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.lead-num{font:600 clamp(2.4rem,6vw,3.2rem)/1 var(--font-display);color:var(--ink);letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.lead-label{font:400 1rem var(--font-ui);color:var(--ink-2)}
.statrow{display:flex;flex-wrap:wrap;margin-top:18px;border-top:1px solid var(--line);padding-top:16px}
.stat{padding:0 22px;border-right:1px solid var(--line)}
.stat:first-child{padding-left:0}
.stat:last-child{border-right:0}
.stat dt{font:500 .76rem var(--font-ui);color:var(--ink-3);margin-bottom:4px}
.stat dd{font:600 1.4rem var(--font-display);color:var(--ink);font-variant-numeric:tabular-nums}
.dd-clay{color:var(--clay-ink)}
.meter{height:6px;border-radius:999px;background:var(--surface-sunk);overflow:hidden;margin-top:18px}
.meter-fill{display:block;height:100%;width:0;background:var(--clay);border-radius:999px;transition:width .55s var(--ease)}
.rep-block{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:22px clamp(18px,3vw,28px)}
.block-title{font:600 1.05rem var(--font-display);color:var(--ink);margin-bottom:16px;letter-spacing:-.01em}
.bars{list-style:none;display:flex;flex-direction:column;gap:14px}
.bar-row{display:grid;grid-template-columns:minmax(120px,200px) 1fr auto;align-items:center;gap:14px}
.bar-name{font:500 .9rem var(--font-ui);color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{height:10px;border-radius:999px;background:var(--surface-sunk);overflow:hidden}
.bar-fill{display:block;height:100%;width:0;background:var(--accent);border-radius:999px;transition:width .55s var(--ease)}
.bar-val{font:600 .95rem var(--font-ui);color:var(--ink-2);font-variant-numeric:tabular-nums;min-width:24px;text-align:right}
.empty{text-align:center;padding:48px 24px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg)}
.empty-title{font:600 1.15rem var(--font-display);color:var(--ink);margin:14px 0 6px}
.empty-sub{font:400 .9rem/1.55 var(--font-ui);color:var(--ink-2);max-width:42ch;margin:0 auto}
.skeleton{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:26px;display:flex;flex-direction:column;gap:14px}
.sk-line{height:14px;border-radius:6px;background:linear-gradient(90deg,var(--surface-sunk),color-mix(in oklch,var(--surface-sunk) 40%,var(--surface)),var(--surface-sunk));background-size:200% 100%;animation:sk 1.2s linear infinite}
.sk-line.w1{width:90%}.sk-line.w2{width:60%}.sk-line.w3{width:40%}
.rep-error{font:500 .9rem var(--font-ui);color:var(--clay-ink);padding:20px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg)}

/* login */
#login-overlay{position:fixed;inset:0;background:color-mix(in oklch,var(--ink) 55%,transparent);display:flex;align-items:center;justify-content:center;padding:20px;z-index:110}
#login-box{background:var(--surface);border-radius:var(--r-lg);padding:30px;width:100%;max-width:340px;box-shadow:var(--shadow)}
#login-box h2{font:600 1.2rem var(--font-display);color:var(--ink);margin:12px 0 4px;letter-spacing:-.01em}
#login-box p{font:400 .85rem var(--font-ui);color:var(--ink-2);margin-bottom:18px}
#login-box input{width:100%;padding:11px 12px;border:1px solid var(--line-2);border-radius:var(--r-sm);font:400 .95rem var(--font-ui);color:var(--ink);background:var(--bg);margin-bottom:12px}
#login-box input:focus-visible{outline:2px solid var(--focus);outline-offset:1px;border-color:var(--accent)}
#login-box button{width:100%;padding:11px;background:var(--accent);color:#fff;border:0;border-radius:var(--r-sm);font:600 .95rem var(--font-ui);cursor:pointer;transition:background .15s var(--ease)}
#login-box button:hover{background:var(--accent-strong)}
#login-error{color:var(--clay-ink);font:500 .8rem var(--font-ui);margin-top:10px;display:none}

@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@keyframes sk{from{background-position:200% 0}to{background-position:-200% 0}}
@media(max-width:768px){
  .calendar{grid-template-columns:42px repeat(var(--days),1fr)}
  .cal-head,.cal-hour{font-size:.62rem}
  .cita .nombre{font-size:.66rem}.cita .servicio,.cita .contacto{display:none}
  .brand-sub{display:none}
  .stat{padding:0 14px}
}
@media(max-width:560px){
  .bar-row{grid-template-columns:1fr auto;grid-template-areas:'name val' 'track track';gap:6px 10px}
  .bar-name{grid-area:name}.bar-val{grid-area:val}.bar-track{grid-area:track}
}
@media(prefers-reduced-motion:reduce){
  *{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}
}
</style></head><body>

<div id="login-overlay">
  <div id="login-box">
    <svg class="mark" width="30" height="30" viewBox="0 0 24 24" aria-hidden="true"><g fill="var(--accent)" fill-opacity="0.55"><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(72 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(144 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(216 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(288 12 12)"/></g><circle cx="12" cy="12" r="2.3" fill="var(--accent-strong)"/></svg>
    <h2>Centro de Flores de Bach</h2>
    <p>Ingresá tu usuario y contraseña.</p>
    <input type="text" id="login-user" placeholder="Usuario" autocomplete="username">
    <input type="password" id="login-pass" placeholder="Contraseña" autocomplete="current-password">
    <button onclick="doLogin()">Entrar</button>
    <div id="login-error">Usuario o contraseña incorrectos.</div>
  </div>
</div>

<header class="topbar" id="topbar" hidden>
  <button class="hamburger" id="nav-toggle" aria-label="Abrir menú" aria-expanded="false" aria-controls="nav-drawer" onclick="toggleNav()"><span></span><span></span><span></span></button>
  <div class="brand">
    <svg class="mark" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><g fill="var(--accent)" fill-opacity="0.55"><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(72 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(144 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(216 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(288 12 12)"/></g><circle cx="12" cy="12" r="2.3" fill="var(--accent-strong)"/></svg>
    <div class="brand-text"><span class="brand-name">Centro de Flores de Bach</span><span class="brand-sub">Terapia floral · Nelly Pailacura</span></div>
  </div>
  <span class="topbar-view" id="topbar-view">Agenda</span>
</header>

<div class="nav-backdrop" id="nav-backdrop" hidden onclick="closeNav()"></div>
<nav class="nav-drawer" id="nav-drawer" aria-label="Navegación" hidden>
  <div class="nav-head">
    <svg class="mark" width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><g fill="var(--accent)" fill-opacity="0.55"><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(72 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(144 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(216 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(288 12 12)"/></g><circle cx="12" cy="12" r="2.3" fill="var(--accent-strong)"/></svg>
    <span class="nav-title">Centro de Flores de Bach</span>
  </div>
  <ul class="nav-list">
    <li><button class="nav-item is-active" id="nav-agenda" aria-current="page" onclick="navTo('agenda')">Agenda</button></li>
    <li><button class="nav-item" id="nav-reporte" onclick="navTo('reporte')">Reporte</button></li>
    <li><button class="nav-item" id="nav-fichas" onclick="navTo('fichas')">Fichas</button></li>
  </ul>
  <button class="nav-item nav-logout" id="nav-logout" onclick="closeNav();logout()">Cerrar sesión</button>
</nav>

<div class="detail-backdrop" id="detail-backdrop" hidden onclick="closeDetail()"></div>
<aside class="detail-panel" id="detail-panel" aria-label="Detalle de la cita" hidden>
  <div class="detail-head">
    <h2 class="detail-title">Detalle de la cita</h2>
    <button class="detail-close" aria-label="Cerrar" onclick="closeDetail()">&times;</button>
  </div>
  <dl class="detail-fields" id="detail-fields"></dl>
  <div class="detail-actions" id="detail-actions"></div>
</aside>

<main class="wrap" id="app" hidden>
  <section id="view-agenda" class="view">
    <div class="view-head">
      <h1 class="view-title">Semana</h1>
      <div class="weeknav">
        <button class="nav-btn" onclick="semana(-1)" aria-label="Semana anterior">&#8249;</button>
        <span class="rango" id="rango"></span>
        <button class="nav-btn" onclick="semana(1)" aria-label="Semana siguiente">&#8250;</button>
      </div>
    </div>
    <div class="calendar" id="cal" style="--days:7;display:none"></div>
  </section>

  <section id="view-reporte" class="view" hidden>
    <div class="view-head">
      <h1 class="view-title">Reporte</h1>
      <div class="seg seg-sm" role="group" aria-label="Período">
        <button class="seg-btn is-active" onclick="setRango(7,this)">7 días</button>
        <button class="seg-btn" onclick="setRango(30,this)">30 días</button>
        <button class="seg-btn" onclick="setRango(90,this)">90 días</button>
      </div>
    </div>
    <p class="rep-rango" id="rep-rango"></p>
    <div id="rep-body"></div>
  </section>

  <section id="view-fichas" class="view" hidden>
    <div class="view-head"><h1 class="view-title">Fichas</h1></div>
    <div class="empty">
      <svg class="mark" width="40" height="40" viewBox="0 0 24 24" aria-hidden="true"><g fill="var(--accent)" fill-opacity="0.55"><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(72 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(144 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(216 12 12)"/><ellipse cx="12" cy="6.4" rx="2.5" ry="4.1" transform="rotate(288 12 12)"/></g><circle cx="12" cy="12" r="2.3" fill="var(--accent-strong)"/></svg>
      <p class="empty-title">Fichas de pacientes</p>
      <p class="empty-sub">Acá vas a poder ver la información de cada paciente, su histórico de citas y tus notas. Disponible muy pronto.</p>
    </div>
  </section>
</main>

<script>
function esc(s){var d=document.createElement("div");d.appendChild(document.createTextNode(s==null?"":String(s)));return d.innerHTML}
function $(id){return document.getElementById(id)}
function base(){return location.pathname.replace(/[/]admin[/]panel[/]?$/,"")}
function fmt(d){return d.toISOString().slice(0,10)}
function fmtCorto(d){return ("0"+d.getDate()).slice(-2)+"/"+("0"+(d.getMonth()+1)).slice(-2)}
function lunes(d){var r=new Date(d);var day=r.getDay();r.setDate(r.getDate()-((day+6)%7));return r}

var HORARIOS={0:{i:9,f:18},1:{i:9,f:18},2:{i:9,f:18},3:{i:9,f:18},4:{i:9,f:17},5:{i:9,f:13}};
var DIAS=["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"];
var offset=0,token="",rangoDias=7;

function MARK(sz){return "<svg class='mark' width='"+sz+"' height='"+sz+"' viewBox='0 0 24 24' aria-hidden='true'><g fill='var(--accent)' fill-opacity='0.55'><ellipse cx='12' cy='6.4' rx='2.5' ry='4.1'/><ellipse cx='12' cy='6.4' rx='2.5' ry='4.1' transform='rotate(72 12 12)'/><ellipse cx='12' cy='6.4' rx='2.5' ry='4.1' transform='rotate(144 12 12)'/><ellipse cx='12' cy='6.4' rx='2.5' ry='4.1' transform='rotate(216 12 12)'/><ellipse cx='12' cy='6.4' rx='2.5' ry='4.1' transform='rotate(288 12 12)'/></g><circle cx='12' cy='12' r='2.3' fill='var(--accent-strong)'/></svg>";}

/* auth */
function doLogin(){var u=$("login-user").value.trim(),p=$("login-pass").value;if(u&&p)login(u,p)}
$("login-user").addEventListener("keydown",function(e){if(e.key==="Enter")doLogin()});
$("login-pass").addEventListener("keydown",function(e){if(e.key==="Enter")doLogin()});
function showApp(){$("login-overlay").style.display="none";$("topbar").hidden=false;$("app").hidden=false;$("cal").style.display="grid"}
function showLogin(err){closeNav();closeDetail();$("login-overlay").style.display="flex";$("topbar").hidden=true;$("app").hidden=true;$("login-error").style.display=err?"block":"none";var p=$("login-pass");if(p)p.value=""}
function onAuthLost(){token="";sessionStorage.removeItem("admin_session");showLogin(true)}
function logout(){token="";sessionStorage.removeItem("admin_session");showLogin(false)}

async function login(u,p){
  var r;
  try{r=await fetch(base()+"/admin/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:u,password:p})})}
  catch(e){$("login-error").textContent="No se pudo conectar. Reintentá.";$("login-error").style.display="block";return}
  if(r.ok){var d=await r.json();token=d.token;sessionStorage.setItem("admin_session",token);showApp();switchView("agenda")}
  else if(r.status===429){$("login-error").textContent="Demasiados intentos. Esperá un momento.";$("login-error").style.display="block"}
  else{sessionStorage.removeItem("admin_session");$("login-error").textContent="Usuario o contraseña incorrectos.";$("login-error").style.display="block"}
}

/* Restaura la sesión: valida el token guardado contra un endpoint admin. */
async function restoreSession(){
  var s=sessionStorage.getItem("admin_session");
  if(!s){return}
  var r;
  try{r=await fetch(base()+"/admin/agenda?fecha="+fmt(new Date()),{headers:{"Authorization":"Bearer "+s}})}
  catch(e){return}
  if(r.ok){token=s;showApp();switchView("agenda")}
  else{sessionStorage.removeItem("admin_session")}
}
restoreSession();

/* vistas */
function switchView(view){
  closeDetail();
  ["agenda","reporte","fichas"].forEach(function(v){
    var s=$("view-"+v);if(s){s.hidden=(v!==view)}
    var n=$("nav-"+v);if(n){n.classList.toggle("is-active",v===view);if(v===view){n.setAttribute("aria-current","page")}else{n.removeAttribute("aria-current")}}
  });
  var labels={agenda:"Agenda",reporte:"Reporte",fichas:"Fichas"};
  var tv=$("topbar-view");if(tv){tv.textContent=labels[view]||""}
  if(view==="agenda"){renderAgenda()}else if(view==="reporte"){loadReporte()}
}

/* navegación (menú hamburguesa) */
function openNav(){$("nav-backdrop").hidden=false;$("nav-drawer").hidden=false;$("nav-toggle").setAttribute("aria-expanded","true")}
function closeNav(){var b=$("nav-backdrop"),d=$("nav-drawer"),t=$("nav-toggle");if(b)b.hidden=true;if(d)d.hidden=true;if(t)t.setAttribute("aria-expanded","false")}
function toggleNav(){if($("nav-drawer").hidden){openNav()}else{closeNav()}}
function navTo(view){closeNav();switchView(view)}

/* panel de detalle de cita (agenda) */
function openDetail(id){
  var c=(window._citas||{})[id];if(!c){return}
  window._detailCita=c;
  var dias=["Domingo","Lunes","Martes","Miércoles","Jueves","Viernes","Sábado"];
  var d=new Date(c.fecha+"T00:00:00");
  var contacto=c.cliente_canal==="telegram"?"Telegram @"+c.cliente_contacto:c.cliente_canal==="whatsapp"?"WhatsApp +"+c.cliente_contacto:(c.cliente_contacto||"—");
  function row(k,v){return "<div><dt>"+esc(k)+"</dt><dd>"+esc(v)+"</dd></div>"}
  $("detail-fields").innerHTML=
    row("Paciente",c.cliente_nombre||"Sin nombre")+
    row("Servicio",(c.servicio_nombre||"Consulta")+" · "+(c.servicio_duracion||60)+" min")+
    row("Profesional",c.profesional_nombre||"—")+
    row("Fecha",dias[d.getDay()]+" "+fmtCorto(d))+
    row("Hora",c.hora)+
    row("Contacto",contacto);
  detailActions();
  $("detail-backdrop").hidden=false;$("detail-panel").hidden=false;
}
function detailActions(){$("detail-actions").innerHTML="<button class='btn-danger' onclick='askCancel()'>Cancelar cita</button>"}
function askCancel(){$("detail-actions").innerHTML="<p class='detail-msg'>¿Seguro que querés cancelar esta cita?</p><div class='confirm-row'><button class='btn-danger' onclick='doCancel()'>Sí, cancelar</button><button class='btn-ghost' onclick='detailActions()'>No</button></div>"}
async function doCancel(){
  var c=window._detailCita;if(!c){return}
  $("detail-actions").innerHTML="<p class='detail-msg'>Cancelando…</p>";
  var r;
  try{r=await fetch(base()+"/admin/cita/cancelar",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},body:JSON.stringify({pk:c.pk,sk:c.sk})})}
  catch(e){$("detail-actions").innerHTML="<p class='detail-msg err'>No se pudo cancelar. Reintentá.</p><button class='btn-ghost' onclick='detailActions()'>Volver</button>";return}
  if(r.status===401||r.status===403){onAuthLost();return}
  if(r.ok){$("detail-fields").innerHTML="";$("detail-actions").innerHTML="<p class='detail-msg ok'>Cita cancelada.</p><button class='btn-ghost' onclick='closeDetail()'>Cerrar</button>";renderAgenda()}
  else{$("detail-actions").innerHTML="<p class='detail-msg err'>No se pudo cancelar.</p><button class='btn-ghost' onclick='detailActions()'>Volver</button>"}
}
function closeDetail(){var b=$("detail-backdrop"),p=$("detail-panel");if(b)b.hidden=true;if(p)p.hidden=true;window._detailCita=null}

document.addEventListener("keydown",function(e){if(e.key!=="Escape"){return}if(!$("detail-panel").hidden){closeDetail()}else if(!$("nav-drawer").hidden){closeNav()}});
function semana(dir){offset+=dir;renderAgenda()}

/* agenda */
async function renderAgenda(){
  var hoy=new Date();var lun=lunes(hoy);lun.setDate(lun.getDate()+offset*7);
  var dias=[];for(var i=0;i<7;i++){var d=new Date(lun);d.setDate(d.getDate()+i);dias.push(d)}
  $("rango").textContent=fmtCorto(dias[0])+" – "+fmtCorto(dias[6]);
  var r;
  try{r=await fetch(base()+"/admin/agenda?desde="+fmt(dias[0])+"&hasta="+fmt(dias[6]),{headers:{"Authorization":"Bearer "+token}})}
  catch(e){return}
  if(r.status===401||r.status===403){onAuthLost();return}
  var data=await r.json();
  window._citas={};
  var citasMap={};
  (data.citas||[]).forEach(function(c,i){c._id=i;window._citas[i]=c;var k=c.fecha+"#"+c.hora;(citasMap[k]=citasMap[k]||[]).push(c)});
  var minH=9,maxH=18;
  var html="<div class='cal-corner'></div>";
  dias.forEach(function(d){
    var dn=DIAS[d.getDay()===0?6:d.getDay()-1];
    var t=fmt(d)===fmt(hoy)?" is-today":"";
    html+="<div class='cal-head"+t+"'>"+dn+"<span class='dnum'>"+fmtCorto(d)+"</span></div>";
  });
  for(var h=minH;h<maxH;h++){
    for(var m=0;m<60;m+=30){
      var hStr=("0"+h).slice(-2)+":"+("0"+m).slice(-2);
      html+="<div class='cal-hour'>"+hStr+"</div>";
      dias.forEach(function(d){
        var dw=d.getDay();var horario=HORARIOS[dw===0?6:dw-1];
        var t=fmt(d)===fmt(hoy)?" is-today":"";
        if(!horario||h<horario.i||h>=horario.f){html+="<div class='cal-cell is-closed"+t+"'><span>·</span></div>";return}
        var citas=citasMap[fmt(d)+"#"+hStr]||[];
        html+="<div class='cal-cell"+t+"'>";
        citas.forEach(function(c){
          var contacto=c.cliente_canal==="telegram"?"Telegram @"+c.cliente_contacto:c.cliente_canal==="whatsapp"?"WhatsApp +"+c.cliente_contacto:(c.cliente_contacto||"");
          var cancel=c.estado==="cancelada"?" cita-cancel":"";
          html+="<div class='cita"+cancel+"' onclick='openDetail("+c._id+")'><span class='cita-dot'></span><div class='cita-body'><div class='nombre'>"+esc(c.cliente_nombre||"Sin nombre")+"</div><div class='servicio'>"+esc(c.servicio_nombre||"Consulta")+" · "+esc(String(c.servicio_duracion||60))+" min</div><div class='contacto'>"+esc(contacto)+"</div></div></div>";
        });
        html+="</div>";
      });
    }
  }
  $("cal").innerHTML=html;
}

/* reporte */
function setRango(dias,btn){
  rangoDias=dias;
  var seg=btn.parentNode;
  seg.querySelectorAll(".seg-btn").forEach(function(b){b.classList.toggle("is-active",b===btn)});
  loadReporte();
}
function skeleton(){return "<div class='skeleton'><div class='sk-line w2'></div><div class='sk-line w1'></div><div class='sk-line w3'></div></div>"}
async function loadReporte(){
  var hasta=new Date();var desde=new Date();desde.setDate(desde.getDate()-(rangoDias-1));
  $("rep-rango").textContent="Del "+fmtCorto(desde)+" al "+fmtCorto(hasta);
  var rb=$("rep-body");rb.innerHTML=skeleton();
  var r;
  try{r=await fetch(base()+"/admin/reporte?desde="+fmt(desde)+"&hasta="+fmt(hasta),{headers:{"Authorization":"Bearer "+token}})}
  catch(e){rb.innerHTML="<p class='rep-error'>No se pudo cargar el reporte.</p>";return}
  if(r.status===401||r.status===403){onAuthLost();return}
  if(!r.ok){rb.innerHTML="<p class='rep-error'>No se pudo cargar el reporte.</p>";return}
  renderReporte(await r.json());
}
function renderReporte(data){
  var rb=$("rep-body");
  var total=data.total||0;
  var conf=(data.por_estado&&data.por_estado.confirmada)||0;
  var canc=(data.por_estado&&data.por_estado.cancelada)||0;
  var tasa=Math.round((data.tasa_cancelacion||0)*100);
  if(total===0){
    rb.innerHTML="<div class='empty'>"+MARK(40)+"<p class='empty-title'>Sin citas en este período</p><p class='empty-sub'>Cuando se agenden citas en el rango elegido, el resumen va a aparecer acá.</p></div>";
    return;
  }
  var servicios=Object.keys(data.por_servicio||{}).map(function(k){return [k,data.por_servicio[k]]});
  servicios.sort(function(a,b){return b[1]-a[1]});
  var maxC=servicios.reduce(function(mx,s){return Math.max(mx,s[1])},1);
  var h="<div class='rep-summary'>";
  h+="<div class='lead'><span class='lead-num'>"+total+"</span><span class='lead-label'>"+(total===1?"cita en el período":"citas en el período")+"</span></div>";
  h+="<dl class='statrow'><div class='stat'><dt>Confirmadas</dt><dd>"+conf+"</dd></div><div class='stat'><dt>Canceladas</dt><dd class='dd-clay'>"+canc+"</dd></div><div class='stat'><dt>Tasa de cancelación</dt><dd>"+tasa+"%</dd></div></dl>";
  h+="<div class='meter' role='img' aria-label='Tasa de cancelación "+tasa+" por ciento'><span class='meter-fill' data-w='"+tasa+"%'></span></div></div>";
  h+="<section class='rep-block'><h2 class='block-title'>Por servicio</h2><ul class='bars'>";
  servicios.forEach(function(s){
    var pct=Math.round(s[1]/maxC*100);
    h+="<li class='bar-row'><span class='bar-name'>"+esc(s[0])+"</span><span class='bar-track'><span class='bar-fill' data-w='"+pct+"%'></span></span><span class='bar-val'>"+s[1]+"</span></li>";
  });
  h+="</ul></section>";
  rb.innerHTML=h;
  requestAnimationFrame(function(){
    rb.querySelectorAll("[data-w]").forEach(function(el,i){el.style.transitionDelay=(i*55)+"ms";el.style.width=el.getAttribute("data-w")});
  });
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


async def _send_telegram(chat_id: int, text: str, reply_markup: dict | None = None):
    from config import TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json=payload)


async def _answer_telegram_callback(callback_query_id: str):
    """Confirma el callback ante Telegram para detener el spinner del botón."""
    from config import TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json={"callback_query_id": callback_query_id})


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
        t0 = time.monotonic()
        response = chatbot.handle_message("whatsapp", from_number, clean)
        duration_ms = (time.monotonic() - t0) * 1000
        log_message_handled(
            logger,
            channel="whatsapp",
            user_id=from_number,
            action="message_handled",
            duration_ms=duration_ms,
        )
        await _send_whatsapp(from_number, response)
    except (KeyError, IndexError, ValueError):
        pass
    return {"status": "ok"}


# ── Telegram ──────────────────────────────────────────────────────────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # Empty configured secret fails closed: reject everything
    if not TELEGRAM_WEBHOOK_SECRET or secret != TELEGRAM_WEBHOOK_SECRET:
        logger.warning("telegram webhook rejected: secret mismatch")
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    data = await request.json()

    # Updates de botones inline (callback_query): el callback_data del botón
    # se procesa igual que texto del usuario — misma sanitización y rate limit.
    if isinstance(data, dict) and "callback_query" in data:
        if not validate_telegram_callback(data):
            logger.debug("telegram webhook: callback failed structural validation, skipping")
            return {"status": "ok"}
        cq = data["callback_query"]
        user_id = str(cq["from"]["id"])
        chat_id = cq["message"]["chat"]["id"]
        clean = validate_message_text(cq["data"])
        if clean is None:
            # callback_data inválido u oversized solo puede ser un payload
            # forjado (los botones legítimos llevan datos cortos): se ignora
            return {"status": "ok"}
        await _answer_telegram_callback(cq["id"])
        try:
            t0 = time.monotonic()
            response = chatbot.handle_message("telegram", user_id, clean)
            duration_ms = (time.monotonic() - t0) * 1000
            log_message_handled(
                logger,
                channel="telegram",
                user_id=user_id,
                action="callback_handled",
                duration_ms=duration_ms,
            )
            await _send_telegram(chat_id, response, reply_markup=build_reply_markup(response))
        except (KeyError, TypeError) as e:
            logger.error("telegram webhook: callback handling error", extra={"error": str(e)})
        return {"status": "ok"}

    if not validate_telegram_payload(data):
        logger.debug("telegram webhook: payload failed structural validation, skipping")
        return {"status": "ok"}
    msg = data["message"]
    raw_text = msg.get("text", "")
    user_id = str(msg["from"]["id"])
    chat_id = msg["chat"]["id"]
    clean = validate_message_text(raw_text)
    if clean is None:
        if is_oversized(raw_text):
            await _send_telegram(
                chat_id,
                "Tu mensaje es demasiado largo (máximo 500 caracteres).",
            )
        return {"status": "ok"}
    try:
        t0 = time.monotonic()
        response = chatbot.handle_message("telegram", user_id, clean)
        duration_ms = (time.monotonic() - t0) * 1000
        log_message_handled(
            logger,
            channel="telegram",
            user_id=user_id,
            action="message_handled",
            duration_ms=duration_ms,
        )
        await _send_telegram(chat_id, response, reply_markup=build_reply_markup(response))
    except (KeyError, TypeError) as e:
        logger.error("telegram webhook: message handling error", extra={"error": str(e)})
    return {"status": "ok"}


# ── Mangum handler ────────────────────────────────────────────────────
_mangum = Mangum(app, lifespan="off")


def handler(event, context):
    """Lambda entry point - maneja tanto API Gateway como EventBridge warm-up."""
    if context is not None and hasattr(context, "aws_request_id"):
        logger.append_keys(request_id=context.aws_request_id)
    # EventBridge warm-up o evento no-HTTP — respond quickly without INFO logging
    if "httpMethod" not in event and "requestContext" not in event:
        logger.debug("lambda warm-up event received")
        return {"statusCode": 200, "body": "warm"}
    return _mangum(event, context)
