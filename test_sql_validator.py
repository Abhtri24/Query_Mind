import unittest
from agent_optimized import validate_sql

class TestSQLValidator(unittest.TestCase):
    def test_valid_selects(self):
        # MySQL dialect
        ok, reason = validate_sql("SELECT `id`, `name` FROM `users` WHERE `name` LIKE '%admin%' LIMIT 10", dialect="mysql")
        self.assertTrue(ok, f"Should accept valid MySQL query. Reason: {reason}")
        self.assertEqual(reason, "ok")

        # Postgres dialect (case insensitive ILIKE, casts)
        ok, reason = validate_sql("SELECT id, name FROM users WHERE name ILIKE '%admin%' AND created_at > '2023-01-01'::date LIMIT 10;", dialect="postgresql")
        self.assertTrue(ok, f"Should accept valid Postgres query. Reason: {reason}")
        self.assertEqual(reason, "ok")

        # SQLite dialect
        ok, reason = validate_sql("SELECT id, name FROM users WHERE LOWER(name) = 'admin' LIMIT 10", dialect="sqlite")
        self.assertTrue(ok, f"Should accept valid SQLite query. Reason: {reason}")
        self.assertEqual(reason, "ok")

        # CTEs (WITH)
        ok, reason = validate_sql("WITH cte AS (SELECT 1 AS val) SELECT val FROM cte;", dialect="mysql")
        self.assertTrue(ok, f"Should accept CTE. Reason: {reason}")
        self.assertEqual(reason, "ok")

        # UNION queries
        ok, reason = validate_sql("SELECT 1 UNION SELECT 2", dialect="mysql")
        self.assertTrue(ok, f"Should accept UNION query. Reason: {reason}")
        self.assertEqual(reason, "ok")

        # Subqueries
        ok, reason = validate_sql("SELECT id, (SELECT name FROM profiles p WHERE p.user_id = u.id) FROM users u", dialect="mysql")
        self.assertTrue(ok, f"Should accept subquery. Reason: {reason}")
        self.assertEqual(reason, "ok")

        # Valid SELECT ended with a trailing comment
        ok, reason = validate_sql("SELECT 1; -- comments are fine", dialect="mysql")
        self.assertTrue(ok, f"Should accept SELECT with comment. Reason: {reason}")
        self.assertEqual(reason, "ok")

    def test_exactly_one_statement(self):
        # Multiple statements
        ok, reason = validate_sql("SELECT 1; SELECT 2;", dialect="mysql")
        self.assertFalse(ok, "Should reject multiple statements")
        self.assertIn("Only one SQL statement is allowed", reason)

        ok, reason = validate_sql("SELECT 1; DROP TABLE users;", dialect="mysql")
        self.assertFalse(ok, "Should reject query with appended drop")
        self.assertIn("Only one SQL statement is allowed", reason)

        # Empty/whitespace statements
        ok, reason = validate_sql("", dialect="mysql")
        self.assertFalse(ok, "Should reject empty query")
        self.assertEqual(reason, "Empty query")

        ok, reason = validate_sql("   ", dialect="mysql")
        self.assertFalse(ok, "Should reject whitespace query")
        self.assertEqual(reason, "Empty query")

        ok, reason = validate_sql("-- comment only", dialect="mysql")
        self.assertFalse(ok, "Should reject comment-only query")
        self.assertEqual(reason, "Empty query")

        ok, reason = validate_sql(";", dialect="mysql")
        self.assertFalse(ok, "Should reject semicolon-only query")
        self.assertEqual(reason, "Empty query")

    def test_reject_dml_ddl_and_unsafe(self):
        unsafe_cases = [
            ("INSERT INTO users (name) VALUES ('admin');", "Insert"),
            ("UPDATE users SET name = 'admin' WHERE id = 1;", "Update"),
            ("DELETE FROM users WHERE id = 1;", "Delete"),
            ("DROP TABLE users;", "Drop"),
            ("ALTER TABLE users ADD COLUMN age INT;", "Alter"),
            ("CREATE TABLE users (id INT);", "Create"),
            ("TRUNCATE TABLE users;", "TruncateTable"),
            ("GRANT SELECT ON users TO admin;", "Grant"),
            ("REVOKE SELECT ON users FROM admin;", "Revoke"),
            ("EXEC my_proc;", "Command"),
            ("EXECUTE my_proc;", "Command"),
            ("CALL my_proc();", "Command"),
            ("BEGIN TRANSACTION;", "Transaction"),
            ("COMMIT;", "Commit"),
            ("ROLLBACK;", "Rollback"),
            ("WITH deleted AS (DELETE FROM users RETURNING *) SELECT * FROM deleted;", "Delete"),
        ]

        for sql, expected_node in unsafe_cases:
            ok, reason = validate_sql(sql, dialect="postgresql")
            self.assertFalse(ok, f"Should reject query: '{sql}'")
            self.assertTrue(
                "Only SELECT statements are allowed" in reason or "Blocked modification or command node" in reason or "Empty query" in reason,
                f"Unexpected failure reason: {reason}"
            )

    def test_unparsable_sql(self):
        invalid_cases = [
            "SELECT * FROM",
            "SELECT * FROM WHERE id = 1",
            "SELECT (1",
            "SELECT 1 FROM WHERE",
        ]

        for sql in invalid_cases:
            ok, reason = validate_sql(sql, dialect="mysql")
            self.assertFalse(ok, f"Should reject invalid query: '{sql}'")
            self.assertIn("Unparsable SQL", reason)

if __name__ == "__main__":
    unittest.main()
