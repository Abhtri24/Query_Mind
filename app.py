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
    POST /query/stream         SSE streaming version of /query
    GET  /sessions             list chat sessions
    GET  /sessions/<id>        get full chat history for a session
    DELETE /sessions/<id>      delete a session

    GET  /health               server status (DB, Redis, LLM)
    GET  /budget               hosted LLM token budget status

    POST /connections/<id>/explore     trigger schema memory exploration
    GET  /connections/<id>/memory      get cached schema memory
    DELETE /connections/<id>/memory    delete cached schema memory
    GET  /connections/<id>/sample      sample rows from a table (agentic tool)
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from flask import Flask, g, jsonify, request, session
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
                    create_tables, get_db_session)
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
        return _app_db().get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify({"error": "Authentication required"}), 401

    # ── DB session lifecycle ─────────────────────────────────────────────
    # _app_db() caches one session per request in flask.g.
    # teardown_appcontext closes it at the end of every request — no leaks.

    @app.teardown_appcontext
    def teardown_db_session(exception=None):
        db_session = g.pop("db_session", None)
        if db_session is not None:
            try:
                db_session.close()
            except Exception:
                pass

    def _app_db():
        if "db_session" not in g:
            g["db_session"] = get_db_session()
        return g["db_session"]

    # ── Flask-Bcrypt ─────────────────────────────────────────────────────
    bcrypt.init_app(app)

    # ── Blueprints ───────────────────────────────────────────────────────
    app.register_blueprint(auth_bp)

    # ── Shared conversation history (Redis + DB fallback) ─────────────────
    from history import _get_history, _append_history, _clear_history

    # ── Helpers ──────────────────────────────────────────────────────────

    def _get_llm_for_request():
        """Picks LLM based on what the user sent in the request."""
        data         = request.get_json(silent=True) or {}
        user_api_key = data.get("api_key") or session.get("api_key")
        provider     = data.get("provider") or session.get("provider", "groq")
        model        = data.get("model") or session.get("model")
        return get_llm(user_api_key=user_api_key, user_provider=provider, user_model=model)

    def _optional_text(data, field, max_len=4000):
        if field not in data or data.get(field) is None:
            return None, None
        value = data.get(field)
        if not isinstance(value, str):
            return None, f"{field} must be a string"
        value = value.strip()
        if not value:
            return None, None
        if len(value) > max_len:
            return None, f"{field} must be at most {max_len} characters"
        return value, None

    def _optional_glossary(data):
        if "glossary" not in data or data.get("glossary") is None:
            return None, None
        glossary = data.get("glossary")
        if not isinstance(glossary, dict):
            return None, "glossary must be an object"
        cleaned = {}
        for key, value in glossary.items():
            if not isinstance(key, str) or not isinstance(value, str):
                return None, "glossary keys and values must be strings"
            key = key.strip()
            value = value.strip()
            if key and value:
                cleaned[key] = value
        return cleaned or None, None

    def _optional_table_list(data, field):
        if field not in data or data.get(field) is None:
            return None, None
        values = data.get(field)
        if not isinstance(values, list):
            return None, f"{field} must be an array of strings"
        cleaned = []
        for value in values:
            if not isinstance(value, str):
                return None, f"{field} must contain only strings"
            value = value.strip()
            if value:
                cleaned.append(value)
        return cleaned or None, None

    def _query_cache_get(conn_id, question):
        """Return (cached_result, cache_key). cached_result is None on miss or no Redis."""
        if not cfg.REDIS_URL:
            return None, None
        try:
            import redis
            import hashlib
            import json
            r   = redis.from_url(cfg.REDIS_URL, socket_connect_timeout=1)
            key = "qc:" + hashlib.sha256(
                f"{conn_id}:{question.lower().strip()}".encode()
            ).hexdigest()[:16]
            val = r.get(key)
            return (json.loads(val), key) if val else (None, key)
        except Exception:
            return None, None

    def _query_cache_set(key, result):
        """Store a successful query result for 5 minutes. Silent on failure."""
        if not key or not cfg.REDIS_URL:
            return
        try:
            import redis
            import json
            r = redis.from_url(cfg.REDIS_URL, socket_connect_timeout=1)
            r.setex(key, 300, json.dumps(result, default=str))
        except Exception:
            pass

    def _connection_profile_response(conn):
        return {
            "description":      conn.description,
            "business_context": conn.business_context,
            "glossary":         conn.glossary_json,
            "important_tables": conn.important_tables_json,
            "ignored_tables":   conn.ignored_tables_json,
        }

    def _resolve_connection(data, s):
        """
        Shared logic: resolve URI + dialect + profile from request data.
        Returns (uri, dialect, profile_connection, conn_id, error_response).
        error_response is non-None when resolution failed.
        """
        conn_id = data.get("connection_id")
        if conn_id:
            saved = s.query(DBConnection).filter_by(
                id=conn_id, user_id=current_user.id
            ).first()
            if not saved:
                return None, None, None, None, (jsonify({"error": "Connection not found"}), 404)
            return decrypt(saved.uri_encrypted), saved.dialect, saved, conn_id, None
        elif data.get("uri"):
            uri = data["uri"].strip()
            return uri, detect_dialect(uri), None, None, None
        return None, None, None, None, (jsonify({"error": "Provide connection_id or uri"}), 400)

    # ─── Health / budget ──────────────────────────────────────────────────

    @app.route("/health")
    def health():
        """Health check — reports status of app DB, Redis, and LLM config."""
        status = {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
        checks = {}

        try:
            import sqlalchemy
            _app_db().execute(sqlalchemy.text("SELECT 1"))
            checks["app_db"] = "ok"
        except Exception as e:
            checks["app_db"] = f"error: {e}"
            status["status"] = "degraded"

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
        Body: { "alias": "my-db", "uri": "postgresql://...", ...optional profile fields }
        """
        data  = request.get_json() or {}
        alias = (data.get("alias") or "").strip()
        uri   = (data.get("uri")   or "").strip()

        if not alias or not uri:
            return jsonify({"error": "alias and uri are required"}), 400
        if len(alias) > cfg.MAX_ALIAS_LENGTH:
            return jsonify({"error": f"alias must be at most {cfg.MAX_ALIAS_LENGTH} characters"}), 400

        description,      err = _optional_text(data, "description")
        if err: return jsonify({"error": err}), 400
        business_context, err = _optional_text(data, "business_context")
        if err: return jsonify({"error": err}), 400
        glossary,         err = _optional_glossary(data)
        if err: return jsonify({"error": err}), 400
        important_tables, err = _optional_table_list(data, "important_tables")
        if err: return jsonify({"error": err}), 400
        ignored_tables,   err = _optional_table_list(data, "ignored_tables")
        if err: return jsonify({"error": err}), 400

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
            description=description,
            business_context=business_context,
            glossary_json=glossary,
            important_tables_json=important_tables,
            ignored_tables_json=ignored_tables,
        )
        s.add(conn)
        s.commit()
        logger.info(f"[Connection] Created id={conn.id} alias='{alias}' dialect={dialect} user={current_user.id}")
        return jsonify({
            "id":      conn.id,
            "alias":   conn.alias,
            "dialect": conn.dialect,
            **_connection_profile_response(conn),
        }), 201

    @app.route("/connections", methods=["GET"])
    @login_required
    def list_connections():
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(100, max(1, int(request.args.get("limit", 20))))
        except ValueError:
            limit = 20

        s    = _app_db()
        rows = (
            s.query(DBConnection)
             .filter_by(user_id=current_user.id)
             .order_by(DBConnection.id.desc())
             .offset((page - 1) * limit)
             .limit(limit)
             .all()
        )
        return jsonify([
            {
                "id":         c.id,
                "alias":      c.alias,
                "dialect":    c.dialect,
                "has_memory": c.schema_memory_json is not None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                **_connection_profile_response(c),
            }
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
            clear_connection_cache(decrypt(conn.uri_encrypted))
        except Exception:
            pass
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
            "connection_id": 3,
            "uri":           "...",        // alternative: one-off URI (not saved)
            "api_key":       "gsk_...",
            "provider":      "groq",
            "model":         "..."
        }
        """
        data     = request.get_json() or {}
        question = (data.get("question") or "").strip()

        if not question:
            return jsonify({"error": "question is required"}), 400
        if len(question) > cfg.MAX_QUESTION_LENGTH:
            return jsonify({"error": f"question must be at most {cfg.MAX_QUESTION_LENGTH} characters"}), 400

        # ── Resolve DB connection ──────────────────────────────────────
        s = _app_db()
        uri, dialect, profile_connection, conn_id, err = _resolve_connection(data, s)
        if err:
            return err

        try:
            db, engine = get_db_from_uri(uri)
        except Exception as e:
            return jsonify({"error": f"DB connection failed: {e}"}), 500

        # ── Resolve LLM ───────────────────────────────────────────────
        try:
            llm = _get_llm_for_request()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 402

        # ── Cache check ───────────────────────────────────────────────
        cached_result, cache_key = _query_cache_get(conn_id, question)
        if cached_result:
            logger.info(f"[Cache] Hit for conn={conn_id}")
            return jsonify({**cached_result, "cached": True})

        # ── Session tracking ──────────────────────────────────────────
        if "chat_session_id" not in session:
            session["chat_session_id"] = str(uuid.uuid4())

        flask_session_id = session["chat_session_id"]
        chat_history     = _get_history(flask_session_id)

        chat_sess = (
            s.query(ChatSession)
             .filter_by(user_id=current_user.id, ended_at=None)
             .order_by(ChatSession.started_at.desc())
             .first()
        )
        if not chat_sess:
            chat_sess = ChatSession(user_id=current_user.id, connection_id=conn_id)
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
                profile=profile_connection,
            )
        except Exception as e:
            logger.exception("run_nl_query raised")
            return jsonify({"error": str(e)}), 500

        elapsed    = time.time() - start
        serialised = result.get("results")  # already list/dict; str for db.run() fallback

        # ── Cache successful result ────────────────────────────────────
        if result["success"]:
            _query_cache_set(cache_key, {
                "success":           result["success"],
                "sql":               result.get("sql"),
                "results":           serialised,
                "explanation":       result.get("explanation"),
                "error":             None,
                "retries":           result.get("retries", 0),
                "healing_log":       result.get("healing_log", []),
                "schema_source":     result.get("schema_source", "live"),
                "results_truncated": result.get("results_truncated", False),
                "plan":              result.get("plan", []),
            })

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

        _append_history(flask_session_id, question, result.get("explanation") or "")

        logger.info(
            f"[Query] user={current_user.id} conn={conn_id} "
            f"success={result['success']} time={elapsed:.2f}s retries={result.get('retries', 0)}"
        )

        return jsonify({
            "success":             result["success"],
            "sql":                 result.get("sql"),
            "results":             serialised,
            "explanation":         result.get("explanation"),
            "error":               result.get("error"),
            "retries":             result.get("retries", 0),
            "healing_log":         result.get("healing_log", []),
            "response_time_s":     round(elapsed, 3),
            "message_id":          msg.id,
            "schema_source":       result.get("schema_source", "live"),
            "results_truncated":   result.get("results_truncated", False),
            "clarification_needed": result.get("clarification_needed"),
            "plan":                result.get("plan", []),
            "cached":              False,
        })

    # ─── Streaming query endpoint (SSE) ───────────────────────────────────

    @app.route("/query/stream", methods=["POST"])
    @login_required
    @limiter.limit(lambda: cfg.RATE_LIMIT_QUERY)
    def query_stream():
        """
        Server-Sent Events version of /query.
        Emits progress events while the agent runs so the client never
        stares at a blank screen for 20+ seconds.

        Event shapes (all JSON on the `data:` line):
            {"status": "checking",      "message": "..."}
            {"status": "clarification", "question": "..."}
            {"status": "planning",      "message": "..."}
            {"status": "planned",       "steps": [...]}
            {"status": "executing",     "step": 1, "total": 2, "message": "..."}
            {"status": "interpreting",  "message": "..."}
            {"status": "done",          ...all /query fields...}
            {"status": "error",         "error": "..."}
        """
        import json
        from flask import Response, stream_with_context
        from agent_optimized import (check_clarification_needed, plan_query,
                                     execute_with_healing, interpret_results)

        data     = request.get_json() or {}
        question = (data.get("question") or "").strip()

        if not question:
            return jsonify({"error": "question is required"}), 400
        if len(question) > cfg.MAX_QUESTION_LENGTH:
            return jsonify({"error": f"question must be at most {cfg.MAX_QUESTION_LENGTH} characters"}), 400

        # ── Resolve connection & LLM (same as /query) ─────────────────
        s = _app_db()
        uri, dialect, profile_connection, conn_id, err = _resolve_connection(data, s)
        if err:
            return err

        try:
            db, engine = get_db_from_uri(uri)
        except Exception as e:
            return jsonify({"error": f"DB connection failed: {e}"}), 500

        try:
            llm = _get_llm_for_request()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 402

        # ── Cache check before opening stream ─────────────────────────
        cached_result, cache_key = _query_cache_get(conn_id, question)
        if cached_result:
            logger.info(f"[Cache] Stream hit for conn={conn_id}")
            # Return a single-event SSE stream for consistency
            payload = json.dumps({"status": "done", "cached": True, **cached_result})

            def _cached_gen():
                yield f"data: {payload}\n\n"

            return Response(
                stream_with_context(_cached_gen()),
                mimetype="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )

        # ── Resolve schema ─────────────────────────────────────────────
        schema_source = "live"
        try:
            memory = load_or_explore(
                uri=uri, db=db, engine=engine, llm=llm,
                dialect=dialect, conn_id=conn_id, app_db_session=s,
            )
            schema        = get_schema_context(memory, question)
            schema_source = "memory"
        except Exception:
            from agent_optimized import get_all_table_names, pick_relevant_tables, fetch_schema
            schema = fetch_schema(db, engine, pick_relevant_tables(
                question, get_all_table_names(db, engine)
            ))

        def generate():
            import json as _json

            def emit(obj: dict) -> str:
                return f"data: {_json.dumps(obj)}\n\n"

            try:
                # ── Step 1: clarification ──────────────────────────────
                yield emit({"status": "checking", "message": "Analysing question..."})
                clarification = check_clarification_needed(llm, question, schema)
                if clarification:
                    yield emit({"status": "clarification", "question": clarification})
                    return

                # ── Step 2: planning ───────────────────────────────────
                yield emit({"status": "planning", "message": "Planning query..."})
                plan = plan_query(llm, question, schema)
                yield emit({"status": "planned", "steps": plan.steps})

                # ── Step 3: execute each sub-query ─────────────────────
                step_results  = []
                total_retries = 0
                all_logs      = []
                last_error    = None

                for i, sub_q in enumerate(plan.steps):
                    yield emit({
                        "status":  "executing",
                        "step":    i + 1,
                        "total":   len(plan.steps),
                        "message": sub_q,
                    })
                    qr = execute_with_healing(
                        question=sub_q, llm=llm, db=db, engine=engine,
                        schema=schema, dialect=dialect,
                        chat_history=None,  # history only for the non-streaming endpoint
                    )
                    all_logs.extend(qr.healing_log)
                    total_retries += qr.retries

                    if not qr.success:
                        last_error = qr.error
                        break

                    step_results.append({
                        "sql":       qr.sql,
                        "rows":      qr.rows,
                        "truncated": qr.results_truncated,
                    })

                if not step_results:
                    yield emit({"status": "error", "error": last_error or "Query failed"})
                    return

                # ── Step 4: interpret ──────────────────────────────────
                yield emit({"status": "interpreting", "message": "Summarising results..."})
                explanation = interpret_results(
                    llm, question,
                    plan.steps[:len(step_results)],
                    step_results,
                )

                last       = step_results[-1]
                done_event = {
                    "status":              "done",
                    "success":             True,
                    "sql":                 last["sql"],
                    "results":             last["rows"],
                    "explanation":         explanation,
                    "error":               None,
                    "retries":             total_retries,
                    "healing_log":         all_logs,
                    "schema_source":       schema_source,
                    "results_truncated":   last["truncated"],
                    "clarification_needed": None,
                    "plan":                plan.steps,
                    "cached":              False,
                }
                yield emit(done_event)

                # ── Cache the successful result ─────────────────────────
                _query_cache_set(cache_key, {k: v for k, v in done_event.items()
                                             if k != "status"})

            except Exception as exc:
                logger.exception("query_stream generator error")
                yield f"data: {_json.dumps({'status': 'error', 'error': str(exc)})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # ─── Session history ──────────────────────────────────────────────────

    @app.route("/sessions", methods=["GET"])
    @login_required
    def list_sessions():
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(100, max(1, int(request.args.get("limit", 20))))
        except ValueError:
            limit = 20

        s    = _app_db()
        rows = (
            s.query(ChatSession)
             .filter_by(user_id=current_user.id)
             .order_by(ChatSession.started_at.desc())
             .offset((page - 1) * limit)
             .limit(limit)
             .all()
        )
        return jsonify([
            {
                "id":            r.id,
                "connection_id": r.connection_id,
                "started_at":    r.started_at.isoformat() if r.started_at else None,
                "ended_at":      r.ended_at.isoformat()   if r.ended_at   else None,
                "message_count": len(r.messages),
            }
            for r in rows
        ])

    @app.route("/sessions/<int:sess_id>", methods=["GET"])
    @login_required
    def get_session(sess_id):
        s         = _app_db()
        chat_sess = s.query(ChatSession).filter_by(id=sess_id, user_id=current_user.id).first()
        if not chat_sess:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "session_id": sess_id,
            "messages": [
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
            ],
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
        """
        s    = _app_db()
        conn = s.query(DBConnection).filter_by(id=conn_id, user_id=current_user.id).first()
        if not conn:
            return jsonify({"error": "Connection not found"}), 404

        try:
            plaintext_uri = decrypt(conn.uri_encrypted)
            db, engine    = get_db_from_uri(plaintext_uri)
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
                ignored_tables=conn.ignored_tables_json,
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
        """Returns the cached schema memory for a connection, if it exists."""
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

    # ─── Table sample endpoint (agentic tool) ─────────────────────────────

    @app.route("/connections/<int:conn_id>/sample", methods=["GET"])
    @login_required
    def sample_table(conn_id):
        """
        Return a small sample of rows from a named table.
        Used by the agent's get_table_sample tool and directly queryable by devs.

        Query params:
            table   (required) table name
            column  (optional) restrict to a specific column
            limit   (optional) number of rows, default 5, max 20
        """
        table_name = (request.args.get("table") or "").strip()
        if not table_name:
            return jsonify({"error": "table query param is required"}), 400

        try:
            limit = min(20, max(1, int(request.args.get("limit", 5))))
        except ValueError:
            limit = 5

        column_name = (request.args.get("column") or "").strip() or None

        s    = _app_db()
        conn = s.query(DBConnection).filter_by(id=conn_id, user_id=current_user.id).first()
        if not conn:
            return jsonify({"error": "Connection not found"}), 404

        try:
            plaintext_uri = decrypt(conn.uri_encrypted)
            _, engine     = get_db_from_uri(plaintext_uri)
        except Exception as e:
            return jsonify({"error": f"DB connection failed: {e}"}), 500

        try:
            from schema_memory import get_table_sample
            rows, columns = get_table_sample(engine, table_name, conn.dialect, limit, column_name)
            return jsonify({
                "table":   table_name,
                "columns": columns,
                "rows":    rows,
                "count":   len(rows),
            })
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.warning(f"[Sample] table={table_name} error={e}")
            return jsonify({"error": f"Could not sample table: {e}"}), 500

    return app


# ─── Entrypoint ───────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    create_tables()
    logger.info("App DB tables ready")
    logger.info("NL2DB backend running at http://localhost:8000")
    app.run(debug=cfg.DEBUG, host="0.0.0.0", port=8000)


# ─── Serve frontend HTML (Option A — quick wire-up) ──────────────────────────

@app.route("/app")
@app.route("/app/<path:subpath>")
def serve_frontend(subpath=None):
    """Serves the standalone HTML frontend."""
    from flask import send_from_directory
    return send_from_directory(".", "index.html")
