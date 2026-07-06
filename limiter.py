"""
limiter.py
----------
Flask-Limiter setup with Redis backend (Upstash) for production,
falling back to in-memory for local dev.

Import `limiter` and call limiter.init_app(app) in create_app().
Use @limiter.limit("20 per hour") on routes.
"""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import current_user
from config import cfg
import logging

logger = logging.getLogger(__name__)


def _get_key() -> str:
    """
    Rate limit key: authenticated user ID if logged in, else IP address.
    This means logged-in users share a budget across IPs (good),
    and anonymous users are limited per-IP (good for auth endpoints).
    """
    try:
        if current_user and current_user.is_authenticated:
            return f"user:{current_user.id}"
    except Exception:
        pass
    return get_remote_address()


def _build_storage_uri() -> str | None:
    if cfg.REDIS_URL:
        # Upstash uses rediss:// (TLS) — flask-limiter accepts it directly
        url = cfg.REDIS_URL
        if url.startswith("redis://") or url.startswith("rediss://"):
            return url
        # Some Upstash URLs are plain host:port — normalise
        return f"redis://{url}"
    logger.warning("[Limiter] No REDIS_URL — using in-memory rate limiting (not safe for multi-worker)")
    return None


storage_uri = _build_storage_uri()

limiter = Limiter(
    key_func=_get_key,
    storage_uri=storage_uri,
    default_limits=[],          # no global default — set per-route
    headers_enabled=True,       # X-RateLimit-* headers in every response
    swallow_errors=True,        # don't crash if Redis is unreachable
)