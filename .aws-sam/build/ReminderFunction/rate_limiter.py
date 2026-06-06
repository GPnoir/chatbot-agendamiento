"""Rate limiter simple en memoria por usuario."""
import time
from collections import defaultdict

# Configuración: máximo N mensajes por ventana de T segundos
MAX_MESSAGES = 20
WINDOW_SECONDS = 60

_requests: dict[str, list[float]] = defaultdict(list)


def is_rate_limited(user_id: str) -> bool:
    """Retorna True si el usuario excedió el límite."""
    now = time.time()
    window_start = now - WINDOW_SECONDS
    # Limpiar entradas viejas
    _requests[user_id] = [t for t in _requests[user_id] if t > window_start]
    if len(_requests[user_id]) >= MAX_MESSAGES:
        return True
    _requests[user_id].append(now)
    return False


def reset():
    """Limpia el estado (útil para tests)."""
    _requests.clear()
