"""Lambda programada: le pregunta a la terapeuta si una cita ya pasada se realizó.

Busca citas confirmadas cuyo horario ya ocurrió (entre ~1 y ~25h atrás) y que
todavía no se preguntaron, y le manda a la terapeuta (ADMIN_USER_ID) un mensaje
por Telegram con botones "Realizada" / "No asistió". Al tocar un botón, el
webhook (lambda_handler) marca el estado con marcar_estado_cita.

Reusa el patrón de reminder_handler.py (Lambda liviana, sin importar la app).
"""
import os
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key
import httpx

TABLE_NAME = os.getenv("DYNAMODB_TABLE", "chatbot-agendamiento")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "1569695377")
_CHILE_OFFSET = timedelta(hours=-4)


def get_table():
    return boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")).Table(TABLE_NAME)


def get_citas_por_consultar(ahora: datetime = None) -> list[dict]:
    """Confirmadas cuyo horario pasó hace entre 1 y 25h y aún no se preguntaron."""
    table = get_table()
    ahora = ahora or (datetime.utcnow() + _CHILE_OFFSET)
    # Ventana de 24h (no más): así abarca a lo sumo 2 fechas de calendario y las
    # dos del set las cubren. La Lambda corre cada hora, así que una cita se
    # pregunta dentro de ~1h de terminada; las 24h son colchón.
    limite_inf = ahora - timedelta(hours=24)
    limite_sup = ahora - timedelta(hours=1)
    fechas = {limite_inf.date().isoformat(), ahora.date().isoformat()}
    citas = []
    for fecha in fechas:
        resp = table.scan(
            FilterExpression=(
                "begins_with(PK, :p) AND fecha = :f AND estado = :e "
                "AND attribute_not_exists(atencion_consultada)"
            ),
            ExpressionAttributeValues={":p": "APPOINTMENT#", ":f": fecha, ":e": "confirmada"},
        )
        for it in resp["Items"]:
            cita_dt = datetime.fromisoformat(f"{it['fecha']}T{it['hora']}:00")
            if limite_inf <= cita_dt <= limite_sup:
                citas.append(it)
    return citas


def _nombre_cliente(cliente_id: str) -> str:
    if not cliente_id:
        return "(sin nombre)"
    resp = get_table().get_item(Key={"PK": "CLIENT", "SK": cliente_id})
    return (resp.get("Item") or {}).get("nombre") or "(sin nombre)"


def _callback(estado: str, cita: dict) -> str:
    # Compacto (< 64 bytes): att|estado|fecha#hora#prof. El webhook lo resuelve.
    return f"att|{estado}|{cita['fecha']}#{cita['hora']}#{cita['profesional_id']}"


def enviar_prompt(cita: dict) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    texto = (
        "¿Se realizó esta atención?\n\n"
        f"👤 {_nombre_cliente(cita.get('cliente_id'))}\n"
        f"📋 {cita.get('servicio_nombre', 'Consulta')}\n"
        f"📅 {cita['fecha']} a las {cita['hora']}"
    )
    markup = {"inline_keyboard": [[
        {"text": "✓ Realizada", "callback_data": _callback("completada", cita)},
        {"text": "✕ No asistió", "callback_data": _callback("no_show", cita)},
    ]]}
    httpx.post(url, json={"chat_id": int(ADMIN_USER_ID), "text": texto, "reply_markup": markup}, timeout=10.0)


def marcar_consultada(pk: str, sk: str) -> None:
    get_table().update_item(
        Key={"PK": pk, "SK": sk},
        UpdateExpression="SET atencion_consultada = :t",
        ExpressionAttributeValues={":t": True},
    )


def handler(event, context):
    """Entry point para EventBridge scheduled rule."""
    citas = get_citas_por_consultar()
    enviados = 0
    for cita in citas:
        try:
            enviar_prompt(cita)
            marcar_consultada(cita["PK"], cita["SK"])
            enviados += 1
        except Exception as e:  # best-effort: un fallo no frena al resto
            print(f"Error en prompt de atención: {e}")
    print(f"Prompts de atención enviados: {enviados}/{len(citas)}")
    return {"statusCode": 200, "body": f"Enviados: {enviados}"}
