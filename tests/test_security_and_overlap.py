"""Tests para rate limiter, input sanitization y solapamiento de horarios."""
import hashlib
import hmac
import json
import time
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import chatbot
import database as db_module
import rate_limiter
from config import MENSAJES
from tests.conftest import TEST_USER

# ---------------------------------------------------------------------------
# Admin endpoint auth tests
# ---------------------------------------------------------------------------

VALID_KEY = "test-admin-key-secure-12345"


@pytest.fixture()
def lambda_client():
    """TestClient wired to lambda_handler.app with a known ADMIN_API_KEY."""
    import config
    import lambda_handler

    with patch.object(config, "ADMIN_API_KEY", VALID_KEY):
        with patch("lambda_handler.ADMIN_API_KEY", VALID_KEY):
            with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
                yield c


@pytest.fixture()
def lambda_client_empty_key():
    """TestClient wired to lambda_handler.app with an empty ADMIN_API_KEY."""
    import config
    import lambda_handler

    with patch.object(config, "ADMIN_API_KEY", ""):
        with patch("lambda_handler.ADMIN_API_KEY", ""):
            with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
                yield c


def _mock_db_scan():
    """Return a mock that simulates an empty DynamoDB scan result."""
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": []}
    return mock_table


class TestAdminAgendaAuth:
    """Authentication tests for /admin/agenda."""

    def test_no_auth_header_rejected(self, lambda_client):
        """Request with no Authorization header must be rejected."""
        resp = lambda_client.get("/admin/agenda")
        assert resp.status_code in (401, 403)

    def test_wrong_key_rejected(self, lambda_client):
        """Request with wrong Bearer token must be rejected."""
        resp = lambda_client.get(
            "/admin/agenda", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code in (401, 403)

    def test_correct_key_accepted(self, lambda_client):
        """Request with correct Bearer token and mocked DB must return 200."""
        with patch("lambda_handler.db.get_table", return_value=_mock_db_scan()):
            resp = lambda_client.get(
                "/admin/agenda",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "citas" in body

    def test_empty_api_key_config_rejects_all(self, lambda_client_empty_key):
        """When ADMIN_API_KEY is empty, even a matching empty Bearer must be rejected."""
        resp = lambda_client_empty_key.get(
            "/admin/agenda", headers={"Authorization": "Bearer "}
        )
        assert resp.status_code in (401, 403)

    def test_empty_api_key_config_rejects_any_key(self, lambda_client_empty_key):
        """When ADMIN_API_KEY is empty, any non-empty Bearer must also be rejected."""
        resp = lambda_client_empty_key.get(
            "/admin/agenda", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code in (401, 403)

    def test_query_param_token_no_longer_works(self, lambda_client):
        """Legacy ?token=... query parameter must NOT grant access."""
        resp = lambda_client.get(f"/admin/agenda?token={VALID_KEY}")
        assert resp.status_code in (401, 403)

    def test_malformed_authorization_rejected(self, lambda_client):
        """Authorization header without 'Bearer' scheme must be rejected."""
        resp = lambda_client.get(
            "/admin/agenda", headers={"Authorization": VALID_KEY}
        )
        assert resp.status_code in (401, 403)

    def test_lowercase_bearer_scheme_accepted(self, lambda_client):
        """RFC 9110: bearer scheme is case-insensitive; 'bearer <key>' must be accepted."""
        with patch("lambda_handler.db.get_table", return_value=_mock_db_scan()):
            resp = lambda_client.get(
                "/admin/agenda",
                headers={"Authorization": f"bearer {VALID_KEY}"},
            )
        assert resp.status_code == 200


class TestAdminPanelAuth:
    """Tests for /admin/panel — must serve login shell, never appointment data."""

    def test_panel_returns_200_no_auth_required(self, lambda_client):
        """The panel HTML page itself must be publicly reachable (login shell)."""
        resp = lambda_client.get("/admin/panel")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_panel_contains_no_appointment_data(self, lambda_client):
        """Panel HTML must not contain server-rendered appointment records.

        The panel is a login shell: it shows a login overlay and hides the
        calendar grid until the JS authenticates client-side. No appointment
        records should be pre-rendered by the server.
        """
        resp = lambda_client.get("/admin/panel")
        html = resp.text
        # Login overlay must be present and calendar hidden on load
        assert "login-overlay" in html
        assert "sessionStorage" in html
        # Calendar grid starts hidden — no pre-rendered appointment divs
        assert 'style="--days:7;display:none"' in html

    def test_panel_contains_no_secret_in_html(self, lambda_client):
        """Panel HTML must not embed ADMIN_API_KEY or any secret value."""
        resp = lambda_client.get("/admin/panel")
        html = resp.text
        assert VALID_KEY not in html

    def test_panel_uses_authorization_header_not_query_param(self, lambda_client):
        """Panel JS must use Authorization header, not ?token= query param."""
        resp = lambda_client.get("/admin/panel")
        html = resp.text
        # Old pattern that leaked token into URL must be gone
        assert "location.search" not in html
        assert "?token=" not in html
        # New pattern: Authorization header in fetch
        assert "Authorization" in html


class TestRateLimiter:
    def test_permite_hasta_limite(self):
        """20 mensajes seguidos no son bloqueados."""
        for i in range(20):
            assert not rate_limiter.is_rate_limited(f"rate_user_{i % 5}")

    def test_bloquea_sobre_limite(self):
        """El mensaje 21 del mismo usuario es bloqueado."""
        user = "rate_heavy_user"
        for _ in range(20):
            rate_limiter.is_rate_limited(user)
        assert rate_limiter.is_rate_limited(user)

    def test_usuarios_independientes(self):
        """Rate limit de un usuario no afecta a otro."""
        for _ in range(20):
            rate_limiter.is_rate_limited("user_a_rate")
        assert rate_limiter.is_rate_limited("user_a_rate")
        assert not rate_limiter.is_rate_limited("user_b_rate")

    def test_reset_limpia_estado(self):
        """reset() permite mensajes de nuevo."""
        user = "rate_reset_user"
        for _ in range(20):
            rate_limiter.is_rate_limited(user)
        assert rate_limiter.is_rate_limited(user)
        rate_limiter.reset()
        assert not rate_limiter.is_rate_limited(user)

    def test_chatbot_responde_rate_limit(self, fresh_db):
        """Chatbot retorna mensaje de rate limit al exceder."""
        for _ in range(20):
            chatbot.handle_message("test", TEST_USER, "menu")
        resp = chatbot.handle_message("test", TEST_USER, "menu")
        assert "rápido" in resp or "⚠️" in resp


class TestInputSanitization:
    def test_mensaje_largo_truncado(self, fresh_db):
        """Mensajes de más de 500 chars se truncan sin error."""
        texto_largo = "a" * 1000
        resp = chatbot.handle_message("test", TEST_USER, texto_largo)
        # No debe crashear, debe retornar bienvenida (texto no es comando válido)
        assert resp is not None

    def test_null_bytes_removidos(self, fresh_db):
        """Null bytes en el mensaje no causan problemas."""
        resp = chatbot.handle_message("test", TEST_USER, "menu\x00\x00")
        assert "Hola" in resp or "🌸" in resp

    def test_espacios_se_limpian(self, fresh_db):
        """Espacios al inicio/fin se limpian."""
        resp = chatbot.handle_message("test", TEST_USER, "  menu  ")
        assert "Hola" in resp or "🌸" in resp


class TestSolapamientoHorarios:
    """Verifica que citas de distintas duraciones no se solapan."""

    def test_cita_60min_bloquea_slots_intermedios(self, fresh_db):
        """Una cita de 60min a las 10:00 bloquea 10:00 y 10:30."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        # Buscar fecha con disponibilidad
        fechas = db_module.get_fechas_disponibles(prof_id, 60, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        # Obtener horas antes de agendar
        horas_antes = db_module.get_horas_disponibles(prof_id, fecha, 30)
        if "10:00" not in horas_antes:
            pytest.skip("10:00 no disponible")

        # Crear cita de 60 min a las 10:00
        cliente = db_module.get_or_create_cliente("test", "overlap_user")
        serv_60 = next(s for s in servicios if s["duracion_min"] == 60)
        db_module.crear_cita(cliente["id"], serv_60["id"], prof_id, fecha.isoformat(), "10:00")

        # Verificar: 10:00 y 10:30 no deben estar disponibles para servicio de 30 min
        horas_despues = db_module.get_horas_disponibles(prof_id, fecha, 30)
        assert "10:00" not in horas_despues
        assert "10:30" not in horas_despues

    def test_cita_30min_no_bloquea_siguiente_hora(self, fresh_db):
        """Una cita de 30min a las 10:00 NO bloquea las 10:30."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        fechas = db_module.get_fechas_disponibles(prof_id, 30, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        horas_antes = db_module.get_horas_disponibles(prof_id, fecha, 30)
        if "10:00" not in horas_antes or "10:30" not in horas_antes:
            pytest.skip("10:00/10:30 no disponibles")

        # Crear cita de 30 min a las 10:00
        cliente = db_module.get_or_create_cliente("test", "overlap_user_2")
        serv_30 = next(s for s in servicios if s["duracion_min"] == 30)
        db_module.crear_cita(cliente["id"], serv_30["id"], prof_id, fecha.isoformat(), "10:00")

        # 10:30 debe seguir disponible
        horas_despues = db_module.get_horas_disponibles(prof_id, fecha, 30)
        assert "10:00" not in horas_despues
        assert "10:30" in horas_despues

    def test_servicio_largo_no_cabe_antes_de_cita(self, fresh_db):
        """Un servicio de 60min no cabe a las 10:00 si hay cita a las 10:30."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        fechas = db_module.get_fechas_disponibles(prof_id, 30, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        horas_antes = db_module.get_horas_disponibles(prof_id, fecha, 60)
        if "10:00" not in horas_antes:
            pytest.skip("10:00 no disponible para 60min")

        # Crear cita de 30 min a las 10:30
        cliente = db_module.get_or_create_cliente("test", "overlap_user_3")
        serv_30 = next(s for s in servicios if s["duracion_min"] == 30)
        db_module.crear_cita(cliente["id"], serv_30["id"], prof_id, fecha.isoformat(), "10:30")

        # 10:00 no debe estar disponible para servicio de 60 min (colisiona con 10:30)
        horas_despues = db_module.get_horas_disponibles(prof_id, fecha, 60)
        assert "10:00" not in horas_despues

    def test_multiples_citas_distintas_duraciones(self, fresh_db):
        """Múltiples citas de distintas duraciones se respetan mutuamente."""
        profesionales = db_module.get_profesionales()
        servicios = db_module.get_servicios()
        if not profesionales or not servicios:
            pytest.skip("Sin datos semilla")

        prof_id = profesionales[0]["id"]
        fechas = db_module.get_fechas_disponibles(prof_id, 30, 14)
        if not fechas:
            pytest.skip("Sin fechas disponibles")
        fecha = fechas[0]

        cliente = db_module.get_or_create_cliente("test", "overlap_user_4")
        serv_60 = next(s for s in servicios if s["duracion_min"] == 60)
        serv_30 = next(s for s in servicios if s["duracion_min"] == 30)

        # Cita 60min a las 09:00, cita 30min a las 11:00
        db_module.crear_cita(cliente["id"], serv_60["id"], prof_id, fecha.isoformat(), "09:00")
        db_module.crear_cita(cliente["id"], serv_30["id"], prof_id, fecha.isoformat(), "11:00")

        horas = db_module.get_horas_disponibles(prof_id, fecha, 30)
        # 09:00 y 09:30 bloqueadas por cita de 60min
        assert "09:00" not in horas
        assert "09:30" not in horas
        # 11:00 bloqueada por cita de 30min
        assert "11:00" not in horas
        # 10:00, 10:30, 11:30 deben estar libres
        assert "10:00" in horas
        assert "10:30" in horas
        assert "11:30" in horas


# ---------------------------------------------------------------------------
# WhatsApp signature verification tests (issue #22)
# ---------------------------------------------------------------------------

_WA_SECRET = "test-whatsapp-app-secret"
_WA_BODY = b'{"object":"whatsapp_business_account","entry":[{"changes":[{"value":{"messages":[{"type":"text","from":"5491100000000","text":{"body":"hola"}}]}}]}]}'


def _make_wa_signature(secret: str, body: bytes) -> str:
    """Compute a valid X-Hub-Signature-256 for a given secret and body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture()
def wa_client_configured():
    """TestClient wired to lambda_handler.app with WHATSAPP_APP_SECRET set."""
    import lambda_handler

    with patch("lambda_handler.WHATSAPP_APP_SECRET", _WA_SECRET):
        # Prevent the handler from actually calling chatbot/send logic
        with patch("lambda_handler.chatbot.handle_message", return_value="ok"):
            with patch("lambda_handler._send_whatsapp", return_value=None):
                with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
                    yield c


@pytest.fixture()
def wa_client_empty_secret():
    """TestClient wired to lambda_handler.app with WHATSAPP_APP_SECRET empty."""
    import lambda_handler

    with patch("lambda_handler.WHATSAPP_APP_SECRET", ""):
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            yield c


class TestWhatsAppSignatureVerification:
    """Regression tests for fail-closed WhatsApp HMAC signature validation."""

    def test_empty_app_secret_rejects_webhook(self, wa_client_empty_secret):
        """Regression: empty WHATSAPP_APP_SECRET must return 403, not accept any request.

        This is the exact scenario that was silently bypassed before the fix.
        """
        resp = wa_client_empty_secret.post(
            "/whatsapp/webhook",
            content=_WA_BODY,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
        assert resp.status_code == 403

    def test_empty_app_secret_no_header_rejects_webhook(self, wa_client_empty_secret):
        """Empty secret with no signature header must also return 403."""
        resp = wa_client_empty_secret.post(
            "/whatsapp/webhook",
            content=_WA_BODY,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    def test_valid_signature_accepted(self, wa_client_configured):
        """Valid HMAC-SHA256 signature with configured secret must be accepted."""
        sig = _make_wa_signature(_WA_SECRET, _WA_BODY)
        resp = wa_client_configured.post(
            "/whatsapp/webhook",
            content=_WA_BODY,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
        assert resp.status_code == 200

    def test_invalid_signature_rejected(self, wa_client_configured):
        """Wrong HMAC digest must return 403."""
        resp = wa_client_configured.post(
            "/whatsapp/webhook",
            content=_WA_BODY,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=wrongdigest",
            },
        )
        assert resp.status_code == 403

    def test_missing_signature_header_rejected(self, wa_client_configured):
        """Absent X-Hub-Signature-256 header must return 403, not crash."""
        resp = wa_client_configured.post(
            "/whatsapp/webhook",
            content=_WA_BODY,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    def test_malformed_header_no_sha256_prefix_rejected(self, wa_client_configured):
        """Header without 'sha256=' prefix must return 403, not crash."""
        # Provide the raw hex without the expected prefix
        raw_hex = hmac.new(_WA_SECRET.encode(), _WA_BODY, hashlib.sha256).hexdigest()
        resp = wa_client_configured.post(
            "/whatsapp/webhook",
            content=_WA_BODY,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": raw_hex,  # missing "sha256=" prefix
            },
        )
        assert resp.status_code == 403
