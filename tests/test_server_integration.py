"""Tests de integración del servidor FastAPI.

Valida endpoints HTTP, webhook de WhatsApp y flujos conversacionales
completos usando TestClient con mock de canales externos.
"""
import json
import pytest
from fastapi.testclient import TestClient

from config import WHATSAPP_VERIFY_TOKEN
from tests.conftest import TEST_USER


class TestHealthEndpoint:
    def test_health_returns_ok(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "chatbot-agendamiento"


class TestWhatsAppWebhookVerification:
    def test_webhook_verification_valida(self, client: TestClient):
        """GET con token correcto devuelve el challenge."""
        resp = client.get(
            "/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": WHATSAPP_VERIFY_TOKEN,
                "hub.challenge": "challenge_123",
            },
        )
        assert resp.status_code == 200
        assert resp.text == "challenge_123"

    def test_webhook_verification_token_invalido(self, client: TestClient):
        """GET con token incorrecto devuelve 403."""
        resp = client.get(
            "/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "token_incorrecto",
                "hub.challenge": "challenge_123",
            },
        )
        assert resp.status_code == 403

    def test_webhook_verification_sin_mode(self, client: TestClient):
        """GET sin hub.mode devuelve 403."""
        resp = client.get(
            "/whatsapp/webhook",
            params={
                "hub.verify_token": WHATSAPP_VERIFY_TOKEN,
                "hub.challenge": "challenge_123",
            },
        )
        assert resp.status_code == 403


class TestWhatsAppWebhookMessage:
    def test_webhook_mensaje_saludo(self, client: TestClient, mock_whatsapp_send):
        """POST con 'menu' dispara bienvenida como respuesta."""
        payload = self._build_whatsapp_payload(TEST_USER, "menu")
        resp = client.post("/whatsapp/webhook", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        assert len(mock_whatsapp_send) >= 1
        assert "Hola" in mock_whatsapp_send[0]["text"] or "🌸" in mock_whatsapp_send[0]["text"]

    def test_webhook_mensaje_no_texto(self, client: TestClient, mock_whatsapp_send):
        """Mensaje sin texto (tipo imagen) no genera respuesta."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": TEST_USER,
                            "type": "image",
                            "image": {"id": "img123"},
                        }]
                    }
                }]
            }]
        }
        resp = client.post("/whatsapp/webhook", json=payload)
        assert resp.status_code == 200
        assert len(mock_whatsapp_send) == 0

    def test_webhook_sin_mensajes(self, client: TestClient, mock_whatsapp_send):
        """Payload sin key 'messages' no genera respuesta."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {"statuses": [{"id": "status1"}]}
                }]
            }]
        }
        resp = client.post("/whatsapp/webhook", json=payload)
        assert resp.status_code == 200
        assert len(mock_whatsapp_send) == 0

    def test_payload_invalido(self, client: TestClient, mock_whatsapp_send):
        """Payload malformado no causa crash."""
        resp = client.post("/whatsapp/webhook", json={})
        assert resp.status_code == 200

    def test_webhook_mantiene_sesion(self, client: TestClient, mock_whatsapp_send):
        """Múltiples mensajes mantienen estado de conversación."""
        payloads = [
            self._build_whatsapp_payload(TEST_USER, "menu"),
            self._build_whatsapp_payload(TEST_USER, "1"),
        ]
        for p in payloads:
            client.post("/whatsapp/webhook", json=p)

        assert len(mock_whatsapp_send) >= 2
        assert "servicio" in mock_whatsapp_send[1]["text"].lower() or "consulta" in mock_whatsapp_send[1]["text"].lower()

    def test_ciclo_completo_agendamiento(self, client: TestClient, mock_whatsapp_send):
        """Flujo completo de agendamiento vía webhook WhatsApp."""
        mensajes = ["menu", "1", "1", "1", "1", "Juan Pérez", "si"]
        for texto in mensajes:
            client.post("/whatsapp/webhook", json=self._build_whatsapp_payload(TEST_USER, texto))

        textos_enviados = [m["text"] for m in mock_whatsapp_send]
        ultimo = textos_enviados[-1] if textos_enviados else ""
        assert any(palabra in ultimo for palabra in ["✅", "Cita agendada", "agendada"])

    def test_ciclo_completo_cancelacion(self, client: TestClient, mock_whatsapp_send):
        """Flujo completo: agendar → cancelar."""
        for texto in ["menu", "1", "1", "1", "1", "Juan Pérez", "si"]:
            client.post("/whatsapp/webhook", json=self._build_whatsapp_payload(TEST_USER, texto))

        mock_whatsapp_send.clear()
        for texto in ["menu", "3", "1", "si"]:
            client.post("/whatsapp/webhook", json=self._build_whatsapp_payload(TEST_USER, texto))

        textos = [m["text"] for m in mock_whatsapp_send]
        assert any("cancelada" in t.lower() for t in textos)

    @staticmethod
    def _build_whatsapp_payload(from_number: str, text_body: str) -> dict:
        return {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": from_number,
                            "type": "text",
                            "text": {"body": text_body},
                        }]
                    }
                }]
            }]
        }





class TestNotFound:
    def test_ruta_inexistente(self, client: TestClient):
        resp = client.get("/ruta-no-existe")
        assert resp.status_code == 404



