"""Motor conversacional para Lambda (usa DynamoDB para BD y sesiones)."""
from datetime import date

import database_dynamo as db
import session_store
from config import MENSAJES, NEGOCIO
from rate_limiter import is_rate_limited

# Estados de conversación
IDLE = "IDLE"
BOOKING_SERVICE = "BOOKING_SERVICE"
BOOKING_PROFESSIONAL = "BOOKING_PROFESSIONAL"
BOOKING_DATE = "BOOKING_DATE"
BOOKING_TIME = "BOOKING_TIME"
BOOKING_NAME = "BOOKING_NAME"
BOOKING_CONFIRM = "BOOKING_CONFIRM"
CANCEL_SELECT = "CANCEL_SELECT"
CANCEL_CONFIRM = "CANCEL_CONFIRM"
MODIFY_SELECT = "MODIFY_SELECT"
MODIFY_DATE = "MODIFY_DATE"
MODIFY_TIME = "MODIFY_TIME"


def _get_session(user_id: str) -> dict:
    return session_store.get_session(user_id)


def _save_session(user_id: str, session: dict):
    session_store.save_session(user_id, session)


def handle_message(canal: str, canal_user_id: str, text: str) -> str:
    """Procesa un mensaje y retorna la respuesta del bot."""
    if is_rate_limited(canal_user_id):
        return "⚠️ Estás enviando mensajes muy rápido. Espera un momento e intenta de nuevo."
    text = text.strip()
    if len(text) > 500:
        text = text[:500]
    text = text.replace("\x00", "")

    session = _get_session(canal_user_id)
    state = session["state"]

    if text.lower() in ("menu", "menú", "inicio", "/start"):
        session = {"state": IDLE, "data": {}}
        _save_session(canal_user_id, session)
        return MENSAJES["bienvenida"].format(**NEGOCIO)

    if state == IDLE:
        resp = _handle_idle(session, canal, canal_user_id, text)
    elif state == BOOKING_SERVICE:
        resp = _handle_booking_service(session, text)
    elif state == BOOKING_PROFESSIONAL:
        resp = _handle_booking_professional(session, text)
    elif state == BOOKING_DATE:
        resp = _handle_booking_date(session, text)
    elif state == BOOKING_TIME:
        resp = _handle_booking_time(session, text)
    elif state == BOOKING_NAME:
        resp = _handle_booking_name(session, canal, canal_user_id, text)
    elif state == BOOKING_CONFIRM:
        resp = _handle_booking_confirm(session, canal, canal_user_id, text)
    elif state == CANCEL_SELECT:
        resp = _handle_cancel_select(session, text)
    elif state == CANCEL_CONFIRM:
        resp = _handle_cancel_confirm(session, text)
    elif state == MODIFY_SELECT:
        resp = _handle_modify_select(session, text)
    elif state == MODIFY_DATE:
        resp = _handle_modify_date(session, text)
    elif state == MODIFY_TIME:
        resp = _handle_modify_time(session, text)
    else:
        session = {"state": IDLE, "data": {}}
        resp = MENSAJES["bienvenida"].format(**NEGOCIO)

    _save_session(canal_user_id, session)
    return resp


def _handle_idle(session, canal, canal_user_id, text):
    if text == "1":
        servicios = db.get_servicios()
        session["data"]["servicios"] = servicios
        session["state"] = BOOKING_SERVICE
        lines = ["¿Qué servicio necesitas?\n"]
        for i, s in enumerate(servicios, 1):
            lines.append(f"{i}️⃣ {s['nombre']} ({s['duracion_min']} min)")
        return "\n".join(lines)
    elif text == "2":
        return _show_citas_for_action(session, canal, canal_user_id, MODIFY_SELECT, "modificar")
    elif text == "3":
        return _show_citas_for_action(session, canal, canal_user_id, CANCEL_SELECT, "cancelar")
    elif text == "4":
        cliente = db.get_or_create_cliente(canal, canal_user_id)
        citas = db.get_citas_cliente(cliente["id"])
        if not citas:
            return "No tienes citas agendadas. Escribe *menu* para volver."
        lines = ["📋 Tus próximas citas:\n"]
        for c in citas:
            lines.append(f"• {c.get('servicio_nombre', '')} con {c.get('profesional_nombre', '')}\n  📅 {c['fecha']} a las {c['hora']}")
        lines.append("\nEscribe *menu* para volver.")
        return "\n".join(lines)
    return MENSAJES["bienvenida"].format(**NEGOCIO)


def _show_citas_for_action(session, canal, canal_user_id, next_state, action):
    cliente = db.get_or_create_cliente(canal, canal_user_id)
    citas = db.get_citas_cliente(cliente["id"])
    if not citas:
        session["state"] = IDLE
        return f"No tienes citas para {action}. Escribe *menu* para volver."
    session["data"]["citas"] = citas
    session["state"] = next_state
    lines = [f"¿Cuál cita deseas {action}?\n"]
    for i, c in enumerate(citas, 1):
        lines.append(f"{i}️⃣ {c.get('servicio_nombre', '')} - {c['fecha']} a las {c['hora']}")
    return "\n".join(lines)


def _handle_booking_service(session, text):
    servicios = session["data"]["servicios"]
    try:
        idx = int(text) - 1
        servicio = servicios[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    session["data"]["servicio"] = servicio
    profesionales = db.get_profesionales()
    if len(profesionales) == 1:
        session["data"]["profesional"] = profesionales[0]
        return _show_fechas(session)
    session["data"]["profesionales"] = profesionales
    session["state"] = BOOKING_PROFESSIONAL
    lines = ["¿Con qué profesional?\n"]
    for i, p in enumerate(profesionales, 1):
        lines.append(f"{i}️⃣ {p['nombre']} - {p['especialidad']}")
    return "\n".join(lines)


def _handle_booking_professional(session, text):
    profesionales = session["data"]["profesionales"]
    try:
        idx = int(text) - 1
        profesional = profesionales[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    session["data"]["profesional"] = profesional
    return _show_fechas(session)


def _show_fechas(session):
    prof = session["data"]["profesional"]
    serv = session["data"]["servicio"]
    fechas = db.get_fechas_disponibles(prof["id"], serv["duracion_min"])
    if not fechas:
        session["state"] = IDLE
        return "😔 No hay disponibilidad esta semana. Escribe *menu* para volver."
    session["data"]["fechas"] = fechas
    session["state"] = BOOKING_DATE
    lines = ["📅 Fechas disponibles:\n"]
    for i, f in enumerate(fechas, 1):
        nombre_dia = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"][f.weekday()]
        lines.append(f"{i}️⃣ {nombre_dia} {f.strftime('%d/%m/%Y')}")
    return "\n".join(lines)


def _handle_booking_date(session, text):
    fechas = session["data"]["fechas"]
    try:
        idx = int(text) - 1
        fecha = fechas[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    session["data"]["fecha"] = fecha
    prof = session["data"]["profesional"]
    serv = session["data"]["servicio"]
    horas = db.get_horas_disponibles(prof["id"], fecha, serv["duracion_min"])
    session["data"]["horas"] = horas
    session["state"] = BOOKING_TIME
    lines = ["🕐 Horas disponibles:\n"]
    for i, h in enumerate(horas, 1):
        lines.append(f"{i}️⃣ {h}")
    return "\n".join(lines)


def _handle_booking_time(session, text):
    horas = session["data"]["horas"]
    try:
        idx = int(text) - 1
        hora = horas[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    session["data"]["hora"] = hora
    session["state"] = BOOKING_NAME
    return "¿A nombre de quién agendo la cita?"


def _handle_booking_name(session, canal, canal_user_id, text):
    session["data"]["nombre_cliente"] = text
    session["state"] = BOOKING_CONFIRM
    serv = session["data"]["servicio"]
    prof = session["data"]["profesional"]
    fecha = session["data"]["fecha"]
    hora = session["data"]["hora"]
    nombre_dia = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"][fecha.weekday()]
    return (
        f"📋 Confirma tu cita:\n\n"
        f"👤 {text}\n"
        f"📌 {serv['nombre']} ({serv['duracion_min']} min)\n"
        f"👩‍⚕️ {prof['nombre']}\n"
        f"📅 {nombre_dia} {fecha.strftime('%d/%m/%Y')} a las {hora}\n\n"
        f"¿Confirmar? (si/no)"
    )


def _handle_booking_confirm(session, canal, canal_user_id, text):
    if text.lower() in ("si", "sí", "s", "1"):
        cliente = db.get_or_create_cliente(canal, canal_user_id, session["data"]["nombre_cliente"])
        serv = session["data"]["servicio"]
        prof = session["data"]["profesional"]
        fecha = session["data"]["fecha"]
        hora = session["data"]["hora"]
        db.crear_cita(cliente["id"], serv["id"], prof["id"], fecha.isoformat(), hora)
        session["state"] = IDLE
        session["data"] = {}
        return MENSAJES["cita_confirmada"].format(
            servicio=serv["nombre"], profesional=prof["nombre"],
            fecha=fecha.strftime("%d/%m/%Y"), hora=hora
        ) + "\n\nEscribe *menu* para volver."
    session["state"] = IDLE
    session["data"] = {}
    return "Cita no agendada. Escribe *menu* para volver."


def _handle_cancel_select(session, text):
    citas = session["data"]["citas"]
    try:
        idx = int(text) - 1
        cita = citas[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    session["data"]["cita_seleccionada"] = cita
    session["state"] = CANCEL_CONFIRM
    return f"¿Cancelar cita de {cita.get('servicio_nombre', '')} el {cita['fecha']} a las {cita['hora']}? (si/no)"


def _handle_cancel_confirm(session, text):
    if text.lower() in ("si", "sí", "s", "1"):
        cita = session["data"]["cita_seleccionada"]
        db.cancelar_cita(cita["PK"], cita["SK"])
        session["state"] = IDLE
        session["data"] = {}
        return MENSAJES["cita_cancelada"].format(fecha=cita["fecha"], hora=cita["hora"]) + "\n\nEscribe *menu* para volver."
    session["state"] = IDLE
    session["data"] = {}
    return "Cancelación abortada. Escribe *menu* para volver."


def _handle_modify_select(session, text):
    citas = session["data"]["citas"]
    try:
        idx = int(text) - 1
        cita = citas[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    session["data"]["cita_seleccionada"] = cita
    servicios = db.get_servicios()
    serv = next((s for s in servicios if s["id"] == cita["servicio_id"]), None)
    session["data"]["servicio"] = serv
    session["data"]["profesional"] = {"id": cita["profesional_id"], "nombre": cita.get("profesional_nombre", "")}
    fechas = db.get_fechas_disponibles(cita["profesional_id"], serv["duracion_min"])
    if not fechas:
        session["state"] = IDLE
        return "No hay disponibilidad para reagendar. Escribe *menu* para volver."
    session["data"]["fechas"] = fechas
    session["state"] = MODIFY_DATE
    lines = ["📅 Nuevas fechas disponibles:\n"]
    for i, f in enumerate(fechas, 1):
        nombre_dia = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"][f.weekday()]
        lines.append(f"{i}️⃣ {nombre_dia} {f.strftime('%d/%m/%Y')}")
    return "\n".join(lines)


def _handle_modify_date(session, text):
    fechas = session["data"]["fechas"]
    try:
        idx = int(text) - 1
        fecha = fechas[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    session["data"]["nueva_fecha"] = fecha
    prof = session["data"]["profesional"]
    serv = session["data"]["servicio"]
    horas = db.get_horas_disponibles(prof["id"], fecha, serv["duracion_min"])
    session["data"]["horas"] = horas
    session["state"] = MODIFY_TIME
    lines = ["🕐 Horas disponibles:\n"]
    for i, h in enumerate(horas, 1):
        lines.append(f"{i}️⃣ {h}")
    return "\n".join(lines)


def _handle_modify_time(session, text):
    horas = session["data"]["horas"]
    try:
        idx = int(text) - 1
        hora = horas[idx]
    except (ValueError, IndexError):
        return MENSAJES["error"]
    cita = session["data"]["cita_seleccionada"]
    fecha = session["data"]["nueva_fecha"]
    db.modificar_cita(cita["PK"], cita["SK"], fecha.isoformat(), hora)
    session["state"] = IDLE
    session["data"] = {}
    return f"✅ Cita modificada a {fecha.strftime('%d/%m/%Y')} a las {hora}.\n\nEscribe *menu* para volver."
