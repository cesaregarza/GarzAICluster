from __future__ import annotations

import base64
import hashlib
import json
import shutil
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.check_agent_workloads_identity_digests import (
    check_agent_workloads_identity_digests,
)

YAML_PARSER = YAML(typ="safe")
REPO_ROOT = Path(__file__).resolve().parents[1]
DRIFT_GATE_RECIPIENT = "age1qny3qstwqglwdyau5x7sp3vy0qmd3petzp4f3slf7u3qrudhdq0qf4cjau"

DIGESTS = {
    "data.workspace_probe": {
        "codeDigest": "sha256:" + "a" * 64,
        "manifestDigest": "sha256:" + "b" * 64,
        "imageDigest": "sha256:" + "c" * 64,
    },
    "opencode.proposer": {
        "codeDigest": "sha256:" + "d" * 64,
        "manifestDigest": "sha256:" + "e" * 64,
        "imageDigest": "sha256:" + "f" * 64,
    },
    "opencode.apply_executor": {
        "codeDigest": "sha256:" + "1" * 64,
        "manifestDigest": "sha256:" + "2" * 64,
        "imageDigest": "sha256:" + "3" * 64,
    },
}

TOKEN_KEYS = {
    "data.workspace_probe": "MANDATE_WORKLOAD_IDENTITY_TOKEN",
    "opencode.proposer": "OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN",
    "opencode.apply_executor": "OPENCODE_APPLY_EXECUTOR_WORKLOAD_IDENTITY_TOKEN",
}
RUNTIME_SECRET_PATH = Path("secrets/agent-workloads/runtime-secret.enc.yaml")
TOKEN_SECRET_PATH = Path("secrets/agent-workloads/workload-identity-tokens.enc.yaml")
TOKEN_METADATA_PATH = Path(
    "secrets/agent-workloads/workload-identity-tokens.metadata.yaml"
)
DIGEST_SPEC_VERSION = "agent-workloads-code-digest-v2"
WORKLOAD_IDENTITY_BUNDLE_DIGEST_VERSION = "workload_identity_bundle.v1"
OLD_MUTABLE_BROKER_ACTION = "cesaregarza/.github/actions/fetch-broker-credentials@main"
SHARED_ACTION_REF = "a1d2fb4a6b288066574b1ac53074ac62e920a07f"
SHARED_DRIFT_GATE_ACTION = (
    "cesaregarza/.github/actions/agent-workloads-identity-digest-drift-gate"
    f"@{SHARED_ACTION_REF}"
)
REPO_SOPS_SECRET_CONTEXT = "secrets.SOPS_AGE_KEY"


def _fixture_repo(
    *,
    include_pins: bool = True,
    token_claim_overrides: dict[str, dict[str, Any]] | None = None,
) -> Path:
    import tempfile

    root = Path(tempfile.mkdtemp())
    core_path = root / "apps/agent-control-plane/values.yaml"
    core_path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        core_path, {"env": {"AGENT_PLATFORM_WORKLOAD_IDENTITY_AUDIENCE": "mandate-api"}}
    )
    values_path = root / "apps" / "agent-workloads" / "values.yaml"
    values_path.parent.mkdir(parents=True)
    values: dict[str, Any] = {
        "workers": {
            worker_id: {
                "image": {"tag": "sha-test", "digest": pins["imageDigest"]},
                "identity": {
                    "workerId": worker_id,
                    "mode": "projected",
                    "serviceAccountNamePrefix": "agent-workloads",
                    "token": {"audience": "mandate-api"},
                    "hmacRollbackTokenKey": TOKEN_KEYS[worker_id],
                    "hmacRollbackRelease": dict(pins),
                },
            }
            for worker_id, pins in DIGESTS.items()
        }
    }
    if include_pins:
        values["mandateReleasePins"] = DIGESTS
        receipt_path = root / "contracts/mandate-worker/receipt.json"
        receipt_path.parent.mkdir(parents=True)
        shutil.copyfile(
            REPO_ROOT / "contracts/mandate-worker/receipt.json", receipt_path
        )

    configmap_path = (
        root / "apps" / "agent-control-plane-registry-overlay" / ("configmap.yaml")
    )
    configmap_path.parent.mkdir(parents=True)
    configmap = _configmap()
    imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
    for entry in imports["imports"]:
        worker_id = entry["id"]
        entry.setdefault("agent", {}).update(
            {
                "identity_audience": "mandate-api",
                "service_account_subject": _release_subject(
                    worker_id, DIGESTS[worker_id]
                ),
            }
        )
    configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
    _write_yaml(configmap_path, configmap)

    runtime_secret_path = root / RUNTIME_SECRET_PATH
    runtime_secret_path.parent.mkdir(parents=True)
    _write_yaml(
        runtime_secret_path,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "agent-workloads-secrets"},
            "stringData": {
                "MANDATE_WORKER_TOKEN": "worker-token",
                "AGENT_WORKLOADS_DATABASE_URL": "postgresql://example.invalid/db",
            },
        },
    )

    token_secret_path = root / TOKEN_SECRET_PATH
    token_claim_overrides = token_claim_overrides or {}
    token_secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "agent-workloads-workload-identity-tokens"},
        "stringData": {
            TOKEN_KEYS[agent_id]: _mwit_token(
                agent_id,
                claim_overrides=token_claim_overrides.get(agent_id),
            )
            for agent_id in DIGESTS
        },
    }
    _write_yaml(token_secret_path, token_secret)
    ciphertext_hash = _write_metadata(root)
    if include_pins:
        values["rolloutChecksums"] = {
            "workloadIdentityTokenSecret": ciphertext_hash,
        }
    _write_yaml(values_path, values)
    return root


def _check(root: Path) -> str:
    return check_agent_workloads_identity_digests(
        repo_root=root,
        values_path=Path("apps/agent-workloads/values.yaml"),
        overlay_configmap_path=Path(
            "apps/agent-control-plane-registry-overlay/configmap.yaml"
        ),
        runtime_secret_path=RUNTIME_SECRET_PATH,
        token_secret_path=TOKEN_SECRET_PATH,
        token_metadata_path=TOKEN_METADATA_PATH,
    )


def _configmap() -> dict[str, Any]:
    imports = []
    data: dict[str, str] = {}
    for agent_id, pins in DIGESTS.items():
        manifest_key = f"agent-{agent_id}.json"
        imports.append(
            {
                "id": agent_id,
                "manifest_path": f"registries/imports/{manifest_key}",
                "manifest_digest": pins["manifestDigest"],
                "image_digest": pins["imageDigest"],
            }
        )
        data[manifest_key] = json.dumps(
            {
                "id": agent_id,
                "digest": pins["manifestDigest"],
                "code_digest": pins["codeDigest"],
                "image": {"digest": pins["imageDigest"]},
            },
            sort_keys=True,
        )
    data["workload_imports.yaml"] = _yaml_text(
        {"schema_version": "workload-imports.v1", "imports": imports}
    )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "agent-control-plane-registry-overlay"},
        "data": data,
    }


def _set_workspace_identity_audience(root: Path, audience: str | None) -> None:
    configmap_path = (
        root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
    )
    configmap = YAML_PARSER.load(configmap_path.read_text())
    imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
    workspace = next(
        entry for entry in imports["imports"] if entry["id"] == "data.workspace_probe"
    )
    agent = workspace["agent"]
    if audience is None:
        agent.pop("identity_audience", None)
    else:
        agent["identity_audience"] = audience
    configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
    _write_yaml(configmap_path, configmap)


def _configure_governed_release_subjects(
    root: Path,
    *,
    previous_by_agent: dict[str, dict[str, str]] | None = None,
) -> None:
    previous_by_agent = previous_by_agent or {}
    values_path = root / "apps" / "agent-workloads" / "values.yaml"
    values = YAML_PARSER.load(values_path.read_text())
    values["opencodeArtifactHandoff"] = {"mode": "governedCore"}
    for agent_id in DIGESTS:
        values["workers"][agent_id]["identity"] = {
            "workerId": agent_id,
            "serviceAccountNamePrefix": "agent-workloads",
            "mode": "projected",
            "token": {"audience": "mandate-api"},
            "previousRelease": previous_by_agent.get(agent_id),
            "hmacRollbackRelease": dict(
                previous_by_agent.get(agent_id) or DIGESTS[agent_id]
            ),
            "hmacRollbackTokenKey": TOKEN_KEYS[agent_id],
        }
    _write_yaml(values_path, values)

    configmap_path = (
        root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
    )
    configmap = YAML_PARSER.load(configmap_path.read_text())
    imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
    for entry in imports["imports"]:
        agent_id = entry["id"]
        agent = entry.setdefault("agent", {})
        agent["identity_audience"] = "mandate-api"
        agent["service_account_subject"] = _release_subject(
            agent_id,
            DIGESTS[agent_id],
        )
        previous = previous_by_agent.get(agent_id)
        if previous is not None:
            agent["previous_release"] = {
                "service_account_subject": _release_subject(agent_id, previous),
                "code_digest": previous["codeDigest"],
                "manifest_digest": previous["manifestDigest"],
                "image_digest": previous["imageDigest"],
            }
    configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
    _write_yaml(configmap_path, configmap)


def _configure_workspace_projected_identity(
    root: Path,
    *,
    previous_release: dict[str, str] | None = None,
) -> None:
    agent_id = "data.workspace_probe"
    values_path = root / "apps" / "agent-workloads" / "values.yaml"
    values = YAML_PARSER.load(values_path.read_text())
    values["workers"]["data.workspace_probe"]["identity"] = {
        "mode": "projected",
        "hmacRollbackTokenKey": TOKEN_KEYS[agent_id],
        "workerId": agent_id,
        "serviceAccountNamePrefix": "agent-workloads",
        "token": {"audience": "mandate-api"},
        "previousRelease": previous_release,
        "hmacRollbackRelease": dict(previous_release or DIGESTS[agent_id]),
    }
    _write_yaml(values_path, values)

    configmap_path = (
        root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
    )
    configmap = YAML_PARSER.load(configmap_path.read_text())
    imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
    workspace = next(entry for entry in imports["imports"] if entry["id"] == agent_id)
    agent = workspace.setdefault("agent", {})
    agent["identity_audience"] = "mandate-api"
    agent["service_account_subject"] = _release_subject(
        agent_id,
        DIGESTS[agent_id],
    )
    if previous_release is not None:
        agent["previous_release"] = {
            "service_account_subject": _release_subject(
                agent_id,
                previous_release,
            ),
            "code_digest": previous_release["codeDigest"],
            "manifest_digest": previous_release["manifestDigest"],
            "image_digest": previous_release["imageDigest"],
        }
    configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
    _write_yaml(configmap_path, configmap)


def _configure_retained_hmac_token(
    root: Path,
    *,
    agent_id: str,
    release: dict[str, str],
) -> None:
    token_secret_path = root / TOKEN_SECRET_PATH
    token_secret = YAML_PARSER.load(token_secret_path.read_text())
    token_secret["stringData"][TOKEN_KEYS[agent_id]] = _mwit_token(
        agent_id,
        release_pins=release,
    )
    _write_yaml(token_secret_path, token_secret)

    token_pins = {
        configured_agent_id: dict(pins) for configured_agent_id, pins in DIGESTS.items()
    }
    token_pins[agent_id] = dict(release)
    ciphertext_hash = _write_metadata(
        root,
        token_pins_by_agent=token_pins,
    )
    values_path = root / "apps" / "agent-workloads" / "values.yaml"
    values = YAML_PARSER.load(values_path.read_text())
    values["rolloutChecksums"]["workloadIdentityTokenSecret"] = ciphertext_hash
    _write_yaml(values_path, values)


def _configure_governed_hmac_identities(root: Path) -> None:
    values_path = root / "apps" / "agent-workloads" / "values.yaml"
    values = YAML_PARSER.load(values_path.read_text())
    values["opencodeArtifactHandoff"] = {"mode": "governedCore"}
    for agent_id in DIGESTS:
        values["workers"][agent_id]["identity"] = {
            "workerId": agent_id,
            "serviceAccountNamePrefix": "agent-workloads",
            "mode": "hmac",
            "token": {"audience": "mandate-api"},
            "previousRelease": None,
        }
    _write_yaml(values_path, values)

    configmap_path = (
        root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
    )
    configmap = YAML_PARSER.load(configmap_path.read_text())
    imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
    for entry in imports["imports"]:
        if entry["id"] in {"opencode.proposer", "opencode.apply_executor"}:
            entry.setdefault("agent", {})["identity_audience"] = "mandate-api"
    configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
    _write_yaml(configmap_path, configmap)


def _release_subject(agent_id: str, release: dict[str, str]) -> str:
    payload = {
        "schema_version": WORKLOAD_IDENTITY_BUNDLE_DIGEST_VERSION,
        "code_digest": release["codeDigest"],
        "manifest_digest": release["manifestDigest"],
        "image_digest": release["imageDigest"],
    }
    suffix = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    worker_name = agent_id.replace(".", "-").replace("_", "-")
    return (
        f"system:serviceaccount:agent-workloads:agent-workloads-{worker_name}-{suffix}"
    )


def _write_metadata(
    root: Path,
    *,
    token_pins_by_agent: dict[str, dict[str, str]] | None = None,
) -> str:
    token_secret_path = root / TOKEN_SECRET_PATH
    ciphertext_hash = (
        "sha256:" + hashlib.sha256(token_secret_path.read_bytes()).hexdigest()
    )
    token_pins_by_agent = token_pins_by_agent or DIGESTS
    metadata = {
        "schema_version": "agent-workloads-workload-identity-tokens.metadata.v1",
        "token_secret_path": TOKEN_SECRET_PATH.as_posix(),
        "tokens": {
            agent_id: {
                "agent_id": agent_id,
                "token_key": TOKEN_KEYS[agent_id],
                "code_digest": pins["codeDigest"],
                "manifest_digest": pins["manifestDigest"],
                "image_digest": pins["imageDigest"],
                "bundle_digest": _workload_identity_bundle_digest(pins),
                "iat": 1700000000,
                "exp": 4102444800,
                "iss": "kubernetes",
                "sub": agent_id,
                "aud": "mandate-api",
                "scp": ["worker_service"],
                "digest_spec_version": DIGEST_SPEC_VERSION,
                "source_commit": "fixture",
                "ciphertext_sha256": ciphertext_hash,
            }
            for agent_id, pins in token_pins_by_agent.items()
        },
    }
    _write_yaml(root / TOKEN_METADATA_PATH, metadata)
    return ciphertext_hash


def _mwit_token(
    agent_id: str,
    *,
    claim_overrides: dict[str, Any] | None = None,
    release_pins: dict[str, str] | None = None,
) -> str:
    pins = release_pins or DIGESTS[agent_id]
    payload = {
        "aud": "mandate-api",
        "bundle_digest": _workload_identity_bundle_digest(pins),
        "code_digest": pins["codeDigest"],
        "exp": 4102444800,
        "iat": 1700000000,
        "image_digest": pins["imageDigest"],
        "iss": "kubernetes",
        "manifest_digest": pins["manifestDigest"],
        "scp": ["worker_service"],
        "sub": agent_id,
    }
    for claim, value in (claim_overrides or {}).items():
        if value is None:
            payload.pop(claim, None)
        else:
            payload[claim] = value
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"mwit_v1.{encoded}.signature"


def _workload_identity_bundle_digest(pins: dict[str, str]) -> str:
    payload = {
        "schema_version": WORKLOAD_IDENTITY_BUNDLE_DIGEST_VERSION,
        "code_digest": pins["codeDigest"],
        "manifest_digest": pins["manifestDigest"],
        "image_digest": pins["imageDigest"],
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(_yaml_text(payload), encoding="utf-8")


def _yaml_text(payload: dict[str, Any]) -> str:
    from io import StringIO

    stream = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(payload, stream)
    return stream.getvalue()


def _sops_age_recipients(path: Path) -> set[str]:
    loaded = YAML_PARSER.load(path.read_text())
    return {
        entry["recipient"]
        for entry in loaded["sops"]["age"]
        if isinstance(entry, dict) and isinstance(entry.get("recipient"), str)
    }


if __name__ == "__main__":
    unittest.main()


def replace_token_identity(root: Path, worker_id: str, claims: dict[str, str]) -> None:
    secret_path = root / TOKEN_SECRET_PATH
    secret = YAML_PARSER.load(secret_path.read_text())
    secret["stringData"][TOKEN_KEYS[worker_id]] = _mwit_token(
        worker_id, claim_overrides=claims
    )
    _write_yaml(secret_path, secret)
    checksum = _write_metadata(root)
    metadata_path = root / TOKEN_METADATA_PATH
    metadata = YAML_PARSER.load(metadata_path.read_text())
    metadata["tokens"][worker_id].update(claims)
    _write_yaml(metadata_path, metadata)
    values_path = root / "apps/agent-workloads/values.yaml"
    values = YAML_PARSER.load(values_path.read_text())
    values["rolloutChecksums"]["workloadIdentityTokenSecret"] = checksum
    _write_yaml(values_path, values)
