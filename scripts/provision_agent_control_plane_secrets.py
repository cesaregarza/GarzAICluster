#!/usr/bin/env python3
"""Provision Agent Control Plane runtime secrets without printing secret values."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import string
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from shutil import which
from urllib.parse import quote, urlparse, urlunparse


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRET_DIR = REPO_ROOT / "secrets" / "agent-control-plane"
DEFAULT_SOPS_KEY = REPO_ROOT / "keys" / "age-private.txt"
DEFAULT_SOPS_RECIPIENTS = REPO_ROOT / "keys" / "age-public.txt"


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
        description="Provision Agent Control Plane Postgres and encrypted Kubernetes secrets."
    )
    parser.add_argument("--namespace", default="agent-control-plane")
    parser.add_argument("--secret-name", default="agent-control-plane-secrets")
    parser.add_argument("--secret-dir", type=Path, default=DEFAULT_SECRET_DIR)
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
        help="Host for the emitted app connection string. Defaults to private-<admin host>.",
    )
    parser.add_argument("--schema", default="agent_control_plane")
    parser.add_argument("--db-user", default="agent_control_plane_user")
    parser.add_argument(
        "--db-password",
        default=os.environ.get("AGENT_CONTROL_PLANE_DB_PASSWORD"),
        help="Existing DB password. Required with --skip-db.",
    )
    parser.add_argument("--skip-db", action="store_true")
    parser.add_argument(
        "--registry-token",
        default=os.environ.get("DO_REGISTRY_READ_TOKEN"),
        help="Read token for registry.digitalocean.com/sendouq.",
    )
    parser.add_argument("--registry-host", default="registry.digitalocean.com")
    parser.add_argument("--registry-email", default=None)
    parser.add_argument("--skip-regcred", action="store_true")
    parser.add_argument(
        "--discord-bot-token",
        default=os.environ.get("AGENT_PLATFORM_DISCORD_BOT_TOKEN")
        or os.environ.get("OPENCLAW_DISCORD_TOKEN"),
        help="Optional Discord bot token for the Agent Control Plane callback adapter.",
    )
    return parser.parse_args()


def validate_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        sys.exit(f"{label} must be a valid Postgres identifier.")
    return value


def generate_password(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_token() -> str:
    return secrets.token_urlsafe(48)


def ensure_command(name: str) -> None:
    if which(name) is None:
        sys.exit(f"{name} not found in PATH.")


def derive_private_host(host: str, prefix: str = "private-") -> str:
    return host if host.startswith(prefix) else f"{prefix}{host}"


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


def run_sql(admin_url: str, database: str, schema: str, role: str, password: str) -> None:
    sql = textwrap.dedent(
        f"""
        \\set ON_ERROR_STOP on

        DO
        $$
        DECLARE
            role_name text := '{role}';
            role_password text := '{password}';
            schema_name text := '{schema}';
            db_name text := '{database}';
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('CREATE ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            ELSE
                EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            END IF;

            EXECUTE format('ALTER ROLE %I IN DATABASE %I SET search_path = %I', role_name, db_name, schema_name);
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', db_name, role_name);
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, role_name);
            EXECUTE format('GRANT CREATE ON DATABASE %I TO %I', db_name, role_name);

            EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema_name);
            EXECUTE format('ALTER SCHEMA %I OWNER TO %I', schema_name, role_name);
            EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', schema_name, role_name);

            EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', role_name);
            EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', role_name);
            EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', role_name);
            EXECUTE format('REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM %I', role_name);

            EXECUTE format('GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA %I TO %I', schema_name, role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON TABLES TO %I', schema_name, role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON SEQUENCES TO %I', schema_name, role_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL PRIVILEGES ON FUNCTIONS TO %I', schema_name, role_name);
        END
        $$;
        """
    )
    subprocess.run(["psql", admin_url, "-q"], input=sql.encode(), check=True, capture_output=True)


def encrypt_to_file(path: Path, plaintext: str) -> None:
    ensure_command("sops")
    path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if "SOPS_AGE_KEY_FILE" not in env and DEFAULT_SOPS_KEY.exists():
        env["SOPS_AGE_KEY_FILE"] = str(DEFAULT_SOPS_KEY)
    recipient = next(
        (
            line.strip()
            for line in DEFAULT_SOPS_RECIPIENTS.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ),
        "",
    )
    if not recipient:
        sys.exit(f"No age recipient found in {DEFAULT_SOPS_RECIPIENTS}.")

    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(plaintext)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [
                "sops",
                "--config",
                "/dev/null",
                "--encrypt",
                "--age",
                recipient,
                "--encrypted-regex",
                "^(data|stringData)$",
                "--input-type",
                "yaml",
                "--output-type",
                "yaml",
                "--output",
                str(path),
                str(tmp_path),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stderr.decode())
            sys.exit(f"sops encryption failed for {path}.")
    finally:
        tmp_path.unlink(missing_ok=True)


def dockerconfigjson(registry_host: str, token: str, email: str | None) -> str:
    auth = base64.b64encode(f"{token}:{token}".encode()).decode()
    entry: dict[str, str] = {
        "username": token,
        "password": token,
        "auth": auth,
    }
    if email:
        entry["email"] = email
    return json.dumps({"auths": {registry_host: entry}}, separators=(",", ":"), sort_keys=True)


def main() -> None:
    load_env_file()
    args = parse_args()

    schema = validate_identifier(args.schema, "schema")
    db_user = validate_identifier(args.db_user, "db-user")
    if not args.admin_url:
        sys.exit("Provide --admin-url or set AGENT_CONTROL_PLANE_DB_ADMIN_URL/BOT_DB_ADMIN_URL.")
    if args.skip_db and not args.db_password:
        sys.exit("Provide --db-password or AGENT_CONTROL_PLANE_DB_PASSWORD when using --skip-db.")
    if not args.skip_regcred and not args.registry_token:
        sys.exit("Provide --registry-token or set DO_REGISTRY_READ_TOKEN.")

    parsed_admin = urlparse(args.admin_url)
    if not parsed_admin.hostname:
        sys.exit("Unable to determine host from admin URL.")

    db_password = args.db_password or generate_password()
    if not args.skip_db:
        ensure_command("psql")
        run_sql(admin_url_for_db(args.admin_url, args.database), args.database, schema, db_user, db_password)

    database_url = build_connection_string(
        args.admin_url,
        db_user,
        db_password,
        args.database,
        args.db_host,
    )
    discord_token_line = (
        f'          AGENT_PLATFORM_DISCORD_BOT_TOKEN: "{args.discord_bot_token}"\n'
        if args.discord_bot_token
        else ""
    )
    runtime_secret = textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: Secret
        metadata:
          name: {args.secret_name}
          namespace: {args.namespace}
        stringData:
          AGENT_PLATFORM_DATABASE_URL: "{database_url}"
          AGENT_PLATFORM_OPENCLAW_TOKEN: "{generate_token()}"
          AGENT_PLATFORM_INTERNAL_WORKER_TOKEN: "{generate_token()}"
          AGENT_PLATFORM_APPROVAL_TOKEN: "{generate_token()}"
          AGENT_PLATFORM_AUDIT_READ_TOKEN: "{generate_token()}"
          AGENT_PLATFORM_AUDIT_WRITE_TOKEN: "{generate_token()}"
{discord_token_line.rstrip()}
        """
    )
    encrypt_to_file(args.secret_dir / "runtime-secret.enc.yaml", runtime_secret)

    if not args.skip_regcred:
        dockerconfig_b64 = base64.b64encode(
            dockerconfigjson(args.registry_host, args.registry_token, args.registry_email).encode()
        ).decode()
        regcred_secret = textwrap.dedent(
            f"""\
            apiVersion: v1
            kind: Secret
            metadata:
              name: regcred
              namespace: {args.namespace}
            type: kubernetes.io/dockerconfigjson
            data:
              .dockerconfigjson: {dockerconfig_b64}
            """
        )
        encrypt_to_file(args.secret_dir / "regcred.enc.yaml", regcred_secret)

    print(f"Provisioned Postgres schema '{schema}' and encrypted Agent Control Plane secrets.")


if __name__ == "__main__":
    main()
