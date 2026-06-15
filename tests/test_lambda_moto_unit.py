"""Tests unitarios del stack Lambda contra DynamoDB simulado con moto (issue #16).

Cubre database_dynamo (CRUD, disponibilidad, bloqueos), session_store
(roundtrip, TTL), y el webhook de Telegram de lambda_handler de punta a punta
usando la tabla moto provista por el fixture autouse dynamo_mock_table.
"""
import json
import time
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import database_dynamo as db
import session_store

TELEGRAM_SECRET = "test_telegram_secret"


def _proximo_lunes() -> date:
    """Próximo lunes estrictamente futuro (horario 09:00-18:00, sin bloqueos)."""
    hoy = date.today()
    dias = (7 - hoy.weekday()) % 7
    return hoy + timedelta(days=dias or 7)


# ---------------------------------------------------------------------------
# database_dynamo
# ---------------------------------------------------------------------------

class TestInitDbSeed:
    def test_seeds_servicios(self):
        servicios = db.get_servicios()
        assert len(servicios) == 3
        nombres = {s["nombre"] for s in servicios}
        assert "Consulta inicial" in nombres

    def test_seeds_profesionales(self):
        profesionales = db.get_profesionales()
        assert len(profesionales) == 1
        assert profesionales[0]["nombre"] == "Terapeuta Nelly Pailacura"

    def test_init_db_idempotente(self):
        db.init_db()
        db.init_db()
        assert len(db.get_servicios()) == 3


class TestClientes:
    def test_crea_cliente_nuevo(self):
        cliente = db.get_or_create_cliente("telegram", "moto_user_1", "Ana")
        assert cliente["nombre"] == "Ana"
        assert cliente["canal"] == "telegram"

    def test_cliente_existente_no_duplica(self):
        c1 = db.get_or_create_cliente("telegram", "moto_user_2", "Beto")
        c2 = db.get_or_create_cliente("telegram", "moto_user_2")
        assert c1["id"] == c2["id"]
        assert c2["nombre"] == "Beto"

    def test_actualiza_nombre_si_estaba_vacio(self):
        db.get_or_create_cliente("whatsapp", "moto_user_3")
        c = db.get_or_create_cliente("whatsapp", "moto_user_3", "Carla")
        assert c["nombre"] == "Carla"


class TestCitas:
    def test_crear_y_listar_cita(self):
        cliente = db.get_or_create_cliente("telegram", "moto_citas_1", "Dora")
        fecha = _proximo_lunes().isoformat()
        cita = db.crear_cita(cliente["id"], 1, 1, fecha, "10:00")
        assert cita["estado"] == "confirmada"
        assert cita["servicio_nombre"] == "Consulta inicial"
        citas = db.get_citas_cliente(cliente["id"])
        assert len(citas) == 1
        assert citas[0]["hora"] == "10:00"

    def test_cancelar_cita(self):
        cliente = db.get_or_create_cliente("telegram", "moto_citas_2", "Elsa")
        fecha = _proximo_lunes().isoformat()
        cita = db.crear_cita(cliente["id"], 1, 1, fecha, "11:00")
        db.cancelar_cita(cita["PK"], cita["SK"])
        assert db.get_citas_cliente(cliente["id"]) == []
        historial = db.get_historial_cliente(cliente["id"])
        assert historial[0]["estado"] == "cancelada"

    def test_modificar_cita_cancela_y_crea(self):
        cliente = db.get_or_create_cliente("telegram", "moto_citas_3", "Fede")
        lunes = _proximo_lunes()
        cita = db.crear_cita(cliente["id"], 2, 1, lunes.isoformat(), "09:00")
        martes = (lunes + timedelta(days=1)).isoformat()
        db.modificar_cita(cita["PK"], cita["SK"], martes, "12:00")
        activas = db.get_citas_cliente(cliente["id"])
        assert len(activas) == 1
        assert activas[0]["fecha"] == martes
        assert activas[0]["hora"] == "12:00"


class TestDobleReserva:
    """El mismo horario de un profesional no puede tener dos citas confirmadas
    (bug: 'duplicado cuando se agenda hora')."""

    def _slot_confirmadas(self, fecha, hora, prof=1):
        from boto3.dynamodb.conditions import Key, Attr
        resp = db.get_table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"APPT#PROF#{prof}")
            & Key("GSI1SK").eq(f"DATE#{fecha}#{hora}"),
            FilterExpression=Attr("estado").eq("confirmada"),
        )
        return resp["Items"]

    def test_dos_pacientes_no_pueden_tomar_el_mismo_slot(self):
        a = db.get_or_create_cliente("telegram", "dup_a", "Ana")
        b = db.get_or_create_cliente("telegram", "dup_b", "Beto")
        lunes = _proximo_lunes().isoformat()
        db.crear_cita(a["id"], 1, 1, lunes, "10:00")
        with pytest.raises(db.SlotNoDisponibleError):
            db.crear_cita(b["id"], 1, 1, lunes, "10:00")
        assert len(self._slot_confirmadas(lunes, "10:00")) == 1

    def test_cancelar_libera_el_slot(self):
        a = db.get_or_create_cliente("telegram", "dup_c", "Ceci")
        b = db.get_or_create_cliente("telegram", "dup_d", "Dani")
        lunes = _proximo_lunes().isoformat()
        cita = db.crear_cita(a["id"], 1, 1, lunes, "11:00")
        db.cancelar_cita(cita["PK"], cita["SK"])
        # el slot quedó libre: otro paciente puede tomarlo
        db.crear_cita(b["id"], 1, 1, lunes, "11:00")
        assert len(self._slot_confirmadas(lunes, "11:00")) == 1

    def test_modificar_a_slot_ocupado_no_pierde_la_cita(self):
        a = db.get_or_create_cliente("telegram", "dup_e", "Eva")
        b = db.get_or_create_cliente("telegram", "dup_f", "Fran")
        lunes = _proximo_lunes().isoformat()
        cita_a = db.crear_cita(a["id"], 1, 1, lunes, "09:00")
        db.crear_cita(b["id"], 1, 1, lunes, "12:00")
        # A intenta reagendar a las 12:00 (ocupado por B) → debe fallar
        with pytest.raises(db.SlotNoDisponibleError):
            db.modificar_cita(cita_a["PK"], cita_a["SK"], lunes, "12:00")
        # la cita de A sigue viva en su horario original
        activas = db.get_citas_cliente(a["id"])
        assert len(activas) == 1
        assert activas[0]["hora"] == "09:00"

    def test_reagendar_al_mismo_slot_no_falla(self):
        a = db.get_or_create_cliente("telegram", "dup_g", "Gabo")
        lunes = _proximo_lunes().isoformat()
        cita = db.crear_cita(a["id"], 1, 1, lunes, "10:00")
        db.modificar_cita(cita["PK"], cita["SK"], lunes, "10:00")  # mismo slot
        activas = db.get_citas_cliente(a["id"])
        assert len(activas) == 1
        assert activas[0]["hora"] == "10:00"


class TestDisponibilidad:
    def test_horario_normal_ofrece_slots(self):
        horas = db.get_horas_disponibles(1, _proximo_lunes(), 60)
        assert "09:00" in horas
        # último slot de 60 min en horario 09:00-18:00
        assert "17:00" in horas
        assert "17:30" not in horas

    def test_cita_existente_bloquea_solapamiento(self):
        cliente = db.get_or_create_cliente("telegram", "moto_overlap", "Gabi")
        lunes = _proximo_lunes()
        # Consulta inicial de 60 min a las 10:00 ocupa 10:00-11:00
        db.crear_cita(cliente["id"], 1, 1, lunes.isoformat(), "10:00")
        horas = db.get_horas_disponibles(1, lunes, 60)
        assert "10:00" not in horas
        assert "10:30" not in horas  # 10:30-11:30 solapa con 10:00-11:00
        assert "09:30" not in horas  # 09:30-10:30 solapa con 10:00-11:00

    def test_cita_adyacente_no_bloquea(self):
        cliente = db.get_or_create_cliente("telegram", "moto_adjacent", "Hugo")
        lunes = _proximo_lunes()
        db.crear_cita(cliente["id"], 1, 1, lunes.isoformat(), "10:00")
        horas = db.get_horas_disponibles(1, lunes, 60)
        assert "09:00" in horas  # termina 10:00 exacto, sin solapar
        assert "11:00" in horas  # empieza cuando la otra termina

    def test_bloqueo_dia_completo(self):
        lunes = _proximo_lunes()
        db.bloquear_fecha(1, lunes.isoformat())
        assert db.get_horas_disponibles(1, lunes, 30) == []

    def test_bloqueo_hora_especifica(self):
        lunes = _proximo_lunes()
        db.bloquear_hora(1, lunes.isoformat(), "09:00")
        horas = db.get_horas_disponibles(1, lunes, 30)
        assert "09:00" not in horas
        assert "09:30" in horas

    def test_desbloquear_fecha(self):
        lunes = _proximo_lunes()
        db.bloquear_fecha(1, lunes.isoformat())
        db.desbloquear_fecha(1, lunes.isoformat())
        assert "09:00" in db.get_horas_disponibles(1, lunes, 30)

    def test_domingo_sin_horario(self):
        domingo = _proximo_lunes() + timedelta(days=6)
        assert db.get_horas_disponibles(1, domingo, 30) == []

    def test_duracion_decimal_no_revienta(self):
        """DynamoDB devuelve números como Decimal; la duración del servicio
        leída fresca de la tabla llega como Decimal. get_horas_disponibles debe
        aceptarla sin lanzar TypeError en timedelta (bug de 'modificar cita')."""
        from decimal import Decimal

        horas = db.get_horas_disponibles(1, _proximo_lunes(), Decimal("60"))
        assert "09:00" in horas
        assert "17:00" in horas

    def test_fechas_disponibles_acepta_decimal(self):
        from decimal import Decimal

        fechas = db.get_fechas_disponibles(1, Decimal("30"))
        assert fechas  # hay al menos una fecha disponible esta semana


# ---------------------------------------------------------------------------
# Flujo "modificar cita" end-to-end (chatbot_lambda + DynamoDB moto)
# ---------------------------------------------------------------------------

class TestModificarFlujo:
    """Regresión del bug: tras elegir la cita a modificar el bot se quedaba
    pegado (TypeError swallowed) en vez de mostrar las nuevas fechas."""

    def _crear_cita_futura(self, canal="telegram", uid="moto_modify"):
        import chatbot_lambda  # noqa: F401  (asegura misma tabla via fixture)

        cliente = db.get_or_create_cliente(canal, uid, "Paciente Modify")
        lunes = _proximo_lunes()
        servicios = db.get_servicios()
        serv = next(s for s in servicios if "inicial" in s["nombre"].lower())
        db.crear_cita(cliente["id"], serv["id"], 1, lunes.isoformat(), "10:00")
        return canal, uid, lunes

    def test_seleccionar_cita_avanza_a_fechas(self):
        import chatbot_lambda as cb

        canal, uid, _ = self._crear_cita_futura()
        cb.handle_message(canal, uid, "menu")
        cb.handle_message(canal, uid, "2")  # modificar
        assert session_store.get_session(uid)["state"] == cb.MODIFY_SELECT

        resp = cb.handle_message(canal, uid, "1")  # seleccionar la cita

        assert "fechas" in resp.lower()
        assert session_store.get_session(uid)["state"] == cb.MODIFY_DATE

    def test_reagendar_completo(self):
        import chatbot_lambda as cb

        canal, uid, _ = self._crear_cita_futura(uid="moto_modify_full")
        cb.handle_message(canal, uid, "menu")
        cb.handle_message(canal, uid, "2")
        cb.handle_message(canal, uid, "1")  # cita
        cb.handle_message(canal, uid, "1")  # nueva fecha
        cb.handle_message(canal, uid, "1")  # nueva hora
        resp = cb.handle_message(canal, uid, "si")  # confirmar
        assert "reagendada" in resp.lower()
        assert session_store.get_session(uid)["state"] == cb.IDLE


class TestBookingConfirmSlotOcupado:
    """Si el slot se ocupa entre que se muestran las horas y el usuario
    confirma, el bot avisa con un mensaje y no revienta (ni duplica)."""

    def test_confirmar_slot_ocupado_avisa(self):
        import chatbot_lambda as cb

        canal, uid = "telegram", "confirm_race"
        cliente = db.get_or_create_cliente(canal, uid, "Race")
        lunes = _proximo_lunes()
        servicios = db.get_servicios()
        serv = next(s for s in servicios if "inicial" in s["nombre"].lower())
        # Llevar la sesión hasta BOOKING_CONFIRM con un slot elegido
        cb.handle_message(canal, uid, "menu")
        cb.handle_message(canal, uid, "1")          # agendar → lista de servicios
        cb.handle_message(canal, uid, "1")          # servicio → lista de fechas
        cb.handle_message(canal, uid, "1")          # fecha → lista de horas
        cb.handle_message(canal, uid, "1")          # hora → pide nombre
        cb.handle_message(canal, uid, "Race")       # nombre → BOOKING_CONFIRM
        sesion = session_store.get_session(uid)
        assert sesion["state"] == cb.BOOKING_CONFIRM
        fecha_sel = sesion["data"]["fecha"]
        hora_sel = sesion["data"]["hora"]
        # Otro paciente ocupa exactamente ese slot
        otro = db.get_or_create_cliente(canal, "confirm_race_otro", "Otro")
        db.crear_cita(otro["id"], serv["id"], 1, fecha_sel.isoformat(), hora_sel)

        resp = cb.handle_message(canal, uid, "si")  # confirmar
        assert "ocup" in resp.lower()
        assert session_store.get_session(uid)["state"] == cb.IDLE
        # solo existe la cita del otro paciente en ese slot
        from boto3.dynamodb.conditions import Key, Attr
        confirmadas = db.get_table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq("APPT#PROF#1")
            & Key("GSI1SK").eq(f"DATE#{fecha_sel.isoformat()}#{hora_sel}"),
            FilterExpression=Attr("estado").eq("confirmada"),
        )["Items"]
        assert len(confirmadas) == 1


# ---------------------------------------------------------------------------
# session_store
# ---------------------------------------------------------------------------

class TestSessionStore:
    def test_sesion_inexistente_retorna_idle(self):
        session = session_store.get_session("moto_no_session")
        assert session == {"state": "IDLE", "data": {}}

    def test_roundtrip_con_fecha(self):
        fecha = _proximo_lunes()
        session_store.save_session(
            "moto_session_1",
            {"state": "BOOKING_TIME", "data": {"fecha": fecha, "horas": ["09:00"]}},
        )
        recuperada = session_store.get_session("moto_session_1")
        assert recuperada["state"] == "BOOKING_TIME"
        assert recuperada["data"]["fecha"] == fecha
        assert recuperada["data"]["horas"] == ["09:00"]

    def test_save_session_escribe_ttl(self, dynamo_mock_table):
        session_store.save_session("moto_session_ttl", {"state": "IDLE", "data": {}})
        item = dynamo_mock_table.get_item(
            Key={"PK": "SESSION", "SK": "USER#moto_session_ttl"}
        )["Item"]
        ttl = int(item["ttl"])
        ahora = int(time.time())
        assert ahora < ttl <= ahora + session_store.SESSION_TTL_SECONDS + 5

    def test_clear_session(self):
        session_store.save_session("moto_session_clear", {"state": "BOOKING_NAME", "data": {}})
        session_store.clear_session("moto_session_clear")
        assert session_store.get_session("moto_session_clear")["state"] == "IDLE"


# ---------------------------------------------------------------------------
# lambda_handler: webhook Telegram end-to-end contra moto
# ---------------------------------------------------------------------------

@pytest.fixture()
def lambda_app_client():
    """TestClient del app Lambda con envío de Telegram capturado."""
    import lambda_handler

    sent: list[dict] = []

    async def fake_send(chat_id, text, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        if markup is None and args:
            markup = args[0]
        sent.append({"chat_id": chat_id, "text": text, "reply_markup": markup})

    with patch.object(lambda_handler, "_send_telegram", side_effect=fake_send):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as client:
            yield client, sent


def _telegram_update(user_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": user_id, "type": "private"},
            "date": int(time.time()),
            "text": text,
        },
    }


class TestTelegramWebhookConMoto:
    def test_menu_responde_bienvenida(self, lambda_app_client):
        client, sent = lambda_app_client
        resp = client.post(
            "/telegram/webhook",
            json=_telegram_update(111222, "menu"),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )
        assert resp.status_code == 200
        assert len(sent) == 1
        # Las opciones van como botones (no duplicadas como texto numerado).
        assert "¿Qué deseas hacer?" in sent[0]["text"]
        labels = [b["text"] for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
        assert "Agendar una hora" in labels

    def test_flujo_agendar_muestra_servicios(self, lambda_app_client):
        client, sent = lambda_app_client
        headers = {"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET}
        client.post("/telegram/webhook", json=_telegram_update(333444, "menu"), headers=headers)
        client.post("/telegram/webhook", json=_telegram_update(333444, "1"), headers=headers)
        labels = [b["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
        assert any("Consulta inicial" in lbl for lbl in labels)

    def test_sesion_persiste_en_dynamo(self, lambda_app_client, dynamo_mock_table):
        client, _ = lambda_app_client
        headers = {"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET}
        client.post("/telegram/webhook", json=_telegram_update(555666, "menu"), headers=headers)
        client.post("/telegram/webhook", json=_telegram_update(555666, "1"), headers=headers)
        item = dynamo_mock_table.get_item(
            Key={"PK": "SESSION", "SK": "USER#555666"}
        ).get("Item")
        assert item is not None
        assert item["state"] == "BOOKING_SERVICE"

    def test_secret_invalido_rechazado(self, lambda_app_client):
        client, sent = lambda_app_client
        resp = client.post(
            "/telegram/webhook",
            json=_telegram_update(777888, "menu"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert resp.status_code == 403
        assert sent == []

    def test_error_interno_no_deja_pegado(self, lambda_app_client):
        """Si el motor lanza una excepción inesperada, el webhook no debe dejar
        al usuario sin respuesta: avisa con un mensaje y resetea la sesión."""
        import lambda_handler

        with patch.object(
            lambda_handler.chatbot, "handle_message", side_effect=RuntimeError("boom")
        ):
            client, sent = lambda_app_client
            resp = client.post(
                "/telegram/webhook",
                json=_telegram_update(424242, "2"),
                headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
            )
        assert resp.status_code == 200
        assert len(sent) == 1  # el usuario recibió un aviso, no silencio
        assert "menu" in sent[0]["text"].lower()
        # la sesión queda en IDLE para poder reintentar
        assert session_store.get_session("424242")["state"] == "IDLE"


# ---------------------------------------------------------------------------
# rate_limiter backend dynamo contra la tabla compartida
# ---------------------------------------------------------------------------

class TestRateLimiterDynamoEnTablaPrincipal:
    def test_contador_persiste_en_tabla(self, monkeypatch, dynamo_mock_table):
        import rate_limiter

        monkeypatch.setenv("RATE_LIMITER_BACKEND", "dynamo")
        assert not rate_limiter.is_rate_limited("moto_rl_user")
        window = int(time.time()) // rate_limiter.WINDOW_SECONDS
        item = dynamo_mock_table.get_item(
            Key={"PK": "RATELIMIT#moto_rl_user", "SK": f"WINDOW#{window}"}
        )["Item"]
        assert int(item["count"]) == 1
        assert "ttl" in item

    def test_bloquea_al_exceder_limite(self, monkeypatch):
        import rate_limiter

        monkeypatch.setenv("RATE_LIMITER_BACKEND", "dynamo")
        for _ in range(rate_limiter.MAX_MESSAGES):
            rate_limiter.is_rate_limited("moto_rl_blocked")
        assert rate_limiter.is_rate_limited("moto_rl_blocked")
