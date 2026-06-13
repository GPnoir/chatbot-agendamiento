#!/usr/bin/env python3
"""Obtiene un refresh token de Google Calendar (issue #14) — uso UNA sola vez.

Flujo OAuth2 "installed app" con loopback local. Sólo usa la stdlib + httpx
(las mismas dependencias del proyecto; nada pesado de Google).

Requisitos previos en Google Cloud Console:
  1. Crear un proyecto y habilitar la "Google Calendar API".
  2. Pantalla de consentimiento OAuth → agregar al profesional como usuario de
     prueba (o publicar la app).
  3. Credenciales → "ID de cliente OAuth" → tipo "App de escritorio".
     Copiar el Client ID y Client Secret.

Uso:
    export GOOGLE_OAUTH_CLIENT_ID=...        # o se piden por consola
    export GOOGLE_OAUTH_CLIENT_SECRET=...
    python scripts/get_google_refresh_token.py

El profesional inicia sesión en el navegador, autoriza, y el script imprime el
refresh token. Guardalo como secreto (GitHub Actions / SAM), NUNCA en el repo.
"""
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
# events: crear/borrar eventos. Alcance mínimo para el sync de citas.
SCOPE = "https://www.googleapis.com/auth/calendar.events"
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"

_auth_code: dict = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib API)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _auth_code["code"] = params.get("code", [None])[0]
        _auth_code["error"] = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Autorización recibida. Ya podés cerrar esta pestaña y volver a la terminal."
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode("utf-8"))

    def log_message(self, *args):  # silencia el log del HTTPServer
        pass


def main() -> int:
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID") or input("Client ID: ").strip()
    client_secret = (
        os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or input("Client Secret: ").strip()
    )
    if not client_id or not client_secret:
        print("❌ Faltan Client ID / Client Secret.", file=sys.stderr)
        return 1

    auth_url = AUTH_URI + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",   # imprescindible para recibir refresh_token
            "prompt": "consent",        # fuerza emitir refresh_token siempre
        }
    )

    print("\n🔗 Abriendo el navegador para autorizar...")
    print(f"   Si no abre solo, pegá esta URL:\n   {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _Handler)
    print(f"⏳ Esperando la autorización en {REDIRECT_URI} ...")
    server.handle_request()  # bloquea hasta el redirect
    server.server_close()

    if _auth_code.get("error"):
        print(f"❌ Autorización rechazada: {_auth_code['error']}", file=sys.stderr)
        return 1
    code = _auth_code.get("code")
    if not code:
        print("❌ No se recibió el código de autorización.", file=sys.stderr)
        return 1

    resp = httpx.post(
        TOKEN_URI,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        print(f"❌ Error al intercambiar el código: {resp.status_code} {resp.text}",
              file=sys.stderr)
        return 1

    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        print(
            "⚠️  La respuesta no incluyó refresh_token. Revocá el acceso en\n"
            "    https://myaccount.google.com/permissions y reintentá (necesita\n"
            "    prompt=consent + access_type=offline).",
            file=sys.stderr,
        )
        return 1

    print("\n✅ Refresh token obtenido. Guardalo como secreto (no lo commitees):\n")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
