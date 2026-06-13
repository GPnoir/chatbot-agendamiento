"""Input validation and security middleware for the chatbot application.

Provides:
- Text sanitization (strip, remove control characters)
- Message text validation (type, length)
- Structural payload validation for Telegram and WhatsApp
- ASGI middleware for request body size limits and CORS
"""
import os
import re
from typing import Optional

from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Maximum allowed message length (characters, measured after sanitization).
# Reads from environment so it can be tuned without code changes.
try:
    from config import MAX_MESSAGE_LENGTH
except ImportError:
    MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "500"))

# Regex for C0 control characters excluding \t (0x09) and \n (0x0a).
# Matches 0x00-0x08, 0x0b-0x1f (and 0x7f DEL for good measure).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_text(text: str) -> str:
    """Return a sanitized copy of *text*.

    Steps applied in order:
    1. Strip leading/trailing whitespace.
    2. Remove null bytes and all C0 control characters except ``\\n`` and ``\\t``.
    """
    text = text.strip()
    text = _CONTROL_CHARS_RE.sub("", text)
    return text


def is_oversized(text: object) -> bool:
    """Return True when *text* is a str whose sanitized form exceeds MAX_MESSAGE_LENGTH.

    Distinguishes genuinely-oversized input from garbage that sanitizes to empty
    (e.g. all C0 control characters), enabling callers to send a rejection reply
    only for the former and silently skip the latter.
    """
    return isinstance(text, str) and len(sanitize_text(text)) > MAX_MESSAGE_LENGTH


def validate_message_text(text: object) -> Optional[str]:
    """Validate and sanitize an incoming message text value.

    Returns the sanitized string when valid, or ``None`` when:
    - *text* is not a ``str``
    - *text* is empty after sanitization
    - *text* exceeds ``MAX_MESSAGE_LENGTH`` characters after sanitization

    Note: oversized messages are *rejected* (return ``None``), never truncated.
    The caller is responsible for sending the user a rejection notice.
    """
    if not isinstance(text, str):
        return None
    clean = sanitize_text(text)
    if not clean:
        return None
    if len(clean) > MAX_MESSAGE_LENGTH:
        return None
    return clean


def validate_telegram_payload(data: object) -> bool:
    """Perform a structural check on a parsed Telegram update dict.

    Returns ``True`` only when the payload has the shape needed to process a
    text message: ``message`` must be a dict with ``from.id`` and ``chat.id``
    present, and if ``text`` is present it must be a ``str``.

    Non-message updates (polls, channel posts, etc.) return ``False`` so the
    caller can skip them gracefully — consistent with current behaviour.
    """
    if not isinstance(data, dict):
        return False
    message = data.get("message")
    if not isinstance(message, dict):
        return False
    from_field = message.get("from")
    if not isinstance(from_field, dict):
        return False
    if not isinstance(from_field.get("id"), (int, str)):
        return False
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return False
    if "id" not in chat:
        return False
    # If text is present, it must be a string
    if "text" in message and not isinstance(message["text"], str):
        return False
    # A message with no text key has nothing for us to process
    if "text" not in message:
        return False
    return True


def validate_telegram_callback(data: object) -> bool:
    """Perform a structural check on a parsed Telegram callback_query update.

    Returns ``True`` only when the update carries a processable callback:
    ``callback_query`` must be a dict with a ``str`` ``id``, ``from.id``
    present, a ``str`` ``data`` payload, and ``message.chat.id`` present
    (needed as the reply destination).

    The ``data`` value is user-controlled input — callers must still run it
    through :func:`validate_message_text` before processing.
    """
    if not isinstance(data, dict):
        return False
    cq = data.get("callback_query")
    if not isinstance(cq, dict):
        return False
    if not isinstance(cq.get("id"), str):
        return False
    from_field = cq.get("from")
    if not isinstance(from_field, dict):
        return False
    if not isinstance(from_field.get("id"), (int, str)):
        return False
    if not isinstance(cq.get("data"), str):
        return False
    message = cq.get("message")
    if not isinstance(message, dict):
        return False
    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return False
    return True


def validate_whatsapp_payload(data: object) -> bool:
    """Perform a structural check on a parsed WhatsApp webhook payload.

    Returns ``True`` only when the payload contains a processable message
    (i.e. ``entry[0].changes[0].value`` is a dict that has a ``messages``
    key).  Status-only payloads return ``False`` so the caller can skip them —
    consistent with current behaviour.
    """
    try:
        if not isinstance(data, dict):
            return False
        entry = data["entry"][0]
        value = entry["changes"][0]["value"]
        if not isinstance(value, dict):
            return False
        return "messages" in value
    except (KeyError, IndexError, TypeError):
        return False


class _BodySizeLimitMiddleware:
    """ASGI middleware that rejects requests whose Content-Length header exceeds a limit.

    Only the Content-Length header is inspected — no body buffering occurs —
    so the check is O(1) and adds negligible latency.  Requests without a
    Content-Length header pass through unchanged (streaming uploads are not
    capped by this middleware).
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = 1_048_576) -> None:
        self._app = app
        self._max = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            content_length_raw = headers.get(b"content-length")
            if content_length_raw is not None:
                try:
                    content_length = int(content_length_raw)
                except ValueError:
                    content_length = 0
                if content_length > self._max:
                    response = Response(
                        content="Request body too large",
                        status_code=413,
                        media_type="text/plain",
                    )
                    await response(scope, receive, send)
                    return
        await self._app(scope, receive, send)


def add_security_middleware(app: ASGIApp, max_body_bytes: int = 1_048_576) -> None:
    """Attach body-size limit and CORS middleware to *app*.

    Body size limit:
        Requests with a ``Content-Length`` header exceeding *max_body_bytes*
        are rejected with HTTP 413 before reaching any route handler.

    CORS:
        Origins are parsed from the ``CORS_ORIGINS`` config value
        (comma-separated).  An empty value disables cross-origin access.

    Args:
        app: The FastAPI/Starlette application instance.
        max_body_bytes: Maximum allowed ``Content-Length`` in bytes.
            Defaults to 1 MiB (1 048 576 bytes).
    """
    try:
        from config import CORS_ORIGINS as _raw_origins
    except ImportError:
        _raw_origins = os.getenv("CORS_ORIGINS", "")

    allow_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

    # Starlette applies middleware in reverse-registration order (last added
    # wraps outermost / runs first).  We add body-size limit LAST so it becomes
    # the outermost layer and runs FIRST — oversized requests are rejected before
    # CORS parsing or any route handler is reached.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(_BodySizeLimitMiddleware, max_body_bytes=max_body_bytes)
