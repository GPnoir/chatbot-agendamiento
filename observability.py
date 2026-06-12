"""Structured logging and observability utilities.

Thin wrapper around aws_lambda_powertools Logger that provides:
- JSON-structured output via get_logger()
- PII-safe user id hashing via hash_user_id()
- Structured message-handled event emission via log_message_handled()
"""
from __future__ import annotations

import hashlib

from aws_lambda_powertools import Logger

_SERVICE_NAME = "chatbot-agendamiento"


def get_logger(name: str | None = None) -> Logger:
    """Return a configured aws_lambda_powertools Logger instance.

    Uses the service name "chatbot-agendamiento". Log level is controlled
    by the LOG_LEVEL environment variable (default: INFO).

    Args:
        name: Optional child logger name. When provided it is appended to the
              service name so log lines can be filtered by component.

    Returns:
        Configured Logger instance with JSON output.
    """
    service = f"{_SERVICE_NAME}/{name}" if name else _SERVICE_NAME
    return Logger(service=service)


def hash_user_id(user_id: str) -> str:
    """Return a short, non-reversible identifier suitable for logs.

    Uses SHA-256 truncated to 8 hex characters. Raw user IDs are PII and
    must never appear in log output.

    Args:
        user_id: The raw user identifier string.

    Returns:
        8-character lowercase hex string derived from SHA-256(user_id).
    """
    return hashlib.sha256(user_id.encode()).hexdigest()[:8]


def log_message_handled(
    logger: Logger,
    *,
    channel: str,
    user_id: str,
    action: str,
    duration_ms: float,
    request_id: str | None = None,
) -> None:
    """Emit a single structured INFO log entry for a handled message.

    Logs the channel, hashed user id, action name, and duration. The raw
    message content and raw user id are NEVER included.

    Args:
        logger: The powertools Logger instance to write to.
        channel: Channel name (e.g. "telegram", "whatsapp").
        user_id: Raw user id — will be hashed before logging.
        action: Short action descriptor (e.g. "message_handled").
        duration_ms: Time taken to handle the message, in milliseconds.
        request_id: Optional AWS request id; included when provided.
    """
    extra: dict = {
        "event": "message_handled",
        "channel": channel,
        "user": hash_user_id(user_id),
        "action": action,
        "duration_ms": duration_ms,
    }
    if request_id is not None:
        extra["request_id"] = request_id

    logger.info("message handled", extra=extra)
