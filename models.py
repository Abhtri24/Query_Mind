"""
models.py
---------
SQLAlchemy models for QueryMind's app metadata.

Changes vs previous version:
- APP_DB_URI from config (Postgres in prod, SQLite in dev)
- DBConnection.uri_encrypted stores Fernet-encrypted URI
- DBConnection.schema_memory_json stores schema memory in DB (survives deploys)
- Timestamps all use timezone-aware UTC
"""

import logging
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from sqlalchemy.sql import func

from config import cfg

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ─── Users ────────────────────────────────────────────────────────────────────

class User(Base, UserMixin):
    __tablename__ = "nl2db_users"

    id              = Column(Integer, primary_key=True)
    username        = Column(String(80),  unique=True, nullable=False, index=True)
    email           = Column(String(120), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    connections = relationship("DBConnection", back_populates="user", cascade="all, delete-orphan")
    sessions    = relationship("ChatSession",  back_populates="user", cascade="all, delete-orphan")


# ─── DB connections ───────────────────────────────────────────────────────────

class DBConnection(Base):
    __tablename__ = "nl2db_connections"

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("nl2db_users.id"), nullable=False)
    alias           = Column(String(120), nullable=False)
    dialect         = Column(String(20),  nullable=False)
    uri_encrypted   = Column(Text, nullable=False)   # Fernet-encrypted URI
    schema_memory_json = Column(Text, nullable=True) # JSON blob, replaces filesystem
    memory_explored_at = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    user     = relationship("User",        back_populates="connections")
    sessions = relationship("ChatSession", back_populates="connection")


# ─── Chat sessions ────────────────────────────────────────────────────────────

class ChatSession(Base):
    __tablename__ = "nl2db_sessions"

    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey("nl2db_users.id"), nullable=False)
    connection_id = Column(Integer, ForeignKey("nl2db_connections.id"), nullable=True)
    started_at    = Column(DateTime(timezone=True), default=_now)
    ended_at      = Column(DateTime(timezone=True), nullable=True)

    user       = relationship("User",         back_populates="sessions")
    connection = relationship("DBConnection", back_populates="sessions")
    messages   = relationship("ChatMessage",  back_populates="session",
                              cascade="all, delete-orphan")


# ─── Chat messages ────────────────────────────────────────────────────────────

class ChatMessage(Base):
    __tablename__ = "nl2db_messages"

    id            = Column(Integer, primary_key=True)
    session_id    = Column(Integer, ForeignKey("nl2db_sessions.id"), nullable=False)
    question      = Column(Text,    nullable=False)
    sql_generated = Column(Text,    nullable=True)
    answer        = Column(Text,    nullable=True)
    error         = Column(Text,    nullable=True)
    retries       = Column(Integer, default=0)
    response_time = Column(Float,   nullable=True)
    schema_source = Column(String(10), nullable=True)  # "memory" | "live"
    created_at    = Column(DateTime(timezone=True), default=_now)

    session = relationship("ChatSession", back_populates="messages")


class SessionHistory(Base):
    __tablename__ = "nl2db_session_history"

    session_id   = Column(String(255), primary_key=True)
    history_json = Column(Text, nullable=False)


# ─── Engine & session helpers ─────────────────────────────────────────────────


def _build_engine():
    from flask import has_app_context, current_app
    uri = cfg.APP_DB_URI
    if has_app_context() and "SQLALCHEMY_DATABASE_URI" in current_app.config:
        uri = current_app.config["SQLALCHEMY_DATABASE_URI"]

    kwargs: dict = {"pool_pre_ping": True}
    if "sqlite" in uri:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Postgres — connection pool tuned for Render/Railway free tier
        kwargs.update({
            "pool_size": 5,
            "max_overflow": 2,
            "pool_recycle": 300,
        })
    return create_engine(uri, **kwargs)


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory():
    return sessionmaker(autocommit=False, autoflush=False, bind=get_engine())


def get_db_session():
    from flask import g, has_app_context
    if has_app_context():
        if 'db_session' not in g:
            g.db_session = get_session_factory()()
        return g.db_session
    else:
        return get_session_factory()()



def create_tables():
    Base.metadata.create_all(bind=get_engine())
    logger.info("App DB tables created / verified")