#!/usr/bin/env python3
"""
Bootstrap a new bot entry for the ArgoCD ApplicationSets.

What it does:
- Create apps/bots/<bot>.yaml pointing at the shared bot chart.
- Scaffold secrets/bots/<bot>/ (README, kustomization, ksops).
- Optionally scaffold a starter values file for the chart.
- Create + encrypt the Discord token secret.
- Optionally provision a per-bot Postgres schema + secret (via provision_bot_db.py).
- Optionally copy the DB CA certificate into the chart (shared mount point).

By default it targets the agent-8s chart and enables Postgres/Prometheus/VPC
egress permissions. Adjust with flags as needed.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PROVISION_SCRIPT = SCRIPTS_DIR / "provision_bot_db.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scaffold a new bot for the ApplicationSets (apps + secrets + optional DB)."
    )
    parser.add_argument("bot_name", help="Bot slug (letters, numbers, dashes).")
    parser.add_argument(
        "--chart-path",
        type=Path,
        default=Path("apps/agent-8s"),
        help="Helm chart path for the bot (default: apps/agent-8s).",
    )
    parser.add_argument(
        "--values-file",
        type=Path,
        default=Path("apps/agent-8s/values.dev.yaml"),
        help="Helm values file to use (default: apps/agent-8s/values.dev.yaml).",
    )
    parser.add_argument(
        "--values-template",
        type=Path,
        help="Optional values file to copy when --scaffold-values is set.",
    )
    parser.add_argument(
        "--scaffold-values",
        action="store_true",
        help="Create --values-file if missing (copies --values-template or writes a stub).",
    )
    parser.add_argument(
        "--repo-url",
        default="https://github.com/cesaregarza/SplatTopConfig.git",
        help="Repo URL for the chart (default: this repo).",
    )
    parser.add_argument(
        "--target-revision",
        default="main",
        help="Git revision for the chart (default: main).",
    )
    parser.add_argument(
        "--namespace",
        help="Override the bot namespace (default: splattop-bot-<bot-name>).",
    )
    parser.add_argument(
        "--postgres",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Postgres egress permission in netpol (default: true).",
    )
    parser.add_argument(
        "--prometheus",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Prometheus scrape permission in netpol (default: true).",
    )
    parser.add_argument(
        "--db-readonly-vpc",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable VPC read-only DB egress in netpol (default: true).",
    )
    parser.add_argument(
        "--token",
        help="Discord token (falls back to BOT_TOKEN env var or prompt).",
    )
    parser.add_argument(
        "--token-secret",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Create + encrypt the Discord token secret (default: true).",
    )
    parser.add_argument(
        "--provision-db",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Provision a Postgres schema + secret (default: follow --postgres).",
    )
    parser.add_argument(
        "--db-admin-url",
        default=os.environ.get("BOT_DB_ADMIN_URL"),
        help="Admin DB URL for provisioning (BOT_DB_ADMIN_URL env fallback).",
    )
    parser.add_argument(
        "--db-name",
        default=os.environ.get("BOT_DB_NAME", "bots"),
        help="Database name to target when provisioning (default: bots).",
    )
    parser.add_argument(
        "--db-secret-file",
        type=Path,
        help="Where to write the DB secret manifest (default: secrets/bots/<bot>/db-secret.enc.yaml).",
    )
    parser.add_argument(
        "--db-secret-name",
        default="bot-db-readonly",
        help="Secret metadata.name to use when provisioning (default: bot-db-readonly).",
    )
    parser.add_argument(
        "--db-schema-key",
        default="DB_SCHEMA",
        help="Key name for the schema field in the DB secret (default: DB_SCHEMA).",
    )
    parser.add_argument(
        "--ca-source",
        type=Path,
        default=REPO_ROOT / "ca-certificate.crt",
        help="Path to the DB CA certificate to copy into the chart (default: ca-certificate.crt).",
    )
    parser.add_argument(
        "--ca-dest",
        type=Path,
        default=REPO_ROOT / "apps/agent-8s/files/do-db-ca.crt",
        help="Destination path for the CA inside the chart (default: apps/agent-8s/files/do-db-ca.crt).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files if they already exist.",
    )
    parser.add_argument(
        "--show-values-sample",
        action="store_true",
        help="Print a starter values YAML snippet (does not write to disk).",
    )
    return parser.parse_args()


def normalize_bot_name(name: str) -> str:
    slug = name.strip().lower()
    if not slug or any(c for c in slug if not (c.islower() or c.isdigit() or c == "-")):
        sys.exit("Bot name must contain only lowercase letters, numbers, and dashes.")
    return slug


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
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        os.environ.setdefault(key, value)


def write_file(path: Path, content: str, force: bool = False) -> None:
    if path.exists() and not force:
        print(f"Skip (exists): {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"Wrote: {path}")


def encrypt_with_sops(path: Path) -> bool:
    """Encrypt the given file in-place with sops if available."""
    if shutil.which("sops") is None:
        print("sops not found in PATH; leaving secret plaintext. Encrypt before committing.")
        return False

    try:
        subprocess.run(
            ["sops", "--encrypt", "--in-place", str(path)],
            check=True,
            capture_output=True,
        )
        print(f"Encrypted with sops: {path}")
        return True
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr.decode())
        print("sops encryption failed; secret remains plaintext.")
        return False


def copy_ca(source: Path, dest: Path, force: bool) -> None:
    if not source.exists():
        print(f"CA source not found, skipping copy: {source}")
        return
    if dest.exists() and not force:
        print(f"CA already present, skip copy (use --force to overwrite): {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    print(f"Copied CA -> {dest}")


def repo_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else REPO_ROOT / path


def values_stub(bot: str, include_db_secret: bool) -> str:
    secrets = ["  - name: bot-token"]
    if include_db_secret:
        secrets.append("  - name: bot-db-readonly")

    lines = [
        f"# Starter values for {bot}. Update image tag/digest and env vars as needed.",
        "image:",
        f"  repository: registry.digitalocean.com/sendouq/{bot}",
        '  tag: "latest"',
        '  digest: ""',
        "  pullPolicy: IfNotPresent",
        "env:",
        '  NODE_ENV: "production"',
        "secretRefs:",
        *secrets,
        "replicaCount: 1",
        "resources:",
        "  requests:",
        "    cpu: 100m",
        "    memory: 128Mi",
    ]
    return "\n".join(lines) + "\n"


def scaffold_app_file(bot: str, args: argparse.Namespace) -> None:
    app_path = REPO_ROOT / "apps" / "bots" / f"{bot}.yaml"
    chart_path = args.chart_path.as_posix() if isinstance(args.chart_path, Path) else str(args.chart_path)
    values_file = args.values_file.as_posix() if isinstance(args.values_file, Path) else str(args.values_file)
    content = dedent(
        f"""\
        botName: "{bot}"
        repoURL: "{args.repo_url}"
        chartPath: "{chart_path}"
        targetRevision: "{args.target_revision}"
        valuesFile: "{values_file}"
        permissions:
          postgres: {str(bool(args.postgres)).lower()}
          prometheus: {str(bool(args.prometheus)).lower()}
          dbReadOnlyVPC: {str(bool(args.db_readonly_vpc)).lower()}
        """
    )
    write_file(app_path, content, force=args.force)


def scaffold_values_file(
    bot: str,
    values_file: Path,
    template: Path | None,
    include_db_secret: bool,
    force: bool,
    scaffold: bool,
    show_sample: bool,
) -> None:
    stub = values_stub(bot, include_db_secret)

    if show_sample:
        print("\nStarter values.yaml:\n")
        print(stub)

    if not scaffold:
        return

    if values_file.exists() and not force:
        print(f"Skip values file (exists): {values_file}")
        return

    if template and template.exists() and template.resolve() != values_file.resolve():
        values_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, values_file)
        print(f"Copied values template {template} -> {values_file}")
        return

    write_file(values_file, stub, force=True)


def scaffold_secrets(bot: str, include_db_secret: bool, force: bool) -> None:
    secrets_dir = REPO_ROOT / "secrets" / "bots" / bot

    db_section = ""
    if include_db_secret:
        db_section = dedent(
            f"""\

            2) Database credentials (if the bot needs Postgres):
               BOT_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \\
                 uv run python scripts/provision_bot_db.py {bot}
            """
        )

    readme = dedent(
        f"""\
        # {bot} Secrets

        Steps to populate/rotate secrets for this bot:

        1) Discord token:
           uv run python scripts/onboard_bot_secret.py {bot} "DISCORD_TOKEN"
           # or set BOT_TOKEN in .env and omit the token argument.{db_section}

        The ApplicationSet reads kustomization/ksops here to render encrypted secrets.
        """
    )

    kustomization = dedent(
        """\
        apiVersion: kustomize.config.k8s.io/v1beta1
        kind: Kustomization
        generators:
          - ksops.yaml
        """
    )

    secret_files = []
    if include_db_secret:
        secret_files.append("  - db-secret.enc.yaml")
    secret_files.append("  - token.enc.yaml")
    secret_files_block = "\n".join(secret_files)

    ksops = dedent(
        f"""\
        apiVersion: viaduct.ai/v1
        kind: ksops
        metadata:
          name: {bot}-secrets
        files:
        {secret_files_block}
        """
    )

    write_file(secrets_dir / "README.md", readme, force=force)
    write_file(secrets_dir / "kustomization.yaml", kustomization, force=force)
    write_file(secrets_dir / "ksops.yaml", ksops, force=force)


def resolve_token(token_arg: str | None) -> str:
    token = (token_arg or os.environ.get("BOT_TOKEN", "")).strip()
    if token:
        return token
    if not sys.stdin.isatty():
        sys.exit("BOT_TOKEN not provided and stdin is not interactive; pass --token or set BOT_TOKEN.")
    token = getpass.getpass("Enter the Discord bot token (input hidden): ").strip()
    if not token:
        sys.exit("Token cannot be empty.")
    return token


def write_token_secret(bot: str, token: str, namespace: str, force: bool) -> None:
    secret_path = REPO_ROOT / "secrets" / "bots" / bot / "token.enc.yaml"
    if secret_path.exists() and not force:
        print(f"Skip token secret (exists): {secret_path}")
        return

    manifest = dedent(
        f"""\
        apiVersion: v1
        kind: Secret
        metadata:
          name: bot-token
          namespace: {namespace}
        stringData:
          BOT_TOKEN: "{token}"
          DISCORD_TOKEN: "{token}"
        """
    )
    write_file(secret_path, manifest, force=True)
    encrypt_with_sops(secret_path)


def provision_bot_db(bot: str, namespace: str, args: argparse.Namespace) -> None:
    secret_path = repo_path(args.db_secret_file) or (REPO_ROOT / "secrets" / "bots" / bot / "db-secret.enc.yaml")

    if secret_path.exists() and not args.force:
        print(f"Skip DB provisioning (secret exists): {secret_path}")
        return

    if not args.db_admin_url:
        sys.exit("Provide --db-admin-url or set BOT_DB_ADMIN_URL to provision the DB.")

    if not PROVISION_SCRIPT.exists():
        sys.exit(f"Missing helper script: {PROVISION_SCRIPT}")

    env = os.environ.copy()
    env["BOT_DB_ADMIN_URL"] = args.db_admin_url
    env["BOT_DB_NAME"] = args.db_name

    cmd = [
        sys.executable,
        str(PROVISION_SCRIPT),
        bot,
        "--secret-file",
        str(secret_path),
        "--secret-name",
        args.db_secret_name,
        "--schema-key",
        args.db_schema_key,
        "--database",
        args.db_name,
    ]

    if namespace:
        cmd.extend(["--namespace", namespace])

    print(f"Provisioning Postgres + secret via provision_bot_db.py -> {secret_path}")
    try:
        subprocess.run(cmd, check=True, env=env)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"provision_bot_db.py failed (exit code {exc.returncode}). See output above.")


def main() -> None:
    load_env_file()
    args = parse_args()
    bot = normalize_bot_name(args.bot_name)
    namespace = args.namespace or f"splattop-bot-{bot}"
    include_db_secret = bool(args.postgres) or (args.provision_db is True)
    provision_db = args.provision_db if args.provision_db is not None else include_db_secret

    values_file_path = repo_path(args.values_file)
    if values_file_path is None:
        sys.exit("Invalid --values-file path.")
    values_template = repo_path(args.values_template) if args.values_template else None

    scaffold_app_file(bot, args)
    scaffold_secrets(bot, include_db_secret=include_db_secret, force=args.force)
    scaffold_values_file(
        bot,
        values_file_path,
        values_template,
        include_db_secret,
        force=args.force,
        scaffold=args.scaffold_values,
        show_sample=args.show_values_sample,
    )

    if include_db_secret:
        copy_ca(args.ca_source, args.ca_dest, force=args.force)
    else:
        print("Skip CA copy (postgres disabled).")

    if args.token_secret:
        token = resolve_token(args.token)
        write_token_secret(bot, token, namespace, force=args.force)
    else:
        print("Skip token secret (--no-token-secret).")

    if provision_db:
        provision_bot_db(bot, namespace, args)
    else:
        print("Skip DB provisioning (--no-provision-db).")

    print("\nDone. Generated scaffolding for:")
    print(f"- apps/bots/{bot}.yaml")
    print(f"- secrets/bots/{bot}/{{README.md,kustomization.yaml,ksops.yaml}}")
    if args.token_secret:
        print(f"- secrets/bots/{bot}/token.enc.yaml")
    if provision_db and include_db_secret:
        print(f"- secrets/bots/{bot}/db-secret.enc.yaml")
    if args.scaffold_values:
        print(f"- {values_file_path}")
    print("\nCommit the generated files (encrypted secrets only after SOPS).")


if __name__ == "__main__":
    main()
