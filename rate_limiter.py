"""Rate limiter with selectable backend: memory (default) or DynamoDB.

Backend selection
-----------------
Reads the ``RATE_LIMITER_BACKEND`` env var at call time via ``config``, so it
can be patched in tests without reloading the module.

- ``memory`` (default): in-process sliding-window counter.  Fast, zero
  dependencies, but resets on every cold start — suitable only for local
  development and unit tests.
- ``dynamo``: DynamoDB fixed-window counter.  Shared across all Lambda
  containers and invocations, providing real persistent rate limiting in
  production.  Uses the same table as the rest of the application.

Failure policy
--------------
If the DynamoDB call raises any exception (network error, throttling,
misconfiguration), ``is_rate_limited`` logs the error and returns ``False``
(fail-open).  This means an infra outage degrades gracefully: users are never
blocked because of a DynamoDB problem.  The tradeoff is that rate limiting is
temporarily ineffective during such an outage.

``reset()``
-----------
Clears the in-memory state only.  It is a test helper and has no effect on
the DynamoDB backend; DynamoDB items expire via TTL.
"""
import os
import time
from collections import defaultdict

from config import RATE_LIMIT_MAX_MESSAGES, RATE_LIMIT_WINDOW_SECONDS
from observability import get_logger, hash_user_id as _hash_user_id

logger = get_logger(__name__)

# Module-level aliases kept for backward compatibility with tests that
# reference ``rate_limiter.MAX_MESSAGES`` or ``rate_limiter.WINDOW_SECONDS``.
MAX_MESSAGES = RATE_LIMIT_MAX_MESSAGES
WINDOW_SECONDS = RATE_LIMIT_WINDOW_SECONDS

# ── Memory backend state ──────────────────────────────────────────────────────
_requests: dict[str, list[float]] = defaultdict(list)


# ── Public API ────────────────────────────────────────────────────────────────

def is_rate_limited(user_id: str) -> bool:
    """Return True if *user_id* has exceeded the rate limit.

    Delegates to the backend chosen by ``RATE_LIMITER_BACKEND``.
    """
    backend = os.getenv("RATE_LIMITER_BACKEND", "memory")
    if backend == "dynamo":
        return _is_rate_limited_dynamo(user_id)
    return _is_rate_limited_memory(user_id)


def reset() -> None:
    """Clear in-memory rate-limit state.

    This is a test helper only.  It has no effect on the DynamoDB backend;
    DynamoDB items expire automatically via their ``ttl`` attribute.
    """
    _requests.clear()


# ── Memory backend ────────────────────────────────────────────────────────────

def _is_rate_limited_memory(user_id: str) -> bool:
    """Sliding-window in-memory counter (original implementation)."""
    now = time.time()
    window_start = now - WINDOW_SECONDS
    _requests[user_id] = [t for t in _requests[user_id] if t > window_start]
    if len(_requests[user_id]) >= MAX_MESSAGES:
        return True
    _requests[user_id].append(now)
    return False


# ── DynamoDB backend ──────────────────────────────────────────────────────────

def _is_rate_limited_dynamo(user_id: str) -> bool:
    """Fixed-window counter backed by DynamoDB.

    Uses an atomic ``ADD`` on a counter attribute so concurrent Lambda
    invocations do not race.  Item schema::

        PK  = "RATELIMIT#<user_id>"
        SK  = "WINDOW#<window_int>"
        count  (Number) — incremented atomically
        ttl    (Number) — Unix epoch; item auto-expires ~2 windows after creation

    Returns True (blocked) if the counter after increment exceeds MAX_MESSAGES.
    Returns False (allow) on any DynamoDB error — see module docstring.
    """
    try:
        import database_dynamo
        from botocore.exceptions import ClientError
    except ImportError:
        logger.error("DynamoDB rate limiter backend unavailable — failing open")
        return False

    try:
        now = time.time()
        window = int(now) // WINDOW_SECONDS
        window_end = (window + 1) * WINDOW_SECONDS
        ttl_value = window_end + 2 * WINDOW_SECONDS

        table = database_dynamo.get_table()
        resp = table.update_item(
            Key={
                "PK": f"RATELIMIT#{user_id}",
                "SK": f"WINDOW#{window}",
            },
            UpdateExpression="ADD #count :one SET #ttl = if_not_exists(#ttl, :ttl)",
            ExpressionAttributeNames={
                "#count": "count",
                "#ttl": "ttl",
            },
            ExpressionAttributeValues={
                ":one": 1,
                ":ttl": ttl_value,
            },
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp["Attributes"]["count"])
        return count > MAX_MESSAGES
    except ClientError:
        logger.error(
            "DynamoDB rate limiter ClientError for user=%s — failing open",
            _hash_user_id(user_id),
        )
        return False
    except Exception:
        logger.error(
            "Unexpected rate limiter error for user=%s — failing open",
            _hash_user_id(user_id),
            exc_info=True,
        )
        return False
