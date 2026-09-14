"""Projected release binding and retained rollback configuration checks."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

try:
    from scripts.identity_digest_helpers import (
        WORKLOAD_IDENTITY_BUNDLE_VERSION,
        DriftGateError,
        _required_mapping,
        _required_str,
        _validate_digest,
    )
except ModuleNotFoundError:
    from identity_digest_helpers import (
        WORKLOAD_IDENTITY_BUNDLE_VERSION,
        DriftGateError,
        _required_mapping,
        _required_str,
        _validate_digest,
    )


def configured_workers(values: dict[str, Any]) -> dict[str, Any]:
    workers = _required_mapping(values, "workers", "worker values")
    if not workers:
        raise DriftGateError("workers must be a non-empty mapping")
    for worker_id, worker in workers.items():
        if (
            not isinstance(worker_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", worker_id) is None
        ):
            raise DriftGateError("worker id must be a canonical identifier")
        if not isinstance(worker, dict):
            raise DriftGateError(f"workers.{worker_id} must be a mapping")
        identity = _required_mapping(worker, "identity", f"workers.{worker_id}")
        if identity.get("workerId") != worker_id:
            raise DriftGateError(
                f"workers.{worker_id}.identity.workerId must equal {worker_id}"
            )
        if identity.get("mode") != "projected":
            raise DriftGateError(f"workers.{worker_id}.identity.mode must be projected")
    return workers


def retained_hmac_configuration(
    workers: dict[str, Any],
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    keys, pins = {}, {}
    for worker_id, worker in workers.items():
        identity = worker["identity"]
        label = f"workers.{worker_id}.identity"
        has_key = "hmacRollbackTokenKey" in identity
        has_release = "hmacRollbackRelease" in identity
        if has_key != has_release:
            raise DriftGateError(
                f"{label} must declare hmacRollbackTokenKey and hmacRollbackRelease together"
            )
        if not has_key:
            continue
        key = _required_str(identity, "hmacRollbackTokenKey", label)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in keys.values():
            raise DriftGateError(
                f"{label} rollback token key must be canonical and unique"
            )
        keys[worker_id] = key
        pins[worker_id] = _explicit_hmac_rollback_release(identity, label)
    return keys, pins


def assert_release_subject_bindings(
    *,
    workers: dict[str, Any],
    overlay_pins: dict[str, dict[str, str]],
    overlay_imports: dict[str, dict[str, Any]],
    workload_namespace: str,
    default_identity_audience: str,
) -> None:
    if not isinstance(workload_namespace, str) or not workload_namespace.strip():
        raise DriftGateError("workload namespace must be non-empty")
    seen = {}
    for worker_id, worker in workers.items():
        identity = worker["identity"]
        label = f"workers.{worker_id}.identity"
        prefix = _required_str(identity, "serviceAccountNamePrefix", label)
        subject = _release_service_account_subject(
            namespace=workload_namespace,
            prefix=prefix,
            worker_id=worker_id,
            release=overlay_pins[worker_id],
        )
        _claim_unique_subject(seen, subject=subject, owner=f"{worker_id}:current")
        agent = _required_mapping(
            overlay_imports[worker_id], "agent", f"{worker_id} import"
        )
        if agent.get("service_account_subject") != subject:
            raise DriftGateError(
                f"{worker_id} service_account_subject differs from projected render"
            )
        token = _required_mapping(identity, "token", label)
        audience = _required_str(token, "audience", f"{label}.token")
        if agent.get("identity_audience", default_identity_audience) != audience:
            raise DriftGateError(
                f"{worker_id} identity_audience differs from projected render"
            )
        _assert_previous_release(
            worker_id, identity, agent, workload_namespace, prefix, seen
        )


def _assert_previous_release(
    worker_id: str,
    identity: dict[str, Any],
    agent: dict[str, Any],
    namespace: str,
    prefix: str,
    seen: dict[str, str],
) -> None:
    previous = identity.get("previousRelease")
    imported = agent.get("previous_release")
    if previous is None:
        if imported is not None:
            raise DriftGateError(
                f"{worker_id} registry previous_release is not rendered by Helm values"
            )
        return
    if not isinstance(previous, dict):
        raise DriftGateError(
            f"workers.{worker_id}.identity.previousRelease must be a mapping"
        )
    if not isinstance(imported, dict):
        raise DriftGateError(
            f"{worker_id} registry previous_release is required for rollout overlap"
        )
    subject = _release_service_account_subject(
        namespace=namespace, prefix=prefix, worker_id=worker_id, release=previous
    )
    _claim_unique_subject(seen, subject=subject, owner=f"{worker_id}:previous")
    expected = {
        "service_account_subject": subject,
        "code_digest": previous["codeDigest"],
        "manifest_digest": previous["manifestDigest"],
        "image_digest": previous["imageDigest"],
    }
    if imported != expected:
        raise DriftGateError(
            f"{worker_id} registry previous_release differs from projected render"
        )


def _explicit_hmac_rollback_release(
    identity: dict[str, Any],
    label: str,
) -> dict[str, str]:
    rollback = _required_mapping(identity, "hmacRollbackRelease", label)
    label = f"{label}.hmacRollbackRelease"
    keys = ("codeDigest", "manifestDigest", "imageDigest")
    if set(rollback) != set(keys):
        raise DriftGateError(f"{label} must contain exactly the three release digests")
    result = {}
    for key in keys:
        digest = _required_str(rollback, key, label)
        _validate_digest(digest, f"{label}.{key}")
        result[key] = digest
    return result


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
    if (
        len(service_account_name) > 63
        or re.fullmatch(
            r"[a-z0-9]([-a-z0-9]*[a-z0-9])?",
            service_account_name,
        )
        is None
    ):
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
            f"projected worker subject maps to multiple releases: "
            f"{subject} ({existing}, {owner})"
        )
    seen[subject] = owner
