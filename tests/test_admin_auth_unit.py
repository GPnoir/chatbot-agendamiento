"""Tests del módulo de autenticación del panel admin (admin_auth).

Cubre hashing de contraseña (PBKDF2) y tokens de sesión firmados (HMAC),
sin dependencias de FastAPI ni AWS.
"""
import time

import admin_auth


class TestPassword:
    def test_hash_verify_roundtrip(self):
        h = admin_auth.hash_password("s3cret!", iterations=1000)
        assert admin_auth.verify_password("s3cret!", h)

    def test_wrong_password_fails(self):
        h = admin_auth.hash_password("s3cret!", iterations=1000)
        assert not admin_auth.verify_password("otra-clave", h)

    def test_hash_tiene_algo_y_es_salteado(self):
        h1 = admin_auth.hash_password("misma", iterations=1000)
        h2 = admin_auth.hash_password("misma", iterations=1000)
        assert h1.startswith("pbkdf2_sha256$")
        # Distinto salt → distinto hash, aunque la contraseña sea igual.
        assert h1 != h2

    def test_verify_hash_malformado_es_false(self):
        assert not admin_auth.verify_password("x", "no-es-un-hash")
        assert not admin_auth.verify_password("x", "")
        assert not admin_auth.verify_password("x", "pbkdf2_sha256$abc")


class TestSessionToken:
    SECRET = "test-session-secret-1234567890"

    def test_issue_verify_roundtrip(self):
        token = admin_auth.issue_session_token("nelly", self.SECRET, ttl_seconds=3600)
        payload = admin_auth.verify_session_token(token, self.SECRET)
        assert payload is not None
        assert payload["sub"] == "nelly"

    def test_token_expirado_rechazado(self):
        now = int(time.time())
        token = admin_auth.issue_session_token("nelly", self.SECRET, ttl_seconds=10, now=now - 100)
        assert admin_auth.verify_session_token(token, self.SECRET, now=now) is None

    def test_firma_invalida_con_otro_secret(self):
        token = admin_auth.issue_session_token("nelly", self.SECRET)
        assert admin_auth.verify_session_token(token, "otro-secret") is None

    def test_payload_manipulado_rechazado(self):
        # Tomar el payload de un sujeto distinto y pegarle la firma de otro token.
        legit = admin_auth.issue_session_token("nelly", self.SECRET)
        attacker = admin_auth.issue_session_token("intruso", self.SECRET)
        forged_payload = attacker.split(".")[0]
        legit_sig = legit.split(".")[1]
        forged = forged_payload + "." + legit_sig
        assert admin_auth.verify_session_token(forged, self.SECRET) is None

    def test_token_basura_rechazado(self):
        assert admin_auth.verify_session_token("", self.SECRET) is None
        assert admin_auth.verify_session_token("sin-punto", self.SECRET) is None
        assert admin_auth.verify_session_token("a.b.c", self.SECRET) is None

    def test_secret_vacio_no_emite_ni_verifica(self):
        import pytest
        with pytest.raises(ValueError):
            admin_auth.issue_session_token("nelly", "")
        token = admin_auth.issue_session_token("nelly", self.SECRET)
        assert admin_auth.verify_session_token(token, "") is None
