"""Tests del endpoint admin para cancelar citas (POST /admin/cita/cancelar)
y de que /admin/agenda exponga las claves (pk/sk) para referenciarlas.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import database_dynamo as db

VALID_KEY = "cancel-test-key-123456"


@pytest.fixture()
def admin_client():
    import lambda_handler

    with patch("lambda_handler.ADMIN_API_KEY", VALID_KEY):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            yield c


def _crear_cita():
    cliente = db.get_or_create_cliente("telegram", "cancel_user", "Tere")
    return db.crear_cita(cliente["id"], 1, 1, "2026-06-20", "10:00")


AUTH = {"Authorization": f"Bearer {VALID_KEY}"}


class TestCancelarCita:
    def test_sin_auth_rechazado(self, admin_client):
        r = admin_client.post(
            "/admin/cita/cancelar",
            json={"pk": "APPOINTMENT#x", "sk": "DATE#2026-06-20#10:00"},
        )
        assert r.status_code == 401

    def test_pk_no_es_cita_400(self, admin_client):
        # No se puede usar el endpoint para mutar otros tipos de registro.
        r = admin_client.post(
            "/admin/cita/cancelar", json={"pk": "CLIENT", "sk": "algo"}, headers=AUTH
        )
        assert r.status_code == 400

    def test_body_invalido_400(self, admin_client):
        r = admin_client.post("/admin/cita/cancelar", json={"pk": "APPOINTMENT#x"}, headers=AUTH)
        assert r.status_code == 400

    def test_cita_inexistente_404(self, admin_client):
        r = admin_client.post(
            "/admin/cita/cancelar",
            json={"pk": "APPOINTMENT#nope", "sk": "DATE#2026-06-20#10:00"},
            headers=AUTH,
        )
        assert r.status_code == 404

    def test_cancela_cita_real(self, admin_client):
        cita = _crear_cita()
        r = admin_client.post(
            "/admin/cita/cancelar",
            json={"pk": cita["PK"], "sk": cita["SK"]},
            headers=AUTH,
        )
        assert r.status_code == 200
        item = db.get_table().get_item(Key={"PK": cita["PK"], "SK": cita["SK"]}).get("Item")
        assert item["estado"] == "cancelada"


class TestAgendaExponeClaves:
    def test_agenda_incluye_pk_sk(self, admin_client):
        _crear_cita()
        r = admin_client.get("/admin/agenda?fecha=2026-06-20", headers=AUTH)
        assert r.status_code == 200
        citas = r.json()["citas"]
        assert citas, "debería haber al menos una cita"
        assert citas[0]["pk"].startswith("APPOINTMENT#")
        assert citas[0]["sk"].startswith("DATE#")
