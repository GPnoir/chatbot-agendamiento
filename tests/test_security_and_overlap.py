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
# DynamoDB-backed rate limiter tests (issue #23)
# ---------------------------------------------------------------------------

try:
    from moto import mock_aws
    _MOTO_AVAILABLE = True
except ImportError:
    _MOTO_AVAILABLE = False

pytestmark_dynamo = pytest.mark.skipif(
    not _MOTO_AVAILABLE, reason="moto not installed"
)


@pytest.fixture()
def dynamo_rate_limit_table(monkeypatch):
    """Provision a moto-backed DynamoDB table and wire env + module cache.

    Strategy: create the moto table, then inject it directly into
    ``database_dynamo._table`` so that ``get_table()`` returns the moto-backed
    Table without caring about env var ordering or TABLE_NAME being evaluated
    at import time.  The singleton is reset both before (so moto creates it)
    and after (so later tests are not poisoned).
    """
    if not _MOTO_AVAILABLE:
        pytest.skip("moto not installed")

    import boto3
    import database_dynamo

    monkeypatch.setenv("RATE_LIMITER_BACKEND", "dynamo")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")

    with mock_aws():
        # Create the table matching template.yaml schema (PK/SK, TTL on ttl)
        dynamo = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamo.create_table(
            TableName="chatbot-agendamiento-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
        )
        table.wait_until_exists()

        # Inject the moto-backed Table directly into the singleton cache.
        # This bypasses TABLE_NAME (set at import time) and ensures get_table()
        # returns the moto table for the duration of the test.
        original_table = database_dynamo._table
        database_dynamo._table = table
        try:
            yield table
        finally:
            # Restore so subsequent tests start clean
            database_dynamo._table = original_table


@pytestmark_dynamo
class TestDynamoRateLimiter:
    """DynamoDB fixed-window backend tests."""

    def test_under_limit_allowed(self, dynamo_rate_limit_table):
        """Calls below the limit must not be rate-limited."""
        for i in range(19):
            assert not rate_limiter.is_rate_limited("dynamo_user_under"), \
                f"Call {i + 1} should be allowed"

    def test_over_limit_blocked(self, dynamo_rate_limit_table):
        """The 21st call within the same window must be rate-limited."""
        user = "dynamo_user_over"
        # Exhaust the limit (20 calls)
        for _ in range(20):
            rate_limiter.is_rate_limited(user)
        # 21st must be blocked
        assert rate_limiter.is_rate_limited(user)

    def test_independent_users_are_independent(self, dynamo_rate_limit_table):
        """Hitting the limit for user A must not affect user B."""
        for _ in range(20):
            rate_limiter.is_rate_limited("dynamo_user_a_indep")
        assert rate_limiter.is_rate_limited("dynamo_user_a_indep")
        assert not rate_limiter.is_rate_limited("dynamo_user_b_indep")

    def test_window_rollover_unblocks_user(self, dynamo_rate_limit_table):
        """When the fixed window rolls over, a previously blocked user is unblocked."""
        user = "dynamo_user_rollover"
        from config import RATE_LIMIT_WINDOW_SECONDS

        # Move time to start of a fresh window
        base_time = 1_000_000.0
        with patch("time.time", return_value=base_time):
            for _ in range(20):
                rate_limiter.is_rate_limited(user)
            assert rate_limiter.is_rate_limited(user)

        # Advance past the window boundary
        next_window_time = base_time + RATE_LIMIT_WINDOW_SECONDS + 1
        with patch("time.time", return_value=next_window_time):
            assert not rate_limiter.is_rate_limited(user)

    def test_ttl_attribute_present_and_future(self, dynamo_rate_limit_table):
        """Rate-limit items must have a ttl attribute set in the future."""
        import boto3
        user = "dynamo_user_ttl"
        now = time.time()
        rate_limiter.is_rate_limited(user)

        # Scan the table for the rate limit item
        table = dynamo_rate_limit_table
        resp = table.scan(
            FilterExpression="begins_with(PK, :pk)",
            ExpressionAttributeValues={":pk": "RATELIMIT#"},
        )
        items = [i for i in resp["Items"] if i["PK"] == f"RATELIMIT#{user}"]
        assert items, "Rate limit item must exist in DynamoDB"
        assert "ttl" in items[0], "ttl attribute must be present"
        assert int(items[0]["ttl"]) > now, "ttl must be in the future"

    def test_dynamo_exception_fails_open(self, dynamo_rate_limit_table):
        """If DynamoDB raises, is_rate_limited must return False (fail-open)."""
        import database_dynamo
        from botocore.exceptions import ClientError
        mock_table = MagicMock()
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "UpdateItem",
        )
        with patch.object(database_dynamo, "get_table", return_value=mock_table):
            result = rate_limiter.is_rate_limited("dynamo_user_fail_open")
        assert result is False, "Must fail open — infra outage must not block users"

    def test_dynamo_unexpected_exception_fails_open(self, dynamo_rate_limit_table):
        """Non-ClientError bugs must also fail open, never crash message handling."""
        import database_dynamo
        mock_table = MagicMock()
        mock_table.update_item.side_effect = KeyError("Attributes")
        with patch.object(database_dynamo, "get_table", return_value=mock_table):
            result = rate_limiter.is_rate_limited("dynamo_user_unexpected")
        assert result is False, "Must fail open on unexpected errors"

    def test_memory_backend_still_works(self):
        """Memory backend (default) must behave as before, unaffected by this feature."""
        with patch.dict("os.environ", {"RATE_LIMITER_BACKEND": "memory"}, clear=False):
            # Ensure the in-memory state is clean
            rate_limiter.reset()
            user = "mem_backend_user"
            for _ in range(20):
                rate_limiter.is_rate_limited(user)
            assert rate_limiter.is_rate_limited(user)

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
        """Chatbot-internal truncation resilience (defense in depth).

        Passes a long string directly to chatbot.handle_message (bypassing
        handler-level validation) to verify the chatbot layer does not crash.
        Handler-level rejection behavior (sending the Spanish reply and returning
        early) is covered by TestOversizedMessageRejection.
        """
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


# ---------------------------------------------------------------------------
# Input validation unit tests (issue #24)
# ---------------------------------------------------------------------------

class TestSanitizeText:
    """Unit tests for input_validation.sanitize_text."""

    def test_strips_leading_trailing_whitespace(self):
        from input_validation import sanitize_text
        assert sanitize_text("  hello  ") == "hello"

    def test_removes_null_bytes(self):
        from input_validation import sanitize_text
        assert sanitize_text("hola\x00mundo") == "holamundo"

    def test_removes_c0_control_chars_except_newline_tab(self):
        from input_validation import sanitize_text
        # \x01-\x08 and \x0b-\x1f should be stripped; \n and \t preserved
        result = sanitize_text("a\x01b\x07c\x1fd")
        assert result == "abcd"

    def test_preserves_newline(self):
        from input_validation import sanitize_text
        result = sanitize_text("line1\nline2")
        assert result == "line1\nline2"

    def test_preserves_tab(self):
        from input_validation import sanitize_text
        result = sanitize_text("col1\tcol2")
        assert result == "col1\tcol2"

    def test_mixed_control_chars_with_preserved(self):
        from input_validation import sanitize_text
        # \x0b is vertical tab — should be removed; \n stays
        result = sanitize_text("a\x0bb\nc")
        assert result == "ab\nc"

    def test_empty_string_stays_empty(self):
        from input_validation import sanitize_text
        assert sanitize_text("") == ""

    def test_normal_text_unchanged(self):
        from input_validation import sanitize_text
        text = "Hola, quiero agendar una cita para el lunes."
        assert sanitize_text(text) == text


class TestValidateMessageText:
    """Unit tests for input_validation.validate_message_text."""

    def test_non_str_input_returns_none(self):
        from input_validation import validate_message_text
        assert validate_message_text(42) is None
        assert validate_message_text(None) is None
        assert validate_message_text(["text"]) is None

    def test_exactly_500_chars_accepted(self):
        from input_validation import validate_message_text
        text = "a" * 500
        result = validate_message_text(text)
        assert result == text

    def test_501_chars_returns_none(self):
        from input_validation import validate_message_text
        text = "a" * 501
        assert validate_message_text(text) is None

    def test_empty_after_sanitize_returns_none(self):
        from input_validation import validate_message_text
        # Only null bytes — empty after sanitize
        assert validate_message_text("\x00\x01\x02") is None

    def test_only_whitespace_returns_none(self):
        from input_validation import validate_message_text
        assert validate_message_text("   ") is None

    def test_valid_short_text_returned_sanitized(self):
        from input_validation import validate_message_text
        result = validate_message_text("  hola\x00  ")
        assert result == "hola"

    def test_499_chars_accepted(self):
        from input_validation import validate_message_text
        text = "b" * 499
        assert validate_message_text(text) == text

    def test_length_checked_after_sanitize(self):
        """A 501-char string that becomes <=500 after stripping leading spaces is accepted."""
        from input_validation import validate_message_text
        # 1 space + 500 'a' = 501 chars raw; after strip → 500 chars → accepted
        text = " " + "a" * 500
        result = validate_message_text(text)
        assert result == "a" * 500


class TestValidateTelegramPayload:
    """Unit tests for input_validation.validate_telegram_payload."""

    def test_valid_payload_returns_true(self):
        from input_validation import validate_telegram_payload
        payload = {
            "message": {
                "from": {"id": 12345},
                "chat": {"id": 99},
                "text": "hola",
            }
        }
        assert validate_telegram_payload(payload) is True

    def test_missing_message_key_returns_false(self):
        from input_validation import validate_telegram_payload
        assert validate_telegram_payload({"update_id": 1}) is False

    def test_message_not_dict_returns_false(self):
        from input_validation import validate_telegram_payload
        assert validate_telegram_payload({"message": "not a dict"}) is False

    def test_missing_from_id_returns_false(self):
        from input_validation import validate_telegram_payload
        payload = {"message": {"from": {}, "chat": {"id": 1}}}
        assert validate_telegram_payload(payload) is False

    def test_missing_chat_id_returns_false(self):
        from input_validation import validate_telegram_payload
        payload = {"message": {"from": {"id": 1}, "chat": {}}}
        assert validate_telegram_payload(payload) is False

    def test_non_dict_payload_returns_false(self):
        from input_validation import validate_telegram_payload
        assert validate_telegram_payload("not a dict") is False
        assert validate_telegram_payload(None) is False

    def test_text_as_int_returns_false(self):
        from input_validation import validate_telegram_payload
        payload = {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 1},
                "text": 12345,  # must be str
            }
        }
        assert validate_telegram_payload(payload) is False

    def test_no_text_key_is_valid(self):
        """Non-text updates (stickers, etc.) have no 'text'; handler skips them — return False."""
        from input_validation import validate_telegram_payload
        payload = {
            "message": {
                "from": {"id": 1},
                "chat": {"id": 1},
                # no 'text' key — e.g. photo or sticker
            }
        }
        # No text → handler has nothing to process; False is correct (skip gracefully)
        assert validate_telegram_payload(payload) is False


class TestValidateWhatsAppPayload:
    """Unit tests for input_validation.validate_whatsapp_payload."""

    def _make_wa_payload(self, include_messages=True, text_body="hola"):
        value = {}
        if include_messages:
            value["messages"] = [{"type": "text", "from": "123", "text": {"body": text_body}}]
        else:
            value["statuses"] = [{"id": "s1"}]
        return {"entry": [{"changes": [{"value": value}]}]}

    def test_valid_payload_with_messages_returns_true(self):
        from input_validation import validate_whatsapp_payload
        assert validate_whatsapp_payload(self._make_wa_payload()) is True

    def test_status_only_payload_returns_false(self):
        """Status-only payloads have no messages; handler skips → return False."""
        from input_validation import validate_whatsapp_payload
        assert validate_whatsapp_payload(self._make_wa_payload(include_messages=False)) is False

    def test_non_dict_returns_false(self):
        from input_validation import validate_whatsapp_payload
        assert validate_whatsapp_payload("bad") is False
        assert validate_whatsapp_payload(None) is False

    def test_missing_entry_returns_false(self):
        from input_validation import validate_whatsapp_payload
        assert validate_whatsapp_payload({}) is False

    def test_empty_entry_list_returns_false(self):
        from input_validation import validate_whatsapp_payload
        assert validate_whatsapp_payload({"entry": []}) is False


# ---------------------------------------------------------------------------
# Body size limit tests (issue #24)
# ---------------------------------------------------------------------------

def _make_oversized_body(size_bytes: int = 1_048_577) -> bytes:
    """Return a raw bytes body that exceeds the default 1 MiB limit.

    We use plain bytes (not JSON) so the size is exactly *size_bytes* and the
    Content-Length header that httpx derives matches our expectation.
    """
    return b"x" * size_bytes


def _make_size_limited_app():
    """Return a minimal FastAPI app with only the body-size-limit middleware.

    Using a dedicated app per test avoids the Starlette 'middleware_stack
    already cached' problem that occurs when the real app is reused across
    fixtures.  The middleware under test is the same class used in production.
    """
    from fastapi import FastAPI
    from input_validation import _BodySizeLimitMiddleware

    app = FastAPI()
    app.add_middleware(_BodySizeLimitMiddleware, max_body_bytes=1_048_576)

    @app.post("/probe")
    async def _probe():
        return {"ok": True}

    return app


class TestBodySizeLimit:
    """Content-Length > 1 MiB must be rejected with 413 by the middleware."""

    def test_middleware_rejects_oversized_body(self):
        """_BodySizeLimitMiddleware must return 413 when Content-Length > 1 MiB."""
        app = _make_size_limited_app()
        oversized = _make_oversized_body()
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post("/probe", content=oversized)
        assert resp.status_code == 413

    def test_middleware_accepts_body_at_limit(self):
        """_BodySizeLimitMiddleware must pass through a body just under 1 MiB."""
        app = _make_size_limited_app()
        body = b"x" * (1_048_576 - 1)
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post("/probe", content=body)
        assert resp.status_code == 200

    def test_middleware_accepts_body_exactly_at_limit(self):
        """Body exactly at 1 MiB (not exceeding) must pass through."""
        app = _make_size_limited_app()
        body = b"x" * 1_048_576
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.post("/probe", content=body)
        assert resp.status_code == 200

    def test_add_security_middleware_installs_body_limit(self):
        """add_security_middleware must register _BodySizeLimitMiddleware on the app."""
        from fastapi import FastAPI
        from input_validation import add_security_middleware, _BodySizeLimitMiddleware
        fresh = FastAPI()
        add_security_middleware(fresh)
        mw_classes = [m.cls for m in fresh.user_middleware]
        assert _BodySizeLimitMiddleware in mw_classes


# ---------------------------------------------------------------------------
# CORS middleware tests (issue #24)
# ---------------------------------------------------------------------------

class TestCORSMiddleware:
    """CORS middleware configuration tests."""

    def test_add_security_middleware_registers_cors(self):
        """add_security_middleware must register CORSMiddleware on a fresh app."""
        from fastapi import FastAPI
        from starlette.middleware.cors import CORSMiddleware
        from input_validation import add_security_middleware
        import importlib, config as cfg_mod, input_validation as iv_mod

        fresh_app = FastAPI()
        with patch.dict("os.environ", {"CORS_ORIGINS": "https://example.com"}):
            # Reload config so CORS_ORIGINS is picked up
            importlib.reload(cfg_mod)
            importlib.reload(iv_mod)
            try:
                iv_mod.add_security_middleware(fresh_app)
            finally:
                # Restore both modules to pristine state so subsequent tests
                # see the original CORS_ORIGINS value.
                importlib.reload(cfg_mod)
                importlib.reload(iv_mod)

        middleware_classes = [m.cls for m in fresh_app.user_middleware]
        assert CORSMiddleware in middleware_classes

    def test_cors_allow_origin_header_present_for_configured_origin(self):
        """Preflight request with a matching origin must get allow-origin in response."""
        from fastapi import FastAPI
        from starlette.middleware.cors import CORSMiddleware
        from input_validation import _BodySizeLimitMiddleware

        fresh_app = FastAPI()
        # Manually wire known-good allow_origins so this test doesn't depend on env reload order
        fresh_app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://example.com"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        fresh_app.add_middleware(_BodySizeLimitMiddleware, max_body_bytes=1_048_576)

        @fresh_app.get("/health")
        async def _health():
            return {"status": "ok"}

        with TestClient(fresh_app, raise_server_exceptions=True) as c:
            resp = c.options(
                "/health",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
        assert "access-control-allow-origin" in resp.headers

    def test_cors_no_allow_origin_when_empty_origins(self):
        """With empty allow_origins, cross-origin requests must not get allow-origin header."""
        from fastapi import FastAPI
        from starlette.middleware.cors import CORSMiddleware
        from input_validation import _BodySizeLimitMiddleware

        fresh_app = FastAPI()
        # Empty origins — default config
        fresh_app.add_middleware(
            CORSMiddleware,
            allow_origins=[],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        fresh_app.add_middleware(_BodySizeLimitMiddleware, max_body_bytes=1_048_576)

        @fresh_app.get("/health")
        async def _health():
            return {"status": "ok"}

        with TestClient(fresh_app, raise_server_exceptions=True) as c:
            resp = c.get("/health", headers={"Origin": "https://evil.com"})
        assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------------------
# Webhook payload validation + oversized message (issue #24)
# ---------------------------------------------------------------------------

_TG_SECRET_HEADER = "test-tg-webhook-secret"


@pytest.fixture()
def lambda_client_for_validation():
    """TestClient for lambda_handler.app with signature patched for WhatsApp tests."""
    import lambda_handler
    with patch("lambda_handler.WHATSAPP_APP_SECRET", _WA_SECRET):
        with patch("lambda_handler.TELEGRAM_WEBHOOK_SECRET", _TG_SECRET_HEADER):
            with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
                yield c


@pytest.fixture()
def server_client_for_validation():
    """TestClient for server.app (Telegram webhook only)."""
    import server as server_module
    server_module.app.router.on_startup.clear()
    server_module.app.router.on_shutdown.clear()
    with patch("server.TELEGRAM_WEBHOOK_SECRET", _TG_SECRET_HEADER):
        with TestClient(server_module.app, raise_server_exceptions=True) as c:
            yield c


def _make_tg_payload(text: str, user_id: int = 11111, chat_id: int = 22222) -> dict:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id, "first_name": "Test"},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def _make_wa_msg_payload(text: str, from_number: str = "5491100000000") -> bytes:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "type": "text",
                        "from": from_number,
                        "text": {"body": text},
                    }]
                }
            }]
        }]
    }
    return json.dumps(payload).encode()


class TestMalformedPayloadSkipped:
    """Malformed Telegram/WhatsApp payloads must return 200 without processing."""

    def test_malformed_telegram_lambda_returns_200(self, lambda_client_for_validation):
        """Telegram payload missing 'message.from.id' must return 200, no processing."""
        with patch("lambda_handler.chatbot.handle_message") as mock_handle:
            resp = lambda_client_for_validation.post(
                "/telegram/webhook",
                json={"update_id": 1, "message": {"text": "hi", "chat": {"id": 1}}},
                headers={"X-Telegram-Bot-Api-Secret-Token": _TG_SECRET_HEADER},
            )
        assert resp.status_code == 200
        mock_handle.assert_not_called()

    def test_empty_telegram_payload_lambda_returns_200(self, lambda_client_for_validation):
        """Empty dict as Telegram payload must return 200, no processing."""
        with patch("lambda_handler.chatbot.handle_message") as mock_handle:
            resp = lambda_client_for_validation.post(
                "/telegram/webhook",
                json={},
                headers={"X-Telegram-Bot-Api-Secret-Token": _TG_SECRET_HEADER},
            )
        assert resp.status_code == 200
        mock_handle.assert_not_called()

    def test_malformed_whatsapp_lambda_returns_200(self, lambda_client_for_validation):
        """WhatsApp payload missing 'entry' must return 200, no processing."""
        body = b"{}"
        sig = _make_wa_signature(_WA_SECRET, body)
        with patch("lambda_handler.chatbot.handle_message") as mock_handle:
            with patch("lambda_handler._send_whatsapp", return_value=None):
                resp = lambda_client_for_validation.post(
                    "/whatsapp/webhook",
                    content=body,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
                )
        assert resp.status_code == 200
        mock_handle.assert_not_called()


class TestOversizedMessageRejection:
    """Message text over 500 chars must not reach chatbot; user gets a rejection reply."""

    def test_oversized_tg_message_lambda_not_processed(self, lambda_client_for_validation):
        """Telegram message >500 chars must NOT call chatbot.handle_message."""
        long_text = "a" * 501
        with patch("lambda_handler.chatbot.handle_message") as mock_handle:
            with patch("lambda_handler._send_telegram") as mock_send:
                resp = lambda_client_for_validation.post(
                    "/telegram/webhook",
                    json=_make_tg_payload(long_text),
                    headers={"X-Telegram-Bot-Api-Secret-Token": _TG_SECRET_HEADER},
                )
        assert resp.status_code == 200
        mock_handle.assert_not_called()

    def test_oversized_tg_message_lambda_sends_rejection(self, lambda_client_for_validation):
        """Telegram message >500 chars must send the Spanish rejection reply."""
        long_text = "a" * 501
        with patch("lambda_handler.chatbot.handle_message", return_value="should not be called"):
            with patch("lambda_handler._send_telegram") as mock_send:
                lambda_client_for_validation.post(
                    "/telegram/webhook",
                    json=_make_tg_payload(long_text),
                    headers={"X-Telegram-Bot-Api-Secret-Token": _TG_SECRET_HEADER},
                )
        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args if mock_send.call_args.kwargs else (mock_send.call_args[0], {})
        args = mock_send.call_args[0]
        sent_text = args[1] if len(args) >= 2 else mock_send.call_args.kwargs.get("text", "")
        assert "largo" in sent_text or "500" in sent_text

    def test_oversized_wa_message_lambda_not_processed(self, lambda_client_for_validation):
        """WhatsApp message >500 chars must NOT call chatbot.handle_message."""
        long_text = "b" * 501
        body = _make_wa_msg_payload(long_text)
        sig = _make_wa_signature(_WA_SECRET, body)
        with patch("lambda_handler.chatbot.handle_message") as mock_handle:
            with patch("lambda_handler._send_whatsapp", return_value=None):
                resp = lambda_client_for_validation.post(
                    "/whatsapp/webhook",
                    content=body,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
                )
        assert resp.status_code == 200
        mock_handle.assert_not_called()

    def test_oversized_wa_message_lambda_sends_rejection(self, lambda_client_for_validation):
        """WhatsApp message >500 chars must send the Spanish rejection reply."""
        long_text = "b" * 501
        body = _make_wa_msg_payload(long_text)
        sig = _make_wa_signature(_WA_SECRET, body)
        with patch("lambda_handler.chatbot.handle_message", return_value="nope"):
            with patch("lambda_handler._send_whatsapp") as mock_send:
                lambda_client_for_validation.post(
                    "/whatsapp/webhook",
                    content=body,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
                )
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        sent_text = args[1] if len(args) >= 2 else mock_send.call_args.kwargs.get("text", "")
        assert "largo" in sent_text or "500" in sent_text


# ---------------------------------------------------------------------------
# Production-app body size limit and CORS preflight tests (F2)
# ---------------------------------------------------------------------------

_OVERSIZED_BODY = b"x" * 1_048_577


class TestProductionAppBodyLimit:
    """Body > 1 MiB must be rejected with 413 by the real production app stacks."""

    def test_lambda_handler_app_rejects_oversized_body(self):
        """lambda_handler.app must return 413 for a 1_048_577-byte POST body."""
        import lambda_handler
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            resp = c.post(
                "/whatsapp/webhook",
                content=_OVERSIZED_BODY,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 413

    def test_server_app_rejects_oversized_body(self):
        """server.app must return 413 for a 1_048_577-byte POST body."""
        import server as server_module
        server_module.app.router.on_startup.clear()
        server_module.app.router.on_shutdown.clear()
        with TestClient(server_module.app, raise_server_exceptions=True) as c:
            resp = c.post(
                "/whatsapp/webhook",
                content=_OVERSIZED_BODY,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 413


class TestProductionAppCORSPreflight:
    """OPTIONS preflight from an unlisted origin must not echo allow-origin header."""

    def test_lambda_handler_app_no_cors_for_evil_origin(self):
        """lambda_handler.app must not return access-control-allow-origin for evil origin."""
        import lambda_handler
        with TestClient(lambda_handler.app, raise_server_exceptions=True) as c:
            resp = c.options(
                "/whatsapp/webhook",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
        assert "access-control-allow-origin" not in resp.headers

    def test_server_app_no_cors_for_evil_origin(self):
        """server.app must not return access-control-allow-origin for evil origin."""
        import server as server_module
        server_module.app.router.on_startup.clear()
        server_module.app.router.on_shutdown.clear()
        with TestClient(server_module.app, raise_server_exceptions=True) as c:
            resp = c.options(
                "/whatsapp/webhook",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
        assert "access-control-allow-origin" not in resp.headers
