from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.check_agent_workloads_identity_digests import (
    DriftGateError,
    check_agent_workloads_identity_digests,
)


YAML_PARSER = YAML(typ="safe")

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
    "data.workspace_probe": "DATA_WORKSPACE_PROBE_WORKLOAD_IDENTITY_TOKEN",
    "opencode.proposer": "OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN",
    "opencode.apply_executor": "OPENCODE_APPLY_EXECUTOR_WORKLOAD_IDENTITY_TOKEN",
}


class AgentWorkloadsIdentityDigestGateTests(unittest.TestCase):
    def test_gate_skips_without_release_pins(self) -> None:
        root = _fixture_repo(include_pins=False)

        result = check_agent_workloads_identity_digests(
            repo_root=root,
            values_path=Path("apps/agent-workloads/values.yaml"),
            overlay_configmap_path=Path(
                "apps/agent-control-plane-registry-overlay/configmap.yaml"
            ),
            runtime_secret_path=Path("secrets/agent-workloads/runtime-secret.enc.yaml"),
        )

        self.assertIn("gate inactive", result)

    def test_gate_accepts_matching_release_pins_overlay_and_tokens(self) -> None:
        root = _fixture_repo()

        result = check_agent_workloads_identity_digests(
            repo_root=root,
            values_path=Path("apps/agent-workloads/values.yaml"),
            overlay_configmap_path=Path(
                "apps/agent-control-plane-registry-overlay/configmap.yaml"
            ),
            runtime_secret_path=Path("secrets/agent-workloads/runtime-secret.enc.yaml"),
        )

        self.assertIn("match release pins", result)

    def test_gate_rejects_values_overlay_code_digest_mismatch(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        values["mandateReleasePins"]["opencode.proposer"]["codeDigest"] = (
            "sha256:" + "9" * 64
        )
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(DriftGateError, "mandateReleasePins.codeDigest"):
            check_agent_workloads_identity_digests(
                repo_root=root,
                values_path=Path("apps/agent-workloads/values.yaml"),
                overlay_configmap_path=Path(
                    "apps/agent-control-plane-registry-overlay/configmap.yaml"
                ),
                runtime_secret_path=Path(
                    "secrets/agent-workloads/runtime-secret.enc.yaml"
                ),
            )

    def test_gate_rejects_stale_workload_identity_token_code_digest(self) -> None:
        root = _fixture_repo(
            token_digests={
                **{agent_id: pins["codeDigest"] for agent_id, pins in DIGESTS.items()},
                "opencode.proposer": "sha256:" + "9" * 64,
            }
        )

        with self.assertRaisesRegex(DriftGateError, "code_digest mismatch"):
            check_agent_workloads_identity_digests(
                repo_root=root,
                values_path=Path("apps/agent-workloads/values.yaml"),
                overlay_configmap_path=Path(
                    "apps/agent-control-plane-registry-overlay/configmap.yaml"
                ),
                runtime_secret_path=Path(
                    "secrets/agent-workloads/runtime-secret.enc.yaml"
                ),
            )

    def test_gate_rejects_malformed_identity_token(self) -> None:
        root = _fixture_repo()
        secret_path = root / "secrets" / "agent-workloads" / "runtime-secret.enc.yaml"
        secret = YAML_PARSER.load(secret_path.read_text())
        secret["stringData"]["OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN"] = "not-a-token"
        _write_yaml(secret_path, secret)

        with self.assertRaisesRegex(DriftGateError, "not an mwit_v1 token"):
            check_agent_workloads_identity_digests(
                repo_root=root,
                values_path=Path("apps/agent-workloads/values.yaml"),
                overlay_configmap_path=Path(
                    "apps/agent-control-plane-registry-overlay/configmap.yaml"
                ),
                runtime_secret_path=Path(
                    "secrets/agent-workloads/runtime-secret.enc.yaml"
                ),
            )


def _fixture_repo(
    *,
    include_pins: bool = True,
    token_digests: dict[str, str] | None = None,
) -> Path:
    import tempfile

    root = Path(tempfile.mkdtemp())
    values_path = root / "apps" / "agent-workloads" / "values.yaml"
    values_path.parent.mkdir(parents=True)
    values: dict[str, Any] = {"image": {"tag": "sha-test"}}
    if include_pins:
        values["mandateReleasePins"] = DIGESTS
    _write_yaml(values_path, values)

    configmap_path = root / "apps" / "agent-control-plane-registry-overlay" / (
        "configmap.yaml"
    )
    configmap_path.parent.mkdir(parents=True)
    _write_yaml(configmap_path, _configmap())

    secret_path = root / "secrets" / "agent-workloads" / "runtime-secret.enc.yaml"
    secret_path.parent.mkdir(parents=True)
    token_digests = token_digests or {
        agent_id: pins["codeDigest"] for agent_id, pins in DIGESTS.items()
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "agent-workloads-secrets"},
        "stringData": {
            TOKEN_KEYS[agent_id]: _mwit_token(code_digest)
            for agent_id, code_digest in token_digests.items()
        },
    }
    _write_yaml(secret_path, secret)
    return root


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


def _mwit_token(code_digest: str) -> str:
    payload = {
        "aud": "mandate-api",
        "code_digest": code_digest,
        "exp": 4102444800,
        "iss": "kubernetes",
        "scp": ["worker_service"],
        "sub": "system:serviceaccount:agent-workloads:worker",
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    return f"mwit_v1.{encoded}.signature"


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(_yaml_text(payload), encoding="utf-8")


def _yaml_text(payload: dict[str, Any]) -> str:
    from io import StringIO

    stream = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(payload, stream)
    return stream.getvalue()


if __name__ == "__main__":
    unittest.main()
