#!/usr/bin/env python3
"""Set Agent Workloads OpenAI/Codex auth in SOPS without printing it."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRET_FILE = REPO_ROOT / "secrets" / "agent-workloads" / "runtime-secret.enc.yaml"
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
    codex_auth_json_file = os.environ.get("AGENT_WORKLOADS_CODEX_AUTH_JSON_FILE")
    parser = argparse.ArgumentParser(
        description="Store OPENAI_API_KEY or OPENAI_CODEX_AUTH_JSON in Agent Workloads SOPS."
    )
    parser.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET_FILE)
    parser.add_argument(
        "--openai-api-key",
        default=os.environ.get("AGENT_WORKLOADS_OPENAI_API_KEY"),
        help="OpenAI Platform API key. Defaults to AGENT_WORKLOADS_OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--codex-auth-json",
        default=os.environ.get("AGENT_WORKLOADS_CODEX_AUTH_JSON"),
        help="Raw Codex auth.json content. Defaults to AGENT_WORKLOADS_CODEX_AUTH_JSON.",
    )
    parser.add_argument(
        "--codex-auth-json-file",
        type=Path,
        default=Path(codex_auth_json_file) if codex_auth_json_file else None,
        help="Path to Codex auth.json to read without printing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate auth input and report which secret key would be updated.",
    )
    return parser.parse_args()


def ensure_command(name: str) -> None:
    if which(name) is None:
        sys.exit(f"{name} not found in PATH.")


def resolve_secret(args: argparse.Namespace) -> tuple[str, str]:
    provided = [
        bool(args.openai_api_key),
        bool(args.codex_auth_json),
        bool(args.codex_auth_json_file),
    ]
    if sum(provided) != 1:
        sys.exit(
            "Provide exactly one of AGENT_WORKLOADS_OPENAI_API_KEY, "
            "AGENT_WORKLOADS_CODEX_AUTH_JSON, or AGENT_WORKLOADS_CODEX_AUTH_JSON_FILE."
        )

    if args.openai_api_key:
        return "OPENAI_API_KEY", validate_openai_api_key(args.openai_api_key)

    if args.codex_auth_json_file:
        try:
            raw_auth_json = args.codex_auth_json_file.read_text()
        except OSError as exc:
            sys.exit(f"Could not read Codex auth.json file: {exc}")
    else:
        raw_auth_json = args.codex_auth_json
    return "OPENAI_CODEX_AUTH_JSON", validate_codex_auth_json(raw_auth_json)


def validate_openai_api_key(value: str | None) -> str:
    if not value:
        sys.exit("OpenAI API key was empty.")
    stripped = value.strip()
    if not stripped.startswith("sk-"):
        sys.exit("Expected an OpenAI Platform API key beginning with sk-.")
    return stripped


def validate_codex_auth_json(value: str | None) -> str:
    if not value:
        sys.exit("Codex auth.json content was empty.")
    stripped = value.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        sys.exit(f"Codex auth.json was not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        sys.exit("Codex auth.json must be a JSON object.")
    auth_mode = _codex_auth_mode(parsed)
    if auth_mode == "apikey":
        if _valid_codex_api_key_auth(parsed):
            return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        sys.exit("Codex auth.json auth_mode apikey requires OPENAI_API_KEY.")
    if auth_mode in {"chatgpt", "chatgptauthtokens"}:
        if _valid_codex_chatgpt_auth(parsed):
            return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        sys.exit(f"Codex auth.json auth_mode {parsed.get('auth_mode')} requires tokens.access_token.")
    if auth_mode is not None:
        sys.exit(f"Codex auth.json auth_mode is not supported: {parsed.get('auth_mode')}.")
    if _valid_codex_api_key_auth(parsed) or _valid_codex_chatgpt_auth(parsed):
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    sys.exit("Codex auth.json must include OPENAI_API_KEY or tokens.access_token.")


def _codex_auth_mode(parsed: dict[str, Any]) -> str | None:
    raw = parsed.get("auth_mode")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.replace("_", "").lower()


def _valid_codex_api_key_auth(parsed: dict[str, Any]) -> bool:
    value = parsed.get("OPENAI_API_KEY")
    return isinstance(value, str) and value.strip().startswith("sk-")


def _valid_codex_chatgpt_auth(parsed: dict[str, Any]) -> bool:
    tokens = parsed.get("tokens")
    if not isinstance(tokens, dict):
        return False
    return isinstance(tokens.get("access_token"), str) and bool(tokens["access_token"].strip())


def main() -> None:
    load_env_file()
    args = parse_args()
    secret_key, secret_value = resolve_secret(args)
    if args.dry_run:
        print(f"Would update {args.secret_file.relative_to(REPO_ROOT)} with {secret_key}.")
        return

    ensure_command("sops")
    env = os.environ.copy()
    if "SOPS_AGE_KEY_FILE" not in env and DEFAULT_SOPS_KEY.exists():
        env["SOPS_AGE_KEY_FILE"] = str(DEFAULT_SOPS_KEY)

    expression = f'["stringData"][{json.dumps(secret_key)}] {json.dumps(secret_value)}'
    result = subprocess.run(
        ["sops", "--set", expression, str(args.secret_file)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr.replace(secret_value, "[redacted]"))
        sys.exit(f"sops update failed for {args.secret_file}.")

    relative_path = args.secret_file.relative_to(REPO_ROOT)
    print(f"Updated {relative_path} with {secret_key}.")


if __name__ == "__main__":
    main()
