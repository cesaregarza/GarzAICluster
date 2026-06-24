#!/usr/bin/env python3
"""Provision the Agent Control Plane read-only SQL broker secret."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
XSCRAPER_ANALYTICAL_RELATIONS = (
    "xscraper.players",
    "xscraper.player_latest",
    "xscraper.player_season",
    "xscraper.season_results",
    "xscraper.weapon_leaderboard",
    "xscraper.aliases",
    "xscraper.schedules",
)


@dataclass(frozen=True)
class ReadonlySqlTarget:
    name: str
    default_database: str
    default_role: str
    secret_key: str
    admin_url_envs: tuple[str, ...]
    database_envs: tuple[str, ...]
    host_envs: tuple[str, ...]
    password_envs: tuple[str, ...]
    relations_envs: tuple[str, ...]
    default_relations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProvisionConfig:
    target: ReadonlySqlTarget
    admin_url: str | None
    database: str
    db_host: str | None
    role: str
    password: str | None
    schemas: list[str] | None
    relations: list[str]
    secret_file: Path
    secret_key: str
    skip_db: bool


READONLY_SQL_TARGETS = {
    "bots": ReadonlySqlTarget(
        name="bots",
        default_database="bots",
        default_role="agent_control_plane_readonly_sql",
        secret_key="AGENT_PLATFORM_READONLY_SQL_DATABASE_URL",
        admin_url_envs=("AGENT_CONTROL_PLANE_DB_ADMIN_URL", "BOT_DB_ADMIN_URL"),
        database_envs=("AGENT_CONTROL_PLANE_DB_NAME", "BOT_DB_NAME"),
        host_envs=("AGENT_CONTROL_PLANE_DB_HOST", "BOT_DB_HOST"),
        password_envs=("AGENT_CONTROL_PLANE_READONLY_SQL_PASSWORD",),
        relations_envs=("AGENT_CONTROL_PLANE_READONLY_SQL_RELATIONS",),
    ),
    "xscraper_analytical": ReadonlySqlTarget(
        name="xscraper_analytical",
        default_database="xscraper",
        default_role="agent_control_plane_xscraper_readonly_sql",
        secret_key="AGENT_PLATFORM_READONLY_SQL_ANALYTICAL_DATABASE_URL",
        admin_url_envs=(
            "AGENT_CONTROL_PLANE_XSCRAPER_DB_ADMIN_URL",
            "XSCRAPER_DB_ADMIN_URL",
            "BOT_DB_ADMIN_URL",
        ),
        database_envs=("AGENT_CONTROL_PLANE_XSCRAPER_DB_NAME", "XSCRAPER_DB_NAME"),
        host_envs=("AGENT_CONTROL_PLANE_XSCRAPER_DB_HOST", "XSCRAPER_DB_HOST", "BOT_DB_HOST"),
        password_envs=(
            "AGENT_CONTROL_PLANE_XSCRAPER_READONLY_SQL_PASSWORD",
            "AGENT_PLATFORM_READONLY_SQL_ANALYTICAL_PASSWORD",
        ),
        relations_envs=(
            "AGENT_CONTROL_PLANE_XSCRAPER_READONLY_SQL_RELATIONS",
            "AGENT_PLATFORM_READONLY_SQL_ANALYTICAL_RELATIONS",
        ),
        default_relations=XSCRAPER_ANALYTICAL_RELATIONS,
    ),
}


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
        "--target",
        choices=sorted(READONLY_SQL_TARGETS),
        default=os.environ.get("AGENT_CONTROL_PLANE_READONLY_SQL_TARGET", "bots"),
        help=(
            "Named readonly SQL target to provision. Defaults to bots; "
            "xscraper_analytical writes the preset-bound analytical DSN."
        ),
    )
    parser.add_argument(
        "--admin-url",
        default=None,
        help="Postgres admin URL. Defaults to target-specific admin URL env vars.",
    )
    parser.add_argument(
        "--database",
        default=None,
    )
    parser.add_argument(
        "--db-host",
        default=None,
        help="Host for the emitted connection string. Defaults to private-<admin host>.",
    )
    parser.add_argument("--db-user", default=None)
    parser.add_argument(
        "--db-password",
        default=None,
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
    parser.add_argument(
        "--secret-key",
        default=None,
        help="Runtime secret stringData key to write. Defaults to the selected target.",
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


def validate_secret_key(value: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        sys.exit("secret key must be an uppercase environment variable name.")
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


def first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def resolve_config(args: argparse.Namespace) -> ProvisionConfig:
    target = READONLY_SQL_TARGETS[args.target]
    relations = args.relations or split_env_list(first_env(target.relations_envs))
    if not relations:
        relations = list(target.default_relations)
    return ProvisionConfig(
        target=target,
        admin_url=args.admin_url or first_env(target.admin_url_envs),
        database=args.database or first_env(target.database_envs) or target.default_database,
        db_host=args.db_host or first_env(target.host_envs),
        role=args.db_user or target.default_role,
        password=args.db_password or first_env(target.password_envs),
        schemas=args.schemas,
        relations=relations,
        secret_file=args.secret_file,
        secret_key=args.secret_key or target.secret_key,
        skip_db=args.skip_db,
    )


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


def set_encrypted_secret(secret_file: Path, secret_key: str, database_url: str) -> None:
    ensure_command("sops")
    env = os.environ.copy()
    if "SOPS_AGE_KEY_FILE" not in env and DEFAULT_SOPS_KEY.exists():
        env["SOPS_AGE_KEY_FILE"] = str(DEFAULT_SOPS_KEY)
    result = subprocess.run(
        [
            "sops",
            "--set",
            f'["stringData"]["{secret_key}"] "{database_url}"',
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
    config = resolve_config(args)
    if not config.admin_url:
        joined = "/".join(config.target.admin_url_envs)
        sys.exit(f"Provide --admin-url or set one of: {joined}.")
    if config.skip_db and not config.password:
        joined = "/".join(config.target.password_envs)
        sys.exit(
            f"Provide --db-password or one of {joined} when using --skip-db."
        )

    role = validate_identifier(config.role, "db-user")
    secret_key = validate_secret_key(config.secret_key)
    relation_inputs = config.relations
    relations = [validate_relation_identifier(relation) for relation in relation_inputs]
    if not relations:
        sys.exit(
            f"Provide --relation or one of {config.target.relations_envs}. "
            "CES-263 forbids blanket schema grants."
        )
    relation_schemas = dedupe([schema for schema, _relation in relations])
    schemas = [
        validate_identifier(schema, "schema")
        for schema in (config.schemas or relation_schemas)
    ]
    password = config.password or generate_password()

    if not config.skip_db:
        ensure_command("psql")
        run_sql(config.admin_url, config.database, role, password, schemas, relations)

    database_url = build_connection_string(
        config.admin_url,
        role,
        password,
        config.database,
        config.db_host,
    )
    set_encrypted_secret(config.secret_file, secret_key, database_url)
    print(
        f"Provisioned {config.target.name} readonly SQL role and encrypted "
        f"{secret_key}."
    )


if __name__ == "__main__":
    main()
