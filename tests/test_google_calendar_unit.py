"""Tests unitarios de google_calendar (issue #14).

Sync best-effort de citas al Google Calendar del profesional vía OAuth2
refresh token. Toda la red está mockeada (httpx.post / httpx.delete).

Cubre:
- feature flag / credenciales ausentes -> no-op (sin llamadas HTTP)
- construcción del evento (start/end con timezone, summary, description)
- cálculo de la ventana según la duración del servicio
- caching del access token entre operaciones
- best-effort: errores de red/credenciales NUNCA propagan
- wiring con database_dynamo (persistir/leer gcal_event_id)
"""
import httpx
import pytest

import config
import google_calendar as gcal


CREDS = {
    "GOOGLE_CALENDAR_ENABLED": True,
    "GOOGLE_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "refresh-token",
    "GOOGLE_CALENDAR_ID": "primary",
    "GOOGLE_CALENDAR_TIMEZONE": "America/Santiago",
}

CITA = {
    "id": "2026-06-22#15:00#1",
    "cliente_id": "telegram:123",
    "cliente_nombre": "María Pérez",
    "servicio_id": 1,
    "servicio_nombre": "Consulta inicial",
    "servicio_duracion": 60,
    "profesional_id": 1,
    "profesional_nombre": "Terapeuta Nelly Pailacura",
    "fecha": "2026-06-22",
    "hora": "15:00",
    "estado": "confirmada",
}


class _FakeResp:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


@pytest.fixture
def enabled(monkeypatch):
    """Habilita la feature con credenciales falsas y limpia el cache de token."""
    for key, value in CREDS.items():
        monkeypatch.setattr(config, key, value, raising=False)
    gcal._reset_token_cache()
    yield
    gcal._reset_token_cache()


def _fake_post_factory(token_resp=None, event_resp=None, counter=None):
    """Devuelve un fake de httpx.post que distingue token vs evento por URL."""
    token_resp = token_resp or _FakeResp({"access_token": "at-123", "expires_in": 3600})
    event_resp = event_resp or _FakeResp({"id": "evt_abc"})

    def fake_post(url, **kwargs):
        if url == gcal._TOKEN_URI:
            if counter is not None:
                counter["token"] = counter.get("token", 0) + 1
            return token_resp
        if counter is not None:
            counter["event"] = counter.get("event", 0) + 1
        return event_resp

    return fake_post


# --- Feature flag / credenciales ---------------------------------------------

def test_disabled_by_default_create_is_noop(monkeypatch):
    """Sin la flag, sync_create no hace red y devuelve None."""
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ENABLED", False, raising=False)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("no debería llamar a la red estando deshabilitado")

    monkeypatch.setattr(httpx, "post", boom)
    assert gcal.sync_create(CITA) is None
    assert called["n"] == 0


def test_enabled_flag_but_missing_creds_is_disabled(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_SECRET", "x", raising=False)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_REFRESH_TOKEN", "x", raising=False)
    assert gcal.is_enabled() is False


def test_is_enabled_true_with_full_creds(enabled):
    assert gcal.is_enabled() is True


# --- Creación de evento -------------------------------------------------------

def test_sync_create_posts_event_and_returns_id(enabled, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        if url == gcal._TOKEN_URI:
            return _FakeResp({"access_token": "at-123", "expires_in": 3600})
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        return _FakeResp({"id": "evt_abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    event_id = gcal.sync_create(CITA)

    assert event_id == "evt_abc"
    assert captured["url"].endswith("/calendars/primary/events")
    assert captured["headers"]["Authorization"] == "Bearer at-123"
    body = captured["json"]
    assert body["start"] == {"dateTime": "2026-06-22T15:00:00", "timeZone": "America/Santiago"}
    assert body["end"] == {"dateTime": "2026-06-22T16:00:00", "timeZone": "America/Santiago"}
    assert "Consulta inicial" in body["summary"]
    assert "María Pérez" in body["description"]


def test_sync_create_end_uses_service_duration(enabled, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        if url == gcal._TOKEN_URI:
            return _FakeResp({"access_token": "at-123", "expires_in": 3600})
        captured["json"] = kwargs.get("json")
        return _FakeResp({"id": "evt_x"})

    monkeypatch.setattr(httpx, "post", fake_post)
    cita = {**CITA, "servicio_duracion": 45, "hora": "09:30"}
    gcal.sync_create(cita)
    assert captured["json"]["start"]["dateTime"] == "2026-06-22T09:30:00"
    assert captured["json"]["end"]["dateTime"] == "2026-06-22T10:15:00"


def test_access_token_cached_between_calls(enabled, monkeypatch):
    counter = {}
    monkeypatch.setattr(httpx, "post", _fake_post_factory(counter=counter))
    gcal.sync_create(CITA)
    gcal.sync_create(CITA)
    assert counter["token"] == 1   # token pedido una sola vez
    assert counter["event"] == 2   # pero dos eventos creados


# --- Best-effort: los errores no propagan ------------------------------------

def test_sync_create_swallows_http_error(enabled, monkeypatch):
    def fake_post(url, **kwargs):
        if url == gcal._TOKEN_URI:
            return _FakeResp({"access_token": "at-123", "expires_in": 3600})
        return _FakeResp(status_code=500)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert gcal.sync_create(CITA) is None  # no levanta excepción


def test_sync_create_swallows_token_failure(enabled, monkeypatch):
    def fake_post(url, **kwargs):
        raise httpx.ConnectError("sin red")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert gcal.sync_create(CITA) is None


# --- Cancelación de evento ----------------------------------------------------

def test_sync_cancel_deletes_event(enabled, monkeypatch):
    captured = {}

    monkeypatch.setattr(httpx, "post", _fake_post_factory())

    def fake_delete(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _FakeResp(status_code=204)

    monkeypatch.setattr(httpx, "delete", fake_delete)
    assert gcal.sync_cancel("evt_abc") is True
    assert captured["url"].endswith("/calendars/primary/events/evt_abc")
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_sync_cancel_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ENABLED", False, raising=False)
    called = {"n": 0}
    monkeypatch.setattr(httpx, "delete", lambda *a, **k: called.__setitem__("n", 1))
    assert gcal.sync_cancel("evt_abc") is False
    assert called["n"] == 0


def test_sync_cancel_treats_410_as_success(enabled, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_post_factory())
    monkeypatch.setattr(httpx, "delete", lambda url, **k: _FakeResp(status_code=410))
    assert gcal.sync_cancel("evt_gone") is True


def test_sync_cancel_swallows_error(enabled, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_post_factory())

    def boom(url, **kwargs):
        raise httpx.ConnectError("sin red")

    monkeypatch.setattr(httpx, "delete", boom)
    assert gcal.sync_cancel("evt_abc") is False


def test_sync_cancel_empty_id_is_noop(enabled, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(httpx, "delete", lambda *a, **k: called.__setitem__("n", 1))
    assert gcal.sync_cancel("") is False
    assert called["n"] == 0


# --- Wiring con database_dynamo ----------------------------------------------

class TestDynamoCalendarWiring:
    """crear_cita persiste gcal_event_id; cancelar_cita lo lee y borra el evento."""

    def test_crear_cita_persists_event_id(self, monkeypatch):
        import database_dynamo as db
        monkeypatch.setattr(db.google_calendar, "sync_create", lambda cita: "evt_999")

        cliente = db.get_or_create_cliente("telegram", "999", "Test User")
        item = db.crear_cita(cliente["id"], 1, 1, "2026-06-22", "15:00")

        assert item["gcal_event_id"] == "evt_999"
        stored = db.get_table().get_item(Key={"PK": item["PK"], "SK": item["SK"]})["Item"]
        assert stored["gcal_event_id"] == "evt_999"

    def test_crear_cita_without_event_id_omits_field(self, monkeypatch):
        import database_dynamo as db
        monkeypatch.setattr(db.google_calendar, "sync_create", lambda cita: None)
        cliente = db.get_or_create_cliente("telegram", "998", "Test User")
        item = db.crear_cita(cliente["id"], 1, 1, "2026-06-22", "16:00")
        assert "gcal_event_id" not in item

    def test_cancelar_cita_deletes_event(self, monkeypatch):
        import database_dynamo as db
        monkeypatch.setattr(db.google_calendar, "sync_create", lambda cita: "evt_777")
        cancelled = {}
        monkeypatch.setattr(
            db.google_calendar, "sync_cancel",
            lambda event_id: cancelled.__setitem__("id", event_id) or True,
        )
        cliente = db.get_or_create_cliente("telegram", "997", "Test User")
        item = db.crear_cita(cliente["id"], 1, 1, "2026-06-22", "17:00")
        db.cancelar_cita(item["PK"], item["SK"])
        assert cancelled["id"] == "evt_777"

    def test_cancelar_cita_without_event_id_skips_sync(self, monkeypatch):
        import database_dynamo as db
        monkeypatch.setattr(db.google_calendar, "sync_create", lambda cita: None)
        called = {"n": 0}
        monkeypatch.setattr(
            db.google_calendar, "sync_cancel",
            lambda event_id: called.__setitem__("n", called["n"] + 1),
        )
        cliente = db.get_or_create_cliente("telegram", "996", "Test User")
        item = db.crear_cita(cliente["id"], 1, 1, "2026-06-22", "18:00")
        db.cancelar_cita(item["PK"], item["SK"])
        assert called["n"] == 0
