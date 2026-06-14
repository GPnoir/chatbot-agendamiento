"""Tests de Fichas de pacientes: capa de datos (clientes/notas) y endpoints."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import database_dynamo as db

VALID_KEY = "fichas-test-key-123456"
AUTH = {"Authorization": f"Bearer {VALID_KEY}"}


@pytest.fixture()
def admin_client():
    import lambda_handler

    with patch("lambda_handler.ADMIN_API_KEY", VALID_KEY):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            yield c


def _cliente(nombre="Ana Vera"):
    user = "ficha_" + nombre.split()[0].lower()
    return db.get_or_create_cliente("telegram", user, nombre)


# ── Capa de datos ─────────────────────────────────────────────────────
class TestNotasDB:
    def test_agregar_y_listar(self):
        cli = _cliente("Ana Vera")
        db.agregar_nota(cli["id"], "Primera nota")
        db.agregar_nota(cli["id"], "Segunda nota")
        textos = [n["texto"] for n in db.get_notas_cliente(cli["id"])]
        assert "Primera nota" in textos and "Segunda nota" in textos

    def test_notas_vacias(self):
        cli = _cliente("Sin Notas")
        assert db.get_notas_cliente(cli["id"]) == []

    def test_get_clientes_lista(self):
        _cliente("Ana Vera")
        _cliente("Beto Soto")
        assert len(db.get_clientes()) >= 2

    def test_get_cliente_por_id(self):
        cli = _cliente("Ana Vera")
        assert db.get_cliente(cli["id"])["nombre"] == "Ana Vera"
        assert db.get_cliente("CHAN#telegram#noexiste") is None


# ── Endpoints ─────────────────────────────────────────────────────────
class TestFichasEndpoints:
    def test_clientes_sin_auth_401(self, admin_client):
        assert admin_client.get("/admin/clientes").status_code == 401

    def test_clientes_lista(self, admin_client):
        _cliente("Ana Vera")
        r = admin_client.get("/admin/clientes", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["total"] >= 1
        assert "id" in r.json()["clientes"][0]

    def test_cliente_sin_id_400(self, admin_client):
        assert admin_client.get("/admin/cliente", headers=AUTH).status_code == 400

    def test_cliente_inexistente_404(self, admin_client):
        r = admin_client.get("/admin/cliente", params={"id": "CHAN#telegram#nope"}, headers=AUTH)
        assert r.status_code == 404

    def test_cliente_ficha_completa(self, admin_client):
        cli = _cliente("Ana Vera")
        db.crear_cita(cli["id"], 1, 1, "2026-06-25", "10:00")
        db.agregar_nota(cli["id"], "Nota de prueba")
        r = admin_client.get("/admin/cliente", params={"id": cli["id"]}, headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["cliente"]["id"] == cli["id"]
        assert len(data["historial"]) == 1
        assert data["notas"][0]["texto"] == "Nota de prueba"

    def test_agregar_nota_ok(self, admin_client):
        cli = _cliente("Beto Soto")
        r = admin_client.post(
            "/admin/cliente/nota",
            json={"cliente_id": cli["id"], "texto": "Reacciona bien a Mimulus"},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert db.get_notas_cliente(cli["id"])[0]["texto"] == "Reacciona bien a Mimulus"

    def test_agregar_nota_sin_auth_401(self, admin_client):
        r = admin_client.post("/admin/cliente/nota", json={"cliente_id": "x", "texto": "y"})
        assert r.status_code == 401

    def test_agregar_nota_vacia_400(self, admin_client):
        cli = _cliente("Caro Diaz")
        r = admin_client.post(
            "/admin/cliente/nota", json={"cliente_id": cli["id"], "texto": "   "}, headers=AUTH
        )
        assert r.status_code == 400

    def test_agregar_nota_cliente_inexistente_404(self, admin_client):
        r = admin_client.post(
            "/admin/cliente/nota",
            json={"cliente_id": "CHAN#telegram#nope", "texto": "hola"},
            headers=AUTH,
        )
        assert r.status_code == 404
