"""Sincronización de citas con Google Calendar (issue #14).

El profesional autoriza la app una vez (OAuth2 con acceso offline) y se guarda
su *refresh token*. En runtime se intercambia ``refresh_token -> access_token``
contra el endpoint de Google usando ``httpx`` — sin dependencias pesadas de
Google. El access token se cachea entre invocaciones (contenedor Lambda caliente).

Principios de diseño:

- **Best-effort**: cualquier fallo de red o de credenciales se loguea y devuelve
  ``None`` / ``False``. NUNCA propaga una excepción: una caída de Google jamás
  debe romper ni demorar una reserva.
- **Feature flag**: si :data:`config.GOOGLE_CALENDAR_ENABLED` es falso o faltan
  credenciales, todas las operaciones son no-op.
- **Sin secretos en logs** (regla de seguridad del proyecto): se loguea el
  resultado de la operación, nunca tokens ni el cuerpo del evento.

Para obtener el refresh token la primera vez, ver
``scripts/get_google_refresh_token.py``.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

import config
from observability import get_logger

logger = get_logger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars"
_HTTP_TIMEOUT = 10.0

# Cache del access token entre invocaciones (contenedor Lambda caliente).
_token_cache: dict = {"token": None, "expires_at": 0.0}


def is_enabled() -> bool:
    """True sólo si la feature está activada y las credenciales OAuth presentes."""
    return bool(
        config.GOOGLE_CALENDAR_ENABLED
        and config.GOOGLE_OAUTH_CLIENT_ID
        and config.GOOGLE_OAUTH_CLIENT_SECRET
        and config.GOOGLE_OAUTH_REFRESH_TOKEN
    )


def _reset_token_cache() -> None:
    """Limpia el cache del access token. Sólo para tests."""
    _token_cache["token"] = None
    _token_cache["expires_at"] = 0.0


def _get_access_token() -> Optional[str]:
    """Devuelve un access token válido, refrescándolo si hace falta."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    resp = httpx.post(
        _TOKEN_URI,
        data={
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
            "refresh_token": config.GOOGLE_OAUTH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    # Renovamos 60s antes de la expiración real, como margen de seguridad.
    expires_in = int(payload.get("expires_in", 3600))
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(expires_in - 60, 0)
    return token


def _event_window(fecha: str, hora: str, duracion_min: int) -> tuple[str, str]:
    """(inicio, fin) en formato RFC3339 sin offset; el timezone va aparte."""
    inicio = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
    fin = inicio + timedelta(minutes=int(duracion_min or 60))
    fmt = "%Y-%m-%dT%H:%M:%S"
    return inicio.strftime(fmt), fin.strftime(fmt)


def _build_event(cita: dict) -> dict:
    """Construye el payload del evento de Google Calendar a partir de la cita."""
    inicio, fin = _event_window(
        cita["fecha"], cita["hora"], cita.get("servicio_duracion", 60)
    )
    tz = config.GOOGLE_CALENDAR_TIMEZONE
    servicio = cita.get("servicio_nombre", "Cita")
    profesional = cita.get("profesional_nombre", "")
    cliente = cita.get("cliente_nombre") or cita.get("cliente_id", "")
    descripcion = (
        f"Servicio: {servicio}\n"
        f"Profesional: {profesional}\n"
        f"Cliente: {cliente}\n"
        "Agendado vía chatbot."
    )
    summary = f"{servicio} — {cliente}".strip(" —")
    return {
        "summary": summary,
        "description": descripcion,
        "start": {"dateTime": inicio, "timeZone": tz},
        "end": {"dateTime": fin, "timeZone": tz},
    }


def sync_create(cita: dict) -> Optional[str]:
    """Crea el evento en Google Calendar. Devuelve el event id o ``None``.

    Best-effort: nunca propaga errores.
    """
    if not is_enabled():
        return None
    try:
        token = _get_access_token()
        if not token:
            return None
        url = f"{_CALENDAR_API}/{config.GOOGLE_CALENDAR_ID}/events"
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=_build_event(cita),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        event_id = resp.json().get("id")
        logger.info("gcal_event_created")
        return event_id
    except Exception:
        logger.warning("gcal_create_failed")
        return None


def sync_cancel(event_id: str) -> bool:
    """Borra el evento del Google Calendar. Devuelve True si quedó borrado.

    Best-effort: nunca propaga errores. Trata 410 (ya borrado) como éxito.
    """
    if not is_enabled() or not event_id:
        return False
    try:
        token = _get_access_token()
        if not token:
            return False
        url = f"{_CALENDAR_API}/{config.GOOGLE_CALENDAR_ID}/events/{event_id}"
        resp = httpx.delete(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code not in (200, 204, 410):
            resp.raise_for_status()
        logger.info("gcal_event_cancelled")
        return True
    except Exception:
        logger.warning("gcal_cancel_failed")
        return False
