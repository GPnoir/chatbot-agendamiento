"""Tests del auto-prompt post-cita (attendance_prompt) y del callback que lo
cierra en el webhook: la terapeuta marca realizada/no-asistió desde el botón.
"""
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import attendance_prompt as ap
import database_dynamo as db

TELEGRAM_SECRET = "test_telegram_secret"

_CHILE = timedelta(hours=-4)


def _cita_hace(horas: int, uid="att_u", prof=1, serv=1):
    dt = (datetime.utcnow() + _CHILE) - timedelta(hours=horas)
    cli = db.get_or_create_cliente("telegram", uid, "Paco")
    return db.crear_cita(cli["id"], serv, prof, dt.date().isoformat(), dt.strftime("%H:%M"))


class TestAutoPrompt:
    def test_encuentra_confirmada_pasada(self):
        c = _cita_hace(2)
        pend = ap.get_citas_por_consultar(ahora=datetime.utcnow() + _CHILE)
        assert (c["fecha"], c["hora"]) in {(x["fecha"], x["hora"]) for x in pend}

    def test_ignora_cita_futura(self):
        dt = (datetime.utcnow() + _CHILE) + timedelta(hours=3)
        cli = db.get_or_create_cliente("telegram", "att_fut", "Futu")
        db.crear_cita(cli["id"], 1, 1, dt.date().isoformat(), dt.strftime("%H:%M"))
        pend = ap.get_citas_por_consultar(ahora=datetime.utcnow() + _CHILE)
        assert (dt.date().isoformat(), dt.strftime("%H:%M")) not in {(x["fecha"], x["hora"]) for x in pend}

    def test_handler_envia_y_marca(self):
        c = _cita_hace(2, uid="att_h")
        with patch.object(ap, "httpx") as mock_httpx:
            ap.handler({}, None)
        assert mock_httpx.post.called
        item = db.get_table().get_item(Key={"PK": c["PK"], "SK": c["SK"]})["Item"]
        assert item.get("atencion_consultada") is True

    def test_no_reenvia_si_ya_consultada(self):
        c = _cita_hace(2, uid="att_dup")
        with patch.object(ap, "httpx"):
            ap.handler({}, None)
        pend = ap.get_citas_por_consultar(ahora=datetime.utcnow() + _CHILE)
        assert (c["fecha"], c["hora"]) not in {(x["fecha"], x["hora"]) for x in pend}

    def test_callback_data_compacto(self):
        c = _cita_hace(2, uid="att_cb")
        cb = ap._callback("completada", c)
        assert cb.startswith("att|completada|")
        assert len(cb.encode()) <= 64  # límite de Telegram


class TestBuscarPorSlot:
    def test_resuelve_cita_por_slot(self):
        c = _cita_hace(2, uid="att_slot")
        encontrada = db.buscar_cita_por_slot(1, c["fecha"], c["hora"])
        assert encontrada and encontrada["PK"] == c["PK"] and encontrada["SK"] == c["SK"]


class TestAttendanceCallback:
    """El botón del prompt cierra el loop: la terapeuta marca el estado."""

    def _update(self, uid, data, update_id=900001):
        return {"update_id": update_id, "callback_query": {
            "id": "cbq-att", "from": {"id": uid, "is_bot": False, "first_name": "T"},
            "data": data,
            "message": {"message_id": 5, "chat": {"id": uid, "type": "private"}, "date": int(time.time())},
        }}

    def _post(self, uid, cita, estado="completada"):
        import lambda_handler
        data = f"att|{estado}|{cita['fecha']}#{cita['hora']}#1"
        with patch.object(lambda_handler, "ADMIN_USER_ID", "777"), \
             patch.object(lambda_handler, "_send_telegram", new=AsyncMock()), \
             patch.object(lambda_handler, "_answer_telegram_callback", new=AsyncMock()), \
             patch.object(lambda_handler, "_edit_telegram_message", new=AsyncMock()):
            with TestClient(lambda_handler.app) as client:
                return client.post("/telegram/webhook", json=self._update(uid, data),
                                   headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET})

    def test_admin_marca_completada(self):
        c = _cita_hace(2, uid="att_wh")
        r = self._post(777, c, "completada")
        assert r.status_code == 200
        item = db.get_table().get_item(Key={"PK": c["PK"], "SK": c["SK"]})["Item"]
        assert item["estado"] == "completada"

    def test_no_admin_no_marca(self):
        c = _cita_hace(2, uid="att_wh2")
        r = self._post(999, c, "completada")  # 999 no es la terapeuta
        assert r.status_code == 200
        item = db.get_table().get_item(Key={"PK": c["PK"], "SK": c["SK"]})["Item"]
        assert item["estado"] == "confirmada"
