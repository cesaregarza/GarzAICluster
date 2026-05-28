import unittest

from scripts import provision_agent_control_plane_readonly_sql as provision


class RedactionTests(unittest.TestCase):
    def test_redacts_password_literals_and_postgres_urls(self) -> None:
        message = (
            "ALTER ROLE app WITH LOGIN PASSWORD 'super-secret'\n"
            "psql postgresql://user:pass@example.internal:25060/bots?sslmode=require failed"
        )

        redacted = provision._redact_secret_literals(message)

        self.assertNotIn("super-secret", redacted)
        self.assertNotIn("user:pass@example", redacted)
        self.assertIn("PASSWORD '<redacted>'", redacted)
        self.assertIn("postgresql://<redacted>", redacted)


if __name__ == "__main__":
    unittest.main()
