"""
app.py
------
Flask REST API for NL2DB — talk to any database in plain English.

Endpoint map:
    POST /auth/signup          create account
    POST /auth/login           login
    POST /auth/logout          logout
    GET  /auth/me              whoami

    POST /connections          save a new DB connection
    GET  /connections          list user's saved connections
    DELETE /connections/<id>   remove a connection

    POST /query                run a natural language query
    GET  /sessions             list chat sessions
    GET  /sessions/<id>        get full chat history for a session
    DELETE /sessions/<id>      delete a session

    GET  /health               server status (DB, Redis, LLM)
    GET  /budget               hosted LLM token budget status
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify, request, session
from flask_cors import CORS
from flask_login import LoginManager, current_user, login_required
from langchain_core.messages import AIMessage, HumanMessage

from agent_optimized import run_nl_query
from auth import auth_bp, bcrypt
from config import cfg
from crypto import decrypt, encrypt, is_encrypted
from db_connector import (clear_connection_cache, detect_dialect,
                          get_db_from_uri, list_cached_connections)
from limiter import limiter
from llm_provider import get_budget_status, get_llm
from models import (ChatMessage, ChatSession, DBConnection, User,
                    create_tables, get_session_factory)
from schema_memory import (delete_memory_from_db, explore_and_save,
                           get_schema_context, load_or_explore,
                           memory_summary_for_api)

# ─── Logging setup ────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)
_file_handler    = RotatingFileHandler("logs/nl2db.log", maxBytes=1_000_000, backupCount=5)
_console_handler = logging.StreamHandler()
_file_handler.setLevel(logging.DEBUG)
_console_handler.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[_file_handler, _console_handler],
)
logger = logging.getLogger(__name__)


# ─── App factory ─────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)

    # ── Security config ──────────────────────────────────────────────────
    app.secret_key = cfg.SECRET_KEY
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"]   = cfg.ENV == "production"
    app.config["MAX_CONTENT_LENGTH"]      = 1 * 1024 * 1024  # 1 MB

    # ── CORS ─────────────────────────────────────────────────────────────
    CORS(app, origins=cfg.ALLOWED_ORIGINS, supports_credentials=True)

    # ── Flask-Limiter ────────────────────────────────────────────────────
    limiter.init_app(app)

    # ── Flask-Login ──────────────────────────────────────────────────────
    login_manager = LoginManager()
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        s = get_session_factory()()
        return s.query(User).get(int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify({"error": "Authentication required"}), 401

    # ── Flask-Bcrypt ─────────────────────────────────────────────────────
    bcrypt.init_app(app)

    # ── Blueprints ───────────────────────────────────────────────────────
    app.register_blueprint(auth_bp)

    # ── In-memory conversation history store (per session_id) ─────────────
    # { session_id: [HumanMessage, AIMessage, ...] }
    _history_store: dict = {}
    _history_lock  = threading.Lock()

    def _get_history(session_id: str) -> list:
        with _history_lock:
            return _history_store.setdefault(session_id, [])

    def _append_history(session_id: str, question: str, answer: str):
        with _history_lock:
            h = _history_store.setdefault(session_id, [])
            h.append(HumanMessage(content=question))
            h.append(AIMessage(content=answer or ""))
            # Keep last 10 messages (5 turns)
            _history_store[session_id] = h[-10:]

    def _clear_history(session_id: str):
        with _history_lock:
            _history_store.pop(session_id, None)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _app_db():
        return get_session_factory()()

    def _get_llm_for_request():
        """Picks LLM based on what the user sent in the request."""
        data         = request.get_json(silent=True) or {}
        user_api_key = data.get("api_key") or session.get("api_key")
        provider     = data.get("provider") or session.get("provider", "groq")
        model        = data.get("model") or session.get("model")
        return get_llm(user_api_key=user_api_key, user_provider=provider, user_model=model)

    # ─── Health / budget ──────────────────────────────────────────────────

    @app.route("/health")
    def health():
        """
        Health check — reports status of app DB, Redis, and LLM config.
        """
        status = {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
        checks = {}

        # App database
        try:
            s = _app_db()
            s.execute(__import__("sqlalchemy").text("SELECT 1"))
            checks["app_db"] = "ok"
        except Exception as e:
            checks["app_db"] = f"error: {e}"
            status["status"] = "degraded"

        # Redis
        if cfg.REDIS_URL:
            try:
                import redis
                r = redis.from_url(cfg.REDIS_URL, socket_connect_timeout=2)
                r.ping()
                checks["redis"] = "ok"
            except Exception as e:
                checks["redis"] = f"error: {e}"
                status["status"] = "degraded"
        else:
            checks["redis"] = "not configured (using in-memory rate limiting)"

        # LLM
        checks["llm_provider"] = cfg.HOSTED_LLM_PROVIDER
        checks["llm_key_set"]  = bool(cfg.HOSTED_LLM_API_KEY)

        status["checks"] = checks
        return jsonify(status)

    @app.route("/budget")
    def budget():
        return jsonify(get_budget_status())

    # ─── DB Connections ───────────────────────────────────────────────────

    @app.route("/connections", methods=["POST"])
    @login_required
    def add_connection():
        """
        Save a new DB connection for the current user.

        Body: { "alias": "my-db", "uri": "postgresql://..." }
        Optionally test the connection before saving.
        """
        data  = request.get_json() or {}
        alias = (data.get("alias") or "").strip()
        uri   = (data.get("uri")   or "").strip()

        if not alias or not uri:
            return jsonify({"error": "alias and uri are required"}), 400

        if len(alias) > cfg.MAX_ALIAS_LENGTH:
            return jsonify({"error": f"alias must be at most {cfg.MAX_ALIAS_LENGTH} characters"}), 400

        # Test connection
        try:
            get_db_from_uri(uri)
        except Exception as e:
            return jsonify({"error": f"Could not connect: {e}"}), 400

        dialect = detect_dialect(uri)
        s       = _app_db()
        conn    = DBConnection(
            user_id=current_user.id,
            alias=alias,
            dialect=dialect,
            uri_encrypted=encrypt(uri),
        )
        s.add(conn)
        s.commit()
        logger.info(f"[Connection] Created id={conn.id} alias='{alias}' dialect={dialect} user={current_user.id}")
        return jsonify({
            "id":      conn.id,
            "alias":   conn.alias,
            "dialect": conn.dialect,
        }), 201

    @app.route("/connections", methods=["GET"])
    @login_required
    def list_connections():
        s    = _app_db()
        rows = s.query(DBConnection).filter_by(user_id=current_user.id).all()
        return jsonify([
            {"id": c.id, "alias": c.alias, "dialect": c.dialect,
             "has_memory": c.schema_memory_json is not None,
             "created_at": c.created_at.isoformat() if c.created_at else None}
            for c in rows
        ])

    @app.route("/connections/<int:conn_id>", methods=["DELETE"])
    @login_required
    def delete_connection(conn_id):
        s    = _app_db()
        conn = s.query(DBConnection).filter_by(id=conn_id, user_id=current_user.id).first()
        if not conn:
            return jsonify({"error": "Not found"}), 404
        try:
            plaintext_uri = decrypt(conn.uri_encrypted)
            clear_connection_cache(plaintext_uri)
        except Exception:
            pass  # URI might be corrupt — still allow deletion
        s.delete(conn)
        s.commit()
        logger.info(f"[Connection] Deleted id={conn_id} user={current_user.id}")
        return jsonify({"message": "deleted"})

    # ─── Query endpoint ───────────────────────────────────────────────────

    @app.route("/query", methods=["POST"])
    @login_required
    @limiter.limit(lambda: cfg.RATE_LIMIT_QUERY)
    def query():
        """
        Run a natural language query against a connected database.

        Body:
        {
            "question":      "How many orders were placed last month?",
            "connection_id": 3,          // saved connection (or send uri + alias directly)
            "uri":           "...",      // alternative: one-off URI (not saved)
            "api_key":       "gsk_...", // optional: user's own LLM key
            "provider":      "groq",    // groq | gemini | openai
            "model":         "..."       // optional model override
        }
        """
        data     = request.get_json() or {}
        question = (data.get("question") or "").strip()

        if not question:
            return jsonify({"error": "question is required"}), 400

        if len(question) > cfg.MAX_QUESTION_LENGTH:
            return jsonify({"error": f"question must be at most {cfg.MAX_QUESTION_LENGTH} characters"}), 400

        # ── Resolve DB connection ──────────────────────────────────────
        s    = _app_db()
        uri  = None
        dialect = "mysql"
        conn_id = data.get("connection_id")

        if conn_id:
            saved = s.query(DBConnection).filter_by(id=conn_id, user_id=current_user.id).first()
            if not saved:
                return jsonify({"error": "Connection not found"}), 404
            uri     = decrypt(saved.uri_encrypted)
            dialect = saved.dialect
        elif data.get("uri"):
            uri     = data["uri"].strip()
            dialect = detect_dialect(uri)
        else:
            return jsonify({"error": "Provide connection_id or uri"}), 400

        try:
            db, engine = get_db_from_uri(uri)
        except Exception as e:
            return jsonify({"error": f"DB connection failed: {e}"}), 500

        # ── Resolve LLM ───────────────────────────────────────────────
        try:
            llm = _get_llm_for_request()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 402  # 402 = payment/quota issue

        # ── Session tracking ──────────────────────────────────────────
        if "chat_session_id" not in session:
            session["chat_session_id"] = str(uuid.uuid4())

        flask_session_id = session["chat_session_id"]
        chat_history     = _get_history(flask_session_id)

        # Find or create a ChatSession row
        chat_sess = (
            s.query(ChatSession)
             .filter_by(user_id=current_user.id, ended_at=None)
             .order_by(ChatSession.started_at.desc())
             .first()
        )
        if not chat_sess:
            chat_sess = ChatSession(
                user_id=current_user.id,
                connection_id=conn_id,
            )
            s.add(chat_sess)
            s.commit()

        # ── Run the agent ─────────────────────────────────────────────
        start = time.time()
        try:
            result = run_nl_query(
                question=question,
                llm=llm,
                db=db,
                engine=engine,
                dialect=dialect,
                chat_history=chat_history,
                uri=uri,
                conn_id=conn_id,
                app_db_session=s,
            )
        except Exception as e:
            logger.exception("run_nl_query raised")
            return jsonify({"error": str(e)}), 500

        elapsed = time.time() - start

        # ── Persist message ───────────────────────────────────────────
        msg = ChatMessage(
            session_id=chat_sess.id,
            question=question,
            sql_generated=result.get("sql"),
            answer=result.get("explanation") or result.get("error"),
            error=result.get("error") if not result["success"] else None,
            retries=result.get("retries", 0),
            response_time=elapsed,
            schema_source=result.get("schema_source", "live"),
        )
        s.add(msg)
        s.commit()

        # ── Update in-memory history ──────────────────────────────────
        _append_history(flask_session_id, question, result.get("explanation") or "")

        logger.info(
            f"[Query] user={current_user.id} conn={conn_id} "
            f"success={result['success']} time={elapsed:.2f}s retries={result.get('retries', 0)}"
        )

        # ── Serialise results ─────────────────────────────────────────
        raw_results = result.get("results")
        if isinstance(raw_results, str):
            # db.run() returns a string by default; parse it for the frontend
            serialised = raw_results
        else:
            serialised = raw_results  # already list/dict from custom executor

        return jsonify({
            "success":       result["success"],
            "sql":           result.get("sql"),
            "results":       serialised,
            "explanation":   result.get("explanation"),
            "error":         result.get("error"),
            "retries":       result.get("retries", 0),
            "healing_log":   result.get("healing_log", []),
            "response_time_s": round(elapsed, 3),
            "message_id":    msg.id,
            "schema_source": result.get("schema_source", "live"),
        })

    # ─── Session history ──────────────────────────────────────────────────

    @app.route("/sessions", methods=["GET"])
    @login_required
    def list_sessions():
        s    = _app_db()
        rows = (
            s.query(ChatSession)
             .filter_by(user_id=current_user.id)
             .order_by(ChatSession.started_at.desc())
             .limit(50)
             .all()
        )
        return jsonify([
            {
                "id":           r.id,
                "connection_id": r.connection_id,
                "started_at":   r.started_at.isoformat() if r.started_at else None,
                "ended_at":     r.ended_at.isoformat()   if r.ended_at   else None,
                "message_count": len(r.messages),
            }
            for r in rows
        ])

    @app.route("/sessions/<int:sess_id>", methods=["GET"])
    @login_required
    def get_session(sess_id):
        s        = _app_db()
        chat_sess = s.query(ChatSession).filter_by(id=sess_id, user_id=current_user.id).first()
        if not chat_sess:
            return jsonify({"error": "Not found"}), 404

        messages = [
            {
                "id":            m.id,
                "question":      m.question,
                "sql":           m.sql_generated,
                "answer":        m.answer,
                "error":         m.error,
                "retries":       m.retries,
                "response_time": m.response_time,
                "created_at":    m.created_at.isoformat() if m.created_at else None,
            }
            for m in chat_sess.messages
        ]
        return jsonify({
            "session_id": sess_id,
            "messages":   messages,
        })

    @app.route("/sessions/<int:sess_id>", methods=["DELETE"])
    @login_required
    def delete_session(sess_id):
        s         = _app_db()
        chat_sess = s.query(ChatSession).filter_by(id=sess_id, user_id=current_user.id).first()
        if not chat_sess:
            return jsonify({"error": "Not found"}), 404
        s.delete(chat_sess)
        s.commit()
        _clear_history(session.get("chat_session_id", ""))
        return jsonify({"message": "session deleted"})

    @app.route("/sessions/current/clear", methods=["POST"])
    @login_required
    def clear_current_session():
        """Wipes in-memory chat history for the current browser session."""
        _clear_history(session.get("chat_session_id", ""))
        return jsonify({"message": "conversation history cleared"})

    # ─── Schema Memory endpoints ──────────────────────────────────────────

    @app.route("/connections/<int:conn_id>/explore", methods=["POST"])
    @login_required
    @limiter.limit(lambda: cfg.RATE_LIMIT_EXPLORE)
    def explore_connection(conn_id):
        """
        Trigger (or re-trigger) schema memory exploration for a saved connection.
        Safe to call multiple times — overwrites the cached JSON.

        Body: { "api_key": "...", "provider": "groq" }  (optional)
        """
        s    = _app_db()
        conn = s.query(DBConnection).filter_by(id=conn_id, user_id=current_user.id).first()
        if not conn:
            return jsonify({"error": "Connection not found"}), 404

        try:
            plaintext_uri = decrypt(conn.uri_encrypted)
            db, engine = get_db_from_uri(plaintext_uri)
        except Exception as e:
            return jsonify({"error": f"DB connection failed: {e}"}), 500

        try:
            llm = _get_llm_for_request()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 402

        try:
            memory = explore_and_save(
                uri=plaintext_uri, db=db, engine=engine,
                llm=llm, dialect=conn.dialect,
                conn_id=conn_id, app_db_session=s,
            )
            logger.info(f"[Explore] conn={conn_id} tables={len(memory.tables)}")
            return jsonify({
                "message":     "exploration complete",
                "table_count": len(memory.tables),
                "db_summary":  memory.db_summary,
                "explored_at": memory.explored_at,
            })
        except Exception as e:
            logger.exception("explore_connection failed")
            return jsonify({"error": str(e)}), 500

    @app.route("/connections/<int:conn_id>/memory", methods=["GET"])
    @login_required
    def get_connection_memory(conn_id):
        """
        Returns the cached schema memory for a connection, if it exists.
        """
        s    = _app_db()
        conn = s.query(DBConnection).filter_by(id=conn_id, user_id=current_user.id).first()
        if not conn:
            return jsonify({"error": "Connection not found"}), 404

        if not conn.schema_memory_json:
            return jsonify({
                "exists":  False,
                "message": "No schema memory yet. POST /connections/<id>/explore to generate it.",
            })

        from schema_memory import _from_json
        memory = _from_json(conn.schema_memory_json)
        return jsonify({"exists": True, **memory_summary_for_api(memory)})

    @app.route("/connections/<int:conn_id>/memory", methods=["DELETE"])
    @login_required
    def delete_connection_memory(conn_id):
        """Deletes the cached schema memory so it will be re-explored on next query."""
        s    = _app_db()
        conn = s.query(DBConnection).filter_by(id=conn_id, user_id=current_user.id).first()
        if not conn:
            return jsonify({"error": "Connection not found"}), 404
        delete_memory_from_db(conn_id, s)
        return jsonify({"message": "memory deleted — will re-explore on next query"})

    return app


# ─── Entrypoint ───────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    create_tables()
    logger.info("App DB tables ready")
    logger.info("NL2DB backend running at http://localhost:8000")
    app.run(debug=cfg.DEBUG, host="0.0.0.0", port=8000)


# ─── Serve frontend HTML (Option A — quick wire-up) ──────────────────────────
# Remove this section once the React frontend is ready

@app.route("/app")
@app.route("/app/<path:subpath>")
def serve_frontend(subpath=None):
    """Serves the standalone HTML frontend."""
    from flask import send_from_directory
    return send_from_directory(".", "index.html")