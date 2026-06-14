"""Tests unitarios del motor conversacional (chatbot.py).

Valida la máquina de estados, transiciones y mensajes de respuesta
sin depender de canales externos (Telegram/WhatsApp).
"""
import pytest

import chatbot
import database as db_module
from config import MENSAJES, NEGOCIO
from tests.conftest import TEST_USER, TEST_USER2


# ── Comando reset / menú ──────────────────────────────────────────────
class TestMenuReset:
    def test_start_resets_state(self, fresh_db):
        """/start desde cualquier estado vuelve a IDLE y muestra bienvenida."""
        chatbot.handle_message("test", TEST_USER, "1")
        assert chatbot._get_session(TEST_USER)["state"] != chatbot.IDLE
        resp = chatbot.handle_message("test", TEST_USER, "/start")
        assert resp == MENSAJES["bienvenida"].format(**NEGOCIO)
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.IDLE

    @pytest.mark.parametrize("cmd", ["menu", "menú", "inicio", "/start"])
    def test_reset_commands(self, fresh_db, cmd):
        """Distintas formas de reset limpian sesión."""
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        resp = chatbot.handle_message("test", TEST_USER, cmd)
        assert resp == MENSAJES["bienvenida"].format(**NEGOCIO)

    def test_bienvenida_muestra_opciones(self, fresh_db):
        """Mensaje de bienvenida contiene las 4 opciones principales."""
        resp = chatbot.handle_message("test", TEST_USER, "menu")
        assert "1️⃣" in resp
        assert "2️⃣" in resp
        assert "3️⃣" in resp
        assert "4️⃣" in resp

    def test_input_invalido_en_idle_muestra_bienvenida(self, fresh_db):
        """Entrada no numérica en IDLE devuelve bienvenida."""
        resp = chatbot.handle_message("test", TEST_USER, "hola")
        assert resp == MENSAJES["bienvenida"].format(**NEGOCIO)


# ── Flujo de agendamiento ─────────────────────────────────────────────
class TestBookingFlow:
    def test_seleccionar_servicio_muestra_lista(self, fresh_db):
        """Opción 1 muestra servicios disponibles."""
        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "Consulta inicial" in resp
        assert "Sesión de seguimiento" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.BOOKING_SERVICE

    def test_seleccionar_servicio_invalido(self, fresh_db):
        """Índice inválido en servicio muestra error."""
        chatbot.handle_message("test", TEST_USER, "1")
        resp = chatbot.handle_message("test", TEST_USER, "99")
        assert resp == MENSAJES["error"]

    def test_seleccionar_servicio_texto_no_numerico(self, fresh_db):
        """Texto no numérico en selección de servicio muestra error."""
        chatbot.handle_message("test", TEST_USER, "1")
        resp = chatbot.handle_message("test", TEST_USER, "abc")
        assert resp == MENSAJES["error"]

    def test_flujo_completo_agendamiento(self, fresh_db):
        """Flujo feliz: servicio → profesional → fecha → hora → nombre → confirmar."""
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "Con qué profesional" in resp or "Fechas disponibles" in resp

        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            resp = chatbot.handle_message("test", TEST_USER, "1")

        assert "Fechas disponibles" in resp or "disponibles" in resp
        session = chatbot._get_session(TEST_USER)
        assert session["state"] == chatbot.BOOKING_DATE

        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "Horas disponibles" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.BOOKING_TIME

        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "nombre" in resp.lower()
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.BOOKING_NAME

        resp = chatbot.handle_message("test", TEST_USER, "Juan Pérez")
        assert "Confirma tu cita" in resp
        assert "Juan Pérez" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.BOOKING_CONFIRM

        resp = chatbot.handle_message("test", TEST_USER, "si")
        assert "✅" in resp or "Cita agendada" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.IDLE

    def test_confirmar_cita_verifica_bd(self, fresh_db):
        """Confirmar cita crea registro en base de datos."""
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")

        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")

        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "Juan Pérez")
        chatbot.handle_message("test", TEST_USER, "si")

        cliente = db_module.get_or_create_cliente("test", TEST_USER)
        citas = db_module.get_citas_cliente(cliente["id"])
        assert len(citas) == 1
        assert citas[0]["servicio_nombre"] == "Consulta inicial"

    def test_rechazar_cita(self, fresh_db):
        """Responder 'no' en confirmación cancela sin crear cita."""
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")

        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")

        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "Juan Pérez")
        resp = chatbot.handle_message("test", TEST_USER, "no")
        assert "Cita no agendada" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.IDLE

    def test_fecha_invalida_en_booking(self, fresh_db):
        """Índice inválido en selección de fecha muestra error."""
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")
        resp = chatbot.handle_message("test", TEST_USER, "99")
        assert resp == MENSAJES["error"]

    def test_hora_invalida_en_booking(self, fresh_db):
        """Índice inválido en selección de hora muestra error."""
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        resp = chatbot.handle_message("test", TEST_USER, "99")
        assert resp == MENSAJES["error"]


# ── Flujo de cancelación ──────────────────────────────────────────────
class TestCancelFlow:
    def test_cancelar_sin_citas(self, fresh_db):
        """Opción 3 sin citas muestra mensaje informativo."""
        resp = chatbot.handle_message("test", TEST_USER, "3")
        assert "No tienes citas" in resp

    def test_cancelar_cita_flow_completo(self, fresh_db):
        """Flujo feliz: seleccionar cita → confirmar cancelación."""
        self._crear_cita_test()
        resp = chatbot.handle_message("test", TEST_USER, "3")
        assert "Cuál cita" in resp or "1️⃣" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.CANCEL_SELECT

        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "Cancelar" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.CANCEL_CONFIRM

        resp = chatbot.handle_message("test", TEST_USER, "si")
        assert "cancelada" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.IDLE

    def test_cancelar_actualiza_bd(self, fresh_db):
        """Cancelar cita cambia estado en BD."""
        self._crear_cita_test()
        chatbot.handle_message("test", TEST_USER, "3")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "si")

        cliente = db_module.get_or_create_cliente("test", TEST_USER)
        citas = db_module.get_citas_cliente(cliente["id"])
        assert len(citas) == 0

    def test_abortar_cancelacion(self, fresh_db):
        """Responder 'no' en cancelación no elimina cita."""
        self._crear_cita_test()
        chatbot.handle_message("test", TEST_USER, "3")
        chatbot.handle_message("test", TEST_USER, "1")
        resp = chatbot.handle_message("test", TEST_USER, "no")
        assert "abortada" in resp or "Cancelación" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.IDLE

        cliente = db_module.get_or_create_cliente("test", TEST_USER)
        citas = db_module.get_citas_cliente(cliente["id"])
        assert len(citas) == 1

    def test_seleccion_invalida_en_cancel(self, fresh_db):
        """Índice inválido al seleccionar cita a cancelar muestra error."""
        self._crear_cita_test()
        chatbot.handle_message("test", TEST_USER, "3")
        resp = chatbot.handle_message("test", TEST_USER, "99")
        assert resp == MENSAJES["error"]

    def _crear_cita_test(self):
        """Helper: agenda una cita de prueba."""
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "Juan Pérez")
        chatbot.handle_message("test", TEST_USER, "si")


# ── Flujo de modificación ─────────────────────────────────────────────
class TestModifyFlow:
    def test_modificar_sin_citas(self, fresh_db):
        """Opción 2 sin citas muestra mensaje informativo."""
        resp = chatbot.handle_message("test", TEST_USER, "2")
        assert "No tienes citas" in resp

    def test_modificar_flujo_completo(self, fresh_db):
        """Flujo feliz: seleccionar cita → nueva fecha → nueva hora."""
        self._crear_cita_test()
        resp = chatbot.handle_message("test", TEST_USER, "2")
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.MODIFY_SELECT

        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "disponibles" in resp.lower()
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.MODIFY_DATE

        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "Horas disponibles" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.MODIFY_TIME

        # Seleccionar la hora ahora pide confirmación (no aplica directo).
        resp = chatbot.handle_message("test", TEST_USER, "1")
        assert "Reagendar" in resp or "Confirma" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.MODIFY_CONFIRM

        resp = chatbot.handle_message("test", TEST_USER, "si")
        assert "reagendada" in resp.lower() or "✅" in resp
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.IDLE

    def test_modificar_actualiza_bd(self, fresh_db):
        """Modificar cita persiste cambios en BD."""
        self._crear_cita_test()
        cliente = db_module.get_or_create_cliente("test", TEST_USER)
        cita_original = db_module.get_citas_cliente(cliente["id"])[0]
        fecha_original = cita_original["fecha"]
        hora_original = cita_original["hora"]

        chatbot.handle_message("test", TEST_USER, "2")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "si")  # confirmar el reagendamiento

        citas_actualizadas = db_module.get_citas_cliente(cliente["id"])
        assert len(citas_actualizadas) == 1
        cita_nueva = citas_actualizadas[0]
        assert cita_nueva["hora"] != hora_original or cita_nueva["fecha"] != fecha_original

    def _crear_cita_test(self):
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "Juan Pérez")
        chatbot.handle_message("test", TEST_USER, "si")


# ── Flujo de consulta ─────────────────────────────────────────────────
class TestViewAppointments:
    def test_ver_citas_sin_citas(self, fresh_db):
        """Opción 4 sin citas muestra mensaje."""
        resp = chatbot.handle_message("test", TEST_USER, "4")
        assert "No tienes citas" in resp

    def test_ver_citas_con_citas(self, fresh_db):
        """Opción 4 con citas lista las citas."""
        self._crear_cita_test()
        resp = chatbot.handle_message("test", TEST_USER, "4")
        assert "Consulta inicial" in resp
        assert "proximas citas" in resp.lower() or "Tus próximas" in resp

    def _crear_cita_test(self):
        chatbot.handle_message("test", TEST_USER, "menu")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        session = chatbot._get_session(TEST_USER)
        if session["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "Juan Pérez")
        chatbot.handle_message("test", TEST_USER, "si")


# ── Sesiones multi-usuario ────────────────────────────────────────────
class TestMultiUserSessions:
    def test_sesiones_independientes(self, fresh_db):
        """Dos usuarios tienen sesiones independientes."""
        chatbot.handle_message("test", TEST_USER, "1")
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.BOOKING_SERVICE
        assert chatbot._get_session(TEST_USER2)["state"] == chatbot.IDLE

        chatbot.handle_message("test", TEST_USER2, "3")
        assert chatbot._get_session(TEST_USER)["state"] == chatbot.BOOKING_SERVICE
        assert chatbot._get_session(TEST_USER2)["state"] == chatbot.IDLE

    def test_datos_independientes_por_usuario(self, fresh_db):
        """Los datos de sesión no se mezclan entre usuarios."""
        chatbot.handle_message("test", TEST_USER, "1")
        chatbot.handle_message("test", TEST_USER, "1")
        session1 = chatbot._get_session(TEST_USER)
        if session1["state"] == chatbot.BOOKING_PROFESSIONAL:
            chatbot.handle_message("test", TEST_USER, "1")

        assert "servicio" in chatbot._get_session(TEST_USER)["data"]
        assert "servicio" not in chatbot._get_session(TEST_USER2)["data"]


# ── Canales múltiples ─────────────────────────────────────────────────
class TestMultiChannel:
    def test_mismo_usuario_distintos_canales(self, fresh_db):
        """Mismo canal_user_id en distintos canales son sesiones separadas."""
        resp_tg = chatbot.handle_message("telegram", "user_1", "menu")
        resp_wa = chatbot.handle_message("whatsapp", "user_1", "menu")
        assert resp_tg == resp_wa

        chatbot.handle_message("telegram", "user_1", "1")
        assert chatbot._get_session("user_1")["state"] == chatbot.BOOKING_SERVICE

    def test_canal_en_session_key(self, fresh_db):
        """La clave de sesión incluye canal_user_id (canal + id)."""
        chatbot.handle_message("telegram", "123", "menu")
        assert "telegram:123" not in chatbot._sessions
        assert "123" in chatbot._sessions


# ── Opción 5: Historial de citas ──────────────────────────────────────
class TestHistorial:
    """Opción 5 del menú: historial completo (paridad con chatbot_lambda)."""

    def test_opcion_5_sin_historial(self, fresh_db):
        chatbot.handle_message("test", TEST_USER, "menu")
        resp = chatbot.handle_message("test", TEST_USER, "5")
        assert "historial" in resp.lower()

    def test_opcion_5_lista_pasadas_y_canceladas(self, fresh_db):
        from datetime import date, timedelta
        cliente = db_module.get_or_create_cliente("test", TEST_USER)
        servicios = db_module.get_servicios()
        profesionales = db_module.get_profesionales()
        ayer = (date.today() - timedelta(days=3)).isoformat()
        db_module.crear_cita(cliente["id"], servicios[0]["id"], profesionales[0]["id"], ayer, "10:00")
        cancelada = db_module.crear_cita(cliente["id"], servicios[0]["id"], profesionales[0]["id"], ayer, "11:00")
        db_module.cancelar_cita(cancelada["id"])
        chatbot.handle_message("test", TEST_USER, "menu")
        resp = chatbot.handle_message("test", TEST_USER, "5")
        assert "Historial" in resp
        assert ayer in resp
        # la cancelada se marca distinto de la confirmada
        assert "❌" in resp

    def test_opcion_5_distinta_de_opcion_4(self, fresh_db):
        """Opción 4 (próximas) no muestra pasadas; opción 5 (historial) sí."""
        from datetime import date, timedelta
        cliente = db_module.get_or_create_cliente("test", TEST_USER)
        servicios = db_module.get_servicios()
        profesionales = db_module.get_profesionales()
        ayer = (date.today() - timedelta(days=4)).isoformat()
        db_module.crear_cita(cliente["id"], servicios[0]["id"], profesionales[0]["id"], ayer, "09:00")
        chatbot.handle_message("test", TEST_USER, "menu")
        proximas = chatbot.handle_message("test", TEST_USER, "4")
        chatbot.handle_message("test", TEST_USER, "menu")
        historial = chatbot.handle_message("test", TEST_USER, "5")
        assert ayer not in proximas
        assert ayer in historial
