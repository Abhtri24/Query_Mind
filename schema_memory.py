"""
schema_memory.py
----------------
Persistent schema memory — stored in the nl2db_connections.schema_memory_json
column (survives deploys, works on Render/Railway ephemeral filesystems).

Public API:
    load_or_explore(uri, db, engine, llm, dialect, conn_id, app_db_session, ignored_tables)
    explore_and_save(uri, db, engine, llm, dialect, conn_id, app_db_session, ignored_tables)
    get_schema_context(memory, question, ignored_tables)
    get_table_sample(engine, table_name, dialect, limit, column_name)
    memory_summary_for_api(memory)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_SAMPLE_ROWS = 3
MAX_TABLES      = 60
MAX_COLS_SHOWN  = 40

# Tables that are almost never useful for NL queries — skip them unless explicitly referenced
_SYSTEM_TABLE_PREFIXES = (
    "information_schema",
    "pg_",
    "sql_",
    "sys_",
    "mysql.",
    "alembic_",
)

_SKIP_SAMPLE_COLUMNS = {
    "password", "password_hash", "hashed_password", "secret",
    "token", "api_key", "private_key", "preference_vector",
    "embedding_id", "embedding",
}


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class ColumnInfo:
    name:          str
    type:          str
    nullable:      bool = True
    sample_values: List[str] = field(default_factory=list)


@dataclass
class TableInfo:
    name:        str
    row_count:   int
    columns:     List[ColumnInfo]
    description: str = ""


@dataclass
class MemoryData:
    dialect:        str
    db_summary:     str
    tables:         List[TableInfo]
    explored_at:    str
    schema_version: int = 1


# ─── Serialisation ────────────────────────────────────────────────────────────

def _to_json(m: MemoryData) -> str:
    return json.dumps(asdict(m))


def _from_json(raw: str) -> MemoryData:
    d = json.loads(raw)
    tables = [
        TableInfo(
            name=t["name"],
            row_count=t["row_count"],
            description=t.get("description", ""),
            columns=[ColumnInfo(**c) for c in t["columns"]],
        )
        for t in d.get("tables", [])
    ]
    return MemoryData(
        dialect=d["dialect"],
        db_summary=d.get("db_summary", ""),
        tables=tables,
        explored_at=d.get("explored_at", ""),
        schema_version=d.get("schema_version", 1),
    )


# ─── DB persistence ───────────────────────────────────────────────────────────

def _load_from_db(conn_id: int, app_db_session) -> Optional[MemoryData]:
    if conn_id is None or app_db_session is None:
        return None
    try:
        from models import DBConnection
        conn = app_db_session.get(DBConnection, conn_id)
        if conn and conn.schema_memory_json:
            return _from_json(conn.schema_memory_json)
    except Exception as e:
        logger.warning(f"[Memory] DB load failed: {e}")
    return None


def _save_to_db(conn_id: int, data: MemoryData, app_db_session) -> None:
    if conn_id is None or app_db_session is None:
        return
    try:
        from models import DBConnection
        conn = app_db_session.get(DBConnection, conn_id)
        if conn:
            conn.schema_memory_json = _to_json(data)
            conn.memory_explored_at = datetime.now(timezone.utc)
            app_db_session.commit()
            logger.info(f"[Memory] Saved to DB for conn_id={conn_id}")
    except Exception as e:
        logger.warning(f"[Memory] DB save failed: {e}")
        app_db_session.rollback()


def delete_memory_from_db(conn_id: int, app_db_session) -> None:
    if conn_id is None or app_db_session is None:
        return
    try:
        from models import DBConnection
        conn = app_db_session.get(DBConnection, conn_id)
        if conn:
            conn.schema_memory_json = None
            conn.memory_explored_at = None
            app_db_session.commit()
    except Exception as e:
        logger.warning(f"[Memory] DB delete failed: {e}")
        app_db_session.rollback()


# ─── Identifier quoting ───────────────────────────────────────────────────────

def _quote_identifier(name: str, dialect: str) -> str:
    """Return a safely quoted identifier for the given dialect."""
    normalized = (dialect or "").lower()
    if normalized == "mysql":
        escaped = name.replace("`", "``")
        return f"`{escaped}`"
    else:
        # PostgreSQL, SQLite, and standard SQL use double-quotes
        escaped = name.replace('"', '""')
        return f'"{escaped}"'


# ─── Introspection ────────────────────────────────────────────────────────────

def _introspect(
    db,
    engine,
    dialect: str,
    ignored_tables: Optional[List[str]] = None,
) -> List[TableInfo]:
    from sqlalchemy import inspect as sa_inspect, text

    ignored_lower = {t.lower() for t in (ignored_tables or [])}

    inspector      = sa_inspect(engine)
    all_table_names = inspector.get_table_names()
    table_names    = [
    t for t in all_table_names
    if t.lower() not in ignored_lower
    and not any(t.lower().startswith(p) for p in _SYSTEM_TABLE_PREFIXES)
][:MAX_TABLES]
    if ignored_lower:
        skipped = [t for t in all_table_names if t.lower() in ignored_lower]
        if skipped:
            logger.info(f"[Memory] Skipping ignored tables during introspection: {skipped}")

    tables: List[TableInfo] = []

    with engine.connect() as conn:
        for tname in table_names:
            try:
                raw_cols = inspector.get_columns(tname)
            except Exception:
                raw_cols = []

            columns: List[ColumnInfo] = []
            qt = _quote_identifier(tname, dialect)
            for col in raw_cols[:MAX_COLS_SHOWN]:
                col_info = ColumnInfo(
                    name=col["name"],
                    type=str(col["type"]),
                    nullable=col.get("nullable", True),
                )
                if col["name"].lower() not in _SKIP_SAMPLE_COLUMNS:
                    try:
                        qc = _quote_identifier(col["name"], dialect)
                        q  = text(
                            f"SELECT {qc} FROM {qt} "
                            f"WHERE {qc} IS NOT NULL "
                            f"LIMIT {MAX_SAMPLE_ROWS}"
                        )
                        rows = conn.execute(q).fetchall()
                        col_info.sample_values = [str(r[0])[:80] for r in rows]
                    except Exception:
                        pass
                columns.append(col_info)
                columns.append(col_info)

            row_count = 0
            try:
                row_count = conn.execute(text(f"SELECT COUNT(*) FROM {qt}")).scalar() or 0
            except Exception:
                pass

            tables.append(TableInfo(name=tname, row_count=row_count, columns=columns))

    return tables


# ─── LLM summarisation ────────────────────────────────────────────────────────

def _build_exploration_prompt(tables: List[TableInfo], dialect: str) -> str:
    lines = [
        f"You are analysing a {dialect.upper()} database.",
        "Here are its tables, columns, and sample data:\n",
    ]
    for t in tables:
        lines.append(f"TABLE: {t.name}  ({t.row_count:,} rows)")
        for c in t.columns:
            samples = ", ".join(f'"{v}"' for v in c.sample_values[:2]) if c.sample_values else "—"
            lines.append(f"  {c.name} ({c.type}) — e.g. {samples}")
        lines.append("")

    lines += [
        "Respond ONLY with valid JSON — no markdown, no prose outside the JSON.",
        "",
        "Required shape:",
        "{",
        '  "db_summary": "One paragraph: what is this database about?",',
        '  "tables": [{"name": "<table>", "description": "One sentence."}, ...]',
        "}",
    ]
    return "\n".join(lines)


def _strip_llm_json(raw: str) -> str:
    """Strip markdown code fences that LLMs sometimes wrap JSON in."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    return raw.strip()


def _run_llm_summary(llm, tables: List[TableInfo], dialect: str) -> tuple[str, Dict[str, str]]:
    from langchain_core.messages import HumanMessage

    prompt = _build_exploration_prompt(tables, dialect)
    try:
        response   = llm.invoke([HumanMessage(content=prompt)])
        raw        = response.content if hasattr(response, "content") else str(response)
        parsed     = json.loads(_strip_llm_json(raw))
        db_summary = parsed.get("db_summary", "")
        table_descs = {t["name"]: t.get("description", "") for t in parsed.get("tables", [])}
        return db_summary, table_descs
    except Exception as e:
        logger.warning(f"[Memory] LLM summary failed: {e}")
        return "Schema memory collected but LLM summarisation failed.", {}


# ─── Table sample (agentic tool) ──────────────────────────────────────────────

def get_table_sample(
    engine,
    table_name: str,
    dialect: str,
    limit: int = 5,
    column_name: Optional[str] = None,
) -> Tuple[List[Dict], List[str]]:
    """
    Return a small sample of rows from a table.

    Used by the agent when it needs to inspect actual data values to write
    better SQL (e.g. check what format a date column uses, what enum values
    exist, or how a foreign key is structured).

    Also exposed as GET /connections/<id>/sample for direct developer use.

    Args:
        engine:      SQLAlchemy engine for the target database
        table_name:  name of the table to sample
        dialect:     database dialect for identifier quoting
        limit:       max rows to return (capped at 20)
        column_name: if given, restrict to a single column

    Returns:
        (rows, columns)  where rows is a list of dicts and columns is a list of names

    Raises:
        ValueError if the table doesn't exist
        Exception  for DB-level errors (caller should handle)
    """
    from sqlalchemy import inspect as sa_inspect, text
    import datetime
    from decimal import Decimal

    limit = min(20, max(1, limit))

    # Validate table exists — prevents SQL injection via table_name
    inspector   = sa_inspect(engine)
    known_tables = {t.lower() for t in inspector.get_table_names()}
    if table_name.lower() not in known_tables:
        raise ValueError(f"Table '{table_name}' does not exist in this database.")

    qt = _quote_identifier(table_name, dialect)

    if column_name:
        # Validate column exists too
        cols = inspector.get_columns(table_name)
        known_cols = {c["name"].lower() for c in cols}
        if column_name.lower() not in known_cols:
            raise ValueError(f"Column '{column_name}' does not exist in table '{table_name}'.")
        qc       = _quote_identifier(column_name, dialect)
        select_  = f"SELECT {qc} FROM {qt} WHERE {qc} IS NOT NULL LIMIT {limit}"
        col_names = [column_name]
    else:
        select_   = f"SELECT * FROM {qt} LIMIT {limit}"
        col_names = None  # resolved after query

    def serialise(val):
        if isinstance(val, (datetime.datetime, datetime.date)):
            return val.isoformat()
        if isinstance(val, Decimal):
            return float(val)
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="replace")
        return val

    with engine.connect() as conn:
        res  = conn.execute(text(select_))
        cols = col_names or list(res.keys())
        rows = [
            {col: serialise(row[i]) for i, col in enumerate(cols)}
            for row in res.fetchall()
        ]

    logger.debug(f"[Sample] {table_name}: {len(rows)} rows, {len(cols)} cols")
    return rows, cols


# ─── Public API ───────────────────────────────────────────────────────────────

def explore_and_save(
    uri: str,
    db,
    engine,
    llm,
    dialect: str = "mysql",
    conn_id: int = None,
    app_db_session=None,
    ignored_tables: Optional[List[str]] = None,
) -> MemoryData:
    logger.info(f"[Memory] Exploring database (dialect={dialect})")
    tables      = _introspect(db, engine, dialect, ignored_tables=ignored_tables)
    db_summary, table_descs = _run_llm_summary(llm, tables, dialect)
    for t in tables:
        t.description = table_descs.get(t.name, "")

    data = MemoryData(
        dialect=dialect,
        db_summary=db_summary,
        tables=tables,
        explored_at=datetime.now(timezone.utc).isoformat(),
    )
    _save_to_db(conn_id, data, app_db_session)
    return data


def load_or_explore(
    uri: str,
    db,
    engine,
    llm,
    dialect: str = "mysql",
    conn_id: int = None,
    app_db_session=None,
    ignored_tables: Optional[List[str]] = None,
) -> MemoryData:
    cached = _load_from_db(conn_id, app_db_session)
    if cached:
        logger.debug(f"[Memory] Cache hit for conn_id={conn_id}")
        return cached
    logger.info("[Memory] No cache — running first-connect exploration")
    return explore_and_save(
        uri, db, engine, llm, dialect, conn_id, app_db_session,
        ignored_tables=ignored_tables,
    )


def get_schema_context(
    memory: MemoryData,
    question: str,
    max_tables: int = 12,
    ignored_tables: Optional[List[str]] = None,
) -> str:
    ignored_lower = {t.lower() for t in (ignored_tables or [])}

    candidate_tables = [
        t for t in memory.tables
        if t.name.lower() not in ignored_lower
    ]

    q = question.lower()
    scored: list[tuple[int, TableInfo]] = []
    for t in candidate_tables:
        score  = 0
        tokens = t.name.lower().replace("_", " ").split()
        for tok in tokens:
            if tok and tok in q:
                score += 3
        for col in t.columns:
            for tok in col.name.lower().replace("_", " ").split():
                if tok and len(tok) > 2 and tok in q:
                    score += 1
        if t.description and any(w in q for w in t.description.lower().split() if len(w) > 3):
            score += 2
        scored.append((score, t))

    scored.sort(key=lambda x: (-x[0], x[1].name))

    if not any(s > 0 for s, _ in scored):
        selected = sorted(candidate_tables, key=lambda t: -t.row_count)[:max_tables]
    else:
        selected = [t for _, t in scored[:max_tables]]

    parts = [
        f"DATABASE OVERVIEW: {memory.db_summary}",
        f"Dialect: {memory.dialect.upper()}",
        "",
        "RELEVANT TABLES:",
    ]
    TOKEN_BUDGET = 3000   # ~12k chars ≈ 3k tokens, safe for any model
    char_budget  = TOKEN_BUDGET * 4
    used         = 0

    for t in selected:
        desc  = f"  — {t.description}" if t.description else ""
        block = [f"\nTABLE {t.name} ({t.row_count:,} rows){desc}"]
        for col in t.columns:
            samples  = f"  e.g. {', '.join(repr(v) for v in col.sample_values[:2])}" if col.sample_values else ""
            nullable = "" if col.nullable else " NOT NULL"
            block.append(f"  {col.name}  {col.type}{nullable}{samples}")
        chunk = "\n".join(block)
        if used + len(chunk) > char_budget:
            parts.append(f"\n-- {len(selected) - selected.index(t)} more tables omitted (token budget) --")
            break
        parts.append(chunk)
        used += len(chunk)

    return "\n".join(parts)


def memory_summary_for_api(memory: MemoryData) -> dict:
    return {
        "dialect":     memory.dialect,
        "db_summary":  memory.db_summary,
        "explored_at": memory.explored_at,
        "table_count": len(memory.tables),
        "tables": [
            {
                "name":         t.name,
                "row_count":    t.row_count,
                "column_count": len(t.columns),
                "description":  t.description,
            }
            for t in memory.tables
        ],
    }