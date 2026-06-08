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


class AgentWorkloadsNetworkPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = _render_agent_workloads_prod()

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


if __name__ == "__main__":
    unittest.main()
