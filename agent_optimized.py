"""
agent_optimized.py
------------------
Generic NL → SQL agent with self-healing critic-executor loop.

Agentic pipeline (in order):
    1. clarify      — detect ambiguous questions before any SQL is run
    2. plan         — decompose multi-step questions into sub-queries
    3. generate     — produce SQL for each sub-query via LLM
    4. critic       — AST-validate SQL (sqlglot) before touching the DB
    5. execute      — run with LIMIT injection + timeout enforcement
    6. heal         — diagnose + retry on DB errors (up to MAX_RETRIES)
    7. interpret    — plain-English answer synthesised from all results

Works with any database. No domain-specific assumptions.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


# ─── Schema helpers ───────────────────────────────────────────────────────────

def fetch_schema(db, engine, table_names: List[str] = None) -> str:
    """
    Returns schema DDL for the given tables (or all tables if none specified).
    Falls back gracefully if get_table_info is unavailable.
    """
    try:
        if table_names:
            return db.get_table_info(table_names)
        return db.get_table_info()
    except Exception:
        pass

    try:
        from sqlalchemy import inspect as sa_inspect, text
        inspector = sa_inspect(engine)
        tables = table_names or inspector.get_table_names()
        parts = []
        for t in tables:
            try:
                cols = inspector.get_columns(t)
                col_defs = ", ".join(f"{c['name']} {c['type']}" for c in cols)
                parts.append(f"CREATE TABLE {t} ({col_defs});")
            except Exception:
                parts.append(f"-- Could not inspect table: {t}")
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"[Schema] Fallback schema fetch failed: {e}")
        return "(Schema unavailable)"


def get_all_table_names(db, engine) -> List[str]:
    """Returns all usable table names from the connected database."""
    try:
        return db.get_usable_table_names()
    except Exception:
        pass
    try:
        from sqlalchemy import inspect as sa_inspect
        return sa_inspect(engine).get_table_names()
    except Exception as e:
        logger.warning(f"[Schema] Could not list tables: {e}")
        return []


def pick_relevant_tables(question: str, all_tables: List[str], max_tables: int = 12) -> List[str]:
    """
    Lightweight relevance filter — picks tables whose names appear in the question,
    then fills up to max_tables with remaining tables so the LLM always has context.
    """
    q = question.lower()
    matched, rest = [], []

    for t in all_tables:
        tokens = re.split(r"[_\-]", t.lower())
        if any(tok and tok in q for tok in tokens) or t.lower() in q:
            matched.append(t)
        else:
            rest.append(t)

    selected = matched if matched else all_tables
    return selected[:max_tables]


# ─── SQL extraction & validation ──────────────────────────────────────────────

def extract_sql(text: str) -> Optional[str]:
    """Pulls the first SELECT statement out of LLM output."""
    text = text.strip()

    m = re.search(r"(?i)final answer:\s*```sql\s*([\s\S]*?)\s*```", text)
    if m and m.group(1).strip().upper().startswith("SELECT"):
        return m.group(1).strip()

    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]*?)\s*```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m = re.search(r"(SELECT[\s\S]+?)(?:;|\n\n|$)", text, re.IGNORECASE)
    if m:
        return re.sub(r"`+$", "", m.group(1).strip())

    return None


def validate_sql(sql: str, dialect: str = "mysql") -> Tuple[bool, str]:
    """Safety check using AST parsing. Returns (ok, reason)."""
    if not sql or not sql.strip():
        return False, "Empty query"

    dialect_map = {
        "postgresql": "postgres",
        "postgres":   "postgres",
        "mysql":      "mysql",
        "sqlite":     "sqlite",
    }
    sqlglot_dialect = dialect_map.get(dialect.lower(), dialect)

    try:
        parsed = sqlglot.parse(sql, read=sqlglot_dialect)
    except ParseError as e:
        return False, f"Unparsable SQL: {str(e)}"
    except Exception as e:
        return False, f"Error parsing SQL: {str(e)}"

    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]

    if len(statements) == 0:
        return False, "Empty query"
    if len(statements) > 1:
        return False, "Only one SQL statement is allowed"

    stmt = statements[0]

    if not isinstance(stmt, exp.Query):
        return False, f"Only SELECT statements are allowed (found {stmt.__class__.__name__})"

    FORBIDDEN_NODES = (
        exp.Insert, exp.Update, exp.Delete, exp.Merge,
        exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
        exp.Grant, exp.Revoke, exp.Command,
        exp.Transaction, exp.Commit, exp.Rollback,
    )

    for node in stmt.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return False, f"Blocked modification or command node: {node.__class__.__name__}"

    return True, "ok"


# ─── Prompt builders ──────────────────────────────────────────────────────────

_DIALECT_NOTES = {
    "mysql":      "MySQL: use LIKE for strings, NOW()/DATE()/YEAR()/MONTH() for dates.",
    "postgresql": "PostgreSQL: use ILIKE for case-insensitive matching, EXTRACT() for dates, :: for casting.",
    "sqlite":     "SQLite: use LOWER() for case-insensitive matching, strftime() for dates.",
}


def _build_prompt(
    schema: str,
    dialect: str,
    previous_sql: str = None,
    error: str = None,
    strategy: str = None,
    profile_context: str = None,
) -> str:
    retry_block = ""
    if previous_sql and error:
        hint = f"\nStrategy: {strategy}" if strategy else ""
        retry_block = (
            f"\n⚠️ PREVIOUS ATTEMPT FAILED — FIX THIS:\n"
            f"SQL tried:\n```sql\n{previous_sql}\n```\n"
            f"Error: {error}{hint}\n"
        )

    dialect_note = _DIALECT_NOTES.get(dialect, "Use standard SQL.")
    profile_block = f"\nDATABASE PROFILE:\n{profile_context}\n" if profile_context else ""

    return f"""You are an expert {dialect.upper()} SQL assistant for a generic database query tool.
{retry_block}
{profile_block}
DATABASE SCHEMA:
{schema}

{dialect_note}

RULES:
- Only write SELECT queries — never INSERT, UPDATE, DELETE, DROP, etc.
- Only reference tables and columns that exist in the schema above.
- Qualify ambiguous column names with table aliases.
- If filtering by a string, prefer LIKE with wildcards unless the user gives an exact value.

REQUIRED OUTPUT FORMAT:
Final Answer:
```sql
SELECT ...
FROM ...
WHERE ...;
```
Output only the Final Answer block — no prose before or after it.
"""


def _build_healing_strategy(error: str) -> str:
    e = error.lower()
    if "unknown column" in e or ("column" in e and "exist" in e):
        m = re.search(r"unknown column '([^']+)'", e)
        col = f" '{m.group(1)}'" if m else ""
        return f"Column{col} does not exist. Check the schema and use only real column names."
    if "table" in e and ("exist" in e or "found" in e):
        return "A table doesn't exist. Use only table names from the schema."
    if "syntax" in e or "sql syntax" in e:
        return "Fix the SQL syntax — check joins, quotes, parentheses, and dialect."
    if "ambiguous" in e:
        return "Ambiguous column — prefix every column with its table alias."
    return "Read the error carefully and fix the query."


# ─── Execution helpers ────────────────────────────────────────────────────────

def _sqlglot_dialect_name(dialect: str) -> str:
    return {
        "postgresql": "postgres",
        "postgres":   "postgres",
        "mysql":      "mysql",
        "sqlite":     "sqlite",
    }.get((dialect or "").lower(), dialect)


def _ensure_result_limit(sql: str, dialect: str, row_limit: int) -> tuple[str, bool]:
    """
    Inject a LIMIT via AST only when the query does not already have one.
    Returns (sql_to_execute, was_guardrail_applied).
    """
    try:
        parsed = sqlglot.parse_one(sql, read=_sqlglot_dialect_name(dialect))
        if parsed is None or not isinstance(parsed, exp.Query):
            return sql, False
        if parsed.args.get("limit") is not None:
            return sql, False
        parsed.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
        return parsed.sql(dialect=_sqlglot_dialect_name(dialect)), True
    except Exception as e:
        logger.warning(f"[Exec] Could not inject default LIMIT: {e}")
        return sql, False


class QueryTimeoutError(RuntimeError):
    pass


def _apply_query_timeout(conn, dialect: str, timeout_ms: int) -> None:
    if timeout_ms <= 0:
        return
    from sqlalchemy import text
    normalized = (dialect or "").lower()
    if normalized in ("postgresql", "postgres"):
        conn.execute(text(f"SET statement_timeout = {int(timeout_ms)}"))
    elif normalized == "mysql":
        conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_ms)}"))


def _reset_query_timeout(conn, dialect: str) -> None:
    from sqlalchemy import text
    normalized = (dialect or "").lower()
    if normalized in ("postgresql", "postgres"):
        conn.execute(text("SET statement_timeout = 0"))
    elif normalized == "mysql":
        conn.execute(text("SET SESSION MAX_EXECUTION_TIME = 0"))


def _is_timeout_error(err: Exception) -> bool:
    message = str(err).lower()
    return any(fragment in message for fragment in (
        "statement timeout",
        "query timed out",
        "timeout expired",
        "max_execution_time",
        "maximum statement execution time exceeded",
        "query execution was interrupted",
        "canceling statement due to statement timeout",
    ))


def _execute_sql(engine, sql: str, dialect: str, row_limit: int, timeout_ms: int):
    """
    Execute sql against engine with LIMIT injection, timeout enforcement,
    and value serialisation. Returns (sql_executed, rows, results_truncated).
    """
    from sqlalchemy import text
    import datetime
    from decimal import Decimal

    sql_to_execute, limit_applied = _ensure_result_limit(sql, dialect, row_limit)

    try:
        with engine.connect() as conn:
            timeout_applied = False
            try:
                _apply_query_timeout(conn, dialect, timeout_ms)
                timeout_applied = True
            except Exception as e:
                logger.warning(f"[Exec] Could not apply query timeout for {dialect}: {e}")

            try:
                res = conn.execute(text(sql_to_execute))
                if not res.returns_rows:
                    return sql_to_execute, [], False

                cols    = list(res.keys())
                db_rows = res.fetchmany(row_limit + 1)
            finally:
                if timeout_applied:
                    try:
                        _reset_query_timeout(conn, dialect)
                    except Exception as e:
                        logger.warning(f"[Exec] Could not reset query timeout for {dialect}: {e}")

    except QueryTimeoutError:
        raise
    except Exception as e:
        if _is_timeout_error(e):
            raise QueryTimeoutError(f"Query timed out after {timeout_ms} ms") from e
        raise

    def serialise_value(val):
        if isinstance(val, (datetime.datetime, datetime.date)):
            return val.isoformat()
        if isinstance(val, Decimal):
            return float(val)
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="replace")
        return val

    results_truncated = limit_applied or len(db_rows) > row_limit
    result = [
        {col: serialise_value(row[i]) for i, col in enumerate(cols)}
        for row in db_rows[:row_limit]
    ]
    return sql_to_execute, result, results_truncated


# ─── Core result type ─────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    success:           bool
    sql:               str
    rows:              object = None
    error:             str    = None
    retries:           int    = 0
    healing_log:       List[str] = field(default_factory=list)
    results_truncated: bool   = False


# ─── Agentic layer 1: Clarification ──────────────────────────────────────────

def check_clarification_needed(llm, question: str, schema: str) -> Optional[str]:
    """
    Ask the LLM if the question is too ambiguous to answer without a follow-up.
    Returns a clarifying question string if needed, or None if the question is clear enough.

    This runs before any SQL is generated — saves DB calls on bad-premise questions.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    system = f"""You are a query intent analyser for a database assistant.

Given a user's question and a database schema, decide if the question is specific
enough to answer with SQL, or if a single clarifying question would significantly
improve the result.

DATABASE SCHEMA:
{schema}

Rules:
- Only flag genuinely ambiguous questions where multiple valid interpretations exist
  and would produce very different SQL (e.g. "show me sales" — which time range? which region?).
- Do NOT flag questions that have a reasonable default interpretation.
- Do NOT ask for info that can be inferred from the schema.

Respond ONLY with valid JSON, no prose, no markdown fences:
  {{"needs_clarification": false}}
  OR
  {{"needs_clarification": true, "question": "One short clarifying question."}}
"""
    try:
        resp = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=f'User asked: "{question}"'),
        ])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        import json
        parsed = json.loads(raw)
        if parsed.get("needs_clarification") and parsed.get("question"):
            return parsed["question"]
    except Exception as e:
        logger.debug(f"[Clarify] Skipped (error: {e})")
    return None


# ─── Agentic layer 2: Query planner ──────────────────────────────────────────

@dataclass
class QueryPlan:
    steps:       List[str]   # ordered sub-questions to answer
    needs_merge: bool        # True → final interpreter must combine multiple result sets
    rationale:   str         # why the plan was created (logged, not shown to user)


def plan_query(llm, question: str, schema: str) -> QueryPlan:
    """
    Decompose a complex question into ordered sub-questions.
    Simple questions return a single-step plan (passthrough).

    Examples:
      "top 5 customers" → 1 step (simple)
      "compare this month's revenue vs last month, broken down by region" → 2 steps
      "which sales rep had the most wins, and what was their average deal size?" → 2 steps
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    import json

    system = f"""You are a query planning agent for a text-to-SQL system.

Given a user question and a database schema, decompose the question into the minimum
number of SQL sub-queries needed to answer it completely.

DATABASE SCHEMA:
{schema}

Rules:
- A question answerable by a single SELECT → return exactly 1 step.
- Only split into multiple steps when truly necessary (different aggregation scopes,
  comparisons between independent groupings, etc.).
- Each step should be a complete, self-contained question in plain English.
- Maximum 4 steps.

Respond ONLY with valid JSON, no prose, no markdown fences:
{{
  "steps": ["Sub-question 1.", "Sub-question 2."],
  "needs_merge": true,
  "rationale": "Why this decomposition."
}}
"""
    try:
        resp = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=f'User asked: "{question}"'),
        ])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)
        steps = parsed.get("steps") or [question]
        if not isinstance(steps, list) or not steps:
            steps = [question]
        return QueryPlan(
            steps=steps[:4],
            needs_merge=bool(parsed.get("needs_merge", len(steps) > 1)),
            rationale=parsed.get("rationale", ""),
        )
    except Exception as e:
        logger.debug(f"[Planner] Skipped (error: {e}) — using single-step passthrough")
        return QueryPlan(steps=[question], needs_merge=False, rationale="passthrough")

def summarise_rows_for_llm(rows: list, max_chars: int = 3000) -> str:
    """
    Compress query results into a token-efficient summary before sending to LLM.
    Small results get a compact table. Large results get stats + a sample.
    """
    if not rows:
        return "No rows returned."

    n    = len(rows)
    cols = list(rows[0].keys())

    if n <= 20:
        header = " | ".join(cols)
        lines  = [header, "-" * len(header)]
        for row in rows:
            lines.append(" | ".join(str(row.get(c, ""))[:30] for c in cols))
        return "\n".join(lines)

    # Large result: numeric stats + 5-row sample
    lines = [f"{n} rows. Columns: {', '.join(cols)}", ""]
    for col in cols:
        vals = [row[col] for row in rows if isinstance(row.get(col), (int, float))]
        if vals:
            lines.append(
                f"{col}: min={min(vals):.2f}  max={max(vals):.2f}  "
                f"avg={sum(vals)/len(vals):.2f}  sum={sum(vals):.2f}"
            )
    lines.append("\nSample (first 5 rows):")
    for row in rows[:5]:
        lines.append("  " + " | ".join(f"{k}={str(v)[:25]}" for k, v in row.items()))

    result = "\n".join(lines)
    return result[:max_chars]
# ─── Agentic layer 3: Result interpreter ─────────────────────────────────────

def interpret_results(
    llm,
    original_question: str,
    steps: List[str],
    step_results: List[Dict],   # list of {sql, rows, truncated}
) -> str:
    """
    Synthesise all sub-query results into a plain-English answer.
    Replaces the simple explain_result() for multi-step queries.
    Handles single-step too — more context-aware than the old explainer.
    """
    from langchain_core.messages import HumanMessage

    results_block = ""
    for i, (step, res) in enumerate(zip(steps, step_results), 1):
        trunc = " (truncated — more rows exist)" if res.get("truncated") else ""
        results_block += (
            f"\nStep {i}: {step}\n"
            f"SQL: {res['sql']}\n"
            f"Result{trunc}:\n{summarise_rows_for_llm(res['rows'])}\n"
        )

    prompt = (
        f'The user asked: "{original_question}"\n\n'
        f"Here are the query results:\n{results_block}\n\n"
        "Answer the user's question in plain English. Be concise (2-4 sentences). "
        "Highlight key numbers or comparisons. "
        "If results were truncated, mention that only a sample is shown. "
        "Never mention SQL, tables, or technical database details."
    )
    try:
        r = llm.invoke([HumanMessage(content=prompt)])
        return r.content if hasattr(r, "content") else str(r)
    except Exception as e:
        logger.warning(f"[Interpreter] {e}")
        # Fallback: return raw result of the last successful step
        if step_results:
            return str(step_results[-1].get("rows", ""))[:500]
        return "Could not generate explanation."


# ─── Legacy single-query explainer (kept for compatibility) ──────────────────

def explain_result(llm, question: str, sql: str, rows) -> str:
    """Summarises a single query result in plain English."""
    if not rows:
        return "No data found for that query."
    return interpret_results(
        llm,
        original_question=question,
        steps=[question],
        step_results=[{"sql": sql, "rows": rows, "truncated": False}],
    )


# ─── Critic-executor loop ─────────────────────────────────────────────────────

def execute_with_healing(
    question: str,
    llm,
    db,
    engine,
    schema: str,
    dialect: str = "mysql",
    chat_history: list = None,
    profile_context: str = None,
) -> QueryResult:
    """
    Generate → validate → execute, with up to MAX_RETRIES self-healing retries.
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from config import cfg

    prev_sql = None
    error    = None
    strategy = None
    log      = []
    current_schema    = schema
    result_row_limit  = max(1, int(getattr(cfg, "QUERY_RESULT_ROW_LIMIT", 100)))
    db_query_timeout_ms = max(1, int(getattr(cfg, "DB_QUERY_TIMEOUT_MS", 5000)))

    for attempt in range(MAX_RETRIES + 1):
        # ── 1. Build prompt ───────────────────────────────────────────────
        system_prompt = _build_prompt(current_schema, dialect, prev_sql, error, strategy, profile_context)
        messages = [SystemMessage(content=system_prompt)]
        if chat_history:
            messages.extend(chat_history[-4:])

        user_msg = (
            question if attempt == 0
            else f"Fix the query. Error was: {error}\nOriginal question: {question}"
        )
        messages.append(HumanMessage(content=user_msg))

        # ── 2. Call LLM ───────────────────────────────────────────────────
        try:
            response = llm.invoke(messages)
            llm_out  = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            return QueryResult(False, prev_sql or "", error=f"LLM error: {e}",
                               retries=attempt, healing_log=log)

        sql = extract_sql(llm_out)
        if not sql:
            error    = "No SELECT query found in LLM response."
            strategy = "Output only a Final Answer block with ```sql ... ``` inside."
            log.append(f"[{attempt+1}] SQL extraction failed")
            continue

        # ── 3. Critic ─────────────────────────────────────────────────────
        ok, reason = validate_sql(sql, dialect)
        if not ok:
            error    = reason
            strategy = f"Fix: {reason}"
            prev_sql = sql
            log.append(f"[{attempt+1}] Critic rejected: {reason}")
            continue

        # ── 4. Execute ────────────────────────────────────────────────────
        try:
            sql_executed, result, results_truncated = _execute_sql(
                engine, sql, dialect, result_row_limit, db_query_timeout_ms
            )
            log.append(f"[{attempt+1}] Executed OK" + (" (results truncated)" if results_truncated else ""))
            return QueryResult(True, sql_executed, rows=result, retries=attempt,
                               healing_log=log, results_truncated=results_truncated)

        except QueryTimeoutError as te:
            err_str = str(te)
            log.append(f"[{attempt+1}] Timeout: {err_str}")
            return QueryResult(False, sql, error=err_str, retries=attempt, healing_log=log)

        except Exception as db_err:
            err_str  = str(db_err)
            strategy = _build_healing_strategy(err_str)
            log.append(f"[{attempt+1}] DB error: {err_str[:200]}")
            error    = err_str
            prev_sql = sql

            if "unknown column" in err_str.lower() or "column" in err_str.lower():
                affected = [t for t in get_all_table_names(db, engine) if t.lower() in sql.lower()]
                if affected:
                    tighter = fetch_schema(db, engine, affected)
                    if tighter:
                        current_schema = tighter
                        log.append(f"[{attempt+1}] Injected live schema for: {affected}")

    return QueryResult(False, prev_sql or "", error=error,
                       retries=MAX_RETRIES, healing_log=log)


# ─── Public entry point ───────────────────────────────────────────────────────

def run_nl_query(
    question: str,
    llm,
    db,
    engine,
    dialect: str = "mysql",
    chat_history: list = None,
    uri: str = None,
    memory=None,
    conn_id: int = None,
    app_db_session=None,
    profile=None,
    skip_clarification: bool = False,
    skip_planning: bool = False,
    ignored_tables: Optional[List[str]] = None,
) -> Dict:
    """
    Full agentic pipeline:
      clarify → plan → [generate → critic → execute → heal] × N steps → interpret

    Returns:
        {
            success            : bool,
            sql                : str | None,         (last/primary SQL run)
            results            : list | None,        (rows from last step)
            explanation        : str | None,
            error              : str | None,
            retries            : int,
            healing_log        : list[str],
            schema_source      : "memory" | "live",
            results_truncated  : bool,
            clarification_needed: str | None,        (set when we ask a follow-up)
            plan               : list[str],          (sub-questions executed)
        }
    """
    dialect        = dialect or "mysql"
    schema_source  = "live"
    profile_context = ""

    if profile is not None:
        try:
            from profile_context import build_profile_context
            profile_context = build_profile_context(profile)
        except Exception as e:
            logger.warning(f"[Profile] Could not build profile context: {e}")

    # ── Schema resolution ─────────────────────────────────────────────────
    if memory is None and uri:
        try:
            from schema_memory import load_or_explore
            memory = load_or_explore(
                uri=uri, db=db, engine=engine, llm=llm, dialect=dialect,
                conn_id=conn_id, app_db_session=app_db_session,
            )
        except Exception as e:
            logger.warning(f"[Memory] Could not load/explore: {e}")

    if memory is not None:
        try:
            from schema_memory import get_schema_context
            schema_tables_only = get_schema_context(memory, question, ignored_tables=ignored_tables, detail="tables_only")
            schema_slim        = get_schema_context(memory, question, ignored_tables=ignored_tables, detail="slim")
            schema_full        = get_schema_context(memory, question, ignored_tables=ignored_tables, detail="full")
            schema_source      = "memory"

            logger.info("=" * 80)
            logger.info("SCHEMA COMPRESSION TEST")
            logger.info(f"tables_only : {len(schema_tables_only):,} chars")
            logger.info(f"slim        : {len(schema_slim):,} chars")
            logger.info(f"full        : {len(schema_full):,} chars")
            logger.info("=" * 80)

            logger.info(f"[Memory] Using cached schema memory ({len(memory.tables)} tables)")
        except Exception as e:
            logger.warning(f"[Memory] get_schema_context failed: {e}")
            memory = None

    if memory is None:
        all_tables = get_all_table_names(db, engine)
        if ignored_tables:
            ignored_lower = {t.lower() for t in ignored_tables}
            all_tables = [t for t in all_tables if t.lower() not in ignored_lower]
        relevant   = pick_relevant_tables(question, all_tables)
        live_schema = fetch_schema(db, engine, relevant)
        schema_tables_only = live_schema
        schema_slim        = live_schema
        schema_full        = live_schema
        logger.info("[Memory] Using live schema fetch (no memory available)")

    # ── Step 1: Clarification check ───────────────────────────────────────
    if not skip_clarification:
        logger.info("[Stage] Clarifier -> tables_only schema")
        clarification = check_clarification_needed(llm, question, schema_tables_only)
        if clarification:
            logger.info(f"[Clarify] Asking: {clarification}")
            return {
                "success":             False,
                "sql":                 None,
                "results":             None,
                "explanation":         None,
                "error":               None,
                "retries":             0,
                "healing_log":         ["[Clarify] Question flagged as ambiguous"],
                "schema_source":       schema_source,
                "results_truncated":   False,
                "clarification_needed": clarification,
                "plan":                [],
            }

    # ── Step 2: Query planning ────────────────────────────────────────────
    if not skip_planning:
        logger.info("[Stage] Planner -> slim schema")
        plan = plan_query(llm, question, schema_slim)
        logger.info(f"[Planner] {len(plan.steps)} step(s): {plan.steps}")
    else:
        logger.info("[Stage] Planner -> disabled (skipping)")
        plan = QueryPlan(steps=[question], needs_merge=False, rationale="skipped")

    # ── Step 3: Execute each sub-query ────────────────────────────────────
    step_results = []
    all_logs     = []
    total_retries = 0
    last_error    = None

    for i, sub_question in enumerate(plan.steps):
        logger.info(f"[Plan] Step {i+1}/{len(plan.steps)}: {sub_question}")
        
        logger.info("[Stage] Execution -> full schema")
        qr = execute_with_healing(
            question=sub_question,
            llm=llm,
            db=db,
            engine=engine,
            schema=schema_full,
            dialect=dialect,
            chat_history=chat_history if i == 0 else None,  # only first step gets history
            profile_context=profile_context,
        )
        all_logs.extend(qr.healing_log)
        total_retries += qr.retries

        if not qr.success:
            last_error = qr.error
            logger.warning(f"[Plan] Step {i+1} failed: {qr.error}")
            # On partial failure we still try to interpret what we have
            break

        step_results.append({
            "sql":       qr.sql,
            "rows":      qr.rows,
            "truncated": qr.results_truncated,
        })

    # ── Step 4: Interpret ─────────────────────────────────────────────────
    if step_results:
        explanation = interpret_results(
            llm,
            original_question=question,
            steps=plan.steps[:len(step_results)],
            step_results=step_results,
        )
        last = step_results[-1]
        return {
            "success":              True,
            "sql":                  last["sql"],
            "results":              last["rows"],
            "explanation":          explanation,
            "error":                None,
            "retries":              total_retries,
            "healing_log":          all_logs,
            "schema_source":        schema_source,
            "results_truncated":    last["truncated"],
            "clarification_needed": None,
            "plan":                 plan.steps,
        }

    # All steps failed
    return {
        "success":              False,
        "sql":                  None,
        "results":              None,
        "explanation":          None,
        "error":                last_error,
        "retries":              total_retries,
        "healing_log":          all_logs,
        "schema_source":        schema_source,
        "results_truncated":    False,
        "clarification_needed": None,
        "plan":                 plan.steps,
    }


__all__ = ["run_nl_query", "extract_sql", "validate_sql", "check_clarification_needed",
           "plan_query", "interpret_results"]