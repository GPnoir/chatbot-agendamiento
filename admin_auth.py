"""Autenticación del panel admin: hash de contraseña + tokens de sesión firmados.

Solo stdlib (hashlib/hmac/secrets/base64/json/time). No depende de FastAPI ni de
AWS, así que se testea aislado. El secret de firma se pasa por parámetro para no
acoplar el módulo a la config.

- Contraseña: PBKDF2-HMAC-SHA256 con salt aleatorio, codificada como
  ``pbkdf2_sha256$<iteraciones>$<salt_b64>$<hash_b64>``.
- Sesión: token sin estado ``<payload_b64>.<hmac_b64>`` donde payload es
  ``{"sub","exp"}``; se valida firma (tiempo constante) y expiración.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

_ALGO = "pbkdf2_sha256"
_DEFAULT_ROUNDS = 200_000
_DEFAULT_TTL = 8 * 3600  # 8 horas


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── Contraseña ────────────────────────────────────────────────────────
def hash_password(password: str, *, salt: Optional[bytes] = None,
                  iterations: int = _DEFAULT_ROUNDS) -> str:
    """Deriva un hash PBKDF2-SHA256 salteado y lo codifica como string portable."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Compara *password* contra un hash almacenado, en tiempo constante.

    Devuelve False ante cualquier hash malformado (nunca lanza).
    """
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iters_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, AttributeError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# ── Token de sesión ───────────────────────────────────────────────────
def issue_session_token(sub: str, secret: str, *, ttl_seconds: int = _DEFAULT_TTL,
                        now: Optional[int] = None) -> str:
    """Emite un token de sesión firmado con HMAC-SHA256.

    Lanza ValueError si *secret* está vacío (falla cerrado: sin secret no hay
    sesiones válidas posibles).
    """
    if not secret:
        raise ValueError("SESSION_SECRET no configurado")
    issued = int(now if now is not None else time.time())
    payload = {"sub": sub, "exp": issued + ttl_seconds}
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64e(sig)}"


def verify_session_token(token: str, secret: str, *, now: Optional[int] = None) -> Optional[dict]:
    """Valida un token de sesión. Devuelve el payload o None.

    None si: token/secret vacío, formato inválido, firma incorrecta o expirado.
    """
    if not token or not secret:
        return None
    try:
        payload_b64, sig_b64 = token.split(".")
    except (ValueError, AttributeError):
        return None
    expected_sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        got_sig = _b64d(sig_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected_sig, got_sig):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, TypeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, int):
        return None
    current = int(now if now is not None else time.time())
    if current >= exp:
        return None
    return payload
