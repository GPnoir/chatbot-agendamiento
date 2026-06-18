"""Base de datos DynamoDB para agendamiento (reemplazo de SQLite)."""
import os
from datetime import date, datetime, timedelta
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr

from config import SERVICIOS, PROFESIONALES, HORARIOS_DEFAULT, TRAMO_DEFAULT, TRAMOS_PRECIO
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
    """Seed data si la tabla está vacía; si ya existe, sincroniza el catálogo."""
    table = get_table()
    # Check si ya hay servicios
    resp = table.query(KeyConditionExpression=Key("PK").eq("SERVICE"), Limit=1)
    if resp["Items"]:
        # Tabla ya sembrada: aplicar cambios de catálogo (precios por tramo,
        # servicios retirados) sin re-sembrar a mano.
        _sync_catalogo_servicios(table)
        return
    # Seed servicios
    with table.batch_writer() as batch:
        for i, s in enumerate(SERVICIOS, 1):
            batch.put_item(Item={
                "PK": "SERVICE", "SK": f"SERVICE#{i}",
                "id": i, "nombre": s["nombre"],
                "duracion_min": s["duracion"],
                "descripcion": s.get("descripcion", ""),
                "precios": s.get("precios", {}),
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


def _sync_catalogo_servicios(table) -> None:
    """Idempotente: actualiza el catálogo de servicios sobre una tabla ya
    sembrada (precios por tramo) y desactiva los servicios retirados de config
    (p. ej. 'Preparación de esencias'). Permite que cambios de catálogo lleguen
    a producción sin re-sembrar a mano."""
    # Upsert de los servicios vigentes (id estable por índice).
    for i, s in enumerate(SERVICIOS, 1):
        table.update_item(
            Key={"PK": "SERVICE", "SK": f"SERVICE#{i}"},
            UpdateExpression=(
                "SET #n = :n, duracion_min = :d, descripcion = :de, "
                "precios = :p, activo = :a, id = :id"
            ),
            ExpressionAttributeNames={"#n": "nombre"},
            ExpressionAttributeValues={
                ":n": s["nombre"], ":d": s["duracion"],
                ":de": s.get("descripcion", ""), ":p": s.get("precios", {}),
                ":a": True, ":id": i,
            },
        )
    # Desactivar cualquier SERVICE# con índice mayor al catálogo actual.
    resp = table.query(KeyConditionExpression=Key("PK").eq("SERVICE"))
    for it in resp.get("Items", []):
        try:
            idx = int(it.get("id", 0))
        except (TypeError, ValueError):
            continue
        if idx > len(SERVICIOS) and it.get("activo"):
            table.update_item(
                Key={"PK": it["PK"], "SK": it["SK"]},
                UpdateExpression="SET activo = :a",
                ExpressionAttributeValues={":a": False},
            )


def get_servicios() -> list[dict]:
    table = get_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq("SERVICE"),
        FilterExpression=Attr("activo").eq(True),
    )
    return resp["Items"]


def get_precios_por_servicio() -> dict:
    """Mapa {servicio_id(int): {tramo: precio(int)}} con los precios vigentes.

    Sirve para resolver el precio de una cita cuando le falta el snapshot
    (citas creadas antes de que se guardara `precio`).
    """
    out: dict = {}
    for s in get_servicios():
        try:
            sid = int(s.get("id"))
        except (TypeError, ValueError):
            continue
        precios = s.get("precios") or {}
        out[sid] = {k: int(v) for k, v in precios.items()}
    return out


def precio_efectivo(cita: dict, precios_por_servicio: Optional[dict] = None) -> Optional[int]:
    """Precio a mostrar/contabilizar para una cita.

    Usa el snapshot `precio` si está; si falta (cita vieja), lo deriva del
    `tramo` de la cita (por defecto 'adulto') contra los precios vigentes del
    servicio. Así el panel nunca muestra '-' con el tramo por defecto y los
    reportes contabilizan también las citas sin snapshot.
    """
    precio = cita.get("precio")
    if precio not in (None, ""):
        try:
            p = int(precio)
            if p > 0:
                return p
        except (TypeError, ValueError):
            pass
    if precios_por_servicio is None:
        precios_por_servicio = get_precios_por_servicio()
    try:
        sid = int(cita.get("servicio_id"))
    except (TypeError, ValueError):
        return None
    precios = precios_por_servicio.get(sid) or {}
    tramo = cita.get("tramo") or TRAMO_DEFAULT
    if tramo in precios:
        return precios[tramo]
    if TRAMO_DEFAULT in precios:
        return precios[TRAMO_DEFAULT]
    return None


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
    # DynamoDB devuelve los números como Decimal: si la duración llega leída
    # fresca de la tabla (p. ej. al reagendar), timedelta() la rechaza. La
    # normalizamos a int para proteger a todos los llamadores.
    servicio_duracion = int(servicio_duracion)
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


def get_proximo_slot(profesional_id: int, servicio_duracion: int, dias: int = 14):
    """Primer (date, 'HH:MM') disponible en los próximos `dias` días, o None.

    Reagendamiento inteligente (#11): tras cancelar, se ofrece este slot.
    """
    hoy = date.today()
    for i in range(1, dias + 1):
        d = hoy + timedelta(days=i)
        horas = get_horas_disponibles(profesional_id, d, int(servicio_duracion))
        if horas:
            return d, horas[0]
    return None


class SlotNoDisponibleError(Exception):
    """El horario solicitado para el profesional ya está reservado.

    Se levanta cuando no se puede tomar el lock atómico del slot (otra cita
    confirmada lo ocupa). Evita la doble reserva del mismo horario.
    """

    def __init__(self, profesional_id, fecha: str, hora: str):
        self.profesional_id = profesional_id
        self.fecha = fecha
        self.hora = hora
        super().__init__(f"Slot ocupado: prof {profesional_id} {fecha} {hora}")


def _slot_pk(profesional_id, fecha: str, hora: str) -> str:
    return f"SLOT#{profesional_id}#{fecha}#{hora}"


def _acquire_slot(table, profesional_id, fecha: str, hora: str, cliente_id) -> None:
    """Reserva atómica del horario del profesional: un único confirmado por slot.

    Usa una escritura condicional sobre un ítem-lock dedicado. Si ya existe
    (otra cita tomó el horario), lanza SlotNoDisponibleError.
    """
    try:
        table.put_item(
            Item={
                "PK": _slot_pk(profesional_id, fecha, hora),
                "SK": "LOCK",
                "cliente_id": cliente_id,
                "profesional_id": profesional_id,
                "fecha": fecha,
                "hora": hora,
                "created_at": datetime.utcnow().isoformat(),
            },
            ConditionExpression=Attr("PK").not_exists(),
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise SlotNoDisponibleError(profesional_id, fecha, hora)


def _release_slot(table, profesional_id, fecha: str, hora: str) -> None:
    """Libera el lock del horario (al cancelar o reagendar)."""
    table.delete_item(Key={"PK": _slot_pk(profesional_id, fecha, hora), "SK": "LOCK"})


def crear_cita(cliente_id: str, servicio_id: int, profesional_id: int, fecha: str, hora: str) -> dict:
    table = get_table()
    # Tomar el lock del slot ANTES de escribir: garantiza que dos reservas
    # simultáneas (race / reintentos de webhook) no dupliquen el horario.
    _acquire_slot(table, profesional_id, fecha, hora, cliente_id)
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
        # Snapshot del tramo y precio: la cita conserva su valor histórico aunque
        # luego cambien los precios. El tramo arranca en "adulto" y la terapeuta
        # lo ajusta desde el panel.
        precios = serv.get("precios") or {}
        item["tramo"] = TRAMO_DEFAULT
        if TRAMO_DEFAULT in precios:
            item["precio"] = precios[TRAMO_DEFAULT]
    profesionales = get_profesionales()
    prof = next((p for p in profesionales if p["id"] == profesional_id), None)
    if prof:
        item["profesional_nombre"] = prof["nombre"]
    try:
        # Sync best-effort a Google Calendar (issue #14): guardamos el event id
        # para poder borrar/actualizar el evento al cancelar o modificar la cita.
        event_id = google_calendar.sync_create(item)
        if event_id:
            item["gcal_event_id"] = event_id
        table.put_item(Item=item)
    except Exception:
        # Si falla la escritura de la cita, liberamos el lock para no dejar el
        # horario bloqueado sin una cita real detrás.
        _release_slot(table, profesional_id, fecha, hora)
        raise
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
    # Liberar el lock del slot para que el horario vuelva a estar disponible.
    if item:
        _release_slot(table, item["profesional_id"], item["fecha"], item["hora"])


def modificar_cita(cita_pk: str, cita_sk: str, nueva_fecha: str, nueva_hora: str):
    table = get_table()
    # Obtener cita actual
    resp = table.get_item(Key={"PK": cita_pk, "SK": cita_sk})
    if "Item" not in resp:
        return
    cita = resp["Item"]
    # Reagendar al mismo horario: no hay nada que cambiar (y evita que el lock
    # del propio slot choque consigo mismo).
    if cita["fecha"] == nueva_fecha and cita["hora"] == nueva_hora:
        return
    # Crear la nueva PRIMERO: si el horario está ocupado, crear_cita lanza
    # SlotNoDisponibleError y la cita original queda intacta (no se pierde).
    crear_cita(cita["cliente_id"], cita["servicio_id"], cita["profesional_id"], nueva_fecha, nueva_hora)
    # Recién entonces cancelar la vieja (libera su slot).
    cancelar_cita(cita_pk, cita_sk)


def actualizar_tramo_cita(cita_pk: str, cita_sk: str, tramo: str) -> None:
    """Asigna el tramo de precio de una cita y recalcula su precio snapshot.

    Lo usa el panel para registrar la categoría del paciente (convenio TEA/TDAH,
    niño o adulto particular). Lanza ValueError si el tramo no existe.
    """
    if tramo not in TRAMOS_PRECIO:
        raise ValueError(f"Tramo inválido: {tramo}")
    table = get_table()
    resp = table.get_item(Key={"PK": cita_pk, "SK": cita_sk})
    item = resp.get("Item")
    if not item:
        return
    servicios = get_servicios()
    serv = next((s for s in servicios if s["id"] == item.get("servicio_id")), None)
    precios = (serv or {}).get("precios") or {}
    update = "SET tramo = :t, updated_at = :u"
    values = {":t": tramo, ":u": datetime.utcnow().isoformat()}
    if tramo in precios:
        update += ", precio = :p"
        values[":p"] = precios[tramo]
    table.update_item(Key={"PK": cita_pk, "SK": cita_sk},
                      UpdateExpression=update, ExpressionAttributeValues=values)


# Estados que la terapeuta puede marcar desde el panel (validar la atención).
# 'cancelada' NO está acá: tiene su propio camino (cancelar_cita) que libera el
# slot y borra el evento del calendar.
ESTADOS_ATENCION = ("confirmada", "completada", "no_show")


def buscar_cita_por_slot(profesional_id, fecha: str, hora: str) -> Optional[dict]:
    """Busca la cita de un slot (prof + fecha + hora) vía GSI1. Devuelve la no
    cancelada si hay varias. Lo usa el prompt de atención para resolver el token
    del botón a la cita real."""
    table = get_table()
    resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"APPT#PROF#{profesional_id}")
        & Key("GSI1SK").eq(f"DATE#{fecha}#{hora}"),
    )
    items = resp.get("Items", [])
    for it in items:
        if it.get("estado") != "cancelada":
            return it
    return items[0] if items else None


def marcar_estado_cita(cita_pk: str, cita_sk: str, estado: str) -> None:
    """Marca el estado de atención de una cita (completada / no_show / confirmada).

    Lo usa el panel para registrar si la sesión se realizó. Lanza ValueError si
    el estado no es uno de los permitidos (para cancelar, usar cancelar_cita).
    """
    if estado not in ESTADOS_ATENCION:
        raise ValueError(f"Estado inválido: {estado}")
    table = get_table()
    table.update_item(
        Key={"PK": cita_pk, "SK": cita_sk},
        UpdateExpression="SET estado = :e, updated_at = :u",
        ExpressionAttributeValues={":e": estado, ":u": datetime.utcnow().isoformat()},
    )


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


def _hhmm_a_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _capacidad_min_rango(desde: str, hasta: str) -> int:
    """Minutos de atención disponibles según HORARIOS_DEFAULT en el rango."""
    d = date.fromisoformat(desde)
    fin = date.fromisoformat(hasta)
    total = 0
    while d <= fin:
        horario = HORARIOS_DEFAULT.get(d.weekday())
        if horario:
            total += max(0, _hhmm_a_min(horario["fin"]) - _hhmm_a_min(horario["inicio"]))
        d += timedelta(days=1)
    return total


def resumen_citas_rango(desde: str, hasta: str) -> dict:
    """Agrega métricas de negocio de citas en un rango (issues #15 + métricas).

    Devuelve, además de los conteos base (total, por_estado, por_servicio,
    tasa_cancelacion):
    - tasa_no_show
    - facturacion (suma de `precio` de las completadas) + ingresos_por_servicio
    - pacientes_nuevos vs pacientes_recurrentes (primera cita histórica dentro
      del rango = nuevo)
    - horas_ocupadas vs horas_disponibles y ocupacion (0..1)
    Solo las citas *completadas* cuentan como facturación.
    """
    citas = get_citas_rango(desde, hasta)
    precios_map = get_precios_por_servicio()
    por_estado: dict[str, int] = {}
    por_servicio: dict[str, int] = {}
    ingresos_por_servicio: dict[str, int] = {}
    facturacion = 0
    ocupadas_min = 0
    clientes: set = set()
    for c in citas:
        estado = c.get("estado", "desconocido")
        por_estado[estado] = por_estado.get(estado, 0) + 1
        servicio = c.get("servicio_nombre", "Sin servicio")
        por_servicio[servicio] = por_servicio.get(servicio, 0) + 1
        if c.get("cliente_id"):
            clientes.add(c["cliente_id"])
        if estado != "cancelada":
            ocupadas_min += int(c.get("servicio_duracion", 60) or 60)
        if estado == "completada":
            # Deriva el precio del tramo si la cita no tiene snapshot (citas viejas).
            precio = precio_efectivo(c, precios_map) or 0
            facturacion += precio
            ingresos_por_servicio[servicio] = ingresos_por_servicio.get(servicio, 0) + precio

    # Pacientes nuevos vs recurrentes: nuevo si su primera cita histórica
    # (cualquier estado) cae dentro del rango.
    nuevos = recurrentes = 0
    for cid in clientes:
        hist = get_historial_cliente(cid)
        if not hist:
            continue
        primera = min(h["fecha"] for h in hist)
        if primera >= desde:
            nuevos += 1
        else:
            recurrentes += 1

    total = len(citas)
    canceladas = por_estado.get("cancelada", 0)
    no_shows = por_estado.get("no_show", 0)
    cap_min = _capacidad_min_rango(desde, hasta)
    return {
        "desde": desde,
        "hasta": hasta,
        "total": total,
        "por_estado": por_estado,
        "por_servicio": por_servicio,
        "tasa_cancelacion": (canceladas / total) if total else 0.0,
        "tasa_no_show": (no_shows / total) if total else 0.0,
        "facturacion": facturacion,
        "ingresos_por_servicio": ingresos_por_servicio,
        "pacientes_nuevos": nuevos,
        "pacientes_recurrentes": recurrentes,
        "horas_ocupadas": round(ocupadas_min / 60, 1),
        "horas_disponibles": round(cap_min / 60, 1),
        "ocupacion": (ocupadas_min / cap_min) if cap_min else 0.0,
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
