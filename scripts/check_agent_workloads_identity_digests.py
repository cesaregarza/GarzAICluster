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
REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
RUNTIME_SECRET_PATH = Path("secrets/agent-workloads/runtime-secret.enc.yaml")
TOKEN_SECRET_PATH = Path("secrets/agent-workloads/workload-identity-tokens.enc.yaml")
TOKEN_METADATA_PATH = Path(
    "secrets/agent-workloads/workload-identity-tokens.metadata.yaml"
)
SHA256_DIGEST_RE = re.compile(r"^sha256:[a-fA-F0-9]{64}$")
TOKEN_PREFIX = "mwit_v1"
DIGEST_SPEC_VERSION = "agent-workloads-code-digest-v2"
TOKEN_METADATA_SCHEMA_VERSION = "agent-workloads-workload-identity-tokens.metadata.v1"
WORKLOAD_IDENTITY_BUNDLE_VERSION = "workload_identity_bundle.v1"
WORKLOAD_NAMESPACE = "agent-workloads"
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
    parser.add_argument(
        "--workload-namespace",
        default=WORKLOAD_NAMESPACE,
        help="Kubernetes namespace used in projected ServiceAccount subjects.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compatibility flag; this command always checks and exits non-zero on drift.",
    )
    args = parser.parse_args()

    try:
        result = check_agent_workloads_identity_digests(
            repo_root=args.repo_root,
            values_path=args.values_path,
            overlay_configmap_path=args.overlay_configmap_path,
            runtime_secret_path=args.runtime_secret_path,
            token_secret_path=args.token_secret_path,
            token_metadata_path=args.token_metadata_path,
            workload_namespace=args.workload_namespace,
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
    workload_namespace: str = WORKLOAD_NAMESPACE,
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

    overlay_pins, overlay_imports = _load_overlay_release_state(
        repo_root / overlay_configmap_path
    )
    for agent_id in sorted(expected_agents):
        _assert_pin_matches_overlay(agent_id, release_pins[agent_id], overlay_pins[agent_id])
        _assert_values_image_digest_matches_pin(
            agent_id,
            values,
            release_pins[agent_id],
        )
    _assert_opencode_release_subject_bindings(
        values=values,
        overlay_pins=overlay_pins,
        overlay_imports=overlay_imports,
        workload_namespace=workload_namespace,
    )
    retained_hmac_pins = _retained_hmac_release_pins(
        values=values,
        overlay_pins=overlay_pins,
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
        expected_code_digest = retained_hmac_pins[agent_id]["codeDigest"]
        if token_code_digest != expected_code_digest:
            raise DriftGateError(
                f"{token_key} code_digest mismatch: expected {expected_code_digest}, "
                f"got {token_code_digest}"
            )
        _assert_token_bundle_claims_match(
            agent_id=agent_id,
            token_key=token_key,
            claims=claims,
            overlay_pin=retained_hmac_pins[agent_id],
        )
        token_claims_by_agent[agent_id] = claims

    _assert_token_metadata_matches(
        metadata_path=repo_root / token_metadata_path,
        token_secret_path=secret_path,
        configured_token_secret_path=token_secret_path,
        token_release_pins=retained_hmac_pins,
        token_claims_by_agent=token_claims_by_agent,
    )
    _assert_rollout_checksum_matches(
        values=values,
        ciphertext_sha256="sha256:" + hashlib.sha256(secret_path.read_bytes()).hexdigest(),
    )

    return (
        "agent-workloads deployed images and workload identity bundle claims "
        "match release pins and retained rollback tuples."
    )


def _load_overlay_release_state(
    configmap_path: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    data = _load_overlay_data(configmap_path)
    if not isinstance(data, dict):
        raise DriftGateError("registry overlay ConfigMap must contain data")
    imports = YAML_PARSER.load(data.get("workload_imports.yaml") or "")
    if not isinstance(imports, dict) or not isinstance(imports.get("imports"), list):
        raise DriftGateError("registry overlay workload_imports.yaml must contain imports")

    imports_by_id = {
        entry["id"]: entry
        for entry in imports["imports"]
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
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
    return pins, imports_by_id


def _assert_opencode_release_subject_bindings(
    *,
    values: dict[str, Any],
    overlay_pins: dict[str, dict[str, str]],
    overlay_imports: dict[str, dict[str, Any]],
    workload_namespace: str,
) -> None:
    handoff = values.get("opencodeArtifactHandoff")
    if not isinstance(handoff, dict) or handoff.get("mode") != "governedCore":
        return
    if not isinstance(workload_namespace, str) or not workload_namespace.strip():
        raise DriftGateError("workload namespace must be non-empty")

    current_subjects: dict[str, str] = {}
    all_subjects: dict[str, str] = {}
    for agent_id, values_key in OPENCODE_VALUES_KEYS_BY_AGENT_ID.items():
        worker_values = _required_mapping(values, values_key, "agent-workloads values")
        identity = _required_mapping(worker_values, "identity", values_key)
        worker_id = _required_str(identity, "workerId", f"{values_key}.identity")
        if worker_id != agent_id:
            raise DriftGateError(
                f"{values_key}.identity.workerId must equal {agent_id}"
            )
        prefix = _required_str(
            identity,
            "serviceAccountNamePrefix",
            f"{values_key}.identity",
        )
        current_subject = _release_service_account_subject(
            namespace=workload_namespace,
            prefix=prefix,
            worker_id=agent_id,
            release=overlay_pins[agent_id],
        )
        current_subjects[agent_id] = current_subject
        _claim_unique_subject(
            all_subjects,
            subject=current_subject,
            owner=f"{agent_id}:current",
        )

        import_entry = overlay_imports[agent_id]
        agent = _required_mapping(import_entry, "agent", f"{agent_id} import")
        token = _required_mapping(identity, "token", f"{values_key}.identity")
        expected_audience = _required_str(
            token,
            "audience",
            f"{values_key}.identity.token",
        )
        if agent.get("identity_audience") != expected_audience:
            raise DriftGateError(
                f"{agent_id} identity_audience differs from governed render: "
                f"expected {expected_audience}, got {agent.get('identity_audience')}"
            )

        identity_mode = _required_str(
            identity,
            "mode",
            f"{values_key}.identity",
        )
        configured_previous = identity.get("previousRelease")
        imported_previous = agent.get("previous_release")
        actual_subject = agent.get("service_account_subject")
        if identity_mode == "hmac":
            if actual_subject is not None or imported_previous is not None:
                raise DriftGateError(
                    f"{agent_id} HMAC identity must not declare projected release subjects"
                )
            if configured_previous is not None:
                raise DriftGateError(
                    f"{values_key}.identity.previousRelease requires projected mode"
                )
            continue
        if identity_mode != "projected":
            raise DriftGateError(
                f"{values_key}.identity.mode must be hmac or projected"
            )
        if actual_subject != current_subject:
            raise DriftGateError(
                f"{agent_id} service_account_subject differs from governed render: "
                f"expected {current_subject}, got {actual_subject}"
            )
        if configured_previous is None:
            if imported_previous is not None:
                raise DriftGateError(
                    f"{agent_id} registry previous_release is not rendered by Helm values"
                )
            continue
        if not isinstance(configured_previous, dict):
            raise DriftGateError(
                f"{values_key}.identity.previousRelease must be a mapping"
            )
        if not isinstance(imported_previous, dict):
            raise DriftGateError(
                f"{agent_id} registry previous_release is required for rollout overlap"
            )
        previous_subject = _release_service_account_subject(
            namespace=workload_namespace,
            prefix=prefix,
            worker_id=agent_id,
            release=configured_previous,
        )
        _claim_unique_subject(
            all_subjects,
            subject=previous_subject,
            owner=f"{agent_id}:previous",
        )
        expected_previous = {
            "service_account_subject": previous_subject,
            "code_digest": _required_str(
                configured_previous,
                "codeDigest",
                f"{values_key}.identity.previousRelease",
            ),
            "manifest_digest": _required_str(
                configured_previous,
                "manifestDigest",
                f"{values_key}.identity.previousRelease",
            ),
            "image_digest": _required_str(
                configured_previous,
                "imageDigest",
                f"{values_key}.identity.previousRelease",
            ),
        }
        if imported_previous != expected_previous:
            raise DriftGateError(
                f"{agent_id} registry previous_release differs from governed render"
            )

    if len(set(current_subjects.values())) != len(current_subjects):
        raise DriftGateError(
            "governed OpenCode proposer and apply subjects must be distinct"
        )


def _retained_hmac_release_pins(
    *,
    values: dict[str, Any],
    overlay_pins: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    retained_pins = {
        agent_id: dict(release)
        for agent_id, release in overlay_pins.items()
    }
    handoff = values.get("opencodeArtifactHandoff")
    if not isinstance(handoff, dict) or handoff.get("mode") != "governedCore":
        return retained_pins

    for agent_id, values_key in OPENCODE_VALUES_KEYS_BY_AGENT_ID.items():
        worker_values = _required_mapping(
            values,
            values_key,
            "agent-workloads values",
        )
        identity = _required_mapping(worker_values, "identity", values_key)
        if identity.get("mode") != "projected":
            continue
        previous_release = identity.get("previousRelease")
        if previous_release is None:
            continue
        if not isinstance(previous_release, dict):
            raise DriftGateError(
                f"{values_key}.identity.previousRelease must be a mapping"
            )
        retained_pins[agent_id] = {
            "codeDigest": _required_str(
                previous_release,
                "codeDigest",
                f"{values_key}.identity.previousRelease",
            ),
            "manifestDigest": _required_str(
                previous_release,
                "manifestDigest",
                f"{values_key}.identity.previousRelease",
            ),
            "imageDigest": _required_str(
                previous_release,
                "imageDigest",
                f"{values_key}.identity.previousRelease",
            ),
        }
    return retained_pins


def _release_service_account_subject(
    *,
    namespace: str,
    prefix: str,
    worker_id: str,
    release: dict[str, Any],
) -> str:
    digests = {
        "code_digest": _required_str(release, "codeDigest", worker_id),
        "manifest_digest": _required_str(release, "manifestDigest", worker_id),
        "image_digest": _required_str(release, "imageDigest", worker_id),
    }
    for label, digest in digests.items():
        _validate_digest(digest, f"{worker_id} {label}")
    payload = {
        "schema_version": WORKLOAD_IDENTITY_BUNDLE_VERSION,
        **digests,
    }
    suffix = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    worker_name = re.sub(r"[^a-z0-9]+", "-", worker_id.lower()).strip("-")
    service_account_name = f"{prefix}-{worker_name}-{suffix}"
    if len(service_account_name) > 63 or re.fullmatch(
        r"[a-z0-9]([-a-z0-9]*[a-z0-9])?",
        service_account_name,
    ) is None:
        raise DriftGateError(
            f"{worker_id} release-scoped ServiceAccount name is invalid"
        )
    return f"system:serviceaccount:{namespace}:{service_account_name}"


def _claim_unique_subject(
    seen: dict[str, str],
    *,
    subject: str,
    owner: str,
) -> None:
    existing = seen.get(subject)
    if existing is not None:
        raise DriftGateError(
            f"governed OpenCode subject maps to multiple releases: "
            f"{subject} ({existing}, {owner})"
        )
    seen[subject] = owner


def _required_mapping(
    mapping: dict[str, Any],
    key: str,
    label: str,
) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise DriftGateError(f"{label} {key} must be a mapping")
    return value


def _load_overlay_data(configmap_path: Path) -> dict[str, str]:
    if configmap_path.exists():
        configmap = _load_yaml(configmap_path)
        data = configmap.get("data")
        if not isinstance(data, dict):
            raise DriftGateError("registry overlay ConfigMap must contain data")
        return {
            key: value
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    overlay_dir = configmap_path.parent
    kustomization_path = overlay_dir / "kustomization.yaml"
    if not kustomization_path.exists():
        raise DriftGateError("registry overlay ConfigMap must contain data")
    return _load_overlay_data_from_kustomization(overlay_dir, kustomization_path)


def _load_overlay_data_from_kustomization(
    overlay_dir: Path,
    kustomization_path: Path,
) -> dict[str, str]:
    kustomization = _load_yaml(kustomization_path)
    generators = kustomization.get("configMapGenerator")
    if not isinstance(generators, list):
        raise DriftGateError("registry overlay kustomization missing configMapGenerator")
    generator = next(
        (
            item
            for item in generators
            if isinstance(item, dict)
            and item.get("name") == REGISTRY_OVERLAY_CONFIGMAP_NAME
        ),
        None,
    )
    if generator is None:
        raise DriftGateError(
            f"registry overlay kustomization missing {REGISTRY_OVERLAY_CONFIGMAP_NAME}"
        )
    files = generator.get("files")
    if not isinstance(files, list):
        raise DriftGateError("registry overlay ConfigMap generator must contain files")
    data: dict[str, str] = {}
    for raw_spec in files:
        if not isinstance(raw_spec, str) or not raw_spec:
            raise DriftGateError("registry overlay ConfigMap file spec is invalid")
        key, relative_path = _parse_kustomize_file_spec(raw_spec)
        source_path = (overlay_dir / relative_path).resolve()
        try:
            source_path.relative_to(overlay_dir.resolve())
        except ValueError as exc:
            raise DriftGateError(
                f"registry overlay ConfigMap file escapes overlay directory: {relative_path}"
            ) from exc
        if not source_path.is_file():
            raise DriftGateError(
                f"registry overlay ConfigMap source file not found: {relative_path}"
            )
        data[key] = source_path.read_text(encoding="utf-8")
    return data


def _parse_kustomize_file_spec(file_spec: str) -> tuple[str, str]:
    if "=" in file_spec:
        key, relative_path = file_spec.split("=", 1)
    else:
        relative_path = file_spec
        key = Path(relative_path).name
    if not key or not relative_path:
        raise DriftGateError(f"registry overlay ConfigMap file spec is invalid: {file_spec}")
    if "/" in key or key in {"", ".", ".."}:
        raise DriftGateError(f"registry overlay ConfigMap key is invalid: {key}")
    return key, relative_path


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


def _assert_token_bundle_claims_match(
    *,
    agent_id: str,
    token_key: str,
    claims: dict[str, Any],
    overlay_pin: dict[str, str],
) -> None:
    expected_manifest_digest = overlay_pin["manifestDigest"]
    actual_manifest_digest = _required_digest_claim(
        claims,
        claim="manifest_digest",
        token_key=token_key,
    )
    if actual_manifest_digest != expected_manifest_digest:
        raise DriftGateError(
            f"{token_key} manifest_digest mismatch: expected {expected_manifest_digest}, "
            f"got {actual_manifest_digest}"
        )

    expected_image_digest = overlay_pin["imageDigest"]
    actual_image_digest = _required_digest_claim(
        claims,
        claim="image_digest",
        token_key=token_key,
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
        claims,
        claim="bundle_digest",
        token_key=token_key,
    )
    if actual_bundle_digest != expected_bundle_digest:
        raise DriftGateError(
            f"{token_key} bundle_digest mismatch for {agent_id}: "
            f"expected {expected_bundle_digest}, got {actual_bundle_digest}"
        )


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


def _assert_token_metadata_matches(
    *,
    metadata_path: Path,
    token_secret_path: Path,
    configured_token_secret_path: Path,
    token_release_pins: dict[str, dict[str, str]],
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


def _assert_rollout_checksum_matches(
    *,
    values: dict[str, Any],
    ciphertext_sha256: str,
) -> None:
    rollout_checksums = values.get("rolloutChecksums")
    if not isinstance(rollout_checksums, dict):
        raise DriftGateError("rolloutChecksums must be a mapping")
    actual = rollout_checksums.get("workloadIdentityTokenSecret")
    if actual != ciphertext_sha256:
        raise DriftGateError(
            "rolloutChecksums.workloadIdentityTokenSecret mismatch: "
            f"expected {ciphertext_sha256}, got {actual}"
        )


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
