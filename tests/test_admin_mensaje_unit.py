"""Tests del endpoint admin para contactar al paciente vía el bot:
POST /admin/cliente/mensaje — envía un mensaje por el canal del paciente,
opcionalmente con botones para reagendar/cancelar su cita.

Seguridad: nunca confía en un destino del body; usa el canal/canal_user_id del
cliente almacenado (regla #4 de CLAUDE.md).
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import database_dynamo as db

VALID_KEY = "mensaje-test-key-123456"
AUTH = {"Authorization": f"Bearer {VALID_KEY}"}


@pytest.fixture()
def admin_client():
    import lambda_handler

    with patch("lambda_handler.ADMIN_API_KEY", VALID_KEY):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            yield c


class TestMensajeEndpoint:
    def test_sin_auth_rechazado(self, admin_client):
        r = admin_client.post("/admin/cliente/mensaje",
                              json={"cliente_id": "x", "texto": "hola"})
        assert r.status_code == 401

    def test_cliente_inexistente_404(self, admin_client):
        r = admin_client.post("/admin/cliente/mensaje",
                              json={"cliente_id": "CHAN#telegram#nope", "texto": "hola"},
                              headers=AUTH)
        assert r.status_code == 404

    def test_texto_vacio_400(self, admin_client):
        cli = db.get_or_create_cliente("telegram", "msg_empty", "Ana")
        r = admin_client.post("/admin/cliente/mensaje",
                              json={"cliente_id": cli["id"], "texto": "   "}, headers=AUTH)
        assert r.status_code == 400

    def test_envia_telegram_sin_acciones(self, admin_client):
        import lambda_handler
        cli = db.get_or_create_cliente("telegram", "555111", "Bruno")
        with patch.object(lambda_handler, "_send_telegram", new=AsyncMock()) as send:
            r = admin_client.post("/admin/cliente/mensaje",
                                  json={"cliente_id": cli["id"], "texto": "Te espero mañana"},
                                  headers=AUTH)
        assert r.status_code == 200
        send.assert_awaited_once()
        args, kwargs = send.call_args
        assert int(args[0]) == 555111          # se envía al chat_id almacenado
        assert "mañana" in args[1]
        assert kwargs.get("reply_markup") in (None, {}) or "reply_markup" not in kwargs

    def test_envia_telegram_con_acciones_lleva_botones(self, admin_client):
        import lambda_handler
        cli = db.get_or_create_cliente("telegram", "555222", "Cata")
        with patch.object(lambda_handler, "_send_telegram", new=AsyncMock()) as send:
            r = admin_client.post("/admin/cliente/mensaje",
                                  json={"cliente_id": cli["id"], "texto": "¿Seguimos?", "acciones": True},
                                  headers=AUTH)
        assert r.status_code == 200
        _, kwargs = send.call_args
        markup = kwargs.get("reply_markup")
        assert markup and "inline_keyboard" in markup
        datas = {b["callback_data"] for row in markup["inline_keyboard"] for b in row}
        assert datas == {"2", "3"}             # reagendar / cancelar (menú del bot)

    def test_envia_whatsapp_con_acciones_botones(self, admin_client):
        import lambda_handler
        cli = db.get_or_create_cliente("whatsapp", "5491100000000", "Dani")
        with patch.object(lambda_handler, "_send_whatsapp_buttons", new=AsyncMock()) as send:
            r = admin_client.post("/admin/cliente/mensaje",
                                  json={"cliente_id": cli["id"], "texto": "Hola", "acciones": True},
                                  headers=AUTH)
        assert r.status_code == 200
        args, _ = send.call_args
        assert args[0] == "5491100000000"
        ids = {bid for bid, _title in args[2]}
        assert ids == {"2", "3"}   # botones interactivos = menú del bot

    def test_envia_whatsapp_sin_acciones_texto_plano(self, admin_client):
        import lambda_handler
        cli = db.get_or_create_cliente("whatsapp", "5491100000001", "Eze")
        with patch.object(lambda_handler, "_send_whatsapp", new=AsyncMock()) as send:
            r = admin_client.post("/admin/cliente/mensaje",
                                  json={"cliente_id": cli["id"], "texto": "Hola"}, headers=AUTH)
        assert r.status_code == 200
        send.assert_awaited_once()
