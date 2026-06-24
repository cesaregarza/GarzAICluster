from __future__ import annotations

import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.grant_ownership import (
    CONFIGMAP_PATH,
    OWNERSHIP_DOC_PATH,
    OWNERSHIP_MAP_PATH,
    OWNERSHIP_SOURCE_PATH,
)
from scripts.workload_enablement import (
    AGENT_WORKLOADS_RUNTIME_SECRET_PATH,
    AGENT_WORKLOADS_VALUES_PATH,
    WorkloadEnablementError,
    apply_workload_enablement,
    plan_workload_enablement,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")
YAML_WRITER = YAML()


class WorkloadEnablementTests(unittest.TestCase):
    def test_existing_readonly_query_enablement_plans_no_changes(self) -> None:
        root = _fixture_repo()
        document = _write_enablement(
            root,
            {
                "schema_version": "mandate-workload-enablement.v1",
                "kind": "MandateWorkloadEnablement",
                "workload": "data.workspace_probe",
                "capability": "agent_workloads.readonly_query",
                "grant": {"binding": "private-admin-controlled-capabilities"},
                "model_lease": {
                    "allowed_profile": "openai.gpt-5.3-codex-spark",
                },
                "worker": {"claims": True},
            },
        )

        result = plan_workload_enablement(repo_root=root, document_path=document)

        self.assertEqual(result.changed_files, ())
        self.assertFalse(result.gaps)
        self.assertIn("policy_grant_present", _codes(result.actions))
        self.assertIn("model_profile_present", _codes(result.actions))
        self.assertIn("worker_claim_present", _codes(result.actions))

    def test_write_adds_policy_grant_and_worker_claim_only(self) -> None:
        root = _fixture_repo()
        _remove_policy_grant(root, "agent_workloads.readonly_query")
        _set_worker_capabilities(root, "data.workspace_probe", ["agent_workloads.db_probe"])
        document = _write_enablement(
            root,
            {
                "schema_version": "mandate-workload-enablement.v1",
                "kind": "MandateWorkloadEnablement",
                "workload": "data.workspace_probe",
                "capability": "agent_workloads.readonly_query",
                "grant": {"binding": "private-admin-controlled-capabilities"},
                "worker": {"claims": True},
            },
        )

        result = apply_workload_enablement(
            repo_root=root,
            document_path=document,
            write=True,
            pr_body_path=root / "mandate-apply-pr.md",
        )

        self.assertEqual(
            result.changed_files,
            (
                "apps/agent-control-plane-registry-overlay/configmap.yaml",
                "apps/agent-workloads/values.yaml",
            ),
        )
        self.assertIn("policy_grant_added", _codes(result.actions))
        self.assertIn("worker_claim_added", _codes(result.actions))
        self.assertIn(
            "agent_workloads.readonly_query",
            _policy_allow(root, "private-admin-controlled-capabilities"),
        )
        self.assertEqual(
            _worker_capabilities(root, "data.workspace_probe"),
            ["agent_workloads.db_probe", "agent_workloads.readonly_query"],
        )
        secret_text = (root / AGENT_WORKLOADS_RUNTIME_SECRET_PATH).read_text()
        self.assertIn("ENC[AES256_GCM", secret_text)
        body = (root / "mandate-apply-pr.md").read_text()
        self.assertIn("No live ConfigMap, Secret, or Kubernetes object is mutated.", body)
        self.assertIn("Secret values are never read or written", body)

    def test_missing_secret_is_named_as_operator_sops_gap(self) -> None:
        root = _fixture_repo()
        document = _write_enablement(
            root,
            {
                "schema_version": "mandate-workload-enablement.v1",
                "kind": "MandateWorkloadEnablement",
                "workload": "data.workspace_probe",
                "capability": "agent_workloads.readonly_query",
                "secrets": [{"key": "MISSING_READONLY_DATABASE_URL"}],
            },
        )

        result = plan_workload_enablement(repo_root=root, document_path=document)

        self.assertIn("secret_key_ref_missing", _codes(result.gaps))
        self.assertIn("sops_secret_key_missing", _codes(result.gaps))
        self.assertFalse(result.changed_files)

    def test_forbidden_grant_user_edit_fails_closed(self) -> None:
        root = _fixture_repo()
        document = _write_enablement(
            root,
            {
                "schema_version": "mandate-workload-enablement.v1",
                "kind": "MandateWorkloadEnablement",
                "workload": "data.workspace_probe",
                "capability": "agent_workloads.readonly_query",
                "grant": {
                    "binding": "private-admin-controlled-capabilities",
                    "users": ["94265880216612864"],
                },
            },
        )

        with self.assertRaisesRegex(
            WorkloadEnablementError,
            "grant contains unsupported keys: users",
        ):
            plan_workload_enablement(repo_root=root, document_path=document)

    def test_multiple_model_profiles_fail_closed(self) -> None:
        root = _fixture_repo()
        document = _write_enablement(
            root,
            {
                "schema_version": "mandate-workload-enablement.v1",
                "kind": "MandateWorkloadEnablement",
                "workload": "data.workspace_probe",
                "capability": "agent_workloads.readonly_query",
                "model_lease": {
                    "allowed_profiles": [
                        "openai.gpt-5.3-codex-spark",
                        "openai.gpt-5.3-codex-large",
                    ],
                },
            },
        )

        with self.assertRaisesRegex(
            WorkloadEnablementError,
            "model_lease contains unsupported keys: allowed_profiles",
        ):
            plan_workload_enablement(repo_root=root, document_path=document)


def _fixture_repo() -> Path:
    root = Path(tempfile.mkdtemp())
    for path in [
        CONFIGMAP_PATH,
        OWNERSHIP_SOURCE_PATH,
        OWNERSHIP_MAP_PATH,
        OWNERSHIP_DOC_PATH,
        AGENT_WORKLOADS_VALUES_PATH,
        AGENT_WORKLOADS_RUNTIME_SECRET_PATH,
    ]:
        source = REPO_ROOT / path
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


def _write_enablement(root: Path, payload: dict[str, Any]) -> Path:
    path = root / "enablement.yaml"
    with path.open("w", encoding="utf-8") as handle:
        YAML_WRITER.dump(payload, handle)
    return path


def _remove_policy_grant(root: Path, capability_id: str) -> None:
    configmap = _load_configmap(root)
    policy = YAML_PARSER.load(configmap["data"]["policy.prod.yaml"])
    allow = _policy_allow_from_payload(policy, "private-admin-controlled-capabilities")
    allow.remove(capability_id)
    configmap["data"]["policy.prod.yaml"] = _yaml_text(policy)
    _write_yaml(root / CONFIGMAP_PATH, configmap)


def _policy_allow(root: Path, binding_id: str) -> list[str]:
    configmap = _load_configmap(root)
    policy = YAML_PARSER.load(configmap["data"]["policy.prod.yaml"])
    return _policy_allow_from_payload(policy, binding_id)


def _policy_allow_from_payload(policy: dict[str, Any], binding_id: str) -> list[str]:
    for binding in policy["bindings"]:
        if binding["id"] == binding_id:
            return binding["capabilities"]["allow"]
    raise AssertionError(f"binding not found: {binding_id}")


def _set_worker_capabilities(
    root: Path,
    workload_id: str,
    capabilities: list[str],
) -> None:
    values = _load_values(root)
    env = _worker_env(values, workload_id)
    env["AGENT_WORKLOADS_WORKER_CAPABILITIES"] = ",".join(capabilities)
    _write_yaml(root / AGENT_WORKLOADS_VALUES_PATH, values)


def _worker_capabilities(root: Path, workload_id: str) -> list[str]:
    values = _load_values(root)
    raw = _worker_env(values, workload_id)["AGENT_WORKLOADS_WORKER_CAPABILITIES"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _worker_env(values: dict[str, Any], workload_id: str) -> dict[str, Any]:
    if workload_id == "data.workspace_probe":
        return values["env"]
    if workload_id == "opencode.proposer":
        return values["opencodeProposer"]["env"]
    if workload_id == "opencode.apply_executor":
        return values["opencodeApplyExecutor"]["env"]
    raise AssertionError(f"unknown worker fixture: {workload_id}")


def _load_configmap(root: Path) -> dict[str, Any]:
    return YAML_PARSER.load((root / CONFIGMAP_PATH).read_text())


def _load_values(root: Path) -> dict[str, Any]:
    return YAML_PARSER.load((root / AGENT_WORKLOADS_VALUES_PATH).read_text())


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        YAML_WRITER.dump(payload, handle)


def _yaml_text(payload: dict[str, Any]) -> str:
    stream = StringIO()
    YAML_WRITER.dump(payload, stream)
    return stream.getvalue()


def _codes(actions: tuple[Any, ...]) -> set[str]:
    return {action.code for action in actions}
