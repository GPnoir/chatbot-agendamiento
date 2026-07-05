"""Lambda para enviar recordatorios 24h antes de la cita."""
import json
import os
from datetime import datetime, timedelta

import boto3
import httpx

TABLE_NAME = os.getenv("DYNAMODB_TABLE", "chatbot-agendamiento")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
# Fuera de la ventana de 24h de Meta, el texto libre se rechaza (error 131047);
# solo pasan templates aprobados. El recordatorio es proactivo, así que casi
# siempre está fuera de la ventana. Vacío ⇒ texto libre (dev/local o mientras
# el template no esté aprobado). Ver docs/whatsapp-templates.md.
WHATSAPP_REMINDER_TEMPLATE = os.getenv("WHATSAPP_REMINDER_TEMPLATE", "")
WHATSAPP_TEMPLATE_LANG = os.getenv("WHATSAPP_TEMPLATE_LANG", "es")


class WhatsAppSendError(Exception):
    """Meta rechazó el envío (p. ej. fuera de la ventana de 24h, token inválido).

    Se usa para NO marcar el recordatorio como enviado cuando en realidad rebotó.
    """


def get_table():
    return boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")).Table(TABLE_NAME)


def get_citas_proximas_24h():
    """Obtiene citas confirmadas en las próximas 24h que no han sido notificadas."""
    table = get_table()
    ahora = datetime.utcnow() - timedelta(hours=4)  # Chile UTC-4
    manana = ahora + timedelta(hours=24)
    fecha_hoy = ahora.date().isoformat()
    fecha_manana = manana.date().isoformat()

    citas = []
    for fecha in [fecha_hoy, fecha_manana]:
        resp = table.scan(
            FilterExpression="begins_with(PK, :prefix) AND fecha = :fecha AND estado = :estado AND attribute_not_exists(recordatorio_enviado)",
            ExpressionAttributeValues={
                ":prefix": "APPOINTMENT#",
                ":fecha": fecha,
                ":estado": "confirmada",
            },
        )
        for item in resp["Items"]:
            # Verificar que la cita es en las próximas 24h
            cita_dt = datetime.fromisoformat(f"{item['fecha']}T{item['hora']}:00")
            if ahora <= cita_dt <= manana:
                citas.append(item)
    return citas


def get_cliente(cliente_id):
    """Obtiene datos del cliente."""
    table = get_table()
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq("CLIENT"),
        FilterExpression=boto3.dynamodb.conditions.Attr("id").eq(cliente_id),
    )
    return resp["Items"][0] if resp["Items"] else None


def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    httpx.post(url, json={"chat_id": int(chat_id), "text": text})


def _wa_url():
    return f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"


def _wa_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}


def _check_meta_response(resp) -> None:
    """Lanza WhatsAppSendError si Meta no aceptó el mensaje.

    Meta responde 2xx con {"messages":[...]} al aceptar, o >=400 con
    {"error":{"code","message"}} al rechazar. Solo se propaga el code/message
    de Meta (no traen secretos); nunca se incluye el token ni el payload.
    """
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
            detalle = f"code={err.get('code')} {err.get('message')}"
        except Exception:
            detalle = "sin cuerpo de error"
        raise WhatsAppSendError(f"Meta {resp.status_code}: {detalle}")


def send_whatsapp(to, text):
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    resp = httpx.post(_wa_url(), json=payload, headers=_wa_headers(), timeout=10.0)
    _check_meta_response(resp)


def send_whatsapp_template(to, template, lang, params):
    """Envía un template aprobado. `params` van al componente body en orden ({{1}}, {{2}}...)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": lang},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
            ],
        },
    }
    resp = httpx.post(_wa_url(), json=payload, headers=_wa_headers(), timeout=10.0)
    _check_meta_response(resp)


def marcar_recordatorio_enviado(pk, sk):
    """Marca la cita como notificada para no enviar duplicados."""
    table = get_table()
    table.update_item(
        Key={"PK": pk, "SK": sk},
        UpdateExpression="SET recordatorio_enviado = :t",
        ExpressionAttributeValues={":t": True},
    )


def set_confirm_session(user_id, cita):
    """Pone al usuario en estado CONFIRM_ATTENDANCE."""
    import time
    table = get_table()
    import json
    data = json.dumps({"cita_pendiente": {"PK": cita["PK"], "SK": cita["SK"]}})
    table.put_item(Item={
        "PK": "SESSION",
        "SK": f"USER#{user_id}",
        "state": "CONFIRM_ATTENDANCE",
        "data_json": data,
        "ttl": int(time.time()) + 86400,  # 24h TTL
    })


def handler(event, context):
    """Entry point para EventBridge scheduled rule."""
    citas = get_citas_proximas_24h()
    enviados = 0

    for cita in citas:
        cliente = get_cliente(cita["cliente_id"])
        if not cliente:
            continue

        texto = (
            f"🔔 Recordatorio de cita\n\n"
            f"📋 {cita.get('servicio_nombre', 'Consulta')}\n"
            f"👩‍⚕️ {cita.get('profesional_nombre', '')}\n"
            f"📅 Mañana a las {cita['hora']}\n\n"
            f"¿Confirmas tu asistencia? Responde *si* o *no*"
        )

        canal = cliente.get("canal", "")
        canal_user_id = cliente.get("canal_user_id", "")

        try:
            if canal == "telegram":
                send_telegram(canal_user_id, texto)
            elif canal == "whatsapp":
                if WHATSAPP_REMINDER_TEMPLATE:
                    # Fuera de la ventana de 24h (lo normal en un recordatorio
                    # proactivo) Meta solo acepta templates. Params: servicio y
                    # cuándo ({{1}}, {{2}}) — ver docs/whatsapp-templates.md.
                    cuando = f"{cita['fecha']} a las {cita['hora']}"
                    send_whatsapp_template(
                        canal_user_id,
                        WHATSAPP_REMINDER_TEMPLATE,
                        WHATSAPP_TEMPLATE_LANG,
                        [cita.get("servicio_nombre", "Consulta"), cuando],
                    )
                else:
                    send_whatsapp(canal_user_id, texto)
            marcar_recordatorio_enviado(cita["PK"], cita["SK"])
            # Poner al usuario en estado de confirmación
            set_confirm_session(canal_user_id, cita)
            enviados += 1
        except Exception as e:
            print(f"Error enviando recordatorio a {canal_user_id}: {e}")

    print(f"Recordatorios enviados: {enviados}/{len(citas)}")
    return {"statusCode": 200, "body": f"Enviados: {enviados}"}
