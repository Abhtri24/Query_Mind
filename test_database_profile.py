import unittest
from unittest.mock import patch

from agent_optimized import _build_prompt
from app import create_app
from models import ChatSession, DBConnection, SessionHistory, User, create_tables, get_db_session
from profile_context import build_profile_context


class TestDatabaseProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

        import models
        models._engine = None

        with cls.app.app_context():
            create_tables()

    @classmethod
    def tearDownClass(cls):
        import models
        models._engine = None

    def setUp(self):
        with self.app.app_context():
            s = get_db_session()
            s.rollback()
            s.query(SessionHistory).delete()
            s.query(ChatSession).delete()
            s.query(DBConnection).delete()
            s.query(User).delete()
            s.commit()

    def test_create_and_list_connection_profile_fields(self):
        client = self.app.test_client()
        signup = client.post(
            "/auth/signup",
            json={"username": "profile_user", "password": "secret123"},
        )
        self.assertEqual(signup.status_code, 201)

        payload = {
            "alias": "books",
            "uri": "sqlite:///books.db",
            "description": "Book recommendation platform.",
            "business_context": "Readers browse books and interact with snippets.",
            "glossary": {"Customer": "Reader", "Order": "Purchase"},
            "important_tables": ["books", "authors"],
            "ignored_tables": ["alembic_version", "logs"],
        }
        with patch("app.get_db_from_uri") as mocked_get_db:
            mocked_get_db.return_value = (object(), object())
            created = client.post("/connections", json=payload)

        self.assertEqual(created.status_code, 201)
        created_json = created.get_json()
        self.assertEqual(created_json["description"], payload["description"])
        self.assertEqual(created_json["business_context"], payload["business_context"])
        self.assertEqual(created_json["glossary"], payload["glossary"])
        self.assertEqual(created_json["important_tables"], payload["important_tables"])
        self.assertEqual(created_json["ignored_tables"], payload["ignored_tables"])

        listed = client.get("/connections")
        self.assertEqual(listed.status_code, 200)
        listed_json = listed.get_json()
        self.assertEqual(len(listed_json), 1)
        self.assertEqual(listed_json[0]["glossary"], payload["glossary"])
        self.assertEqual(listed_json[0]["important_tables"], payload["important_tables"])
        self.assertEqual(listed_json[0]["ignored_tables"], payload["ignored_tables"])

    def test_profile_validation_rejects_invalid_shapes(self):
        client = self.app.test_client()
        client.post("/auth/signup", json={"username": "invalid_profile", "password": "secret123"})

        with patch("app.get_db_from_uri") as mocked_get_db:
            mocked_get_db.return_value = (object(), object())
            response = client.post(
                "/connections",
                json={
                    "alias": "bad",
                    "uri": "sqlite:///bad.db",
                    "glossary": ["Customer", "Reader"],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("glossary must be an object", response.get_json()["error"])

    def test_profile_context_omits_empty_fields_and_formats_prompt_block(self):
        profile = {
            "description": "Book recommendation platform.",
            "business_context": "Readers browse books and interact with snippets.",
            "glossary": {"Customer": "Reader", "Order": "Purchase", "": "ignored"},
            "important_tables": ["books", "authors", ""],
            "ignored_tables": ["logs", "alembic_version"],
        }

        context = build_profile_context(profile)

        self.assertIn("Database Description:\nBook recommendation platform.", context)
        self.assertIn("Business Context:\nReaders browse books and interact with snippets.", context)
        self.assertIn("Customer = Reader", context)
        self.assertIn("Order = Purchase", context)
        self.assertIn("Important Tables:\nbooks\nauthors", context)
        self.assertIn("Ignored Tables:\nlogs\nalembic_version", context)
        self.assertNotIn("ignored", context)

        prompt = _build_prompt("CREATE TABLE books (id INTEGER);", "sqlite", profile_context=context)
        self.assertLess(prompt.find("DATABASE PROFILE:"), prompt.find("DATABASE SCHEMA:"))
        self.assertIn("Customer = Reader", prompt)

    def test_empty_profile_context_is_backwards_compatible(self):
        self.assertEqual(build_profile_context({}), "")
        prompt = _build_prompt("CREATE TABLE books (id INTEGER);", "sqlite")
        self.assertNotIn("DATABASE PROFILE:", prompt)


if __name__ == "__main__":
    unittest.main()
