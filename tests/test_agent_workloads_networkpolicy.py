from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def _render_agent_workloads_prod() -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    result = subprocess.run(
        [
            "helm",
            "template",
            "agent-workloads",
            "helm/agent-workloads",
            "-f",
            "apps/agent-workloads/values.yaml",
        ],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    docs = [
        doc
        for doc in YAML_PARSER.load_all(result.stdout)
        if isinstance(doc, dict) and doc
    ]
    if not docs:
        raise AssertionError("helm rendered no Kubernetes documents")
    return docs


def _find_doc(
    docs: list[dict[str, Any]],
    *,
    kind: str,
    name: str,
) -> dict[str, Any]:
    for doc in docs:
        metadata = doc.get("metadata", {})
        if doc.get("kind") == kind and metadata.get("name") == name:
            return doc
    raise AssertionError(f"{kind}/{name} not rendered")


def _env_by_name(container: dict[str, Any]) -> dict[str, Any]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _container_by_name(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container["name"] == name:
            return container
    raise AssertionError(f"container {name} not rendered")


class AgentWorkloadsNetworkPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = _render_agent_workloads_prod()
        cls.values = YAML_PARSER.load(
            (REPO_ROOT / "apps" / "agent-workloads" / "values.yaml").read_text()
        )

    def test_workspace_probe_network_policy_is_database_only(self) -> None:
        policy = _find_doc(
            self.docs,
            kind="NetworkPolicy",
            name="agent-workloads",
        )

        egress = policy["spec"]["egress"]
        ip_blocks = {
            target["ipBlock"]["cidr"]
            for rule in egress
            for target in rule.get("to", [])
            if "ipBlock" in target
        }
        namespace_targets = {
            target["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in egress
            for target in rule.get("to", [])
            if "namespaceSelector" in target
        }
        ports = {
            (port["port"], port.get("protocol", "TCP"))
            for rule in egress
            for port in rule.get("ports", [])
        }

        self.assertEqual(ip_blocks, {"10.108.0.0/20", "159.203.109.0/32"})
        self.assertNotIn("0.0.0.0/0", ip_blocks)
        self.assertIn("agent-control-plane", namespace_targets)
        self.assertIn("kube-system", namespace_targets)
        self.assertIn((80, "TCP"), ports)
        self.assertIn((8000, "TCP"), ports)
        self.assertIn((25060, "TCP"), ports)
        self.assertIn((53, "UDP"), ports)
        self.assertIn((53, "TCP"), ports)

    def test_workspace_probe_workload_identity_uses_dedicated_token_secret(self) -> None:
        deployment = _find_doc(
            self.docs,
            kind="Deployment",
            name="agent-workloads",
        )
        volumes = {
            volume["name"]: volume
            for volume in deployment["spec"]["template"]["spec"]["volumes"]
        }

        token_volume = volumes["workload-identity-token"]["secret"]
        self.assertEqual(
            token_volume,
            {
                "secretName": "agent-workloads-workload-identity-tokens",
                "items": [
                    {
                        "key": "MANDATE_WORKLOAD_IDENTITY_TOKEN",
                        "path": "token",
                    }
                ],
            },
        )

    def test_workspace_probe_runs_multiple_replicas_for_burst_claims(self) -> None:
        deployment = _find_doc(
            self.docs,
            kind="Deployment",
            name="agent-workloads",
        )
        opencode_deployment = _find_doc(
            self.docs,
            kind="Deployment",
            name="agent-workloads-opencode-proposer",
        )

        self.assertEqual(self.values["replicaCount"], 2)
        self.assertEqual(deployment["spec"]["replicas"], 2)
        self.assertEqual(self.values["opencodeProposer"]["replicaCount"], 1)
        self.assertEqual(opencode_deployment["spec"]["replicas"], 1)

    def test_opencode_apply_executor_runs_without_model_or_provider_credentials(
        self,
    ) -> None:
        deployment = _find_doc(
            self.docs,
            kind="Deployment",
            name="agent-workloads-opencode-proposer",
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        containers = {container["name"] for container in pod_spec["containers"]}
        self.assertEqual(
            containers,
            {"opencode-proposer", "opencode-apply-executor"},
        )

        proposer = _container_by_name(deployment, "opencode-proposer")
        apply_executor = _container_by_name(deployment, "opencode-apply-executor")
        proposer_env = _env_by_name(proposer)
        apply_env = _env_by_name(apply_executor)

        self.assertIn("MANDATE_MODEL_GATEWAY_BASE_URL", proposer_env)
        self.assertNotIn("MANDATE_MODEL_GATEWAY_BASE_URL", apply_env)
        for forbidden in (
            "OPENAI_API_KEY",
            "OPENAI_CODEX_AUTH_JSON",
            "DATABASE_URL",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GIT_SSH_COMMAND",
        ):
            self.assertNotIn(forbidden, apply_env)

        self.assertEqual(
            apply_env["MANDATE_WORKLOAD_IDENTITY_TOKEN"]["valueFrom"]["secretKeyRef"],
            {
                "name": "agent-workloads-workload-identity-tokens",
                "key": "OPENCODE_APPLY_EXECUTOR_WORKLOAD_IDENTITY_TOKEN",
            },
        )
        self.assertEqual(
            proposer_env["MANDATE_WORKLOAD_IDENTITY_TOKEN"]["valueFrom"][
                "secretKeyRef"
            ],
            {
                "name": "agent-workloads-workload-identity-tokens",
                "key": "OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN",
            },
        )
        self.assertEqual(
            apply_env["AGENT_WORKLOADS_WORKER_ID"]["value"],
            "opencode.apply_executor",
        )
        self.assertEqual(
            apply_env["AGENT_WORKLOADS_WORKER_CAPABILITIES"]["value"],
            "agent_workloads.opencode_apply",
        )
        apply_image = self.values["opencodeApplyExecutor"]["image"]
        self.assertEqual(
            apply_executor["image"],
            f"{apply_image['repository']}@{apply_image['digest']}",
        )

        apply_mounts = {
            mount["name"]: mount["mountPath"]
            for mount in apply_executor["volumeMounts"]
        }
        proposer_mounts = {
            mount["name"]: mount["mountPath"]
            for mount in proposer["volumeMounts"]
        }
        self.assertEqual(apply_mounts["apply-tmp"], "/tmp")
        self.assertEqual(proposer_mounts["proposer-tmp"], "/tmp")
        self.assertNotIn("proposer-tmp", apply_mounts)
        self.assertNotIn("apply-tmp", proposer_mounts)
        self.assertEqual(apply_mounts["apply-workspace"], "/workspace/job")
        self.assertEqual(proposer_mounts["job-workspace"], "/workspace/job")
        self.assertEqual(apply_mounts["proposals"], "/workspace/proposals")
        self.assertEqual(proposer_mounts["proposals"], "/workspace/proposals")

    def test_opencode_pod_network_policy_is_broker_jailed(self) -> None:
        policy = _find_doc(
            self.docs,
            kind="NetworkPolicy",
            name="agent-workloads-opencode-proposer",
        )

        egress = policy["spec"]["egress"]
        ip_blocks = {
            target["ipBlock"]["cidr"]
            for rule in egress
            for target in rule.get("to", [])
            if "ipBlock" in target
        }
        namespace_targets = {
            target["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in egress
            for target in rule.get("to", [])
            if "namespaceSelector" in target
        }
        ports = {
            (port["port"], port.get("protocol", "TCP"))
            for rule in egress
            for port in rule.get("ports", [])
        }

        self.assertEqual(ip_blocks, set())
        self.assertIn("agent-control-plane", namespace_targets)
        self.assertIn("kube-system", namespace_targets)
        self.assertIn((80, "TCP"), ports)
        self.assertIn((8000, "TCP"), ports)
        self.assertIn((53, "UDP"), ports)
        self.assertIn((53, "TCP"), ports)


if __name__ == "__main__":
    unittest.main()
