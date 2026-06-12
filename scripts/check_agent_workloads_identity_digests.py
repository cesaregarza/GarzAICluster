#!/usr/bin/env python3
"""Fail closed when agent-workloads release pins drift from identity tokens."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
VALUES_PATH = Path("apps/agent-workloads/values.yaml")
OVERLAY_CONFIGMAP_PATH = Path("apps/agent-control-plane-registry-overlay/configmap.yaml")
RUNTIME_SECRET_PATH = Path("secrets/agent-workloads/runtime-secret.enc.yaml")
TOKEN_SECRET_PATH = Path("secrets/agent-workloads/workload-identity-tokens.enc.yaml")
TOKEN_METADATA_PATH = Path(
    "secrets/agent-workloads/workload-identity-tokens.metadata.yaml"
)
SHA256_DIGEST_RE = re.compile(r"^sha256:[a-fA-F0-9]{64}$")
TOKEN_PREFIX = "mwit_v1"
DIGEST_SPEC_VERSION = "agent-workloads-code-digest-v1"
TOKEN_METADATA_SCHEMA_VERSION = "agent-workloads-workload-identity-tokens.metadata.v1"
TOKEN_KEYS_BY_AGENT_ID = {
    "data.workspace_probe": "MANDATE_WORKLOAD_IDENTITY_TOKEN",
    "opencode.proposer": "OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN",
    "opencode.apply_executor": "OPENCODE_APPLY_EXECUTOR_WORKLOAD_IDENTITY_TOKEN",
}
IMAGE_PATHS_BY_AGENT_ID = {
    "data.workspace_probe": ("image",),
    "opencode.proposer": ("opencodeProposer", "image"),
    "opencode.apply_executor": ("opencodeApplyExecutor", "image"),
}

YAML_PARSER = YAML(typ="safe")


class DriftGateError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare agent-workloads mandateReleasePins and registry overlay "
            "code digests to SOPS-managed workload identity token claims."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--values-path", type=Path, default=VALUES_PATH)
    parser.add_argument("--overlay-configmap-path", type=Path, default=OVERLAY_CONFIGMAP_PATH)
    parser.add_argument("--runtime-secret-path", type=Path, default=RUNTIME_SECRET_PATH)
    parser.add_argument("--token-secret-path", type=Path, default=TOKEN_SECRET_PATH)
    parser.add_argument("--token-metadata-path", type=Path, default=TOKEN_METADATA_PATH)
    args = parser.parse_args()

    try:
        result = check_agent_workloads_identity_digests(
            repo_root=args.repo_root,
            values_path=args.values_path,
            overlay_configmap_path=args.overlay_configmap_path,
            runtime_secret_path=args.runtime_secret_path,
            token_secret_path=args.token_secret_path,
            token_metadata_path=args.token_metadata_path,
        )
    except DriftGateError as exc:
        print(f"agent-workloads identity digest gate failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


def check_agent_workloads_identity_digests(
    *,
    repo_root: Path,
    values_path: Path,
    overlay_configmap_path: Path,
    runtime_secret_path: Path,
    token_secret_path: Path,
    token_metadata_path: Path,
) -> str:
    values = _load_yaml(repo_root / values_path)
    release_pins = values.get("mandateReleasePins")
    if release_pins in (None, {}):
        return "agent-workloads mandateReleasePins absent; identity digest gate inactive."
    if not isinstance(release_pins, dict):
        raise DriftGateError("mandateReleasePins must be a mapping")

    expected_agents = set(TOKEN_KEYS_BY_AGENT_ID)
    pinned_agents = set(release_pins)
    if pinned_agents != expected_agents:
        raise DriftGateError(
            "mandateReleasePins must cover exactly "
            f"{', '.join(sorted(expected_agents))}; got {', '.join(sorted(pinned_agents))}"
        )

    overlay_pins = _load_overlay_pins(repo_root / overlay_configmap_path)
    for agent_id in sorted(expected_agents):
        _assert_pin_matches_overlay(agent_id, release_pins[agent_id], overlay_pins[agent_id])
        _assert_values_image_digest_matches_pin(
            agent_id,
            values,
            release_pins[agent_id],
        )

    _assert_runtime_secret_excludes_tokens(repo_root / runtime_secret_path)
    secret_path = repo_root / token_secret_path
    secret = _load_secret(
        secret_path,
        cwd=repo_root,
        label="workload identity token secret",
    )
    token_claims_by_agent: dict[str, dict[str, Any]] = {}
    for agent_id in sorted(expected_agents):
        token_key = TOKEN_KEYS_BY_AGENT_ID[agent_id]
        token = _secret_value(secret, token_key)
        claims = _workload_identity_claims(token, token_key)
        token_code_digest = str(claims["code_digest"])
        expected_code_digest = overlay_pins[agent_id]["codeDigest"]
        if token_code_digest != expected_code_digest:
            raise DriftGateError(
                f"{token_key} code_digest mismatch: expected {expected_code_digest}, "
                f"got {token_code_digest}"
            )
        token_claims_by_agent[agent_id] = claims

    _assert_token_metadata_matches(
        metadata_path=repo_root / token_metadata_path,
        token_secret_path=secret_path,
        configured_token_secret_path=token_secret_path,
        overlay_pins=overlay_pins,
        token_claims_by_agent=token_claims_by_agent,
    )

    return "agent-workloads deployed images and workload identity code_digests match release pins."


def _load_overlay_pins(configmap_path: Path) -> dict[str, dict[str, str]]:
    configmap = _load_yaml(configmap_path)
    data = configmap.get("data")
    if not isinstance(data, dict):
        raise DriftGateError("registry overlay ConfigMap must contain data")
    imports = YAML_PARSER.load(data.get("workload_imports.yaml") or "")
    if not isinstance(imports, dict) or not isinstance(imports.get("imports"), list):
        raise DriftGateError("registry overlay workload_imports.yaml must contain imports")

    imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
    pins: dict[str, dict[str, str]] = {}
    for agent_id in TOKEN_KEYS_BY_AGENT_ID:
        import_entry = imports_by_id.get(agent_id)
        if not isinstance(import_entry, dict):
            raise DriftGateError(f"registry overlay missing import for {agent_id}")
        manifest_key = Path(_required_str(import_entry, "manifest_path", agent_id)).name
        manifest = json.loads(_required_str(data, manifest_key, "registry overlay data"))
        code_digest = _required_str(manifest, "code_digest", agent_id)
        manifest_digest = _required_str(manifest, "digest", agent_id)
        image = manifest.get("image")
        if not isinstance(image, dict):
            raise DriftGateError(f"{agent_id} manifest image must be a mapping")
        image_digest = _required_str(image, "digest", agent_id)
        _validate_digest(code_digest, f"{agent_id} codeDigest")
        _validate_digest(manifest_digest, f"{agent_id} manifestDigest")
        _validate_digest(image_digest, f"{agent_id} imageDigest")
        if import_entry.get("manifest_digest") != manifest_digest:
            raise DriftGateError(f"{agent_id} import manifest_digest differs from manifest")
        if import_entry.get("image_digest") != image_digest:
            raise DriftGateError(f"{agent_id} import image_digest differs from manifest")
        pins[agent_id] = {
            "codeDigest": code_digest,
            "manifestDigest": manifest_digest,
            "imageDigest": image_digest,
        }
    return pins


def _assert_pin_matches_overlay(
    agent_id: str,
    release_pin: Any,
    overlay_pin: dict[str, str],
) -> None:
    if not isinstance(release_pin, dict):
        raise DriftGateError(f"{agent_id} mandateReleasePins entry must be a mapping")
    for key, expected in overlay_pin.items():
        actual = release_pin.get(key)
        if actual != expected:
            raise DriftGateError(
                f"{agent_id} mandateReleasePins.{key} differs from registry overlay: "
                f"expected {expected}, got {actual}"
            )


def _assert_values_image_digest_matches_pin(
    agent_id: str,
    values: dict[str, Any],
    release_pin: Any,
) -> None:
    if not isinstance(release_pin, dict):
        raise DriftGateError(f"{agent_id} mandateReleasePins entry must be a mapping")
    expected = release_pin.get("imageDigest")
    _validate_digest(expected, f"{agent_id} mandateReleasePins.imageDigest")

    image_path = IMAGE_PATHS_BY_AGENT_ID[agent_id]
    image = _nested_mapping(values, image_path, f"{agent_id} values image")
    actual = image.get("digest")
    _validate_digest(actual, f"{agent_id} values image.digest")
    if actual != expected:
        raise DriftGateError(
            f"{agent_id} values image.digest differs from mandateReleasePins.imageDigest: "
            f"expected {expected}, got {actual}"
        )


def _nested_mapping(
    mapping: dict[str, Any],
    path: tuple[str, ...],
    label: str,
) -> dict[str, Any]:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict) or not isinstance(current.get(key), dict):
            dotted = ".".join(path)
            raise DriftGateError(f"{label} must be a mapping at {dotted}")
        current = current[key]
    return current


def _assert_runtime_secret_excludes_tokens(secret_path: Path) -> None:
    raw = secret_path.read_text(encoding="utf-8")
    for token_key in TOKEN_KEYS_BY_AGENT_ID.values():
        if token_key in raw:
            raise DriftGateError(
                f"runtime secret must not contain workload identity token key {token_key}"
            )


def _load_secret(secret_path: Path, *, cwd: Path, label: str) -> dict[str, Any]:
    raw = secret_path.read_text(encoding="utf-8")
    loaded = YAML_PARSER.load(raw)
    if not isinstance(loaded, dict):
        raise DriftGateError(f"{label} must be a YAML mapping: {secret_path}")
    if "sops" not in loaded:
        return loaded

    env = os.environ.copy()
    try:
        result = subprocess.run(
            ["sops", "--decrypt", str(secret_path)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DriftGateError(f"sops is required to decrypt {label}") from exc
    if result.returncode != 0:
        raise DriftGateError(f"could not decrypt {label} with sops")
    decrypted = YAML_PARSER.load(result.stdout)
    if not isinstance(decrypted, dict):
        raise DriftGateError(f"decrypted {label} must be a YAML mapping")
    return decrypted


def _secret_value(secret: dict[str, Any], token_key: str) -> str:
    string_data = secret.get("stringData")
    if isinstance(string_data, dict):
        value = string_data.get(token_key)
        if isinstance(value, str) and value:
            return value

    data = secret.get("data")
    if isinstance(data, dict):
        encoded = data.get(token_key)
        if isinstance(encoded, str) and encoded:
            try:
                return base64.b64decode(encoded, validate=True).decode()
            except (ValueError, UnicodeDecodeError) as exc:
                raise DriftGateError(f"{token_key} data value is not valid base64") from exc

    raise DriftGateError(f"workload identity token secret missing {token_key}")


def _workload_identity_claims(token: str, token_key: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise DriftGateError(f"{token_key} is not an {TOKEN_PREFIX} token")
    try:
        payload = json.loads(_base64_url_decode(parts[1]).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise DriftGateError(f"{token_key} has malformed token payload") from exc
    if not isinstance(payload, dict):
        raise DriftGateError(f"{token_key} token payload must be a mapping")

    for claim in ("iss", "sub", "aud", "iat", "exp"):
        if claim not in payload:
            raise DriftGateError(f"{token_key} token payload missing {claim}")
    scopes = payload.get("scp")
    if not isinstance(scopes, list) or "worker_service" not in scopes:
        raise DriftGateError(f"{token_key} token payload missing worker_service scope")
    code_digest = payload.get("code_digest")
    _validate_digest(code_digest, f"{token_key} code_digest")
    return payload


def _assert_token_metadata_matches(
    *,
    metadata_path: Path,
    token_secret_path: Path,
    configured_token_secret_path: Path,
    overlay_pins: dict[str, dict[str, str]],
    token_claims_by_agent: dict[str, dict[str, Any]],
) -> None:
    metadata = _load_yaml(metadata_path)
    if metadata.get("schema_version") != TOKEN_METADATA_SCHEMA_VERSION:
        raise DriftGateError(
            "workload identity token metadata has unexpected schema_version"
        )
    if metadata.get("token_secret_path") != configured_token_secret_path.as_posix():
        raise DriftGateError("workload identity token metadata token_secret_path mismatch")
    tokens = metadata.get("tokens")
    if not isinstance(tokens, dict):
        raise DriftGateError("workload identity token metadata tokens must be a mapping")
    expected_agents = set(TOKEN_KEYS_BY_AGENT_ID)
    if set(tokens) != expected_agents:
        raise DriftGateError(
            "workload identity token metadata must cover exactly "
            f"{', '.join(sorted(expected_agents))}; got {', '.join(sorted(tokens))}"
        )

    ciphertext_sha256 = "sha256:" + hashlib.sha256(token_secret_path.read_bytes()).hexdigest()
    for agent_id in sorted(expected_agents):
        entry = tokens[agent_id]
        if not isinstance(entry, dict):
            raise DriftGateError(f"{agent_id} token metadata entry must be a mapping")
        claims = token_claims_by_agent[agent_id]
        expected = {
            "agent_id": agent_id,
            "token_key": TOKEN_KEYS_BY_AGENT_ID[agent_id],
            "code_digest": overlay_pins[agent_id]["codeDigest"],
            "manifest_digest": overlay_pins[agent_id]["manifestDigest"],
            "iss": claims["iss"],
            "sub": claims["sub"],
            "aud": claims["aud"],
            "iat": claims.get("iat"),
            "exp": claims["exp"],
            "digest_spec_version": DIGEST_SPEC_VERSION,
            "ciphertext_sha256": ciphertext_sha256,
        }
        for key, value in expected.items():
            if entry.get(key) != value:
                raise DriftGateError(f"{agent_id} token metadata {key} mismatch")
        if entry.get("scp") != claims["scp"]:
            raise DriftGateError(f"{agent_id} token metadata scp mismatch")
        source_commit = entry.get("source_commit")
        if not isinstance(source_commit, str) or not source_commit:
            raise DriftGateError(f"{agent_id} token metadata source_commit is required")


def _base64_url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}")


def _required_str(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise DriftGateError(f"{label} missing non-empty {key}")
    return value


def _validate_digest(raw: Any, label: str) -> None:
    if not isinstance(raw, str) or SHA256_DIGEST_RE.fullmatch(raw) is None:
        raise DriftGateError(f"{label} must be sha256:<64 hex>")


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise DriftGateError(f"YAML mapping expected: {path}")
    return loaded


if __name__ == "__main__":
    sys.exit(main())
