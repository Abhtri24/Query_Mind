"""
migrate_db.py
-------------
Database migration utility for QueryMind.
- Handles migrating 'uri' to 'uri_encrypted' by encrypting plaintext connections.
- Safely rebuilds/updates the table to drop the old 'uri' column to prevent NOT NULL constraint errors on inserts.
- Supports both SQLite and PostgreSQL.
"""

import sys
import logging
from sqlalchemy import inspect, text, Table, MetaData
from sqlalchemy.orm import Session
from config import cfg
from models import get_engine, Base, DBConnection
from crypto import encrypt, is_encrypted

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")

def run_migration():
    engine = get_engine()
    inspector = inspect(engine)
    
    if not inspector.has_table("nl2db_connections"):
        logger.info("Table 'nl2db_connections' does not exist. Creating tables from scratch...")
        from models import create_tables
        create_tables()
        logger.info("Fresh database tables created successfully.")
        return

    # Check if table already migrated by looking at columns
    columns = [c["name"] for c in inspector.get_columns("nl2db_connections")]
    logger.info(f"Existing columns in 'nl2db_connections': {columns}")

    # If the old 'uri' column exists, we perform a migration/rebuild
    if "uri" in columns:
        logger.info("Starting migration of 'uri' to 'uri_encrypted'...")
        
        # Read the existing connections data
        with Session(engine) as session:
            existing_rows = session.execute(text(
                "SELECT id, user_id, alias, dialect, uri, "
                "schema_memory_json, memory_explored_at, created_at "
                "FROM nl2db_connections"
            )).fetchall()
        
        logger.info(f"Found {len(existing_rows)} connections to migrate.")

        # Re-map/reconstruct data list
        migrated_data = []
        for row in existing_rows:
            raw_uri = row[4]
            # Encrypt URI if not already encrypted
            if raw_uri and not is_encrypted(raw_uri):
                uri_enc = encrypt(raw_uri)
            else:
                uri_enc = raw_uri or ""

            # schema_memory_json, memory_explored_at might be in the source database
            # if the previous half-failed run added them, else None
            schema_mem = None
            if "schema_memory_json" in columns:
                # get column index
                idx = columns.index("schema_memory_json")
                schema_mem = row[idx] if idx < len(row) else None
                
            explored_at = None
            if "memory_explored_at" in columns:
                idx = columns.index("memory_explored_at")
                explored_at = row[idx] if idx < len(row) else None

            migrated_data.append({
                "id": row[0],
                "user_id": row[1],
                "alias": row[2],
                "dialect": row[3],
                "uri_encrypted": uri_enc,
                "schema_memory_json": schema_mem,
                "memory_explored_at": explored_at,
                "created_at": row[7] if len(row) > 7 else None
            })

        # To drop the 'uri' column, the safest cross-DB way is:
        # 1. Rename existing table
        # 2. Create new tables via SQLAlchemy Base.metadata.create_all
        # 3. Copy records over
        # 4. Drop the renamed old table
        logger.info("Renaming existing table to temporary name...")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE nl2db_connections RENAME TO _nl2db_connections_old"))
        
        logger.info("Creating new table structure...")
        from models import create_tables
        create_tables()

        logger.info("Inserting migrated records...")
        with Session(engine) as session:
            for item in migrated_data:
                # Since id is auto-increment but we want to preserve IDs, insert them explicitly
                session.execute(text(
                    "INSERT INTO nl2db_connections (id, user_id, alias, dialect, uri_encrypted, schema_memory_json, memory_explored_at, created_at) "
                    "VALUES (:id, :user_id, :alias, :dialect, :uri_encrypted, :schema_memory_json, :memory_explored_at, :created_at)"
                ), item)
            session.commit()

        logger.info("Dropping temporary old table...")
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE _nl2db_connections_old"))
        logger.info("Migration table rebuild complete.")
    else:
        logger.info("Column 'uri' not found. Checking if any other columns need to be added...")
        # Just in case columns were missing but 'uri' wasn't there
        from models import create_tables
        create_tables()
        
    logger.info("Checking for any unencrypted connection strings in 'uri_encrypted'...")
    with Session(engine) as session:
        rows = session.execute(text("SELECT id, uri_encrypted FROM nl2db_connections WHERE uri_encrypted IS NOT NULL")).fetchall()
        updated = False
        for r_id, val in rows:
            if val and not is_encrypted(val):
                enc_val = encrypt(val)
                session.execute(
                    text("UPDATE nl2db_connections SET uri_encrypted = :enc WHERE id = :id"),
                    {"enc": enc_val, "id": r_id}
                )
                logger.info(f"Encrypted plaintext connection string in 'uri_encrypted' for ID {r_id}")
                updated = True
        if updated:
            session.commit()
            logger.info("Unencrypted connections encrypted.")
        else:
            logger.info("All connection strings in 'uri_encrypted' are secure.")

    logger.info("Ensuring all other database tables are up to date...")
    from models import create_tables
    create_tables()
    logger.info("Database migration successfully finished.")

if __name__ == "__main__":
    run_migration()
