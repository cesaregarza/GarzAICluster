#!/usr/bin/env python3
"""
Provision a bot-specific Postgres schema + user and emit a ready-to-use connection string.

Usage:

    BOT_DB_ADMIN_URL=postgresql://admin:pass@db:25060/xscraper?sslmode=require \\
        uv run python scripts/provision_bot_db.py my-cool-bot

Requirements:
    - `psql` must be installed and reachable in $PATH.
    - The supplied admin URL must have privileges to create roles/schemas.
"""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a per-bot Postgres schema and login restricted to that schema."
    )
    parser.add_argument(
        "bot_name",
        help="Slug for the bot (letters, numbers, and dashes). Used to derive the schema and role names.",
    )
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("BOT_DB_ADMIN_URL"),
        help="Connection string for a superuser/owner role. "
             "Can also be provided via BOT_DB_ADMIN_URL env var.",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("BOT_DB_NAME", "bots"),
        help="Database name to target for the bot schema (default: bots). "
             "Use BOT_DB_NAME env var or --database to override.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("BOT_DB_HOST"),
        help=(
            "Override hostname for generated connection strings (e.g., private DB host). "
            "If unset, a private host is derived by prepending 'private-' to the admin URL host "
            "when needed. You can also set BOT_DB_HOST."
        ),
    )
    parser.add_argument(
        "--namespace",
        help="Optional Kubernetes namespace for the bot (defaults to splattop-bot-<bot-name>).",
    )
    parser.add_argument(
        "--secret-file",
        type=Path,
        help="Optional path to write a plaintext Kubernetes Secret manifest containing the generated connection string.",
    )
    parser.add_argument(
        "--secret-name",
        default="bot-db-readonly",
        help="Secret metadata.name to use when writing --secret-file (default: bot-db-readonly).",
    )
    parser.add_argument(
        "--schema-key",
        default="DATABASE_SCHEMA",
        help="Key name to include the schema in the generated Secret (default: DATABASE_SCHEMA).",
    )
    parser.add_argument(
        "--readonly-role",
        default=os.environ.get("BOT_DB_READONLY_ROLE", "readonly"),
        help="Role that should receive read-only grants on the schema (default: readonly).",
    )
    parser.add_argument(
        "--common-schema",
        default=os.environ.get("BOT_DB_COMMON_SCHEMA", "common"),
        help="Shared schema to grant SELECT/USAGE to both roles (default: common). Set empty to skip.",
    )
    return parser.parse_args()


def ensure_psql_available() -> None:
    if which("psql") is None:
        sys.exit("psql not found in PATH; install postgres client utilities first.")


def normalize_bot_name(bot_name: str) -> str:
    slug = bot_name.strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        sys.exit("Bot name must contain only lowercase letters, numbers, and dashes.")
    return slug


def to_identifier(slug: str) -> str:
    return slug.replace("-", "_")


def generate_password(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_connection_string(
    admin_url: str,
    username: str,
    password: str,
    database: str | None = None,
    host_override: str | None = None,
) -> str:
    parsed = urlparse(admin_url)
    if not parsed.scheme.startswith("postgres"):
        sys.exit("Admin URL must be a postgres:// or postgresql:// connection string.")

    host = host_override or parsed.hostname
    if not host:
        sys.exit("Unable to determine host from admin URL.")

    port = f":{parsed.port}" if parsed.port else ""
    hostpart = host if ":" not in host else f"[{host}]"
    netloc = f"{quote(username)}:{quote(password)}@{hostpart}{port}"
    db_path = f"/{database}" if database else (parsed.path or "/")

    return urlunparse(
        (
            "postgresql",
            netloc,
            db_path,
            "",
            parsed.query or "sslmode=require",
            "",
        )
    )


def derive_private_host(host: str, prefix: str = "private-") -> str:
    """Return host prefixed with 'private-' unless it already has it."""
    if not host:
        sys.exit("Unable to determine host from admin URL.")
    return host if host.startswith(prefix) else f"{prefix}{host}"


def admin_url_for_db(admin_url: str, database: str, host_override: str | None = None) -> str:
    parsed = urlparse(admin_url)
    if not parsed.scheme.startswith("postgres"):
        sys.exit("Admin URL must be a postgres:// or postgresql:// connection string.")

    if host_override:
        netloc_host = host_override
        if parsed.port:
            netloc_host = f"{netloc_host}:{parsed.port}"
        # Preserve userinfo if present
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        netloc = f"{userinfo}{netloc_host}"
    else:
        netloc = parsed.netloc

    return urlunparse(
        (
            parsed.scheme,
            netloc,
            f"/{database}",
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def run_sql(
    admin_url: str,
    db_name: str,
    schema: str,
    role: str,
    password: str,
    readonly_role: str,
    common_schema: str | None,
) -> None:
    common_schema_literal = f"'{common_schema}'" if common_schema else "NULL"
    sql = textwrap.dedent(
        f"""
        \\set ON_ERROR_STOP on

        DO
        $$
        DECLARE
            role_name text := '{role}';
            role_password text := '{password}';
            schema_name text := '{schema}';
            db_name text := '{db_name}';
            readonly_role_name text := '{readonly_role}';
            shared_schema text := {common_schema_literal};
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('CREATE ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            ELSE
                EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = readonly_role_name) THEN
                EXECUTE format('CREATE ROLE %I', readonly_role_name);
            END IF;

            EXECUTE format('ALTER ROLE %I IN DATABASE %I SET search_path = %I', role_name, db_name, schema_name);

            EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', db_name, role_name);
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, role_name);
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, readonly_role_name);
            EXECUTE format('GRANT CREATE ON DATABASE %I TO %I', db_name, role_name);

            EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema_name);
            EXECUTE format('ALTER SCHEMA %I OWNER TO %I', schema_name, role_name);
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('GRANT CREATE ON SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', schema_name, readonly_role_name);

            EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', role_name);
            EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', role_name);
            EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', role_name);
            EXECUTE format('REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM %I', role_name);

            EXECUTE format('GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', schema_name, readonly_role_name);
            EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', schema_name, readonly_role_name);

            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON TABLES TO %I', schema_name, role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON SEQUENCES TO %I', schema_name, role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON FUNCTIONS TO %I', schema_name, role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO %I', schema_name, readonly_role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON SEQUENCES TO %I', schema_name, readonly_role_name);

            IF shared_schema IS NOT NULL AND btrim(shared_schema) <> '' THEN
                IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = shared_schema) THEN
                    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', shared_schema);
                END IF;

                EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', shared_schema, role_name);
                EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', shared_schema, readonly_role_name);

                EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', shared_schema, role_name);
                EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', shared_schema, role_name);
                EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', shared_schema, readonly_role_name);
                EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', shared_schema, readonly_role_name);

                EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO %I', shared_schema, role_name);
                EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON SEQUENCES TO %I', shared_schema, role_name);
                EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO %I', shared_schema, readonly_role_name);
                EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON SEQUENCES TO %I', shared_schema, readonly_role_name);
            END IF;
        END
        $$;
        """
    )

    cmd = [
        "psql",
        admin_url,
        "-q",
    ]

    try:
        subprocess.run(
            cmd,
            input=sql.encode(),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr.decode())
        sys.exit(f"psql failed with exit code {exc.returncode}")


def maybe_write_secret(
    path: Path,
    namespace: str,
    secret_name: str,
    connection_string: str,
    schema_name: str,
    schema_key: str,
) -> None:
    manifest = textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: Secret
        metadata:
          name: {secret_name}
          namespace: {namespace}
        stringData:
          DATABASE_URL: "{connection_string}"
          {schema_key}: "{schema_name}"
        """
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest)
    print(f"Wrote plaintext secret manifest to {path}. Encrypt it with SOPS before committing.")


def maybe_encrypt_secret(path: Path) -> bool:
    """Attempt to encrypt the secret in-place with SOPS if available."""
    if which("sops") is None:
        print("sops not found in PATH; leaving secret plaintext. Encrypt manually before committing.")
        return False

    try:
        subprocess.run(
            ["sops", "--encrypt", "--in-place", str(path)],
            check=True,
            capture_output=True,
        )
        print(f"Encrypted secret with sops: {path}")
        return True
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr.decode())
        print("sops encryption failed; secret remains plaintext. Encrypt manually before committing.")
        return False


def load_env_file() -> None:
    env_path = Path(os.environ.get("SPLATTOPCONFIG_ENV_FILE", ".env"))
    if not env_path.exists():
        return

    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        os.environ.setdefault(key, value)


def main() -> None:
    load_env_file()
    args = parse_args()
    ensure_psql_available()

    if not args.admin_url:
        sys.exit("Provide --admin-url or export BOT_DB_ADMIN_URL.")

    parsed_admin = urlparse(args.admin_url)
    if not parsed_admin.hostname:
        sys.exit("Unable to determine host from admin URL.")
    # Only override the emitted connection string by default; keep admin connections on the provided host
    # unless the operator explicitly sets --host / BOT_DB_HOST.
    connection_host_override = args.host or derive_private_host(parsed_admin.hostname)
    admin_host_override = args.host

    bot_slug = normalize_bot_name(args.bot_name)
    identifier = to_identifier(bot_slug)
    schema_name = f"bot_{identifier}"
    role_name = f"{schema_name}_user"
    readonly_role = args.readonly_role.strip()
    if not readonly_role or not re.fullmatch(r"[A-Za-z0-9_:-]+", readonly_role):
        sys.exit("readonly-role must contain only letters, numbers, underscores, dashes, or colons.")
    common_schema = (args.common_schema or "").strip()
    if common_schema and not re.fullmatch(r"[A-Za-z0-9_:-]+", common_schema):
        sys.exit("common-schema must contain only letters, numbers, underscores, dashes, or colons.")
    common_schema_value = common_schema or None
    namespace = args.namespace or f"splattop-bot-{bot_slug}"
    secret_path = args.secret_file or Path("secrets") / "bots" / bot_slug / "db-secret.enc.yaml"
    password = generate_password()

    print(f"Creating/refreshing schema '{schema_name}' and role '{role_name}'...")
    admin_url_db = admin_url_for_db(args.admin_url, args.database, admin_host_override)
    run_sql(admin_url_db, args.database, schema_name, role_name, password, readonly_role, common_schema_value)

    connection_string = build_connection_string(
        args.admin_url,
        role_name,
        password,
        args.database,
        connection_host_override,
    )
    print("\n✅ Provisioned!")
    print(f"Schema: {schema_name}")
    print(f"Role:   {role_name}")
    print(f"Readonly role: {readonly_role}")
    if common_schema_value:
        print(f"Shared read access on schema: {common_schema_value}")
    print(f"Bot namespace: {namespace}")
    print(f"\nConnection string (store via SOPS):\n{connection_string}\n")

    if secret_path:
        maybe_write_secret(
            secret_path,
            namespace,
            args.secret_name,
            connection_string,
            schema_name,
            args.schema_key,
        )
        maybe_encrypt_secret(secret_path)


if __name__ == "__main__":
    main()
