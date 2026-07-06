"""
config.py
---------
Single source of truth for all configuration.
Crashes fast on startup if required env vars are missing or unsafe.
"""

import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        print(f"[FATAL] Missing required environment variable: {key}", file=sys.stderr)
        sys.exit(1)
    return val


def _warn_default(key: str, default: str, safe_default: bool = False) -> str:
    val = os.getenv(key, default)
    if val == default and not safe_default:
        print(f"[WARNING] {key} is using its default value — set it explicitly in production", file=sys.stderr)
    return val


class Config:
    # ── Security ──────────────────────────────────────────────────────────
    SECRET_KEY: str = ""
    ENCRYPTION_KEY: str = ""         # Fernet key for encrypting DB URIs

    # ── Database (app metadata) ───────────────────────────────────────────
    APP_DB_URI: str = ""             # Postgres in prod, SQLite in dev

    # ── Redis (rate limiting) ─────────────────────────────────────────────
    REDIS_URL: str = ""              # Upstash Redis URL

    # ── LLM ──────────────────────────────────────────────────────────────
    HOSTED_LLM_PROVIDER: str = "groq"
    HOSTED_LLM_API_KEY: str = ""
    HOSTED_DAILY_TOKEN_BUDGET: int = 50000

    # ── CORS ─────────────────────────────────────────────────────────────
    ALLOWED_ORIGINS: list[str] = []

    # ── Rate limits ───────────────────────────────────────────────────────
    RATE_LIMIT_QUERY: str = "20 per hour"      # per user on /query
    RATE_LIMIT_AUTH: str = "10 per minute"     # on /auth/login + /auth/signup
    RATE_LIMIT_EXPLORE: str = "5 per hour"     # on /connections/<id>/explore

    # ── Server ────────────────────────────────────────────────────────────
    MAX_QUESTION_LENGTH: int = 2000
    MAX_ALIAS_LENGTH: int = 80
    REQUEST_TIMEOUT: int = 120       # seconds before LLM call is abandoned

    # ── Environment ───────────────────────────────────────────────────────
    ENV: str = "development"
    DEBUG: bool = False

    @classmethod
    def load(cls) -> "Config":
        c = cls()
        c.ENV = os.getenv("ENV", "development")
        c.DEBUG = c.ENV == "development"

        # Secret key — crash if default in production
        secret = os.getenv("SECRET_KEY", "")
        if not secret:
            print("[FATAL] SECRET_KEY is not set", file=sys.stderr)
            sys.exit(1)
        if secret in ("change-me-in-production", "your-secret-key-change-this") and c.ENV == "production":
            print("[FATAL] SECRET_KEY is using an insecure default in production", file=sys.stderr)
            sys.exit(1)
        c.SECRET_KEY = secret

        # Encryption key — auto-generate and warn if missing
        enc_key = os.getenv("ENCRYPTION_KEY", "")
        if not enc_key:
            from cryptography.fernet import Fernet
            enc_key = Fernet.generate_key().decode()
            print(
                f"[WARNING] ENCRYPTION_KEY not set. Generated ephemeral key — "
                f"add this to your .env to persist it:\nENCRYPTION_KEY={enc_key}",
                file=sys.stderr
            )
        c.ENCRYPTION_KEY = enc_key

        # App DB
        c.APP_DB_URI = os.getenv(
            "APP_DB_URI",
            "sqlite:///nl2db_app.db"   # safe local default
        )

        # Redis — optional in dev, required in prod
        c.REDIS_URL = os.getenv("REDIS_URL", "")
        if not c.REDIS_URL and c.ENV == "production":
            print("[WARNING] REDIS_URL not set in production — rate limiting will use memory store (not safe for multi-worker)", file=sys.stderr)

        # LLM
        c.HOSTED_LLM_PROVIDER = os.getenv("HOSTED_LLM_PROVIDER", "groq")
        c.HOSTED_LLM_API_KEY  = os.getenv("HOSTED_LLM_API_KEY", "")
        c.HOSTED_DAILY_TOKEN_BUDGET = int(os.getenv("HOSTED_DAILY_TOKEN_BUDGET", "50000"))

        # CORS
        origins_raw = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5500")
        c.ALLOWED_ORIGINS = [o.strip() for o in origins_raw.split(",") if o.strip()]

        # Rate limits (overridable per-deploy)
        c.RATE_LIMIT_QUERY   = os.getenv("RATE_LIMIT_QUERY",   "20 per hour")
        c.RATE_LIMIT_AUTH    = os.getenv("RATE_LIMIT_AUTH",    "10 per minute")
        c.RATE_LIMIT_EXPLORE = os.getenv("RATE_LIMIT_EXPLORE", "5 per hour")

        # Lengths
        c.MAX_QUESTION_LENGTH = int(os.getenv("MAX_QUESTION_LENGTH", "2000"))
        c.MAX_ALIAS_LENGTH    = int(os.getenv("MAX_ALIAS_LENGTH",    "80"))

        return c


# Module-level singleton — import this everywhere
cfg = Config.load()