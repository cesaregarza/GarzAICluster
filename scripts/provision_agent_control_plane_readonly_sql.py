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
        help="Schema to grant read-only access to. Repeatable. Defaults to public and common.",
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


def run_sql(admin_url: str, database: str, role: str, password: str, schemas: list[str]) -> None:
    schema_values = ", ".join(f"'{schema}'" for schema in schemas)
    sql = textwrap.dedent(
        f"""
        \\set ON_ERROR_STOP on

        DO
        $$
        DECLARE
            role_name text := '{role}';
            role_password text := '{password}';
            db_name text := '{database}';
            target_schema text;
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
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', db_name, role_name);
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, role_name);

            FOREACH target_schema IN ARRAY ARRAY[{schema_values}]
            LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.schemata s
                    WHERE s.schema_name = target_schema
                ) THEN
                    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I', target_schema, role_name);
                    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', target_schema, role_name);
                    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', target_schema, role_name);
                    EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', target_schema, role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO %I', target_schema, role_name);
                    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON SEQUENCES TO %I', target_schema, role_name);
                END IF;
            END LOOP;
        END
        $$;
        """
    )
    result = subprocess.run(
        ["psql", admin_url_for_db(admin_url, database), "-q"],
        input=sql,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(_redact_secret_literals(result.stderr))
        sys.exit("Postgres read-only role provisioning failed.")


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
    schemas = [
        validate_identifier(schema, "schema")
        for schema in (args.schemas or ["public", "common"])
    ]
    password = args.db_password or generate_password()

    if not args.skip_db:
        ensure_command("psql")
        run_sql(args.admin_url, args.database, role, password, schemas)

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
