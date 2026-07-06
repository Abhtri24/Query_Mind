"""
db_connector.py
---------------
Multi-database connector for NL2DB.

Supports: MySQL, PostgreSQL, SQLite
Each connection is cached by URI to avoid re-creating engines on every request.

Usage:
    from db_connector import get_db_from_uri, detect_dialect, ConnectionError

    db, engine = get_db_from_uri("sqlite:///mydb.db")
    db, engine = get_db_from_uri("postgresql://user:pass@host/dbname")
    db, engine = get_db_from_uri("mysql+pymysql://user:pass@host/dbname")
"""

import logging
import threading
from typing import Tuple

logger = logging.getLogger(__name__)

# ─── Connection cache ─────────────────────────────────────────────────────────
_connection_cache: dict = {}
_cache_lock = threading.Lock()

# ─── Dialect helpers ──────────────────────────────────────────────────────────

DIALECT_ALIASES = {
    "mysql": "mysql",
    "pymysql": "mysql",
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "sqlite": "sqlite",
}


def detect_dialect(uri: str) -> str:
    """
    Returns the canonical dialect name from a SQLAlchemy URI string.

    Examples:
        "mysql+pymysql://..."   → "mysql"
        "postgresql://..."      → "postgresql"
        "sqlite:///..."         → "sqlite"
    """
    uri_lower = uri.lower()
    for key, dialect in DIALECT_ALIASES.items():
        if uri_lower.startswith(key):
            return dialect
    return "unknown"


def _normalize_uri(uri: str) -> str:
    """
    Ensures the URI has the right driver for each dialect.

    - mysql://  → mysql+pymysql://
    - postgres://  → postgresql://  (common shorthand)
    - postgresql:// → kept as-is (psycopg2 is default)
    - sqlite:/// → kept as-is
    """
    if uri.startswith("mysql://"):
        uri = uri.replace("mysql://", "mysql+pymysql://", 1)
    elif uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return uri


def _install_hint(dialect: str) -> str:
    hints = {
        "mysql": "pip install pymysql",
        "postgresql": "pip install psycopg2-binary",
        "sqlite": "(built-in, no install needed)",
    }
    return hints.get(dialect, "pip install the appropriate driver")


# ─── Main connector ───────────────────────────────────────────────────────────

def get_db_from_uri(uri: str, force_refresh: bool = False):
    """
    Returns (SQLDatabase, Engine) for the given URI.
    Results are cached per URI.

    Args:
        uri:           SQLAlchemy-compatible connection string
        force_refresh: If True, drops the cached connection and reconnects

    Returns:
        Tuple of (langchain SQLDatabase, sqlalchemy Engine)

    Raises:
        ValueError:  Invalid or unsupported URI
        RuntimeError: Connection failed
    """
    from sqlalchemy import create_engine, text
    from langchain_community.utilities.sql_database import SQLDatabase

    if not uri or not isinstance(uri, str):
        raise ValueError("A valid database URI string is required.")

    uri = _normalize_uri(uri.strip())
    dialect = detect_dialect(uri)

    if dialect == "unknown":
        raise ValueError(
            f"Unsupported database URI scheme. Supported: mysql, postgresql, sqlite. "
            f"Got: '{uri[:30]}...'"
        )

    with _cache_lock:
        if not force_refresh and uri in _connection_cache:
            logger.debug(f"[DB] Cache hit for dialect={dialect}")
            return _connection_cache[uri]

    # Build engine
    try:
        connect_args = {}
        if dialect == "sqlite":
            connect_args["check_same_thread"] = False

        engine = create_engine(
            uri,
            connect_args=connect_args,
            pool_pre_ping=True,   # Validates connection before use
            pool_recycle=3600,    # Recycle connections after 1 hour
        )

        # Test the connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # Wrap in LangChain SQLDatabase
        db = SQLDatabase(engine)

        logger.info(f"[DB] Connected to {dialect} database successfully")

    except ImportError as e:
        hint = _install_hint(dialect)
        raise RuntimeError(
            f"Missing database driver for {dialect}. Run: {hint}\n"
            f"Original error: {e}"
        )
    except Exception as e:
        raise RuntimeError(
            f"Could not connect to {dialect} database.\n"
            f"Check your connection string and ensure the database server is running.\n"
            f"Error: {e}"
        )

    with _cache_lock:
        _connection_cache[uri] = (db, engine)

    return db, engine


def get_default_db():
    """
    Returns the default database connection using DB_URI from environment.
    Used for backward compatibility with the original dbconfig.py pattern.
    """
    import os
    uri = os.getenv("DB_URI")
    if not uri:
        raise ValueError(
            "DB_URI environment variable is not set. "
            "Set it in your .env file: DB_URI=mysql+pymysql://user:pass@host/dbname"
        )
    return get_db_from_uri(uri)


def run_sql_with_columns(sql: str, uri: str = None):
    """
    Executes a SQL query and returns (rows, column_names).

    Args:
        sql: SELECT query to execute
        uri: Database URI. If None, uses DB_URI from environment.

    Returns:
        Tuple of (list of Row objects, list of column name strings)
    """
    from sqlalchemy import text

    if uri:
        _, engine = get_db_from_uri(uri)
    else:
        _, engine = get_default_db()

    with engine.connect() as conn:
        result = conn.execute(text(sql))
        rows = result.fetchall()
        col_names = list(result.keys())

    return rows, col_names


def list_cached_connections() -> list:
    """Returns list of currently cached URIs (with passwords masked)."""
    with _cache_lock:
        masked = []
        for uri in _connection_cache:
            # Mask password: mysql+pymysql://user:PASS@host → mysql+pymysql://user:***@host
            import re
            safe = re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", uri)
            masked.append(safe)
        return masked


def clear_connection_cache(uri: str = None):
    """Clears one or all cached connections."""
    with _cache_lock:
        if uri:
            uri = _normalize_uri(uri)
            _connection_cache.pop(uri, None)
        else:
            _connection_cache.clear()
