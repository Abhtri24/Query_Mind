"""
models.py
---------
SQLAlchemy models for the NL2DB app's own metadata storage.
Tracks users, DB connections, sessions, and chat history.
"""

import os
from datetime import datetime, timezone

import pytz
from dotenv import load_dotenv
from flask_login import UserMixin
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        Text, create_engine)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.sql import func

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")
Base = declarative_base()


def _now():
    return datetime.now(timezone.utc)


# ─── Users ────────────────────────────────────────────────────────────────────

class User(Base, UserMixin):
    """App login account. Separate from whatever DB the user queries."""
    __tablename__ = "nl2db_users"

    id              = Column(Integer, primary_key=True)
    username        = Column(String(80), unique=True, nullable=False, index=True)
    email           = Column(String(120), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    connections     = relationship("DBConnection", back_populates="user", cascade="all, delete-orphan")
    sessions        = relationship("ChatSession",  back_populates="user", cascade="all, delete-orphan")


# ─── DB connections ───────────────────────────────────────────────────────────

class DBConnection(Base):
    """
    A saved database connection for a user.
    uri is stored so reconnection is fast; alias is a human-readable label.
    """
    __tablename__ = "nl2db_connections"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("nl2db_users.id"), nullable=False)
    alias      = Column(String(120), nullable=False)          # e.g. "prod-postgres"
    dialect    = Column(String(20),  nullable=False)          # mysql | postgresql | sqlite
    uri        = Column(Text, nullable=False)                  # full connection string
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user     = relationship("User",        back_populates="connections")
    sessions = relationship("ChatSession", back_populates="connection")


# ─── Chat sessions ────────────────────────────────────────────────────────────

class ChatSession(Base):
    """One conversation session tied to a user + DB connection."""
    __tablename__ = "nl2db_sessions"

    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey("nl2db_users.id"), nullable=False)
    connection_id = Column(Integer, ForeignKey("nl2db_connections.id"), nullable=True)
    started_at    = Column(DateTime(timezone=True), default=_now)
    ended_at      = Column(DateTime(timezone=True), nullable=True)

    user       = relationship("User",         back_populates="sessions")
    connection = relationship("DBConnection", back_populates="sessions")
    messages   = relationship("ChatMessage",  back_populates="session", cascade="all, delete-orphan")


# ─── Chat messages ────────────────────────────────────────────────────────────

class ChatMessage(Base):
    """
    One question-answer turn inside a session.
    Stores the raw SQL, the result, the explanation, and whether self-healing fired.
    """
    __tablename__ = "nl2db_messages"

    id            = Column(Integer, primary_key=True)
    session_id    = Column(Integer, ForeignKey("nl2db_sessions.id"), nullable=False)
    question      = Column(Text, nullable=False)
    sql_generated = Column(Text, nullable=True)
    answer        = Column(Text, nullable=True)
    error         = Column(Text, nullable=True)
    retries       = Column(Integer, default=0)       # how many self-heal attempts
    response_time = Column(Float,   nullable=True)   # seconds
    created_at    = Column(DateTime(timezone=True), default=_now)

    session = relationship("ChatSession", back_populates="messages")


# ─── DB setup helpers ─────────────────────────────────────────────────────────

def _get_app_engine():
    uri = os.getenv("APP_DB_URI")
    if not uri:
        # Default: SQLite in project root — zero-config for local dev
        uri = "sqlite:///nl2db_app.db"
    return create_engine(uri, connect_args={"check_same_thread": False} if "sqlite" in uri else {})


def get_session_factory():
    engine = _get_app_engine()
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables():
    engine = _get_app_engine()
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    create_tables()
    print("✅ App DB tables created.")
