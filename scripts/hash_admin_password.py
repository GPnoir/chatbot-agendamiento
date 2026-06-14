#!/usr/bin/env python3
"""Genera el ADMIN_PASSWORD_HASH (y un SESSION_SECRET) para el panel admin.

Uso:
    python scripts/hash_admin_password.py

Pide la contraseña sin mostrarla, imprime el hash PBKDF2 para cargar como
secret ADMIN_PASSWORD_HASH, y sugiere un SESSION_SECRET aleatorio. La
contraseña en claro nunca se guarda ni se imprime.
"""
import getpass
import os
import secrets
import sys

# Permite ejecutar el script desde la raíz del repo sin instalar el paquete.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin_auth


def main() -> int:
    pw1 = getpass.getpass("Contraseña del panel admin: ")
    if not pw1:
        print("✗ Contraseña vacía.", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("Repetir contraseña: ")
    if pw1 != pw2:
        print("✗ Las contraseñas no coinciden.", file=sys.stderr)
        return 1

    password_hash = admin_auth.hash_password(pw1)
    session_secret = secrets.token_urlsafe(48)

    print("\nCargá estos valores como secrets de GitHub Actions (o en .env):\n")
    print(f"ADMIN_PASSWORD_HASH={password_hash}")
    print(f"SESSION_SECRET={session_secret}")
    print("\nRecordá también setear ADMIN_USERNAME con el usuario elegido.")
    print("Nota: rotar SESSION_SECRET invalida todas las sesiones activas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
