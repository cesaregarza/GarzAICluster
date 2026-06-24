import argparse
import unittest
from unittest import mock

from scripts import provision_agent_control_plane_readonly_sql as provision


class IdentifierTests(unittest.TestCase):
    def test_validates_schema_qualified_relation(self) -> None:
        self.assertEqual(
            provision.validate_relation_identifier("public.accounts"),
            ("public", "accounts"),
        )

    def test_rejects_unqualified_relation(self) -> None:
        with self.assertRaises(SystemExit):
            provision.validate_relation_identifier("accounts")

    def test_splits_comma_separated_env_lists(self) -> None:
        self.assertEqual(
            provision.split_env_list("public.accounts, common.orders ,,"),
            ["public.accounts", "common.orders"],
        )


class TargetConfigTests(unittest.TestCase):
    def test_default_target_preserves_bots_secret_key(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            config = provision.resolve_config(
                argparse.Namespace(
                    target="bots",
                    admin_url="postgresql://admin@example/bots",
                    database=None,
                    db_host=None,
                    db_user=None,
                    db_password="pw",
                    schemas=None,
                    relations=["common.channel_names"],
                    secret_file=provision.DEFAULT_SECRET_FILE,
                    secret_key=None,
                    skip_db=True,
                )
            )

        self.assertEqual(config.database, "bots")
        self.assertEqual(config.role, "agent_control_plane_readonly_sql")
        self.assertEqual(config.secret_key, "AGENT_PLATFORM_READONLY_SQL_DATABASE_URL")
        self.assertEqual(config.relations, ["common.channel_names"])

    def test_xscraper_target_uses_analytical_defaults(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"XSCRAPER_DB_ADMIN_URL": "postgresql://admin@example/xscraper"},
            clear=True,
        ):
            config = provision.resolve_config(
                argparse.Namespace(
                    target="xscraper_analytical",
                    admin_url=None,
                    database=None,
                    db_host=None,
                    db_user=None,
                    db_password=None,
                    schemas=None,
                    relations=None,
                    secret_file=provision.DEFAULT_SECRET_FILE,
                    secret_key=None,
                    skip_db=False,
                )
            )

        self.assertEqual(config.admin_url, "postgresql://admin@example/xscraper")
        self.assertEqual(config.database, "xscraper")
        self.assertEqual(config.role, "agent_control_plane_xscraper_readonly_sql")
        self.assertEqual(
            config.secret_key,
            "AGENT_PLATFORM_READONLY_SQL_ANALYTICAL_DATABASE_URL",
        )
        self.assertEqual(
            tuple(config.relations),
            provision.XSCRAPER_ANALYTICAL_RELATIONS,
        )


class RoleSqlTests(unittest.TestCase):
    def test_build_role_sql_uses_explicit_relation_grants(self) -> None:
        sql = provision.build_role_sql(
            "bots",
            "agent_control_plane_readonly_sql",
            "secret",
            ["public", "common"],
            [("public", "accounts"), ("common", "orders")],
        )

        self.assertIn("REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC", sql)
        self.assertIn("REVOKE TEMPORARY ON DATABASE %I FROM %I", sql)
        self.assertIn("ALTER ROLE %I IN DATABASE %I SET search_path = %s", sql)
        self.assertIn("GRANT SELECT ON TABLE %I.%I TO %I", sql)
        self.assertIn("approved read-only SQL view must be security_invoker=true", sql)
        self.assertIn("read-only SQL role can SELECT an unapproved relation", sql)
        self.assertNotIn("GRANT SELECT ON ALL TABLES", sql)
        self.assertNotIn("GRANT SELECT ON ALL SEQUENCES", sql)
        self.assertIn("('public', 'accounts')", sql)
        self.assertIn("('common', 'orders')", sql)

    def test_write_like_relation_check_excludes_system_schemas(self) -> None:
        sql = provision.build_role_sql(
            "bots",
            "agent_control_plane_readonly_sql",
            "password",
            ["common"],
            [("common", "channel_names")],
        )

        write_check = sql.split(
            "RAISE EXCEPTION 'read-only SQL role has write-like relation privileges'",
            maxsplit=1,
        )[0].rsplit("IF EXISTS", maxsplit=1)[1]

        self.assertIn("ns.nspname <> 'information_schema'", write_check)
        self.assertIn("ns.nspname NOT LIKE 'pg\\_%'", write_check)


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
