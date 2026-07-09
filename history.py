"""
history.py
----------
Shared conversation history backend.
- Prefers Redis (cached with a 24-hour expiration TTL).
- Falls back to the application database (nl2db_session_history table) if Redis is unavailable.
- Preserves the behavior of keeping the last 5 turns (10 messages).
"""

import json
import logging
import redis
from langchain_core.messages import HumanMessage, AIMessage
from config import cfg

logger = logging.getLogger(__name__)

# Redis client singleton/pool helper
_redis_client = None
_redis_failed = False

def get_redis_client():
    global _redis_client, _redis_failed
    if _redis_failed:
        return None
    if _redis_client is not None:
        return _redis_client
    if not cfg.REDIS_URL:
        return None
    try:
        url = cfg.REDIS_URL
        if not (url.startswith("redis://") or url.startswith("rediss://")):
            url = f"redis://{url}"
        _redis_client = redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
        _redis_client.ping()
        logger.info("[History] Connected to Redis shared history store.")
        return _redis_client
    except Exception as e:
        logger.warning(f"[History] Redis connection check failed: {e}. Falling back to DB.")
        _redis_client = None
        _redis_failed = True
        return None


# Redis and Database persistence configuration
REDIS_KEY_PREFIX = "chat_history:"
TTL = 86400  # 24 hours


def serialize_message(msg):
    # Determine type of message
    if isinstance(msg, HumanMessage):
        m_type = "human"
    elif isinstance(msg, AIMessage):
        m_type = "ai"
    else:
        m_type = getattr(msg, "type", "human")
    return {"type": m_type, "content": msg.content}


def deserialize_message(data):
    if data.get("type") == "human":
        return HumanMessage(content=data.get("content", ""))
    else:
        return AIMessage(content=data.get("content", ""))


def get_redis_history(session_id: str) -> list | None:
    client = get_redis_client()
    if not client:
        return None
    try:
        val = client.get(f"{REDIS_KEY_PREFIX}{session_id}")
        if val:
            return json.loads(val)
        return []
    except Exception as e:
        logger.warning(f"[History] Redis get failed, falling back to DB: {e}")
        return None


def save_redis_history(session_id: str, history_data: list) -> bool:
    client = get_redis_client()
    if not client:
        return False
    try:
        client.set(
            f"{REDIS_KEY_PREFIX}{session_id}",
            json.dumps(history_data),
            ex=TTL
        )
        return True
    except Exception as e:
        logger.warning(f"[History] Redis set failed, falling back to DB: {e}")
        return False


def clear_redis_history(session_id: str) -> bool:
    client = get_redis_client()
    if not client:
        return False
    try:
        client.delete(f"{REDIS_KEY_PREFIX}{session_id}")
        return True
    except Exception as e:
        logger.warning(f"[History] Redis delete failed: {e}")
        return False


def get_db_history(session_id: str) -> list:
    from models import get_db_session, SessionHistory
    db = get_db_session()
    try:
        row = db.get(SessionHistory, session_id)
        if row and row.history_json:
            return json.loads(row.history_json)
    except Exception as e:
        logger.error(f"[History] Failed to read history from DB: {e}")
    return []


def save_db_history(session_id: str, history_data: list):
    from models import get_db_session, SessionHistory
    db = get_db_session()
    try:
        row = db.get(SessionHistory, session_id)
        history_json = json.dumps(history_data)
        if not row:
            row = SessionHistory(session_id=session_id, history_json=history_json)
            db.add(row)
        else:
            row.history_json = history_json
        db.commit()
    except Exception as e:
        logger.error(f"[History] Failed to save history to DB: {e}")
        db.rollback()


def clear_db_history(session_id: str):
    from models import get_db_session, SessionHistory
    db = get_db_session()
    try:
        row = db.get(SessionHistory, session_id)
        if row:
            db.delete(row)
            db.commit()
    except Exception as e:
        logger.error(f"[History] Failed to clear history from DB: {e}")
        db.rollback()


# ─── Public API ───────────────────────────────────────────────────────────────

def _get_history(session_id: str) -> list:
    if not session_id:
        return []

    # 1. Try Redis first
    history_data = get_redis_history(session_id)
    if history_data is not None:
        return [deserialize_message(m) for m in history_data]

    # 2. Fall back to DB
    history_data = get_db_history(session_id)
    return [deserialize_message(m) for m in history_data]


def _append_history(session_id: str, question: str, answer: str):
    if not session_id:
        return

    # Load current history list
    history = _get_history(session_id)
    history.append(HumanMessage(content=question))
    history.append(AIMessage(content=answer or ""))
    # Keep last 10 messages (5 turns)
    history = history[-10:]

    history_data = [serialize_message(m) for m in history]

    # 1. Try Redis first
    saved = save_redis_history(session_id, history_data)
    if not saved:
        # 2. Fallback to DB
        save_db_history(session_id, history_data)


def _clear_history(session_id: str):
    if not session_id:
        return
    # Clear from both stores to ensure consistency
    clear_redis_history(session_id)
    clear_db_history(session_id)
