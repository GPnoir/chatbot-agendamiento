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
        sent.append({"chat_id": chat_id, "text": text})

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
        assert "1️⃣ Agendar una hora" in sent[0]["text"]

    def test_flujo_agendar_muestra_servicios(self, lambda_app_client):
        client, sent = lambda_app_client
        headers = {"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET}
        client.post("/telegram/webhook", json=_telegram_update(333444, "menu"), headers=headers)
        client.post("/telegram/webhook", json=_telegram_update(333444, "1"), headers=headers)
        assert "Consulta inicial" in sent[-1]["text"]

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
