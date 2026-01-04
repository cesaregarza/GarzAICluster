#!/usr/bin/env python3
"""Provision SplatVote DB role/schema and emit encrypted secrets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
import textwrap
from pathlib import Path
from shutil import which
from urllib.parse import urlparse, urlunparse


def load_env_file() -> None:
    env_path = Path(os.environ.get("SPLATTOPCONFIG_ENV_FILE", ".env"))
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
        value = value.strip().strip("\"").strip("'")
        if not key:
            continue
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision SplatVote DB role/schema and generate encrypted secrets."
    )
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("BOT_DB_ADMIN_URL"),
        help="Admin connection string (defaults to BOT_DB_ADMIN_URL).",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("SPLATVOTE_DB_NAME", "splatvote"),
        help="Database name to target (default: splatvote).",
    )
    parser.add_argument(
        "--db-user",
        default=os.environ.get("SPLATVOTE_DB_USER", "splatvote_user"),
        help="Application DB user (default: splatvote_user).",
    )
    parser.add_argument(
        "--db-password",
        default=os.environ.get("SPLATVOTE_DB_PASSWORD"),
        help="Application DB password (auto-generate if unset).",
    )
    parser.add_argument(
        "--db-host",
        default=os.environ.get("SPLATVOTE_DB_HOST"),
        help="DB host to place in the secret (defaults to private-<admin host>).",
    )
    parser.add_argument(
        "--db-port",
        default=os.environ.get("SPLATVOTE_DB_PORT"),
        help="DB port to place in the secret (defaults to admin URL port).",
    )
    parser.add_argument(
        "--schema",
        default=os.environ.get("SPLATVOTE_DB_SCHEMA", "voting"),
        help="Schema name to own (default: voting).",
    )
    parser.add_argument(
        "--namespace",
        default="splatvote-prod",
        help="Kubernetes namespace for secrets (default: splatvote-prod).",
    )
    parser.add_argument(
        "--secret-file",
        type=Path,
        default=Path("secrets/splatvote-prod/vote-db-secrets.enc.yaml"),
        help="Path to write the encrypted DB secret.",
    )
    parser.add_argument(
        "--secret-name",
        default="vote-db-secrets",
        help="Secret metadata.name for DB credentials (default: vote-db-secrets).",
    )
    parser.add_argument(
        "--vote-pepper",
        default=os.environ.get("SPLATVOTE_VOTE_IP_PEPPER"),
        help="Pepper for vote fingerprinting (auto-generate if unset).",
    )
    parser.add_argument(
        "--admin-pepper",
        default=os.environ.get("SPLATVOTE_ADMIN_TOKEN_PEPPER"),
        help="Pepper for admin token hashing (auto-generate if unset).",
    )
    parser.add_argument(
        "--admin-token",
        default=os.environ.get("SPLATVOTE_ADMIN_TOKEN"),
        help="Admin token plaintext (auto-generate if unset).",
    )
    parser.add_argument(
        "--print-admin-token",
        action="store_true",
        help="Print the admin token to stdout.",
    )
    parser.add_argument(
        "--write-env",
        type=Path,
        help="Optional .env file to append generated values (skips existing keys).",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip DB role/schema provisioning.",
    )
    parser.add_argument(
        "--skip-regcred",
        action="store_true",
        help="Skip regcred generation.",
    )
    parser.add_argument(
        "--regcred-file",
        type=Path,
        default=Path("secrets/splatvote-prod/regcred.enc.yaml"),
        help="Path to write the encrypted regcred secret.",
    )
    parser.add_argument(
        "--registry-host",
        default=os.environ.get("DO_REGISTRY_HOST", "registry.digitalocean.com"),
        help="Container registry host (default: registry.digitalocean.com).",
    )
    parser.add_argument(
        "--registry-token",
        default=os.environ.get("DO_REGISTRY_READ_TOKEN"),
        help="Registry token used for regcred (defaults to DO_REGISTRY_READ_TOKEN).",
    )
    parser.add_argument(
        "--registry-email",
        default=os.environ.get("DO_REGISTRY_EMAIL"),
        help="Optional email for docker config auth (defaults to DO_REGISTRY_EMAIL).",
    )
    return parser.parse_args()


def ensure_psql_available() -> None:
    if which("psql") is None:
        sys.exit("psql not found in PATH; install postgres client utilities first.")


def normalize_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        sys.exit(f"{label} must contain only letters, numbers, and underscores.")
    return value


def generate_password(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def derive_private_host(host: str, prefix: str = "private-") -> str:
    if not host:
        sys.exit("Unable to determine host from admin URL.")
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


def run_sql(admin_url: str, db_name: str, schema: str, role: str, password: str) -> None:
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
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('CREATE ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            ELSE
                EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', role_name, role_password);
            END IF;

            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, role_name);
            EXECUTE format('GRANT CREATE ON DATABASE %I TO %I', db_name, role_name);

            EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema_name);
            EXECUTE format('ALTER SCHEMA %I OWNER TO %I', schema_name, role_name);
            EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', schema_name, role_name);

            EXECUTE format('ALTER ROLE %I IN DATABASE %I SET search_path = %I, public', role_name, db_name, schema_name);
        END
        $$;
        """
    )

    try:
        subprocess.run(
            ["psql", admin_url, "-q"],
            input=sql.encode(),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr.decode())
        sys.exit(f"psql failed with exit code {exc.returncode}")


def write_secret(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def encrypt_secret(path: Path) -> bool:
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


def build_regcred_payload(registry_host: str, token: str, email: str | None) -> str:
    auth = base64.b64encode(f"{token}:{token}".encode()).decode()
    entry: dict[str, str] = {
        "username": token,
        "password": token,
        "auth": auth,
    }
    if email:
        entry["email"] = email

    payload = {"auths": {registry_host: entry}}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def append_env(path: Path, updates: dict[str, str]) -> None:
    existing_keys: set[str] = set()
    lines: list[str] = []

    if path.exists():
        lines = path.read_text().splitlines()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key:
                existing_keys.add(key)

    for key, value in updates.items():
        if key in existing_keys:
            continue
        lines.append(f"{key}={value}")

    if lines:
        path.write_text("\n".join(lines) + "\n")


def main() -> None:
    load_env_file()
    args = parse_args()

    if not args.admin_url and not args.skip_db:
        sys.exit("Provide --admin-url or export BOT_DB_ADMIN_URL (or use --skip-db).")

    db_user = normalize_identifier(args.db_user, "db-user")
    db_password = args.db_password or generate_password()

    generated: dict[str, str] = {}

    if args.vote_pepper:
        vote_pepper = args.vote_pepper
    else:
        vote_pepper = generate_token()
        generated["SPLATVOTE_VOTE_IP_PEPPER"] = vote_pepper

    if args.admin_pepper:
        admin_pepper = args.admin_pepper
    else:
        admin_pepper = generate_token()
        generated["SPLATVOTE_ADMIN_TOKEN_PEPPER"] = admin_pepper

    if args.admin_token:
        admin_token = args.admin_token
    else:
        admin_token = generate_token()
        generated["SPLATVOTE_ADMIN_TOKEN"] = admin_token

    admin_hash = hashlib.sha256(f"{admin_pepper}{admin_token}".encode()).hexdigest()

    if not args.skip_db:
        ensure_psql_available()
        parsed = urlparse(args.admin_url)
        if not parsed.hostname:
            sys.exit("Unable to determine host from admin URL.")
        admin_url_db = admin_url_for_db(args.admin_url, args.database)
        run_sql(admin_url_db, args.database, args.schema, db_user, db_password)

    if args.admin_url:
        parsed = urlparse(args.admin_url)
        admin_host = parsed.hostname
        admin_port = str(parsed.port or 5432)
    else:
        admin_host = None
        admin_port = "5432"

    db_host = args.db_host or (derive_private_host(admin_host) if admin_host else "")
    if not db_host:
        sys.exit("DB host is required when --skip-db is used without --db-host.")
    db_port = args.db_port or admin_port
    if not args.db_password:
        generated["SPLATVOTE_DB_PASSWORD"] = db_password
    generated.setdefault("SPLATVOTE_DB_USER", db_user)
    generated.setdefault("SPLATVOTE_DB_NAME", args.database)
    generated.setdefault("SPLATVOTE_DB_HOST", db_host)
    generated.setdefault("SPLATVOTE_DB_PORT", db_port)

    secret_template = textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: Secret
        metadata:
          name: {args.secret_name}
          namespace: {args.namespace}
        stringData:
          DB_HOST: "{db_host}"
          DB_PORT: "{db_port}"
          DB_USER: "{db_user}"
          DB_PASSWORD: "{db_password}"
          DB_NAME: "{args.database}"
          VOTE_IP_PEPPER: "{vote_pepper}"
          ADMIN_TOKEN_PEPPER: "{admin_pepper}"
          ADMIN_API_TOKENS_HASHED: "{admin_hash}"
        """
    )

    write_secret(args.secret_file, secret_template)
    encrypt_secret(args.secret_file)

    if not args.skip_regcred:
        if not args.registry_token:
            sys.exit("Provide --registry-token or set DO_REGISTRY_READ_TOKEN to generate regcred.")
        dockerconfigjson = build_regcred_payload(
            args.registry_host, args.registry_token, args.registry_email
        )
        dockerconfig_b64 = base64.b64encode(dockerconfigjson.encode()).decode()
        regcred_template = textwrap.dedent(
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
        write_secret(args.regcred_file, regcred_template)
        encrypt_secret(args.regcred_file)

    if args.write_env and generated:
        append_env(args.write_env, generated)
        print(f"Updated env file with generated values: {args.write_env}")

    if args.print_admin_token:
        print("\nAdmin token:")
        print(admin_token)
    else:
        print("\nAdmin token generated. Re-run with --print-admin-token to display it.")


if __name__ == "__main__":
    main()
