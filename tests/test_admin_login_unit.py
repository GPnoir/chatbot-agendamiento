"""Tests del login del panel admin (POST /admin/login) y _check_admin_auth.

El login valida usuario+contraseña contra ADMIN_USERNAME/ADMIN_PASSWORD_HASH y
emite un token de sesión firmado. _check_admin_auth acepta ese token o, como
break-glass, la ADMIN_API_KEY cruda.
"""
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import admin_auth

USERNAME = "nelly"
PASSWORD = "clave-super-segura"
SESSION_SECRET = "session-secret-para-tests-1234567890"
API_KEY = "break-glass-key-xyz-0987654321"


@pytest.fixture()
def auth_client():
    """Cliente con auth de panel configurada (hash barato para tests rápidos)."""
    import lambda_handler

    pw_hash = admin_auth.hash_password(PASSWORD, iterations=1000)
    with patch.multiple(
        "lambda_handler",
        ADMIN_USERNAME=USERNAME,
        ADMIN_PASSWORD_HASH=pw_hash,
        SESSION_SECRET=SESSION_SECRET,
        ADMIN_API_KEY=API_KEY,
    ):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            yield c


class TestAdminLogin:
    def test_login_valido_devuelve_token(self, auth_client):
        r = auth_client.post("/admin/login", json={"username": USERNAME, "password": PASSWORD})
        assert r.status_code == 200
        data = r.json()
        assert data["expires_in"] > 0
        payload = admin_auth.verify_session_token(data["token"], SESSION_SECRET)
        assert payload is not None and payload["sub"] == USERNAME

    def test_password_incorrecta_401(self, auth_client):
        r = auth_client.post("/admin/login", json={"username": USERNAME, "password": "mala"})
        assert r.status_code == 401

    def test_usuario_incorrecto_401(self, auth_client):
        r = auth_client.post("/admin/login", json={"username": "intruso", "password": PASSWORD})
        assert r.status_code == 401

    def test_campos_faltantes_400(self, auth_client):
        r = auth_client.post("/admin/login", json={"username": USERNAME})
        assert r.status_code == 400

    def test_rate_limit_tras_muchos_intentos(self, auth_client):
        got_429 = False
        for _ in range(40):
            r = auth_client.post("/admin/login", json={"username": USERNAME, "password": "mala"})
            if r.status_code == 429:
                got_429 = True
                break
        assert got_429


class TestLoginNoConfigurado:
    def test_falla_cerrado_sin_credenciales(self):
        import lambda_handler

        with patch.multiple(
            "lambda_handler", ADMIN_USERNAME="", ADMIN_PASSWORD_HASH="", SESSION_SECRET=""
        ):
            with TestClient(lambda_handler.app) as c:
                r = c.post("/admin/login", json={"username": "x", "password": "y"})
                assert r.status_code == 401


class TestCheckAdminAuthConToken:
    def test_token_de_sesion_da_acceso(self, auth_client):
        token = admin_auth.issue_session_token(USERNAME, SESSION_SECRET)
        r = auth_client.get(
            "/admin/agenda?fecha=2026-06-13", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200

    def test_token_expirado_rechazado(self, auth_client):
        token = admin_auth.issue_session_token(
            USERNAME, SESSION_SECRET, ttl_seconds=1, now=int(time.time()) - 100
        )
        r = auth_client.get("/admin/agenda", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_token_firmado_con_otro_secret_rechazado(self, auth_client):
        token = admin_auth.issue_session_token(USERNAME, "secret-distinto")
        r = auth_client.get("/admin/agenda", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_api_key_sigue_funcionando(self, auth_client):
        r = auth_client.get(
            "/admin/agenda?fecha=2026-06-13", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        assert r.status_code == 200

    def test_sin_auth_rechazado(self, auth_client):
        r = auth_client.get("/admin/agenda")
        assert r.status_code == 401
