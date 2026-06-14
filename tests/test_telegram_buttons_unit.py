"""Tests para botones interactivos de Telegram (issue #10).

Cubre la derivación de inline keyboards desde el texto de respuesta
(telegram_ui), la validación estructural de callback_query
(input_validation) y el flujo completo del webhook con callbacks.
"""
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config import MENSAJES, NEGOCIO
from input_validation import validate_telegram_callback
from telegram_ui import build_reply_markup, build_message, button_label

TELEGRAM_SECRET = "test_telegram_secret"


# ---------------------------------------------------------------------------
# telegram_ui.build_reply_markup
# ---------------------------------------------------------------------------

class TestBuildReplyMarkup:
    def test_menu_bienvenida_genera_5_botones(self):
        texto = MENSAJES["bienvenida"].format(**NEGOCIO)
        markup = build_reply_markup(texto)
        assert markup is not None
        botones = [b for fila in markup["inline_keyboard"] for b in fila]
        assert len(botones) == 5
        assert botones[0]["callback_data"] == "1"
        assert "Agendar una hora" in botones[0]["text"]
        assert botones[4]["callback_data"] == "5"

    def test_lista_servicios_genera_botones(self):
        texto = (
            "¿Qué servicio necesitas?\n\n"
            "1️⃣ Consulta inicial (60 min)\n"
            "2️⃣ Sesión de seguimiento (30 min)\n"
            "3️⃣ Preparación de esencias (45 min)"
        )
        markup = build_reply_markup(texto)
        botones = [b for fila in markup["inline_keyboard"] for b in fila]
        assert len(botones) == 3
        assert botones[1]["callback_data"] == "2"
        assert "seguimiento" in botones[1]["text"]

    def test_opciones_multidigito(self):
        texto = "🕐 Horas disponibles:\n\n9️⃣ 13:00\n10️⃣ 13:30\n11️⃣ 14:00"
        markup = build_reply_markup(texto)
        botones = [b for fila in markup["inline_keyboard"] for b in fila]
        assert len(botones) == 3
        assert botones[1]["callback_data"] == "10"
        assert botones[1]["text"] == "13:30"

    def test_pregunta_si_no_genera_botones_confirmacion(self):
        texto = "¿Confirmar? (si/no)"
        markup = build_reply_markup(texto)
        botones = [b for fila in markup["inline_keyboard"] for b in fila]
        assert len(botones) == 2
        assert botones[0]["callback_data"] == "si"
        assert botones[1]["callback_data"] == "no"

    def test_texto_sin_opciones_retorna_none(self):
        assert build_reply_markup("✅ ¡Perfecto! Te esperamos.") is None

    def test_texto_vacio_retorna_none(self):
        assert build_reply_markup("") is None

    def test_etiquetas_truncadas_a_64_chars(self):
        # Telegram limita el texto del botón; etiquetas largas no deben romper
        texto = "1️⃣ " + "x" * 200
        markup = build_reply_markup(texto)
        boton = markup["inline_keyboard"][0][0]
        assert len(boton["text"]) <= 64


# ---------------------------------------------------------------------------
# input_validation.validate_telegram_callback
# ---------------------------------------------------------------------------

def _callback_update(user_id=123, data="1", with_message=True) -> dict:
    cq = {
        "id": "cbq-1",
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "data": data,
    }
    if with_message:
        cq["message"] = {
            "message_id": 7,
            "chat": {"id": user_id, "type": "private"},
            "date": int(time.time()),
        }
    return {"update_id": 2, "callback_query": cq}


class TestValidateTelegramCallback:
    def test_callback_valido(self):
        assert validate_telegram_callback(_callback_update()) is True

    def test_sin_callback_query(self):
        assert validate_telegram_callback({"update_id": 1, "message": {}}) is False

    def test_data_no_string(self):
        update = _callback_update()
        update["callback_query"]["data"] = 123
        assert validate_telegram_callback(update) is False

    def test_sin_from(self):
        update = _callback_update()
        del update["callback_query"]["from"]
        assert validate_telegram_callback(update) is False

    def test_sin_message_chat(self):
        assert validate_telegram_callback(_callback_update(with_message=False)) is False

    def test_no_dict(self):
        assert validate_telegram_callback("garbage") is False


# ---------------------------------------------------------------------------
# Webhook con callback_query (end-to-end contra moto)
# ---------------------------------------------------------------------------

@pytest.fixture()
def lambda_buttons_client():
    """TestClient del app Lambda capturando sendMessage y answerCallbackQuery."""
    import lambda_handler

    sent: list[dict] = []
    answered: list[str] = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def fake_answer(callback_query_id):
        answered.append(callback_query_id)

    with patch.object(lambda_handler, "_send_telegram", side_effect=fake_send), \
         patch.object(lambda_handler, "_answer_telegram_callback", side_effect=fake_answer):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as client:
            yield client, sent, answered


def _text_update(user_id: int, text: str) -> dict:
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


HEADERS = {"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET}


class TestWebhookCallbackQuery:
    def test_callback_avanza_la_conversacion(self, lambda_buttons_client):
        client, sent, answered = lambda_buttons_client
        client.post("/telegram/webhook", json=_text_update(901, "menu"), headers=HEADERS)
        resp = client.post(
            "/telegram/webhook", json=_callback_update(901, "1"), headers=HEADERS
        )
        assert resp.status_code == 200
        # El callback "1" equivale a escribir "1": ofrece los servicios. Ahora
        # los nombres van en los botones (no duplicados en el texto).
        labels = [b["text"] for row in sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
        assert any("Consulta inicial" in lbl for lbl in labels)

    def test_callback_es_respondido(self, lambda_buttons_client):
        client, _, answered = lambda_buttons_client
        client.post("/telegram/webhook", json=_callback_update(902, "1"), headers=HEADERS)
        assert answered == ["cbq-1"]

    def test_respuesta_con_opciones_lleva_botones(self, lambda_buttons_client):
        client, sent, _ = lambda_buttons_client
        client.post("/telegram/webhook", json=_text_update(903, "menu"), headers=HEADERS)
        markup = sent[-1]["reply_markup"]
        assert markup is not None
        botones = [b for fila in markup["inline_keyboard"] for b in fila]
        assert len(botones) == 5

    def test_respuesta_sin_opciones_no_lleva_botones(self, lambda_buttons_client):
        client, sent, _ = lambda_buttons_client
        # "4" (ver mis citas) sin citas agendadas responde texto plano
        client.post("/telegram/webhook", json=_text_update(904, "menu"), headers=HEADERS)
        client.post("/telegram/webhook", json=_text_update(904, "4"), headers=HEADERS)
        assert "No tienes citas" in sent[-1]["text"]
        assert sent[-1]["reply_markup"] is None

    def test_callback_con_secret_invalido_rechazado(self, lambda_buttons_client):
        client, sent, answered = lambda_buttons_client
        resp = client.post(
            "/telegram/webhook",
            json=_callback_update(905, "1"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert resp.status_code == 403
        assert sent == []
        assert answered == []

    def test_callback_data_oversized_ignorado(self, lambda_buttons_client):
        client, sent, _ = lambda_buttons_client
        update = _callback_update(906, "x" * 600)
        resp = client.post("/telegram/webhook", json=update, headers=HEADERS)
        assert resp.status_code == 200
        assert sent == []

    def test_callback_malformado_ignorado(self, lambda_buttons_client):
        client, sent, _ = lambda_buttons_client
        update = _callback_update(907, "1")
        del update["callback_query"]["from"]
        resp = client.post("/telegram/webhook", json=update, headers=HEADERS)
        assert resp.status_code == 200
        assert sent == []


# ---------------------------------------------------------------------------
# telegram_ui.build_message / button_label
# ---------------------------------------------------------------------------

class TestBuildMessage:
    def test_quita_opciones_numeradas_del_texto(self):
        texto = MENSAJES["bienvenida"].format(**NEGOCIO)
        clean, markup = build_message(texto)
        # Los botones quedan; las líneas "N️⃣ ..." salen del texto (no se duplica).
        assert markup is not None
        assert "1️⃣" not in clean and "2️⃣" not in clean
        # El prompt y lo no-numerado se mantiene.
        assert "¿Qué deseas hacer?" in clean
        # Las etiquetas siguen en los botones.
        labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
        assert "Agendar una hora" in labels

    def test_pregunta_si_no_mantiene_texto(self):
        texto = "¿Cancelar la cita del 10/06 a las 11:00? (si/no)"
        clean, markup = build_message(texto)
        assert markup is not None
        assert clean == texto  # la pregunta es parte del mensaje

    def test_texto_plano_sin_markup(self):
        clean, markup = build_message("Listo, te esperamos.")
        assert markup is None
        assert clean == "Listo, te esperamos."


class TestButtonLabel:
    def test_encuentra_etiqueta_por_callback_data(self):
        message = {"reply_markup": {"inline_keyboard": [
            [{"text": "Agendar una hora", "callback_data": "1"}],
            [{"text": "Ver mis citas", "callback_data": "4"}],
        ]}}
        assert button_label(message, "4") == "Ver mis citas"
        assert button_label(message, "9") is None
        assert button_label({}, "1") is None


class TestCallbackRegistraEleccion:
    """Al tocar un botón, el mensaje original se edita para mostrar la elección."""

    def test_edita_mensaje_con_la_opcion(self):
        import lambda_handler

        edits = []

        async def fake_send(chat_id, text, reply_markup=None):
            pass

        async def fake_answer(callback_query_id):
            pass

        async def fake_edit(chat_id, message_id, text):
            edits.append({"message_id": message_id, "text": text})

        update = {"update_id": 5, "callback_query": {
            "id": "cbq-9",
            "from": {"id": 555, "is_bot": False, "first_name": "T"},
            "data": "1",
            "message": {
                "message_id": 42,
                "chat": {"id": 555, "type": "private"},
                "date": int(time.time()),
                "text": "¿Qué deseas hacer?",
                "reply_markup": {"inline_keyboard": [
                    [{"text": "Agendar una hora", "callback_data": "1"}],
                ]},
            },
        }}
        with patch.object(lambda_handler, "_send_telegram", side_effect=fake_send), \
             patch.object(lambda_handler, "_answer_telegram_callback", side_effect=fake_answer), \
             patch.object(lambda_handler, "_edit_telegram_message", side_effect=fake_edit):
            with TestClient(lambda_handler.app, raise_server_exceptions=True) as client:
                resp = client.post("/telegram/webhook", json=update, headers=HEADERS)
        assert resp.status_code == 200
        assert edits and edits[0]["message_id"] == 42
        assert "Agendar una hora" in edits[0]["text"]
