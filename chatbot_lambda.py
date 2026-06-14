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
MODIFY_CONFIRM = "MODIFY_CONFIRM"
CONFIRM_ATTENDANCE = "CONFIRM_ATTENDANCE"


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
        # Saludo personalizado por hora (Chile UTC-4)
        from datetime import datetime, timezone, timedelta
        hora_chile = datetime.now(timezone(timedelta(hours=-4))).hour
        if hora_chile < 12:
            saludo = "¡Buenos días! 🌸"
        elif hora_chile < 19:
            saludo = "¡Buenas tardes! 🌸"
        else:
            saludo = "¡Buenas noches! 🌸"
        bienvenida = MENSAJES["bienvenida"].format(**NEGOCIO).replace("¡Hola! 🌸", saludo)
        return bienvenida

    # Comandos admin (solo profesional)
    from config import ADMIN_USER_ID
    if canal_user_id == ADMIN_USER_ID and text.startswith("/"):
        resp = _handle_admin_command(text)
        if resp:
            return resp

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
    elif state == MODIFY_CONFIRM:
        resp = _handle_modify_confirm(session, canal, canal_user_id, text)
    elif state == CONFIRM_ATTENDANCE:
        resp = _handle_confirm_attendance(session, canal, canal_user_id, text)
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
        # Verificar límite de citas
        from config import MAX_CITAS_POR_CLIENTE
        citas_activas = db.get_citas_cliente(cliente["id"])
        if len(citas_activas) >= MAX_CITAS_POR_CLIENTE:
            session["state"] = IDLE
            session["data"] = {}
            return f"⚠️ Ya tienes {len(citas_activas)} citas agendadas (máximo {MAX_CITAS_POR_CLIENTE}). Cancela una existente para agendar otra."
        serv = session["data"]["servicio"]
        prof = session["data"]["profesional"]
        fecha = session["data"]["fecha"]
        hora = session["data"]["hora"]
        db.crear_cita(cliente["id"], serv["id"], prof["id"], fecha.isoformat(), hora)
        _notify_profesional(f"📅 Nueva cita agendada:\n👤 {session['data']['nombre_cliente']}\n📋 {serv['nombre']}\n🕐 {fecha.strftime('%d/%m/%Y')} a las {hora}")
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
        _notify_profesional(f"❌ Cita cancelada:\n📋 {cita.get('servicio_nombre', '')}\n🕐 {cita['fecha']} a las {cita['hora']}")
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


def _handle_modify_confirm(session, canal, canal_user_id, text):
    if text.lower() in ("si", "sí", "s", "1"):
        cita = session["data"]["cita_seleccionada"]
        fecha = session["data"]["nueva_fecha"]
        hora = session["data"]["nueva_hora"]
        db.modificar_cita(cita["PK"], cita["SK"], fecha.isoformat(), hora)
        _notify_profesional(
            f"🔄 Cita reagendada:\n📋 {cita.get('servicio_nombre', '')}\n🕐 {fecha.strftime('%d/%m/%Y')} a las {hora}"
        )
        session["state"] = IDLE
        session["data"] = {}
        return f"✅ Cita reagendada a {fecha.strftime('%d/%m/%Y')} a las {hora}.\n\nEscribe *menu* para volver."
    session["state"] = IDLE
    session["data"] = {}
    return "Cambio cancelado. Tu cita queda como estaba. Escribe *menu* para volver."


def _handle_confirm_attendance(session, canal, canal_user_id, text):
    """Maneja respuesta a recordatorio de asistencia."""
    cita = session["data"].get("cita_pendiente")
    if text.lower() in ("si", "sí", "s", "1"):
        session["state"] = IDLE
        session["data"] = {}
        return "✅ ¡Perfecto! Te esperamos. Recuerda llegar 5 minutos antes."
    elif text.lower() in ("no", "n", "2"):
        if cita:
            db.cancelar_cita(cita["PK"], cita["SK"])
            _notify_profesional("❌ Cita cancelada (no confirmó asistencia)")
        session["state"] = IDLE
        session["data"] = {}
        return "❌ Cita cancelada. Escribe *menu* si deseas reagendar."
    return "Responde *si* para confirmar o *no* para cancelar."


def _notify_profesional(msg: str):
    """Envía notificación al profesional por Telegram."""
    import httpx
    from config import ADMIN_USER_ID, TELEGRAM_BOT_TOKEN
    if not TELEGRAM_BOT_TOKEN or not ADMIN_USER_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        httpx.post(url, json={"chat_id": int(ADMIN_USER_ID), "text": msg}, timeout=5.0)
    except Exception:
        pass


def _handle_admin_command(text: str) -> str | None:
    """Comandos del profesional: /bloquear, /desbloquear, /agenda."""
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "/bloquear" and len(parts) >= 2:
        # /bloquear 2026-06-15 o /bloquear 2026-06-15 10:00
        fecha = parts[1]
        if len(parts) >= 3:
            db.bloquear_hora(1, fecha, parts[2])
            return f"🔒 Bloqueada hora {parts[2]} del {fecha}"
        db.bloquear_fecha(1, fecha)
        return f"🔒 Día {fecha} bloqueado completamente"

    elif cmd == "/desbloquear" and len(parts) >= 2:
        db.desbloquear_fecha(1, parts[1])
        return f"🔓 Día {parts[1]} desbloqueado"

    elif cmd == "/agenda":
        from datetime import date
        hoy = date.today().isoformat()
        citas = []
        table = db.get_table()
        resp = table.scan(
            FilterExpression="begins_with(PK, :p) AND fecha = :f AND estado = :e",
            ExpressionAttributeValues={":p": "APPOINTMENT#", ":f": hoy, ":e": "confirmada"},
        )
        citas = sorted(resp["Items"], key=lambda x: x["hora"])
        if not citas:
            return f"📋 Sin citas para hoy ({hoy})"
        lines = [f"📋 Agenda de hoy ({hoy}):\n"]
        for c in citas:
            lines.append(f"  {c['hora']} - {c.get('servicio_nombre', '?')}")
        return "\n".join(lines)

    elif cmd == "/reporte":
        # /reporte [semana|mes] — métricas de citas del período
        from datetime import date, timedelta
        periodo = parts[1].lower() if len(parts) >= 2 else "semana"
        dias = 30 if periodo == "mes" else 7
        hasta = date.today()
        desde = hasta - timedelta(days=dias - 1)
        resumen = db.resumen_citas_rango(desde.isoformat(), hasta.isoformat())
        lines = [
            f"📊 Reporte {periodo} ({desde.strftime('%d/%m')} → {hasta.strftime('%d/%m')}):\n",
            f"Total citas: {resumen['total']}",
            f"✅ Confirmadas: {resumen['por_estado'].get('confirmada', 0)}",
            f"❌ Canceladas: {resumen['por_estado'].get('cancelada', 0)}",
            f"✔️ Completadas: {resumen['por_estado'].get('completada', 0)}",
            f"Tasa de cancelación: {resumen['tasa_cancelacion']:.0%}",
        ]
        if resumen["por_servicio"]:
            lines.append("\nPor servicio:")
            for nombre, cantidad in sorted(
                resumen["por_servicio"].items(), key=lambda kv: -kv[1]
            ):
                lines.append(f"• {nombre}: {cantidad}")
        return "\n".join(lines)

    elif cmd == "/ayuda":
        return (
            "🔧 Comandos admin:\n"
            "/bloquear 2026-06-15 → bloquea día completo\n"
            "/bloquear 2026-06-15 10:00 → bloquea hora\n"
            "/desbloquear 2026-06-15 → desbloquea día\n"
            "/agenda → citas de hoy\n"
            "/reporte [semana|mes] → métricas de citas"
        )
    return None
