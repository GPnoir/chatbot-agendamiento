"""Motor conversacional agnóstico al canal."""
from datetime import date

import database as db
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
MODIFY_CONFIRM = "MODIFY_CONFIRM"

# Almacén de estado en memoria (canal_user_id -> estado)
_sessions: dict[str, dict] = {}


def _get_session(user_id: str) -> dict:
    if user_id not in _sessions:
        _sessions[user_id] = {"state": IDLE, "data": {}}
    return _sessions[user_id]


def handle_message(canal: str, canal_user_id: str, text: str) -> str:
    """Procesa un mensaje y retorna la respuesta del bot."""
    if is_rate_limited(canal_user_id):
        return "⚠️ Estás enviando mensajes muy rápido. Espera un momento e intenta de nuevo."
    text = text.strip()
    # Sanitización: limitar largo y limpiar
    if len(text) > 500:
        text = text[:500]
    text = text.replace("\x00", "")  # Null bytes
    session = _get_session(canal_user_id)
    state = session["state"]

    # Comando reset
    if text.lower() in ("menu", "menú", "inicio", "/start"):
        session["state"] = IDLE
        session["data"] = {}
        return MENSAJES["bienvenida"].format(**NEGOCIO)

    if state == IDLE:
        return _handle_idle(session, canal, canal_user_id, text)
    elif state == BOOKING_SERVICE:
        return _handle_booking_service(session, text)
    elif state == BOOKING_PROFESSIONAL:
        return _handle_booking_professional(session, text)
    elif state == BOOKING_DATE:
        return _handle_booking_date(session, text)
    elif state == BOOKING_TIME:
        return _handle_booking_time(session, text)
    elif state == BOOKING_NAME:
        return _handle_booking_name(session, canal, canal_user_id, text)
    elif state == BOOKING_CONFIRM:
        return _handle_booking_confirm(session, canal, canal_user_id, text)
    elif state == CANCEL_SELECT:
        return _handle_cancel_select(session, text)
    elif state == CANCEL_CONFIRM:
        return _handle_cancel_confirm(session, text)
    elif state == MODIFY_SELECT:
        return _handle_modify_select(session, text)
    elif state == MODIFY_DATE:
        return _handle_modify_date(session, text)
    elif state == MODIFY_TIME:
        return _handle_modify_time(session, text)
    elif state == MODIFY_CONFIRM:
        return _handle_modify_confirm(session, text)

    session["state"] = IDLE
    return MENSAJES["bienvenida"].format(**NEGOCIO)


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
            lines.append(f"• {c['servicio_nombre']} con {c['profesional_nombre']}\n  📅 {c['fecha']} a las {c['hora']}")
        lines.append("\nEscribe *menu* para volver.")
        return "\n".join(lines)
    elif text == "5":
        cliente = db.get_or_create_cliente(canal, canal_user_id)
        historial = db.get_historial_cliente(cliente["id"])
        if not historial:
            return "No tienes historial de citas. Escribe *menu* para volver."
        lines = ["📜 Historial de citas:\n"]
        for c in historial[:10]:
            estado = {"confirmada": "✅", "cancelada": "❌", "completada": "✔️"}.get(c["estado"], "")
            lines.append(f"{estado} {c.get('servicio_nombre', '')} - {c['fecha']} {c['hora']}")
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
        lines.append(f"{i}️⃣ {c['servicio_nombre']} - {c['fecha']} a las {c['hora']}")
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
    # Verificar si ya tenemos nombre del cliente
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
    return f"¿Cancelar cita de {cita['servicio_nombre']} el {cita['fecha']} a las {cita['hora']}? (si/no)"


def _handle_cancel_confirm(session, text):
    if text.lower() in ("si", "sí", "s", "1"):
        cita = session["data"]["cita_seleccionada"]
        db.cancelar_cita(cita["id"])
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
    # Obtener servicio para duración
    servicios = db.get_servicios()
    serv = next((s for s in servicios if s["id"] == cita["servicio_id"]), None)
    session["data"]["servicio"] = serv
    session["data"]["profesional"] = {"id": cita["profesional_id"], "nombre": cita["profesional_nombre"]}
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
    # No aplica el cambio todavía: pide confirmación por si eligió mal la hora.
    session["data"]["nueva_hora"] = hora
    session["state"] = MODIFY_CONFIRM
    cita = session["data"]["cita_seleccionada"]
    fecha = session["data"]["nueva_fecha"]
    nombre_dia = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"][fecha.weekday()]
    return (
        f"📋 Confirma el cambio:\n\n"
        f"📌 {cita.get('servicio_nombre', '')}\n"
        f"📅 {nombre_dia} {fecha.strftime('%d/%m/%Y')} a las {hora}\n\n"
        f"¿Reagendar tu cita? (si/no)"
    )


def _handle_modify_confirm(session, text):
    if text.lower() in ("si", "sí", "s", "1"):
        cita = session["data"]["cita_seleccionada"]
        fecha = session["data"]["nueva_fecha"]
        hora = session["data"]["nueva_hora"]
        db.modificar_cita(cita["id"], fecha.isoformat(), hora)
        session["state"] = IDLE
        session["data"] = {}
        return f"✅ Cita reagendada a {fecha.strftime('%d/%m/%Y')} a las {hora}.\n\nEscribe *menu* para volver."
    session["state"] = IDLE
    session["data"] = {}
    return "Cambio cancelado. Tu cita queda como estaba. Escribe *menu* para volver."
