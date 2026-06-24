#!/usr/bin/env python3
"""Provision the Agent Control Plane read-only SQL broker secret."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import string
import subprocess
import sys
import textwrap
from pathlib import Path
from shutil import which
from urllib.parse import quote, urlparse, urlunparse


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRET_FILE = (
    REPO_ROOT / "secrets" / "agent-control-plane" / "runtime-secret.enc.yaml"
)
DEFAULT_SOPS_KEY = REPO_ROOT / "keys" / "age-private.txt"


def load_env_file() -> None:
    env_path = Path(os.environ.get("SPLATTOPCONFIG_ENV_FILE", REPO_ROOT / ".env"))
    if not env_path.exists():
        return

    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a weak read-only DB role and add its URL to the encrypted "
            "Agent Control Plane runtime secret."
        )
    )
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("AGENT_CONTROL_PLANE_DB_ADMIN_URL")
        or os.environ.get("BOT_DB_ADMIN_URL"),
        help="Postgres admin URL. Defaults to AGENT_CONTROL_PLANE_DB_ADMIN_URL or BOT_DB_ADMIN_URL.",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("AGENT_CONTROL_PLANE_DB_NAME")
        or os.environ.get("BOT_DB_NAME", "bots"),
    )
    parser.add_argument(
        "--db-host",
        default=os.environ.get("AGENT_CONTROL_PLANE_DB_HOST") or os.environ.get("BOT_DB_HOST"),
        help="Host for the emitted connection string. Defaults to private-<admin host>.",
    )
    parser.add_argument("--db-user", default="agent_control_plane_readonly_sql")
    parser.add_argument(
        "--db-password",
        default=os.environ.get("AGENT_CONTROL_PLANE_READONLY_SQL_PASSWORD"),
    )
    parser.add_argument(
        "--schema",
        action="append",
        dest="schemas",
        default=None,
        help=(
            "Schema to include in the role search_path. Repeatable. Defaults to "
            "the schemas from --relation."
        ),
    )
    parser.add_argument(
        "--relation",
        action="append",
        dest="relations",
        default=None,
        help=(
            "Approved table/view to grant SELECT on, as schema.name. Repeatable. "
            "May also be provided as AGENT_CONTROL_PLANE_READONLY_SQL_RELATIONS "
            "with a comma-separated list."
        ),
    )
    parser.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET_FILE)
    parser.add_argument("--skip-db", action="store_true")
    return parser.parse_args()


def ensure_command(name: str) -> None:
    if which(name) is None:
        sys.exit(f"{name} not found in PATH.")


def validate_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        sys.exit(f"{label} must be a valid Postgres identifier.")
    return value


def validate_relation_identifier(value: str) -> tuple[str, str]:
    parts = value.split(".")
    if len(parts) != 2:
        sys.exit("relation must be schema.name.")
    return (
        validate_identifier(parts[0], "relation schema"),
        validate_identifier(parts[1], "relation name"),
    )


def split_env_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def generate_password(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def admin_url_for_db(admin_url: str, database: str) -> str:
    parsed = urlparse(admin_url)
    if not parsed.scheme.startswith("postgres"):
        sys.exit("Admin URL must be a postgres:// or postgresql:// connection string.")

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{database}",
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def derive_private_host(host: str, prefix: str = "private-") -> str:
    return host if host.startswith(prefix) else f"{prefix}{host}"


def build_connection_string(
    admin_url: str,
    username: str,
    password: str,
    database: str,
    host_override: str | None,
) -> str:
    parsed = urlparse(admin_url)
    if not parsed.scheme.startswith("postgres"):
        sys.exit("Admin URL must be a postgres:// or postgresql:// connection string.")
    if not parsed.hostname:
        sys.exit("Unable to determine host from admin URL.")

    host = host_override or derive_private_host(parsed.hostname)
    port = f":{parsed.port}" if parsed.port else ""
    hostpart = host if ":" not in host else f"[{host}]"
    netloc = f"{quote(username)}:{quote(password)}@{hostpart}{port}"

    return urlunparse(
        (
            "postgresql",
            netloc,
            f"/{database}",
            "",
            parsed.query or "sslmode=require",
            "",
        )
    )


def run_sql(
    admin_url: str,
    database: str,
    role: str,
    password: str,
    schemas: list[str],
    relations: list[tuple[str, str]],
) -> None:
    sql = build_role_sql(database, role, password, schemas, relations)
    result = subprocess.run(
        ["psql", admin_url_for_db(admin_url, database), "-q"],
        input=sql,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(_redact_secret_literals(result.stderr))
        sys.exit("Postgres read-only role provisioning failed.")


def build_role_sql(
    database: str,
    role: str,
    password: str,
    schemas: list[str],
    relations: list[tuple[str, str]],
) -> str:
    schema_values = ", ".join(sql_literal(schema) for schema in schemas)
    relation_values = ",\n                ".join(
        f"({sql_literal(schema)}, {sql_literal(relation)})"
        for schema, relation in relations
    )
    sql = textwrap.dedent(
        f"""
        \\set ON_ERROR_STOP on

        DO
        $$
        DECLARE
            role_name text := '{role}';
            role_password text := {sql_literal(password)};
            db_name text := {sql_literal(database)};
            target_schema text;
            target_relation record;
            search_path_sql text;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('CREATE ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            ELSE
                EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles rol
                WHERE rol.rolname = role_name
                  AND (
                    rol.rolsuper
                    OR rol.rolcreatedb
                    OR rol.rolcreaterole
                    OR rol.rolreplication
                    OR rol.rolbypassrls
                  )
            ) THEN
                RAISE EXCEPTION 'read-only SQL role has forbidden attributes';
            END IF;

            EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', role_name);
            EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', role_name, '1000ms');
            SELECT string_agg(format('%I', schema_name), ', ')
              INTO search_path_sql
            FROM unnest(ARRAY[{schema_values}]) AS configured(schema_name);
            EXECUTE format(
                'ALTER ROLE %I IN DATABASE %I SET search_path = %s',
                role_name,
                db_name,
                search_path_sql
            );
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', db_name, role_name);
            EXECUTE format('REVOKE CREATE ON DATABASE %I FROM %I', db_name, role_name);
            EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', db_name);
            EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM %I', db_name, role_name);
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, role_name);

            FOREACH target_schema IN ARRAY ARRAY[{schema_values}]
            LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.schemata s
                    WHERE s.schema_name = target_schema
                ) THEN
                    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I', target_schema, role_name);
                    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', target_schema, role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM %I', target_schema, role_name);
                    EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM %I', target_schema, role_name);
                    EXECUTE format('REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM %I', target_schema, role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON TABLES FROM %I', target_schema, role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM %I', target_schema, role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE EXECUTE ON FUNCTIONS FROM %I', target_schema, role_name);
                ELSE
                    RAISE EXCEPTION 'approved read-only SQL schema does not exist: %', target_schema;
                END IF;
            END LOOP;

            FOR target_relation IN
                SELECT *
                FROM (VALUES
                {relation_values}
                ) AS approved(schema_name, relation_name)
            LOOP
                EXECUTE format(
                    'GRANT SELECT ON TABLE %I.%I TO %I',
                    target_relation.schema_name,
                    target_relation.relation_name,
                    role_name
                );
            END LOOP;

            IF EXISTS (
                SELECT 1
                FROM (VALUES
                {relation_values}
                ) AS approved(schema_name, relation_name)
                JOIN pg_catalog.pg_namespace ns
                  ON ns.nspname = approved.schema_name
                JOIN pg_catalog.pg_class cls
                  ON cls.relnamespace = ns.oid
                 AND cls.relname = approved.relation_name
                WHERE cls.relkind = 'v'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM pg_options_to_table(cls.reloptions) opt
                    WHERE opt.option_name = 'security_invoker'
                      AND opt.option_value = 'true'
                  )
            ) THEN
                RAISE EXCEPTION 'approved read-only SQL view must be security_invoker=true';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class cls
                JOIN pg_catalog.pg_namespace ns
                  ON ns.oid = cls.relnamespace
                WHERE cls.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
                  AND ns.nspname <> 'information_schema'
                  AND ns.nspname NOT LIKE 'pg\\_%'
                  AND has_table_privilege(role_name, cls.oid, 'SELECT')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM (VALUES
                    {relation_values}
                    ) AS approved(schema_name, relation_name)
                    WHERE approved.schema_name = ns.nspname
                      AND approved.relation_name = cls.relname
                  )
            ) THEN
                RAISE EXCEPTION 'read-only SQL role can SELECT an unapproved relation';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class cls
                JOIN pg_catalog.pg_namespace ns
                  ON ns.oid = cls.relnamespace
                WHERE cls.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
                  AND ns.nspname <> 'information_schema'
                  AND ns.nspname NOT LIKE 'pg\\_%'
                  AND (
                    has_table_privilege(role_name, cls.oid, 'INSERT')
                    OR has_table_privilege(role_name, cls.oid, 'UPDATE')
                    OR has_table_privilege(role_name, cls.oid, 'DELETE')
                    OR has_table_privilege(role_name, cls.oid, 'TRUNCATE')
                    OR has_table_privilege(role_name, cls.oid, 'REFERENCES')
                    OR has_table_privilege(role_name, cls.oid, 'TRIGGER')
                  )
            ) THEN
                RAISE EXCEPTION 'read-only SQL role has write-like relation privileges';
            END IF;
        END
        $$;
        """
    )
    return sql


def _redact_secret_literals(value: str) -> str:
    value = re.sub(r"PASSWORD\s+'[^']+'", "PASSWORD '<redacted>'", value)
    return re.sub(r"postgres(?:ql)?://\S+", "postgresql://<redacted>", value)


def set_encrypted_secret(secret_file: Path, database_url: str) -> None:
    ensure_command("sops")
    env = os.environ.copy()
    if "SOPS_AGE_KEY_FILE" not in env and DEFAULT_SOPS_KEY.exists():
        env["SOPS_AGE_KEY_FILE"] = str(DEFAULT_SOPS_KEY)
    result = subprocess.run(
        [
            "sops",
            "--set",
            '["stringData"]["AGENT_PLATFORM_READONLY_SQL_DATABASE_URL"] '
            f'"{database_url}"',
            str(secret_file),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
    )
    if result.returncode != 0:
        sys.stderr.write(_redact_secret_literals(result.stderr.decode()))
        sys.exit(f"sops update failed for {secret_file}.")


def main() -> None:
    load_env_file()
    args = parse_args()
    if not args.admin_url:
        sys.exit("Provide --admin-url or set AGENT_CONTROL_PLANE_DB_ADMIN_URL/BOT_DB_ADMIN_URL.")
    if args.skip_db and not args.db_password:
        sys.exit(
            "Provide --db-password or AGENT_CONTROL_PLANE_READONLY_SQL_PASSWORD "
            "when using --skip-db."
        )

    role = validate_identifier(args.db_user, "db-user")
    relation_inputs = args.relations or split_env_list(
        os.environ.get("AGENT_CONTROL_PLANE_READONLY_SQL_RELATIONS")
    )
    relations = [validate_relation_identifier(relation) for relation in relation_inputs]
    if not relations:
        sys.exit(
            "Provide --relation or AGENT_CONTROL_PLANE_READONLY_SQL_RELATIONS. "
            "CES-263 forbids blanket schema grants."
        )
    relation_schemas = dedupe([schema for schema, _relation in relations])
    schemas = [
        validate_identifier(schema, "schema")
        for schema in (args.schemas or relation_schemas)
    ]
    password = args.db_password or generate_password()

    if not args.skip_db:
        ensure_command("psql")
        run_sql(args.admin_url, args.database, role, password, schemas, relations)

    database_url = build_connection_string(
        args.admin_url,
        role,
        password,
        args.database,
        args.db_host,
    )
    set_encrypted_secret(args.secret_file, database_url)
    print(
        "Provisioned Agent Control Plane readonly SQL role and encrypted runtime secret."
    )


if __name__ == "__main__":
    main()
