"""Tests del envío de recordatorios por WhatsApp respetando la ventana de 24h.

Meta solo acepta texto libre dentro de la ventana de 24h desde el último
mensaje del paciente. El recordatorio es proactivo (se manda ~24h antes de la
cita), así que casi siempre cae FUERA de la ventana y Meta lo rechaza. La
solución: mandar una plantilla (template) pre-aprobada cuando está configurada,
y —clave— chequear la respuesta de Meta para no marcar como enviado un
recordatorio que en realidad rebotó.
"""
from datetime import datetime, timedelta

from unittest.mock import patch

import database_dynamo as db
import reminder_handler as rh

_CHILE = timedelta(hours=-4)


class _FakeResp:
    """Respuesta HTTP mínima para simular a Meta sin salir a la red."""

    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


def _cita_manana_whatsapp(phone="56900000001", nombre="Marta"):
    """Crea un cliente de WhatsApp y una cita ~2h en el futuro (dentro de 24h)."""
    dt = (datetime.utcnow() + _CHILE) + timedelta(hours=2)
    cli = db.get_or_create_cliente("whatsapp", phone, nombre)
    return db.crear_cita(cli["id"], 1, 1, dt.date().isoformat(), dt.strftime("%H:%M"))


def _cita_manana_telegram(uid="55501", nombre="Pepe"):
    dt = (datetime.utcnow() + _CHILE) + timedelta(hours=2)
    cli = db.get_or_create_cliente("telegram", uid, nombre)
    return db.crear_cita(cli["id"], 1, 1, dt.date().isoformat(), dt.strftime("%H:%M"))


class TestRecordatorioWhatsAppTemplate:
    def test_usa_template_cuando_esta_configurado(self):
        cita = _cita_manana_whatsapp(phone="56900000010")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResp(200, {"messages": [{"id": "wamid.X"}]})

        with patch.object(rh, "WHATSAPP_REMINDER_TEMPLATE", "recordatorio_cita"), \
             patch.object(rh, "WHATSAPP_TEMPLATE_LANG", "es"), \
             patch.object(rh.httpx, "post", side_effect=fake_post):
            rh.handler({}, None)

        payload = captured["payload"]
        assert payload["type"] == "template"
        assert payload["template"]["name"] == "recordatorio_cita"
        assert payload["template"]["language"]["code"] == "es"
        # El body lleva 2 parámetros: servicio y cuándo (fecha a las hora).
        params = payload["template"]["components"][0]["parameters"]
        assert [p["type"] for p in params] == ["text", "text"]
        textos = [p["text"] for p in params]
        assert cita["servicio_nombre"] in textos[0]
        assert cita["hora"] in textos[1]

    def test_texto_libre_si_no_hay_template(self):
        _cita_manana_whatsapp(phone="56900000011")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _FakeResp(200, {"messages": [{"id": "wamid.Y"}]})

        with patch.object(rh, "WHATSAPP_REMINDER_TEMPLATE", ""), \
             patch.object(rh.httpx, "post", side_effect=fake_post):
            rh.handler({}, None)

        assert captured["payload"]["type"] == "text"

    def test_telegram_no_usa_template(self):
        """Regresión: Telegram no tiene ventana de 24h; sigue con texto libre."""
        _cita_manana_telegram(uid="55502")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _FakeResp(200, {"result": {}})

        with patch.object(rh, "WHATSAPP_REMINDER_TEMPLATE", "recordatorio_cita"), \
             patch.object(rh.httpx, "post", side_effect=fake_post):
            rh.handler({}, None)

        # Telegram usa sendMessage con {chat_id, text}, nunca un template de WA.
        assert "template" not in captured["payload"]
        assert "chat_id" in captured["payload"]


class TestRespuestaDeMetaChequeada:
    def test_no_marca_enviado_si_meta_rechaza(self):
        cita = _cita_manana_whatsapp(phone="56900000020")

        def fake_post(url, json=None, headers=None, timeout=None):
            # 131047: fuera de la ventana de 24h.
            return _FakeResp(400, {"error": {"code": 131047, "message": "re-engagement"}})

        with patch.object(rh, "WHATSAPP_REMINDER_TEMPLATE", ""), \
             patch.object(rh.httpx, "post", side_effect=fake_post):
            rh.handler({}, None)

        item = db.get_table().get_item(Key={"PK": cita["PK"], "SK": cita["SK"]})["Item"]
        assert "recordatorio_enviado" not in item, (
            "un recordatorio rechazado por Meta no debe marcarse como enviado"
        )

    def test_marca_enviado_si_meta_acepta(self):
        cita = _cita_manana_whatsapp(phone="56900000021")

        def fake_post(url, json=None, headers=None, timeout=None):
            return _FakeResp(200, {"messages": [{"id": "wamid.OK"}]})

        with patch.object(rh, "WHATSAPP_REMINDER_TEMPLATE", ""), \
             patch.object(rh.httpx, "post", side_effect=fake_post):
            rh.handler({}, None)

        item = db.get_table().get_item(Key={"PK": cita["PK"], "SK": cita["SK"]})["Item"]
        assert item.get("recordatorio_enviado") is True


class TestSendHelpers:
    def test_send_whatsapp_lanza_en_error(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            return _FakeResp(401, {"error": {"code": 190, "message": "bad token"}})

        with patch.object(rh.httpx, "post", side_effect=fake_post):
            try:
                rh.send_whatsapp("569", "hola")
                assert False, "debería lanzar WhatsAppSendError"
            except rh.WhatsAppSendError:
                pass

    def test_send_whatsapp_ok_no_lanza(self):
        with patch.object(rh.httpx, "post", side_effect=lambda *a, **k: _FakeResp(200, {"messages": []})):
            rh.send_whatsapp("569", "hola")  # no debe lanzar

    def test_template_payload_shape(self):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _FakeResp(200, {"messages": []})

        with patch.object(rh.httpx, "post", side_effect=fake_post):
            rh.send_whatsapp_template("569", "recordatorio_cita", "es", ["Consulta", "05-07 a las 15:00"])

        p = captured["payload"]
        assert p["messaging_product"] == "whatsapp"
        assert p["to"] == "569"
        assert p["type"] == "template"
        assert p["template"]["name"] == "recordatorio_cita"
        assert p["template"]["language"] == {"code": "es"}
        params = p["template"]["components"][0]["parameters"]
        assert params == [
            {"type": "text", "text": "Consulta"},
            {"type": "text", "text": "05-07 a las 15:00"},
        ]

    def test_no_loguea_el_token_en_el_error(self):
        """El mensaje de error de Meta no debe filtrar el token del bot."""
        def fake_post(url, json=None, headers=None, timeout=None):
            return _FakeResp(400, {"error": {"code": 131047, "message": "outside window"}})

        with patch.object(rh, "WHATSAPP_TOKEN", "SECRET_TOKEN_XYZ"), \
             patch.object(rh.httpx, "post", side_effect=fake_post):
            try:
                rh.send_whatsapp("569", "hola")
            except rh.WhatsAppSendError as e:
                assert "SECRET_TOKEN_XYZ" not in str(e)
