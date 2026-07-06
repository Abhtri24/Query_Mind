"""
schema_memory.py
----------------
Persistent schema memory — stored in the nl2db_connections.schema_memory_json
column (survives deploys, works on Render/Railway ephemeral filesystems).

Falls back to filesystem cache for local dev if DB column is unavailable.

Public API (unchanged from previous version):
    load_or_explore(uri, db, engine, llm, dialect, conn_id, app_db_session)
    explore_and_save(uri, db, engine, llm, dialect, conn_id, app_db_session)
    get_schema_context(memory, question)
    memory_summary_for_api(memory)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SAMPLE_ROWS = 3
MAX_TABLES      = 60
MAX_COLS_SHOWN  = 40


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


# ─── Introspection ────────────────────────────────────────────────────────────

def _introspect(db, engine, dialect: str) -> List[TableInfo]:
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(engine)
    table_names = inspector.get_table_names()[:MAX_TABLES]
    tables: List[TableInfo] = []

    with engine.connect() as conn:
        for tname in table_names:
            try:
                raw_cols = inspector.get_columns(tname)
            except Exception:
                raw_cols = []

            columns: List[ColumnInfo] = []
            for col in raw_cols[:MAX_COLS_SHOWN]:
                col_info = ColumnInfo(
                    name=col["name"],
                    type=str(col["type"]),
                    nullable=col.get("nullable", True),
                )
                try:
                    # Quote column and table names safely
                    q = text(f'SELECT "{col["name"]}" FROM "{tname}" WHERE "{col["name"]}" IS NOT NULL LIMIT {MAX_SAMPLE_ROWS}')
                    rows = conn.execute(q).fetchall()
                    col_info.sample_values = [str(r[0])[:80] for r in rows]
                except Exception:
                    pass
                columns.append(col_info)

            row_count = 0
            try:
                row_count = conn.execute(text(f'SELECT COUNT(*) FROM "{tname}"')).scalar() or 0
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
        'Required shape:',
        '{',
        '  "db_summary": "One paragraph: what is this database about?",',
        '  "tables": [{"name": "<table>", "description": "One sentence."}, ...]',
        '}',
    ]
    return "\n".join(lines)


def _run_llm_summary(llm, tables: List[TableInfo], dialect: str) -> tuple[str, Dict[str, str]]:
    from langchain_core.messages import HumanMessage

    prompt = _build_exploration_prompt(tables, dialect)
    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content if hasattr(response, "content") else str(response)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)
        db_summary = parsed.get("db_summary", "")
        table_descs = {t["name"]: t.get("description", "") for t in parsed.get("tables", [])}
        return db_summary, table_descs
    except Exception as e:
        logger.warning(f"[Memory] LLM summary failed: {e}")
        return "Schema memory collected but LLM summarisation failed.", {}


# ─── Public API ───────────────────────────────────────────────────────────────

def explore_and_save(
    uri: str,
    db,
    engine,
    llm,
    dialect: str = "mysql",
    conn_id: int = None,
    app_db_session=None,
) -> MemoryData:
    logger.info(f"[Memory] Exploring database (dialect={dialect})")
    tables = _introspect(db, engine, dialect)
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
) -> MemoryData:
    cached = _load_from_db(conn_id, app_db_session)
    if cached:
        logger.debug(f"[Memory] Cache hit for conn_id={conn_id}")
        return cached
    logger.info("[Memory] No cache — running first-connect exploration")
    return explore_and_save(uri, db, engine, llm, dialect, conn_id, app_db_session)


def get_schema_context(memory: MemoryData, question: str, max_tables: int = 12) -> str:
    q = question.lower()
    scored: list[tuple[int, TableInfo]] = []
    for t in memory.tables:
        score = 0
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
    selected = [t for _, t in scored[:max_tables]]
    if not any(s > 0 for s, _ in scored):
        selected = [t for _, t in sorted(scored, key=lambda x: -x[1].row_count)[:max_tables]]

    parts = [
        f"DATABASE OVERVIEW: {memory.db_summary}",
        f"Dialect: {memory.dialect.upper()}",
        "",
        "RELEVANT TABLES:",
    ]
    for t in selected:
        desc = f"  — {t.description}" if t.description else ""
        parts.append(f"\nTABLE {t.name} ({t.row_count:,} rows){desc}")
        for col in t.columns:
            samples = f"  e.g. {', '.join(repr(v) for v in col.sample_values[:2])}" if col.sample_values else ""
            nullable = "" if col.nullable else " NOT NULL"
            parts.append(f"  {col.name}  {col.type}{nullable}{samples}")

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