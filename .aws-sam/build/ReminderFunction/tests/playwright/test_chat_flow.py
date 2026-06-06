"""Playwright E2E: simulación de flujo conversacional como usuario.

Envía mensajes al webhook de WhatsApp (como si el usuario estuviera
chateando) y captura las respuestas que el bot intenta enviar.

Depende del fixture chatbot_responses definido en conftest.py que
captura los mensajes salientes mockeando send_message.
"""
import sys
from typing import List

import pytest
from playwright.sync_api import APIRequestContext

# Importamos la lista de captura del conftest
from tests.playwright.conftest import chatbot_responses

TEST_USER = "pw_e2e_whatsapp_user"
BASE_MSG_COUNT = 0


def _whatsapp_payload(from_number: str, text: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": from_number,
                        "type": "text",
                        "text": {"body": text},
                    }]
                }
            }]
        }]
    }


def _send(
    api: APIRequestContext,
    text: str,
    user: str = TEST_USER,
) -> List[dict]:
    """Envía un mensaje como usuario y retorna las respuestas capturadas."""
    prev = len(chatbot_responses)
    api.post("/whatsapp/webhook", data=_whatsapp_payload(user, text))
    return chatbot_responses[prev:]


class TestChatE2EReset:
    def test_comando_reset_muestra_bienvenida(self, api_context: APIRequestContext):
        respuestas = _send(api_context, "menu")
        assert len(respuestas) >= 1
        texto = respuestas[0]["text"]
        assert "Hola" in texto or "🌸" in texto
        assert "1️⃣" in texto


class TestChatE2EBooking:
    def test_paso_1_seleccionar_servicio(self, api_context: APIRequestContext):
        _send(api_context, "menu")
        respuestas = _send(api_context, "1")
        texto = respuestas[0]["text"]
        assert "servicio" in texto.lower() or "consulta" in texto.lower()

    def test_paso_2_seleccionar_fecha(self, api_context: APIRequestContext):
        _send(api_context, "menu")
        _send(api_context, "1")
        r2 = _send(api_context, "1")
        texto = r2[0]["text"]
        assert any(p in texto for p in ["profesional", "Fecha", "fecha", "disponible"])

    def test_indice_invalido_muestra_error(self, api_context: APIRequestContext):
        _send(api_context, "menu")
        _send(api_context, "1")
        r = _send(api_context, "99")
        texto = r[0]["text"]
        assert "No entendí" in texto or "inválida" in texto or "error" in texto.lower()

    def test_flujo_completo_agendamiento(self, api_context: APIRequestContext):
        """Flujo feliz completo como usuario real."""
        pasos = [
            ("menu", "bienvenida"),
            ("1", "servicio"),
            ("1", "profesional/fecha"),
            ("1", "hora"),
            ("1", "nombre"),
            ("Juan Pérez", "confirmación"),
            ("si", "cita agendada"),
        ]
        for i, (texto, etapa) in enumerate(pasos):
            respuestas = _send(api_context, texto)
            if respuestas:
                print(f"  [{i}] {etapa}: {respuestas[0]['text'][:80]}", file=sys.stderr)

        assert len(chatbot_responses) >= len(pasos)
        ultimo_texto = chatbot_responses[-1]["text"]
        assert any(p in ultimo_texto for p in ["✅", "agendada", "Cita agendada"])

    def test_rechazar_cita(self, api_context: APIRequestContext):
        """Usuario rechaza la confirmación."""
        for texto in ["menu", "1", "1", "1", "1", "Juan Pérez"]:
            _send(api_context, texto)
        r = _send(api_context, "no")
        texto = r[0]["text"]
        assert "no agendada" in texto.lower() or "Cita no" in texto
        assert chatbot_responses[-1]["to"] == TEST_USER


class TestChatE2ECancel:
    def test_intentar_cancelar_sin_citas(self, api_context: APIRequestContext):
        """Usuario sin citas intenta cancelar."""
        user_sin_citas = "pw_no_citas_user"
        _send(api_context, "menu", user_sin_citas)
        r = _send(api_context, "3", user_sin_citas)
        assert "No tienes citas" in r[0]["text"]

    def test_cancelar_cita_existente(self, api_context: APIRequestContext):
        """Agendar y luego cancelar."""
        for texto in ["menu", "1", "1", "1", "1", "Juan Pérez", "si"]:
            _send(api_context, texto)

        chatbot_responses.clear()
        for texto in ["menu", "3", "1", "si"]:
            _send(api_context, texto)

        assert any("cancelada" in r["text"].lower() for r in chatbot_responses)

    def test_abortar_cancelacion(self, api_context: APIRequestContext):
        """Iniciar cancelación pero abortar."""
        for texto in ["menu", "1", "1", "1", "1", "Juan Pérez", "si"]:
            _send(api_context, texto)

        chatbot_responses.clear()
        _send(api_context, "menu")
        _send(api_context, "3")
        _send(api_context, "1")
        r = _send(api_context, "no")
        assert any(p in r[0]["text"].lower() for p in ["abortada", "cancelación", "no"])


class TestChatE2EModify:
    def test_modificar_cita(self, api_context: APIRequestContext):
        """Agendar, modificar fecha/hora, verificar cambio."""
        for texto in ["menu", "1", "1", "1", "1", "Juan Pérez", "si"]:
            _send(api_context, texto)

        chatbot_responses.clear()
        for texto in ["menu", "2", "1", "1", "1"]:
            _send(api_context, texto)

        assert any("modificada" in r["text"].lower() or "✅" in r["text"] for r in chatbot_responses)


class TestChatE2EMultipleUsers:
    def test_dos_usuarios_independientes(self, api_context: APIRequestContext):
        """Dos usuarios conversan simultáneamente sin mezclar estados."""
        user_a = "pw_user_a"
        user_b = "pw_user_b"

        _send(api_context, "menu", user_a)
        _send(api_context, "1", user_a)
        _send(api_context, "menu", user_b)

        resp_a = _send(api_context, "1", user_a)
        resp_b = _send(api_context, "1", user_b)

        assert len(resp_a) >= 1
        assert len(resp_b) >= 1
        assert any(p in resp_a[0]["text"].lower() for p in ["servicio", "fecha", "profesional", "disponible"])
        assert any(p in resp_b[0]["text"].lower() for p in ["servicio", "fecha", "profesional", "disponible"])

    def test_dos_usuarios_no_comparten_datos(self, api_context: APIRequestContext):
        """Los datos de sesión de cada usuario son independientes."""
        user_c = "pw_user_c"
        user_d = "pw_user_d"

        _send(api_context, "menu", user_c)
        _send(api_context, "1", user_c)
        _send(api_context, "1", user_c)
        _send(api_context, "1", user_c)

        chatbot_responses.clear()
        _send(api_context, "menu", user_d)
        r = _send(api_context, "1", user_d)

        assert len(r) >= 1
        texto = r[0]["text"]
        assert "servicio" in texto.lower()
        assert "servicio" in texto
