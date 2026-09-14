#!/usr/bin/env python3
"""Fail closed when agent-workloads release pins drift from identity tokens."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    from scripts.registry_overlay_render import (
        RegistryOverlayRenderError,
        render_registry_overlay_data,
    )
except ModuleNotFoundError:  # Direct ``python scripts/check_*.py`` execution.
    from registry_overlay_render import (  # type: ignore[no-redef]
        RegistryOverlayRenderError,
        render_registry_overlay_data,
    )

try:
    from scripts.identity_digest_helpers import (
        YAML_PARSER,
        DriftGateError,
        _assert_token_bundle_claims_match,
        _assert_token_metadata_matches,
        _load_yaml,
        _required_mapping,
        _required_str,
        _validate_digest,
        _workload_identity_claims,
    )
    from scripts.worker_sdk_receipt import SDKReceiptError, load_sdk_receipt
except ModuleNotFoundError:  # Direct ``python scripts/check_*.py`` execution.
    from identity_digest_helpers import (  # type: ignore[no-redef]
        YAML_PARSER,
        DriftGateError,
        _assert_token_bundle_claims_match,
        _assert_token_metadata_matches,
        _load_yaml,
        _required_mapping,
        _required_str,
        _validate_digest,
        _workload_identity_claims,
    )
    from worker_sdk_receipt import (  # type: ignore[no-redef]
        SDKReceiptError,
        load_sdk_receipt,
    )


try:
    from scripts.worker_identity_bindings import (
        assert_release_subject_bindings,
        configured_workers,
        retained_hmac_configuration,
    )
except ModuleNotFoundError:
    from worker_identity_bindings import (
        assert_release_subject_bindings,
        configured_workers,
        retained_hmac_configuration,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
VALUES_PATH = Path("apps/agent-workloads/values.yaml")
OVERLAY_CONFIGMAP_PATH = Path(
    "apps/agent-control-plane-registry-overlay/configmap.yaml"
)
REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
RUNTIME_SECRET_PATH = Path("secrets/agent-workloads/runtime-secret.enc.yaml")
TOKEN_SECRET_PATH = Path("secrets/agent-workloads/workload-identity-tokens.enc.yaml")
TOKEN_METADATA_PATH = Path(
    "secrets/agent-workloads/workload-identity-tokens.metadata.yaml"
)
WORKLOAD_NAMESPACE = "agent-workloads"
SDK_RECEIPT_PATH = Path("contracts/mandate-worker/receipt.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare agent-workloads mandateReleasePins and registry overlay "
            "code digests to SOPS-managed workload identity token claims."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--values-path", type=Path, default=VALUES_PATH)
    parser.add_argument(
        "--overlay-configmap-path", type=Path, default=OVERLAY_CONFIGMAP_PATH
    )
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
        return (
            "agent-workloads mandateReleasePins absent; identity digest gate inactive."
        )
    if not isinstance(release_pins, dict):
        raise DriftGateError("mandateReleasePins must be a mapping")
    try:
        sdk_receipt = load_sdk_receipt(repo_root / SDK_RECEIPT_PATH)
    except SDKReceiptError as exc:
        raise DriftGateError(f"invalid mandate-worker SDK receipt: {exc}") from exc
    digest_spec_version = sdk_receipt["digest_spec_version"]

    workers = configured_workers(values)
    overlay_pins, overlay_imports = _check_current_releases(
        values, workers, release_pins, repo_root / overlay_configmap_path
    )
    core_values = _load_yaml(repo_root / "apps/agent-control-plane/values.yaml")
    core_env = _required_mapping(core_values, "env", "Core values")
    default_identity_audience = _required_str(
        core_env, "AGENT_PLATFORM_WORKLOAD_IDENTITY_AUDIENCE", "Core verifier env"
    )
    assert_release_subject_bindings(
        workers=workers,
        overlay_pins=overlay_pins,
        overlay_imports=overlay_imports,
        workload_namespace=workload_namespace,
        default_identity_audience=default_identity_audience,
    )
    token_keys, retained_hmac_pins = retained_hmac_configuration(workers)

    if token_keys:
        _check_retained_tokens(
            repo_root=repo_root,
            values=values,
            runtime_secret_path=runtime_secret_path,
            token_secret_path=token_secret_path,
            token_metadata_path=token_metadata_path,
            workers=workers,
            token_keys=token_keys,
            retained_hmac_pins=retained_hmac_pins,
            digest_spec_version=digest_spec_version,
        )

    return (
        "agent-workloads deployed images and workload identity bundle claims "
        "match release pins and retained rollback tuples."
    )


def _check_current_releases(
    values: dict[str, Any],
    workers: dict[str, Any],
    release_pins: dict[str, Any],
    overlay_path: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    expected_agents = set(workers)
    pinned_agents = set(release_pins)
    if pinned_agents != expected_agents:
        raise DriftGateError(
            "mandateReleasePins must cover exactly "
            f"{', '.join(sorted(expected_agents))}; got {', '.join(sorted(pinned_agents))}"
        )

    overlay_pins, overlay_imports = _load_overlay_release_state(
        overlay_path, expected_agents
    )
    for agent_id in sorted(expected_agents):
        _assert_pin_matches_overlay(
            agent_id, release_pins[agent_id], overlay_pins[agent_id]
        )
        _assert_values_image_digest_matches_pin(
            agent_id,
            values,
            release_pins[agent_id],
        )
    return overlay_pins, overlay_imports


def _check_retained_tokens(
    *,
    repo_root: Path,
    values: dict[str, Any],
    runtime_secret_path: Path,
    token_secret_path: Path,
    token_metadata_path: Path,
    workers: dict[str, Any],
    token_keys: dict[str, str],
    retained_hmac_pins: dict[str, dict[str, str]],
    digest_spec_version: str,
) -> None:
    _assert_runtime_secret_excludes_tokens(
        repo_root / runtime_secret_path, token_keys.values()
    )
    secret_path = repo_root / token_secret_path
    secret = _load_secret(
        secret_path,
        cwd=repo_root,
        label="workload identity token secret",
    )
    token_claims_by_agent = _check_token_claims(
        secret, workers, token_keys, retained_hmac_pins
    )

    _assert_token_metadata_matches(
        metadata_path=repo_root / token_metadata_path,
        token_secret_path=secret_path,
        configured_token_secret_path=token_secret_path,
        token_release_pins=retained_hmac_pins,
        token_claims_by_agent=token_claims_by_agent,
        digest_spec_version=digest_spec_version,
        token_keys=token_keys,
    )
    _assert_rollout_checksum_matches(
        values=values,
        ciphertext_sha256="sha256:"
        + hashlib.sha256(secret_path.read_bytes()).hexdigest(),
    )


def _check_token_claims(
    secret: dict[str, Any],
    workers: dict[str, Any],
    token_keys: dict[str, str],
    retained_hmac_pins: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    token_claims_by_agent: dict[str, dict[str, Any]] = {}
    for agent_id, token_key in sorted(token_keys.items()):
        token = _secret_value(secret, token_key)
        claims = _workload_identity_claims(token, token_key)
        if claims["sub"] != agent_id:
            raise DriftGateError(f"{token_key} sub must match worker {agent_id}")
        if claims["aud"] != workers[agent_id]["identity"]["token"]["audience"]:
            raise DriftGateError(f"{token_key} aud must match worker identity audience")
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

    return token_claims_by_agent


def _load_overlay_release_state(
    configmap_path: Path,
    expected_agents: set[str],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    data = _load_overlay_data(configmap_path)
    if not isinstance(data, dict):
        raise DriftGateError("registry overlay ConfigMap must contain data")
    imports = YAML_PARSER.load(data.get("workload_imports.yaml") or "")
    if not isinstance(imports, dict) or not isinstance(imports.get("imports"), list):
        raise DriftGateError(
            "registry overlay workload_imports.yaml must contain imports"
        )

    imports_by_id = {
        entry["id"]: entry
        for entry in imports["imports"]
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    pins: dict[str, dict[str, str]] = {}
    for agent_id in sorted(expected_agents):
        import_entry = imports_by_id.get(agent_id)
        if not isinstance(import_entry, dict):
            raise DriftGateError(f"registry overlay missing import for {agent_id}")
        manifest_key = Path(_required_str(import_entry, "manifest_path", agent_id)).name
        manifest = json.loads(
            _required_str(data, manifest_key, "registry overlay data")
        )
        if not isinstance(manifest, dict) or manifest.get("id") != agent_id:
            raise DriftGateError(
                f"{agent_id} manifest id must match its importing worker"
            )
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
            raise DriftGateError(
                f"{agent_id} import manifest_digest differs from manifest"
            )
        if import_entry.get("image_digest") != image_digest:
            raise DriftGateError(
                f"{agent_id} import image_digest differs from manifest"
            )
        pins[agent_id] = {
            "codeDigest": code_digest,
            "manifestDigest": manifest_digest,
            "imageDigest": image_digest,
        }
    return pins, imports_by_id


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
    try:
        return render_registry_overlay_data(configmap_path.parent)
    except RegistryOverlayRenderError as exc:
        raise DriftGateError(str(exc)) from exc


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

    image_path = ("workers", agent_id, "image")
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


def _assert_runtime_secret_excludes_tokens(
    secret_path: Path, token_keys: Iterable[str]
) -> None:
    raw = secret_path.read_text(encoding="utf-8")
    for token_key in token_keys:
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
                raise DriftGateError(
                    f"{token_key} data value is not valid base64"
                ) from exc

    raise DriftGateError(f"workload identity token secret missing {token_key}")


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


if __name__ == "__main__":
    sys.exit(main())
