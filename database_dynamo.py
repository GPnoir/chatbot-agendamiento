"""Base de datos DynamoDB para agendamiento (reemplazo de SQLite)."""
import os
from datetime import date, datetime, timedelta
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr

from config import SERVICIOS, PROFESIONALES, HORARIOS_DEFAULT
import google_calendar

TABLE_NAME = os.getenv("DYNAMODB_TABLE", "chatbot-agendamiento")
_table = None


def get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
        _table = dynamodb.Table(TABLE_NAME)
    return _table


def init_db():
    """Seed data si la tabla está vacía."""
    table = get_table()
    # Check si ya hay servicios
    resp = table.query(KeyConditionExpression=Key("PK").eq("SERVICE"), Limit=1)
    if resp["Items"]:
        return
    # Seed servicios
    with table.batch_writer() as batch:
        for i, s in enumerate(SERVICIOS, 1):
            batch.put_item(Item={
                "PK": "SERVICE", "SK": f"SERVICE#{i}",
                "id": i, "nombre": s["nombre"],
                "duracion_min": s["duracion"],
                "descripcion": s.get("descripcion", ""),
                "activo": True,
            })
        # Seed profesionales
        for i, p in enumerate(PROFESIONALES, 1):
            batch.put_item(Item={
                "PK": "PROFESSIONAL", "SK": f"PROF#{i}",
                "id": i, "nombre": p["nombre"],
                "especialidad": p.get("especialidad", ""),
                "activo": True,
            })
            # Horarios por profesional
            for dia, h in HORARIOS_DEFAULT.items():
                batch.put_item(Item={
                    "PK": f"SCHEDULE#{i}", "SK": f"DAY#{dia}",
                    "profesional_id": i, "dia_semana": dia,
                    "hora_inicio": h["inicio"], "hora_fin": h["fin"],
                })


def get_servicios() -> list[dict]:
    table = get_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq("SERVICE"),
        FilterExpression=Attr("activo").eq(True),
    )
    return resp["Items"]


def get_profesionales() -> list[dict]:
    table = get_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq("PROFESSIONAL"),
        FilterExpression=Attr("activo").eq(True),
    )
    return resp["Items"]


def get_or_create_cliente(canal: str, canal_user_id: str, nombre: str = None) -> dict:
    table = get_table()
    sk = f"CHAN#{canal}#{canal_user_id}"
    resp = table.get_item(Key={"PK": "CLIENT", "SK": sk})
    if "Item" in resp:
        item = resp["Item"]
        if nombre and not item.get("nombre"):
            table.update_item(
                Key={"PK": "CLIENT", "SK": sk},
                UpdateExpression="SET nombre = :n",
                ExpressionAttributeValues={":n": nombre},
            )
            item["nombre"] = nombre
        return item
    item = {
        "PK": "CLIENT", "SK": sk,
        "id": sk, "nombre": nombre or "",
        "canal": canal, "canal_user_id": canal_user_id,
        "created_at": datetime.utcnow().isoformat(),
    }
    table.put_item(Item=item)
    return item


def get_horas_disponibles(profesional_id: int, fecha: date, servicio_duracion: int) -> list[str]:
    """Retorna horas disponibles para un profesional en una fecha, validando solapamiento y bloqueos."""
    table = get_table()
    dia_semana = fecha.weekday()
    fecha_str = fecha.isoformat()

    # Verificar bloqueos
    bloqueos = get_bloqueos(profesional_id, fecha_str)
    if bloqueos["dia_completo"]:
        return []

    # Obtener horario
    resp = table.get_item(Key={"PK": f"SCHEDULE#{profesional_id}", "SK": f"DAY#{dia_semana}"})
    if "Item" not in resp:
        return []
    horario = resp["Item"]

    # Obtener citas existentes ese día (con duración)
    citas_resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"APPT#PROF#{profesional_id}") & Key("GSI1SK").begins_with(f"DATE#{fecha_str}"),
        FilterExpression=Attr("estado").eq("confirmada"),
    )
    bloques_ocupados = []
    for item in citas_resp["Items"]:
        h, m = map(int, item["hora"].split(":"))
        inicio_min = h * 60 + m
        dur = int(item.get("servicio_duracion", 60))
        bloques_ocupados.append((inicio_min, inicio_min + dur))

    horas_bloqueadas = set(bloqueos["horas"])

    # Generar slots y verificar solapamiento
    inicio = datetime.strptime(horario["hora_inicio"], "%H:%M")
    fin = datetime.strptime(horario["hora_fin"], "%H:%M")
    disponibles = []
    current = inicio
    while current + timedelta(minutes=servicio_duracion) <= fin:
        hora_str = current.strftime("%H:%M")
        if hora_str in horas_bloqueadas:
            current += timedelta(minutes=30)
            continue
        slot_inicio = current.hour * 60 + current.minute
        slot_fin = slot_inicio + servicio_duracion
        solapa = any(
            slot_inicio < ocu_fin and slot_fin > ocu_inicio
            for ocu_inicio, ocu_fin in bloques_ocupados
        )
        if not solapa:
            disponibles.append(hora_str)
        current += timedelta(minutes=30)
    return disponibles


def get_fechas_disponibles(profesional_id: int, servicio_duracion: int, dias: int = 7) -> list[date]:
    hoy = date.today()
    fechas = []
    for i in range(1, dias + 1):
        d = hoy + timedelta(days=i)
        if get_horas_disponibles(profesional_id, d, servicio_duracion):
            fechas.append(d)
    return fechas


def crear_cita(cliente_id: str, servicio_id: int, profesional_id: int, fecha: str, hora: str) -> dict:
    table = get_table()
    cita_id = f"{fecha}#{hora}#{profesional_id}"
    item = {
        "PK": f"APPOINTMENT#{cliente_id}",
        "SK": f"DATE#{fecha}#{hora}",
        "GSI1PK": f"APPT#PROF#{profesional_id}",
        "GSI1SK": f"DATE#{fecha}#{hora}",
        "id": cita_id,
        "cliente_id": cliente_id,
        "servicio_id": servicio_id,
        "profesional_id": profesional_id,
        "fecha": fecha,
        "hora": hora,
        "estado": "confirmada",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    # Agregar nombres y duración para lectura fácil
    servicios = get_servicios()
    serv = next((s for s in servicios if s["id"] == servicio_id), None)
    if serv:
        item["servicio_nombre"] = serv["nombre"]
        item["servicio_duracion"] = serv["duracion_min"]
    profesionales = get_profesionales()
    prof = next((p for p in profesionales if p["id"] == profesional_id), None)
    if prof:
        item["profesional_nombre"] = prof["nombre"]
    # Sync best-effort a Google Calendar (issue #14): guardamos el event id
    # para poder borrar/actualizar el evento al cancelar o modificar la cita.
    event_id = google_calendar.sync_create(item)
    if event_id:
        item["gcal_event_id"] = event_id
    table.put_item(Item=item)
    return item


def get_citas_cliente(cliente_id: str) -> list[dict]:
    table = get_table()
    hoy = date.today().isoformat()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"APPOINTMENT#{cliente_id}") & Key("SK").gte(f"DATE#{hoy}"),
        FilterExpression=Attr("estado").eq("confirmada"),
    )
    items = sorted(resp["Items"], key=lambda x: (x["fecha"], x["hora"]))
    return items


def get_historial_cliente(cliente_id: str) -> list[dict]:
    """Retorna todas las citas del cliente (pasadas y canceladas)."""
    table = get_table()
    resp = table.query(KeyConditionExpression=Key("PK").eq(f"APPOINTMENT#{cliente_id}"))
    items = sorted(resp["Items"], key=lambda x: (x["fecha"], x["hora"]), reverse=True)
    return items


# ── Fichas de pacientes (panel admin) ─────────────────────────────────
def get_clientes() -> list[dict]:
    """Lista todos los clientes (pacientes), ordenados por nombre."""
    table = get_table()
    resp = table.query(KeyConditionExpression=Key("PK").eq("CLIENT"))
    items = resp.get("Items", [])
    return sorted(items, key=lambda c: (c.get("nombre") or "").lower())


def get_cliente(cliente_id: str) -> Optional[dict]:
    """Obtiene un cliente por su id (la SK CHAN#...)."""
    resp = get_table().get_item(Key={"PK": "CLIENT", "SK": cliente_id})
    return resp.get("Item")


def agregar_nota(cliente_id: str, texto: str) -> dict:
    """Agrega una nota del terapeuta a la ficha de un cliente."""
    table = get_table()
    ts = datetime.utcnow().isoformat()
    item = {
        "PK": f"NOTE#{cliente_id}",
        "SK": ts,
        "cliente_id": cliente_id,
        "texto": texto,
        "created_at": ts,
    }
    table.put_item(Item=item)
    return item


def get_notas_cliente(cliente_id: str) -> list[dict]:
    """Notas de un cliente, de la más reciente a la más antigua."""
    table = get_table()
    resp = table.query(KeyConditionExpression=Key("PK").eq(f"NOTE#{cliente_id}"))
    items = resp.get("Items", [])
    return sorted(items, key=lambda n: n.get("created_at", ""), reverse=True)


def cancelar_cita(cita_pk: str, cita_sk: str):
    table = get_table()
    # Leemos la cita primero para recuperar el event id de Google Calendar.
    resp = table.get_item(Key={"PK": cita_pk, "SK": cita_sk})
    item = resp.get("Item")
    table.update_item(
        Key={"PK": cita_pk, "SK": cita_sk},
        UpdateExpression="SET estado = :s, updated_at = :u",
        ExpressionAttributeValues={":s": "cancelada", ":u": datetime.utcnow().isoformat()},
    )
    # Sync best-effort a Google Calendar (issue #14): borra el evento asociado.
    if item and item.get("gcal_event_id"):
        google_calendar.sync_cancel(item["gcal_event_id"])


def modificar_cita(cita_pk: str, cita_sk: str, nueva_fecha: str, nueva_hora: str):
    table = get_table()
    # Obtener cita actual
    resp = table.get_item(Key={"PK": cita_pk, "SK": cita_sk})
    if "Item" not in resp:
        return
    cita = resp["Item"]
    # Cancelar la vieja
    cancelar_cita(cita_pk, cita_sk)
    # Crear nueva
    crear_cita(cita["cliente_id"], cita["servicio_id"], cita["profesional_id"], nueva_fecha, nueva_hora)


def get_citas_rango(desde: str, hasta: str) -> list[dict]:
    """Retorna todas las citas (cualquier estado) con fecha entre desde y hasta.

    Las fechas son ISO (YYYY-MM-DD), ambas inclusive. Pensado para reportes:
    incluye canceladas y completadas, a diferencia de get_citas_cliente.
    """
    table = get_table()
    items: list[dict] = []
    scan_kwargs = {
        "FilterExpression": Attr("PK").begins_with("APPOINTMENT#")
        & Attr("fecha").between(desde, hasta),
    }
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(items, key=lambda x: (x["fecha"], x["hora"]))


def resumen_citas_rango(desde: str, hasta: str) -> dict:
    """Agrega métricas de citas en un rango de fechas (issue #15).

    Retorna: total, por_estado (estado → cantidad), por_servicio
    (nombre → cantidad) y tasa_cancelacion (canceladas / total, 0.0 si
    no hay citas).
    """
    citas = get_citas_rango(desde, hasta)
    por_estado: dict[str, int] = {}
    por_servicio: dict[str, int] = {}
    for c in citas:
        estado = c.get("estado", "desconocido")
        por_estado[estado] = por_estado.get(estado, 0) + 1
        servicio = c.get("servicio_nombre", "Sin servicio")
        por_servicio[servicio] = por_servicio.get(servicio, 0) + 1
    total = len(citas)
    canceladas = por_estado.get("cancelada", 0)
    return {
        "desde": desde,
        "hasta": hasta,
        "total": total,
        "por_estado": por_estado,
        "por_servicio": por_servicio,
        "tasa_cancelacion": (canceladas / total) if total else 0.0,
    }


def bloquear_fecha(profesional_id: int, fecha: str, motivo: str = ""):
    """Bloquea un día completo para un profesional."""
    table = get_table()
    table.put_item(Item={
        "PK": f"BLOCK#{profesional_id}",
        "SK": f"DATE#{fecha}",
        "profesional_id": profesional_id,
        "fecha": fecha,
        "motivo": motivo,
    })


def bloquear_hora(profesional_id: int, fecha: str, hora: str):
    """Bloquea una hora específica."""
    table = get_table()
    table.put_item(Item={
        "PK": f"BLOCK#{profesional_id}",
        "SK": f"DATE#{fecha}#{hora}",
        "profesional_id": profesional_id,
        "fecha": fecha,
        "hora": hora,
    })


def get_bloqueos(profesional_id: int, fecha: str) -> dict:
    """Retorna bloqueos para un profesional en una fecha. {'dia_completo': bool, 'horas': [...]}"""
    table = get_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"BLOCK#{profesional_id}") & Key("SK").begins_with(f"DATE#{fecha}"),
    )
    result = {"dia_completo": False, "horas": []}
    for item in resp["Items"]:
        if "hora" in item:
            result["horas"].append(item["hora"])
        else:
            result["dia_completo"] = True
    return result


def desbloquear_fecha(profesional_id: int, fecha: str):
    """Elimina bloqueos de un día."""
    table = get_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"BLOCK#{profesional_id}") & Key("SK").begins_with(f"DATE#{fecha}"),
    )
    with table.batch_writer() as batch:
        for item in resp["Items"]:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
