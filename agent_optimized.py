"""
agent_optimized.py
------------------
Generic NL → SQL agent with self-healing critic-executor loop.

Flow:
    question → schema fetch → SQL generation → critic validation
             → execution → [on failure: diagnose + retry up to 3x]
             → explain result

Works with any database. No domain-specific assumptions.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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

    # SQLAlchemy fallback
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
    No domain assumptions.
    """
    q = question.lower()
    matched, rest = [], []

    for t in all_tables:
        # Match on table name tokens (strip common prefixes like app_, tbl_, etc.)
        tokens = re.split(r"[_\-]", t.lower())
        if any(tok and tok in q for tok in tokens) or t.lower() in q:
            matched.append(t)
        else:
            rest.append(t)

    # If we matched nothing, use all tables (let the LLM decide)
    selected = matched if matched else all_tables
    return selected[:max_tables]


# ─── SQL extraction & validation ──────────────────────────────────────────────

def extract_sql(text: str) -> Optional[str]:
    """Pulls the first SELECT statement out of LLM output."""
    text = text.strip()

    # Preferred: Final Answer: ```sql ... ```
    m = re.search(r"(?i)final answer:\s*```sql\s*([\s\S]*?)\s*```", text)
    if m and m.group(1).strip().upper().startswith("SELECT"):
        return m.group(1).strip()

    # Any ```sql ... ``` block
    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]*?)\s*```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Bare SELECT up to ; or double newline
    m = re.search(r"(SELECT[\s\S]+?)(?:;|\n\n|$)", text, re.IGNORECASE)
    if m:
        return re.sub(r"`+$", "", m.group(1).strip())

    return None


DANGEROUS = [
    r"\bDROP\s+TABLE\b", r"\bDROP\s+DATABASE\b", r"\bDELETE\s+FROM\b",
    r"\bINSERT\s+INTO\b", r"\bUPDATE\s+\w+\s+SET\b", r"\bALTER\s+TABLE\b",
    r"\bCREATE\s+TABLE\b", r"\bTRUNCATE\b", r"\bGRANT\b", r"\bREVOKE\b",
    r"\bEXEC(UTE)?\b",
]


def validate_sql(sql: str) -> Tuple[bool, str]:
    """Safety check. Returns (ok, reason)."""
    if not sql:
        return False, "Empty query"
    cleaned = re.sub(r"--.*", "", sql, flags=re.MULTILINE).strip()
    if not cleaned.upper().startswith("SELECT"):
        return False, "Only SELECT queries are allowed"
    for pat in DANGEROUS:
        if re.search(pat, cleaned, re.IGNORECASE):
            return False, f"Blocked pattern: {pat}"
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

    return f"""You are an expert {dialect.upper()} SQL assistant for a generic database query tool.
{retry_block}
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


def _build_healing_strategy(error: str, empty_result: bool = False) -> str:
    if empty_result:
        return (
            "The query returned no rows. Relax exact string filters: "
            "swap = for LIKE and wrap values in % wildcards."
        )
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


# ─── Core result type ─────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    success: bool
    sql: str
    rows: object = None
    error: str = None
    retries: int = 0
    healing_log: List[str] = field(default_factory=list)


# ─── Critic-executor loop ─────────────────────────────────────────────────────

def execute_with_healing(
    question: str,
    llm,
    db,
    engine,
    schema: str,
    dialect: str = "mysql",
    chat_history: list = None,
) -> QueryResult:
    """
    Generate → validate → execute, with up to MAX_RETRIES self-healing retries.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    prev_sql = None
    error = None
    strategy = None
    log = []
    current_schema = schema  # may be replaced with tighter schema on column errors

    for attempt in range(MAX_RETRIES + 1):
        # ── 1. Build prompt ───────────────────────────────────────────────
        system_prompt = _build_prompt(current_schema, dialect, prev_sql, error, strategy)
        messages = [SystemMessage(content=system_prompt)]
        if chat_history:
            messages.extend(chat_history[-4:])  # last 2 turns of context

        user_msg = (
            question if attempt == 0
            else f"Fix the query. Error was: {error}\nOriginal question: {question}"
        )
        messages.append(HumanMessage(content=user_msg))

        # ── 2. Call LLM ───────────────────────────────────────────────────
        try:
            response = llm.invoke(messages)
            llm_out = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            return QueryResult(False, prev_sql or "", error=f"LLM error: {e}",
                               retries=attempt, healing_log=log)

        sql = extract_sql(llm_out)
        if not sql:
            error = "No SELECT query found in LLM response."
            log.append(f"[{attempt+1}] SQL extraction failed")
            strategy = "Output only a Final Answer block with ```sql ... ``` inside."
            continue

        # ── 3. Critic ─────────────────────────────────────────────────────
        ok, reason = validate_sql(sql)
        if not ok:
            error = reason
            log.append(f"[{attempt+1}] Critic rejected: {reason}")
            strategy = f"Fix: {reason}"
            prev_sql = sql
            continue

        # ── 4. Execute ────────────────────────────────────────────────────
        try:
            result = db.run(sql)
            log.append(f"[{attempt+1}] Executed OK")

            if not result and attempt < MAX_RETRIES:
                strategy = _build_healing_strategy("", empty_result=True)
                log.append(f"[{attempt+1}] Empty result — retrying with looser filters")
                error = "Query returned no rows."
                prev_sql = sql
                continue

            return QueryResult(True, sql, rows=result, retries=attempt, healing_log=log)

        except Exception as db_err:
            err_str = str(db_err)
            strategy = _build_healing_strategy(err_str)
            log.append(f"[{attempt+1}] DB error: {err_str[:200]}")
            error = err_str
            prev_sql = sql

            # On column errors, swap in live schema for the affected tables
            if "unknown column" in err_str.lower() or "column" in err_str.lower():
                affected = [t for t in get_all_table_names(db, engine) if t.lower() in sql.lower()]
                if affected:
                    tighter = fetch_schema(db, engine, affected)
                    if tighter:
                        current_schema = tighter
                        log.append(f"[{attempt+1}] Injected live schema for: {affected}")

    return QueryResult(False, prev_sql or "", error=error,
                       retries=MAX_RETRIES, healing_log=log)


# ─── Explainer ────────────────────────────────────────────────────────────────

def explain_result(llm, question: str, sql: str, rows) -> str:
    """Summarises query results in plain English."""
    if not rows:
        return "No data found for that query."
    from langchain_core.messages import HumanMessage
    prompt = (
        f'User asked: "{question}"\n\n'
        f"SQL run:\n{sql}\n\n"
        f"Result:\n{rows}\n\n"
        "Answer the user's question in 1-2 plain sentences. "
        "Don't mention SQL or technical details."
    )
    try:
        r = llm.invoke([HumanMessage(content=prompt)])
        return r.content if hasattr(r, "content") else str(r)
    except Exception as e:
        logger.warning(f"[Explainer] {e}")
        return str(rows)[:500]


# ─── Public entry point ───────────────────────────────────────────────────────

def run_nl_query(
    question: str,
    llm,
    db,
    engine,
    dialect: str = "mysql",
    chat_history: list = None,
) -> Dict:
    """
    Convert a natural language question to SQL, execute it, explain the result.

    Returns:
        {
            success     : bool,
            sql         : str | None,
            results     : raw DB output | None,
            explanation : str | None,
            error       : str | None,
            retries     : int,
            healing_log : list[str],
        }
    """
    all_tables = get_all_table_names(db, engine)
    relevant   = pick_relevant_tables(question, all_tables)
    schema     = fetch_schema(db, engine, relevant)
    dialect    = dialect or "mysql"

    qr = execute_with_healing(
        question=question,
        llm=llm,
        db=db,
        engine=engine,
        schema=schema,
        dialect=dialect,
        chat_history=chat_history,
    )

    return {
        "success":     qr.success,
        "sql":         qr.sql or None,
        "results":     qr.rows,
        "explanation": explain_result(llm, question, qr.sql, qr.rows) if qr.success else None,
        "error":       qr.error,
        "retries":     qr.retries,
        "healing_log": qr.healing_log,
    }


__all__ = ["run_nl_query", "extract_sql", "validate_sql"]
