"""Tests de los endpoints admin para validar la atención de una cita:
- POST /admin/cita/estado  → marca completada / no_show
- POST /admin/cita/tramo   → asigna el tramo de precio (categoría del paciente)
y de que /admin/agenda incluya las citas ya atendidas (no solo confirmadas).
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import database_dynamo as db

VALID_KEY = "estado-test-key-123456"
AUTH = {"Authorization": f"Bearer {VALID_KEY}"}


@pytest.fixture()
def admin_client():
    import lambda_handler

    with patch("lambda_handler.ADMIN_API_KEY", VALID_KEY):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            yield c


def _crear_cita(uid="estado_ep", fecha="2026-06-20", hora="10:00"):
    cliente = db.get_or_create_cliente("telegram", uid, "Tere")
    return db.crear_cita(cliente["id"], 1, 1, fecha, hora)


class TestEstadoEndpoint:
    def test_sin_auth_rechazado(self, admin_client):
        r = admin_client.post(
            "/admin/cita/estado",
            json={"pk": "APPOINTMENT#x", "sk": "DATE#2026-06-20#10:00", "estado": "completada"},
        )
        assert r.status_code == 401

    def test_pk_no_es_cita_400(self, admin_client):
        r = admin_client.post(
            "/admin/cita/estado",
            json={"pk": "CLIENT", "sk": "algo", "estado": "completada"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_estado_invalido_400(self, admin_client):
        cita = _crear_cita(uid="ep_badestado")
        r = admin_client.post(
            "/admin/cita/estado",
            json={"pk": cita["PK"], "sk": cita["SK"], "estado": "cualquiera"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_cita_inexistente_404(self, admin_client):
        r = admin_client.post(
            "/admin/cita/estado",
            json={"pk": "APPOINTMENT#nope", "sk": "DATE#2026-06-20#10:00", "estado": "completada"},
            headers=AUTH,
        )
        assert r.status_code == 404

    def test_marca_completada(self, admin_client):
        cita = _crear_cita(uid="ep_ok")
        r = admin_client.post(
            "/admin/cita/estado",
            json={"pk": cita["PK"], "sk": cita["SK"], "estado": "completada"},
            headers=AUTH,
        )
        assert r.status_code == 200
        item = db.get_table().get_item(Key={"PK": cita["PK"], "SK": cita["SK"]})["Item"]
        assert item["estado"] == "completada"


class TestTramoEndpoint:
    def test_sin_auth_rechazado(self, admin_client):
        r = admin_client.post(
            "/admin/cita/tramo",
            json={"pk": "APPOINTMENT#x", "sk": "DATE#2026-06-20#10:00", "tramo": "nino"},
        )
        assert r.status_code == 401

    def test_tramo_invalido_400(self, admin_client):
        cita = _crear_cita(uid="ep_badtramo")
        r = admin_client.post(
            "/admin/cita/tramo",
            json={"pk": cita["PK"], "sk": cita["SK"], "tramo": "no_existe"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_asigna_tramo_y_recalcula_precio(self, admin_client):
        cita = _crear_cita(uid="ep_tramo_ok")  # Consulta inicial → adulto 20000
        r = admin_client.post(
            "/admin/cita/tramo",
            json={"pk": cita["PK"], "sk": cita["SK"], "tramo": "convenio_tea"},
            headers=AUTH,
        )
        assert r.status_code == 200
        item = db.get_table().get_item(Key={"PK": cita["PK"], "SK": cita["SK"]})["Item"]
        assert item["tramo"] == "convenio_tea"
        assert int(item["precio"]) == 10000


class TestAgendaIncluyeAtendidas:
    def test_agenda_muestra_completada(self, admin_client):
        cita = _crear_cita(uid="ag_comp", fecha="2026-06-23", hora="09:00")
        db.marcar_estado_cita(cita["PK"], cita["SK"], "completada")
        r = admin_client.get("/admin/agenda?fecha=2026-06-23", headers=AUTH)
        assert r.status_code == 200
        estados = {c["estado"] for c in r.json()["citas"]}
        assert "completada" in estados

    def test_agenda_oculta_cancelada(self, admin_client):
        cita = _crear_cita(uid="ag_canc", fecha="2026-06-24", hora="09:00")
        db.cancelar_cita(cita["PK"], cita["SK"])
        r = admin_client.get("/admin/agenda?fecha=2026-06-24", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["citas"] == []


class TestAgendaSiempreTraePrecio:
    """Regresión: el detalle del panel nunca debe quedar en '-'. El agenda
    resuelve el precio efectivo (snapshot, o derivado del tramo por defecto)."""

    def test_cita_nueva_trae_precio_snapshot(self, admin_client):
        _crear_cita(uid="ag_precio_new", fecha="2026-06-25", hora="09:00")
        r = admin_client.get("/admin/agenda?fecha=2026-06-25", headers=AUTH)
        cita = r.json()["citas"][0]
        assert cita["precio"] == 20000  # Consulta inicial adulto
        assert cita["tramo"] == "adulto"

    def test_cita_vieja_sin_snapshot_trae_precio_derivado(self, admin_client):
        cita = _crear_cita(uid="ag_precio_old", fecha="2026-06-26", hora="09:00")
        # Simula cita vieja: sin snapshot de precio.
        db.get_table().update_item(
            Key={"PK": cita["PK"], "SK": cita["SK"]},
            UpdateExpression="REMOVE precio",
        )
        r = admin_client.get("/admin/agenda?fecha=2026-06-26", headers=AUTH)
        cita_json = r.json()["citas"][0]
        assert cita_json["precio"] == 20000  # derivado del tramo por defecto
        assert cita_json.get("tramo") == "adulto"
