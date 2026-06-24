from __future__ import annotations

import base64
import hashlib
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
REPO_ROOT = Path(__file__).resolve().parents[1]
DRIFT_GATE_RECIPIENT = (
    "age1qny3qstwqglwdyau5x7sp3vy0qmd3petzp4f3slf7u3qrudhdq0qf4cjau"
)

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
DIGEST_SPEC_VERSION = "agent-workloads-code-digest-v1"
WORKLOAD_IDENTITY_BUNDLE_DIGEST_VERSION = "workload_identity_bundle.v1"
OLD_MUTABLE_BROKER_ACTION = (
    "cesaregarza/"
    ".github/actions/"
    "fetch-broker-credentials"
    "@main"
)
SHARED_ACTION_REF = "a1d2fb4a6b288066574b1ac53074ac62e920a07f"
SHARED_DRIFT_GATE_ACTION = (
    "cesaregarza/.github/actions/agent-workloads-identity-digest-drift-gate"
    f"@{SHARED_ACTION_REF}"
)
REPO_SOPS_SECRET_CONTEXT = "secrets." "SOPS_AGE_KEY"


class AgentWorkloadsIdentityDigestGateTests(unittest.TestCase):
    def test_ci_drift_gate_uses_pinned_shared_brokered_sops_key(self) -> None:
        workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
        workflow = YAML_PARSER.load(workflow_path.read_text())
        workflow_text = workflow_path.read_text()
        job = workflow["jobs"]["agent-workloads-identity-digest-drift"]

        self.assertNotIn(REPO_SOPS_SECRET_CONTEXT, workflow_text)
        self.assertNotIn(OLD_MUTABLE_BROKER_ACTION, workflow_text)
        self.assertEqual(job["permissions"]["contents"], "read")
        self.assertEqual(job["permissions"]["id-token"], "write")

        check_step = next(
            step
            for step in job["steps"]
            if step.get("uses", "").startswith(
                "cesaregarza/.github/actions/agent-workloads-identity-digest-drift-gate"
            )
        )
        self.assertEqual(check_step["uses"], SHARED_DRIFT_GATE_ACTION)
        self.assertNotIn(
            "@main",
            check_step["uses"],
        )

    def test_local_drift_gate_copy_is_deleted(self) -> None:
        self.assertFalse(
            (
                REPO_ROOT
                / ".github"
                / "actions"
                / "agent-workloads-identity-digest-drift-gate"
                / "action.yml"
            ).exists()
        )

    def test_plaintext_secret_guard_allows_non_secret_metadata_ledgers(self) -> None:
        workflow_text = (
            REPO_ROOT / ".github" / "workflows" / "deny-plaintext-secrets.yaml"
        ).read_text()

        self.assertIn('[[ "$base" == *.metadata.yaml ]]', workflow_text)

    def test_scoped_sops_recipient_only_decrypts_workload_identity_token_secret(
        self,
    ) -> None:
        sops_config = YAML_PARSER.load((REPO_ROOT / ".sops.yaml").read_text())
        rules = sops_config["creation_rules"]
        token_rule_index = next(
            index
            for index, rule in enumerate(rules)
            if rule["path_regex"]
            == r"^secrets/agent-workloads/workload-identity-tokens\.enc\.yaml$"
        )
        broad_agent_workloads_rule_index = next(
            index
            for index, rule in enumerate(rules)
            if rule["path_regex"] == r"^secrets/agent-workloads/.*\.enc\.yaml$"
        )

        self.assertLess(token_rule_index, broad_agent_workloads_rule_index)
        self.assertIn(DRIFT_GATE_RECIPIENT, rules[token_rule_index]["age"])
        self.assertNotIn(
            DRIFT_GATE_RECIPIENT,
            rules[broad_agent_workloads_rule_index]["age"],
        )

        runtime_recipients = _sops_age_recipients(
            REPO_ROOT / "secrets" / "agent-workloads" / "runtime-secret.enc.yaml"
        )
        token_recipients = _sops_age_recipients(
            REPO_ROOT
            / "secrets"
            / "agent-workloads"
            / "workload-identity-tokens.enc.yaml"
        )
        agent_workloads_regcred_recipients = _sops_age_recipients(
            REPO_ROOT / "secrets" / "agent-workloads" / "regcred.enc.yaml"
        )
        control_plane_recipients = _sops_age_recipients(
            REPO_ROOT / "secrets" / "agent-control-plane" / "runtime-secret.enc.yaml"
        )

        self.assertIn(DRIFT_GATE_RECIPIENT, token_recipients)
        self.assertNotIn(DRIFT_GATE_RECIPIENT, runtime_recipients)
        self.assertNotIn(DRIFT_GATE_RECIPIENT, agent_workloads_regcred_recipients)
        self.assertNotIn(DRIFT_GATE_RECIPIENT, control_plane_recipients)
        runtime_text = (
            REPO_ROOT / "secrets" / "agent-workloads" / "runtime-secret.enc.yaml"
        ).read_text()
        for token_key in TOKEN_KEYS.values():
            self.assertNotIn(token_key, runtime_text)

    def test_gate_skips_without_release_pins(self) -> None:
        root = _fixture_repo(include_pins=False)

        result = _check(root)

        self.assertIn("gate inactive", result)

    def test_gate_accepts_matching_release_pins_overlay_and_tokens(self) -> None:
        root = _fixture_repo()

        result = _check(root)

        self.assertIn("match release pins", result)

    def test_gate_rejects_missing_workload_identity_rollout_checksum(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        del values["rolloutChecksums"]
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(DriftGateError, "rolloutChecksums"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_rollout_checksum(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        values["rolloutChecksums"]["workloadIdentityTokenSecret"] = (
            "sha256:" + "9" * 64
        )
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(
            DriftGateError,
            "rolloutChecksums.workloadIdentityTokenSecret",
        ):
            _check(root)

    def test_gate_rejects_values_overlay_code_digest_mismatch(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        values["mandateReleasePins"]["opencode.proposer"]["codeDigest"] = (
            "sha256:" + "9" * 64
        )
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(DriftGateError, "mandateReleasePins.codeDigest"):
            _check(root)

    def test_gate_rejects_values_image_digest_release_pin_mismatch(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        values["opencodeProposer"]["image"]["digest"] = "sha256:" + "8" * 64
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(DriftGateError, "values image.digest"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_code_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"code_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "code_digest mismatch"):
            _check(root)

    def test_gate_rejects_missing_workload_identity_token_bundle_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"bundle_digest": None},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "missing bundle_digest"):
            _check(root)

    def test_gate_rejects_code_only_workload_identity_token(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {
                    "manifest_digest": None,
                    "image_digest": None,
                    "bundle_digest": None,
                },
            }
        )

        with self.assertRaisesRegex(DriftGateError, "missing manifest_digest"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_manifest_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"manifest_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "manifest_digest mismatch"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_image_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"image_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "image_digest mismatch"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_bundle_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"bundle_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "bundle_digest mismatch"):
            _check(root)

    def test_gate_rejects_malformed_identity_token(self) -> None:
        root = _fixture_repo()
        secret_path = root / TOKEN_SECRET_PATH
        secret = YAML_PARSER.load(secret_path.read_text())
        secret["stringData"]["OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN"] = "not-a-token"
        _write_yaml(secret_path, secret)
        _write_metadata(root)

        with self.assertRaisesRegex(DriftGateError, "not an mwit_v1 token"):
            _check(root)

    def test_gate_rejects_token_keys_left_in_runtime_secret(self) -> None:
        root = _fixture_repo()
        runtime_secret_path = root / RUNTIME_SECRET_PATH
        runtime_secret = YAML_PARSER.load(runtime_secret_path.read_text())
        runtime_secret["stringData"]["OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN"] = (
            _mwit_token("opencode.proposer")
        )
        _write_yaml(runtime_secret_path, runtime_secret)

        with self.assertRaisesRegex(DriftGateError, "runtime secret must not contain"):
            _check(root)

    def test_gate_rejects_stale_token_metadata_ciphertext_hash(self) -> None:
        root = _fixture_repo()
        metadata_path = root / TOKEN_METADATA_PATH
        metadata = YAML_PARSER.load(metadata_path.read_text())
        metadata["tokens"]["opencode.proposer"]["ciphertext_sha256"] = (
            "sha256:" + "9" * 64
        )
        _write_yaml(metadata_path, metadata)

        with self.assertRaisesRegex(DriftGateError, "ciphertext_sha256 mismatch"):
            _check(root)


def _fixture_repo(
    *,
    include_pins: bool = True,
    token_claim_overrides: dict[str, dict[str, Any]] | None = None,
) -> Path:
    import tempfile

    root = Path(tempfile.mkdtemp())
    values_path = root / "apps" / "agent-workloads" / "values.yaml"
    values_path.parent.mkdir(parents=True)
    values: dict[str, Any] = {
        "image": {
            "tag": "sha-test",
            "digest": DIGESTS["data.workspace_probe"]["imageDigest"],
        },
        "opencodeProposer": {
            "image": {
                "tag": "sha-test",
                "digest": DIGESTS["opencode.proposer"]["imageDigest"],
            }
        },
        "opencodeApplyExecutor": {
            "image": {
                "tag": "sha-test",
                "digest": DIGESTS["opencode.apply_executor"]["imageDigest"],
            }
        },
    }
    if include_pins:
        values["mandateReleasePins"] = DIGESTS

    configmap_path = root / "apps" / "agent-control-plane-registry-overlay" / (
        "configmap.yaml"
    )
    configmap_path.parent.mkdir(parents=True)
    _write_yaml(configmap_path, _configmap())

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


def _write_metadata(root: Path) -> str:
    token_secret_path = root / TOKEN_SECRET_PATH
    ciphertext_hash = "sha256:" + hashlib.sha256(token_secret_path.read_bytes()).hexdigest()
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
            for agent_id, pins in DIGESTS.items()
        },
    }
    _write_yaml(root / TOKEN_METADATA_PATH, metadata)
    return ciphertext_hash


def _mwit_token(
    agent_id: str,
    *,
    claim_overrides: dict[str, Any] | None = None,
) -> str:
    pins = DIGESTS[agent_id]
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
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
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
