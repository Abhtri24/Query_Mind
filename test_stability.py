import unittest
import json
import datetime
from decimal import Decimal
from flask import Flask, g
from app import create_app
from models import get_db_session, User, DBConnection, ChatSession, SessionHistory, create_tables
from history import _get_history, _append_history, _clear_history

class TestStabilityAndHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create Flask app in test mode
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        
        # Force rebuilding engine with test URI
        import models
        models._engine = None
        
        # Run create_tables inside app context
        with cls.app.app_context():
            create_tables()

    @classmethod
    def tearDownClass(cls):
        # Reset engine
        import models
        models._engine = None

    def setUp(self):
        # Clean up database tables for each test inside a temporary context
        with self.app.app_context():
            s = get_db_session()
            s.rollback()
            s.query(SessionHistory).delete()
            s.query(ChatSession).delete()
            s.query(DBConnection).delete()
            s.query(User).delete()
            s.commit()

    def test_session_lifecycle_managed_by_context(self):
        # Verify that get_db_session reuse session within context
        with self.app.app_context():
            s1 = get_db_session()
            s2 = get_db_session()
            self.assertIs(s1, s2, "Should return same session within one context")
            self.assertTrue(s1.is_active)

        # Outside request context, new sessions should be created
        with self.app.app_context():
            s3 = get_db_session()
            self.assertIsNot(s1, s3, "Should return different session for a new context")

    def test_session_teardown_closes_session(self):
        # Verify that when app context ends, the session is removed and closed
        ctx = self.app.app_context()
        ctx.push()
        try:
            db_sess = get_db_session()
            self.assertTrue(db_sess.is_active)
            self.assertIn('db_session', g)
        finally:
            ctx.pop() # This triggers teardown_appcontext

        # Pushing a new context should not have 'db_session' in g
        with self.app.app_context():
            self.assertNotIn('db_session', g)

    def test_shared_history_fallback(self):
        with self.app.app_context():
            # Test history serialization and Redis/DB fallback
            session_id = "test-session-123"
            _clear_history(session_id)

            # Append messages
            _append_history(session_id, "What is the capital of France?", "Paris")

            # Retrieve history
            history = _get_history(session_id)
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0].content, "What is the capital of France?")
            self.assertEqual(history[1].content, "Paris")

            # Let's check that it saved in DB/Redis fallback
            s = get_db_session()
            row = s.get(SessionHistory, session_id)
            # It should be either in Redis or DB. Let's make sure it retrieves correctly.
            retrieved = _get_history(session_id)
            self.assertEqual(len(retrieved), 2)

            # Append more to test keeping last 5 turns (10 messages)
            for i in range(10):
                _append_history(session_id, f"Q{i}", f"A{i}")

            history_limit = _get_history(session_id)
            self.assertEqual(len(history_limit), 10, "Should cap at last 10 messages (5 turns)")
            self.assertEqual(history_limit[-2].content, "Q9")
            self.assertEqual(history_limit[-1].content, "A9")

            _clear_history(session_id)
            self.assertEqual(len(_get_history(session_id)), 0)

    def test_result_serialization_custom_types(self):
        # Test serialization of date, datetime, decimal, and bytes
        from sqlalchemy import create_engine, Table, MetaData, Column, Integer, String, Date, DateTime, Numeric, LargeBinary
        engine = create_engine("sqlite:///:memory:")
        metadata = MetaData()
        
        test_table = Table(
            "test_types", metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String),
            Column("created_date", Date),
            Column("created_time", DateTime),
            Column("price", Numeric),
            Column("data", LargeBinary)
        )
        metadata.create_all(engine)

        # Insert some test data
        now = datetime.datetime(2026, 7, 10, 12, 0, 0)
        today = datetime.date(2026, 7, 10)
        with engine.begin() as conn:
            conn.execute(
                test_table.insert(),
                [
                    {
                        "id": 1,
                        "name": "Widget A",
                        "created_date": today,
                        "created_time": now,
                        "price": Decimal("19.99"),
                        "data": b"hello binary"
                    }
                ]
            )

        # Query using connection execute
        from sqlalchemy import text
        with engine.connect() as conn:
            res = conn.execute(text("SELECT * FROM test_types"))
            cols = list(res.keys())
            db_rows = res.fetchall()

            def serialise_value(val):
                if isinstance(val, (datetime.datetime, datetime.date)):
                    return val.isoformat()
                if isinstance(val, Decimal):
                    return float(val)
                if isinstance(val, bytes):
                    return val.decode('utf-8', errors='replace')
                return val

            result = []
            for row in db_rows:
                row_dict = {}
                for i, col in enumerate(cols):
                    row_dict[col] = serialise_value(row[i])
                result.append(row_dict)

            # Assert correct serialization format
            self.assertEqual(len(result), 1)
            item = result[0]
            self.assertEqual(item["id"], 1)
            self.assertEqual(item["name"], "Widget A")
            self.assertTrue(item["created_date"].startswith("2026-07-10"))
            self.assertTrue("2026-07-10" in item["created_time"])
            self.assertEqual(item["price"], 19.99)
            self.assertEqual(item["data"], "hello binary")

    def test_pagination_parsing(self):
        with self.app.app_context():
            s = get_db_session()
            
            # We need a user to authenticate connection queries
            user = User(username="test_paginator", hashed_password="hashed_dummy_password")
            s.add(user)
            s.commit()

            # Add 5 dummy connections
            for i in range(5):
                conn = DBConnection(
                    user_id=user.id,
                    alias=f"conn_{i}",
                    dialect="sqlite",
                    uri_encrypted=f"encrypted_uri_{i}"
                )
                s.add(conn)
            s.commit()

            # Let's query pagination logic inside list_connections manually by simulating it
            def paginate_connections(page, limit):
                offset = (page - 1) * limit
                return (
                    s.query(DBConnection)
                     .filter_by(user_id=user.id)
                     .order_by(DBConnection.id.desc())
                     .offset(offset)
                     .limit(limit)
                     .all()
                )

            # Page 1, limit 2 -> first 2 connections (newest first, so conn_4, conn_3)
            p1 = paginate_connections(page=1, limit=2)
            self.assertEqual(len(p1), 2)
            self.assertEqual(p1[0].alias, "conn_4")
            self.assertEqual(p1[1].alias, "conn_3")

            # Page 2, limit 2 -> next 2 connections (conn_2, conn_1)
            p2 = paginate_connections(page=2, limit=2)
            self.assertEqual(len(p2), 2)
            self.assertEqual(p2[0].alias, "conn_2")
            self.assertEqual(p2[1].alias, "conn_1")

            # Page 3, limit 2 -> last 1 connection (conn_0)
            p3 = paginate_connections(page=3, limit=2)
            self.assertEqual(len(p3), 1)
            self.assertEqual(p3[0].alias, "conn_0")

if __name__ == "__main__":
    unittest.main()
