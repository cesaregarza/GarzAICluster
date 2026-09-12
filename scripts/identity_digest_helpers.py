"""Shared parsing and metadata checks for the workload identity digest gate."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


SHA256_DIGEST_RE = re.compile(r"^sha256:[a-fA-F0-9]{64}$")
TOKEN_PREFIX = "mwit_v1"
TOKEN_METADATA_SCHEMA_VERSION = "agent-workloads-workload-identity-tokens.metadata.v1"
WORKLOAD_IDENTITY_BUNDLE_VERSION = "workload_identity_bundle.v1"
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
OPENCODE_VALUES_KEYS_BY_AGENT_ID = {
    "opencode.proposer": "opencodeProposer",
    "opencode.apply_executor": "opencodeApplyExecutor",
}

YAML_PARSER = YAML(typ="safe")


class DriftGateError(RuntimeError):
    pass


def _base64_url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}")


def _workload_identity_bundle_digest(
    *,
    code_digest: str,
    manifest_digest: str,
    image_digest: str,
) -> str:
    payload = {
        "schema_version": WORKLOAD_IDENTITY_BUNDLE_VERSION,
        "code_digest": code_digest,
        "manifest_digest": manifest_digest,
        "image_digest": image_digest,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def _required_digest_claim(
    claims: dict[str, Any],
    *,
    claim: str,
    token_key: str,
) -> str:
    raw = claims.get(claim)
    if raw is None:
        raise DriftGateError(f"{token_key} token payload missing {claim}")
    _validate_digest(raw, f"{token_key} {claim}")
    return raw


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
    _validate_digest(payload.get("code_digest"), f"{token_key} code_digest")
    return payload


def _assert_token_bundle_claims_match(
    *,
    agent_id: str,
    token_key: str,
    claims: dict[str, Any],
    overlay_pin: dict[str, str],
) -> None:
    expected_manifest_digest = overlay_pin["manifestDigest"]
    actual_manifest_digest = _required_digest_claim(
        claims, claim="manifest_digest", token_key=token_key
    )
    if actual_manifest_digest != expected_manifest_digest:
        raise DriftGateError(
            f"{token_key} manifest_digest mismatch: expected {expected_manifest_digest}, "
            f"got {actual_manifest_digest}"
        )

    expected_image_digest = overlay_pin["imageDigest"]
    actual_image_digest = _required_digest_claim(
        claims, claim="image_digest", token_key=token_key
    )
    if actual_image_digest != expected_image_digest:
        raise DriftGateError(
            f"{token_key} image_digest mismatch: expected {expected_image_digest}, "
            f"got {actual_image_digest}"
        )

    expected_bundle_digest = _workload_identity_bundle_digest(
        code_digest=overlay_pin["codeDigest"],
        manifest_digest=expected_manifest_digest,
        image_digest=expected_image_digest,
    )
    actual_bundle_digest = _required_digest_claim(
        claims, claim="bundle_digest", token_key=token_key
    )
    if actual_bundle_digest != expected_bundle_digest:
        raise DriftGateError(
            f"{token_key} bundle_digest mismatch for {agent_id}: "
            f"expected {expected_bundle_digest}, got {actual_bundle_digest}"
        )


def _assert_token_metadata_matches(
    *,
    metadata_path: Path,
    token_secret_path: Path,
    configured_token_secret_path: Path,
    token_release_pins: dict[str, dict[str, str]],
    token_claims_by_agent: dict[str, dict[str, Any]],
    digest_spec_version: str,
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
        token_release = token_release_pins[agent_id]
        expected = {
            "agent_id": agent_id,
            "token_key": TOKEN_KEYS_BY_AGENT_ID[agent_id],
            "code_digest": token_release["codeDigest"],
            "manifest_digest": token_release["manifestDigest"],
            "image_digest": token_release["imageDigest"],
            "bundle_digest": _workload_identity_bundle_digest(
                code_digest=token_release["codeDigest"],
                manifest_digest=token_release["manifestDigest"],
                image_digest=token_release["imageDigest"],
            ),
            "iss": claims["iss"],
            "sub": claims["sub"],
            "aud": claims["aud"],
            "iat": claims.get("iat"),
            "exp": claims["exp"],
            "digest_spec_version": digest_spec_version,
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


def _required_str(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise DriftGateError(f"{label} missing non-empty {key}")
    return value


def _validate_digest(raw: Any, label: str) -> None:
    if not isinstance(raw, str) or SHA256_DIGEST_RE.fullmatch(raw) is None:
        raise DriftGateError(f"{label} must be sha256:<64 hex>")


def _required_mapping(
    mapping: dict[str, Any],
    key: str,
    label: str,
) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise DriftGateError(f"{label} {key} must be a mapping")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise DriftGateError(f"YAML mapping expected: {path}")
    return loaded
