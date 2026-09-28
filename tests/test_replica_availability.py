from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
YAML_SAFE = YAML(typ="safe")


def render(chart: str, release: str, values: str | None = None,
           settings: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    command = ["helm", "template", release, f"helm/{chart}"]
    if values:
        command += ["-f", values]
    for setting in settings:
        command += ["--set", setting]
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return [doc for doc in YAML_SAFE.load_all(result.stdout) if isinstance(doc, dict)]


def find(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(doc for doc in docs
                if doc["kind"] == kind and doc["metadata"]["name"] == name)


@unittest.skipUnless(shutil.which("helm"), "Helm is required for chart render tests")
class ReplicaAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api_docs = render("splattop", "splattop-prod", "helm/splattop/values-prod.yaml")
        cls.worker_docs = render("agent-workloads", "agent-workloads", "apps/agent-workloads/values.yaml")

    def assert_revision_spread_and_budget(self, docs: list[dict[str, Any]], name: str) -> None:
        deployment = find(docs, "Deployment", name)
        spec = deployment["spec"]
        pod = spec["template"]["spec"]
        labels = spec["selector"]["matchLabels"]
        self.assertEqual(spec["replicas"], 2)
        self.assertTrue(labels.items() <= spec["template"]["metadata"]["labels"].items())
        constraints = pod["topologySpreadConstraints"]
        self.assertEqual(len(constraints), 1)
        spread = constraints[0]
        self.assertEqual(spread["labelSelector"], {"matchLabels": labels})
        self.assertEqual(spread["topologyKey"], "kubernetes.io/hostname")
        self.assertEqual(spread["maxSkew"], 1)
        self.assertEqual(spread["whenUnsatisfiable"], "DoNotSchedule")
        # Old ReplicaSet pods must not make a new revision appear balanced.
        self.assertEqual(spread["matchLabelKeys"], ["pod-template-hash"])
        # A failed/tainted node must not keep the only healthy node ineligible.
        self.assertEqual(spread["nodeTaintsPolicy"], "Honor")
        self.assertNotIn("minDomains", spread)
        budget = find(docs, "PodDisruptionBudget", name)["spec"]
        self.assertEqual(budget["selector"], {"matchLabels": labels})
        self.assertEqual(budget["maxUnavailable"], 1)
        self.assertEqual(budget["unhealthyPodEvictionPolicy"], "AlwaysAllow")

    def test_api_production_spreads_each_revision_and_protects_drains(self) -> None:
        self.assert_revision_spread_and_budget(self.api_docs, "splattop-prod-fastapi")

    def test_api_rollout_waits_for_http_readiness_without_removing_ready_replicas(self) -> None:
        spec = find(self.api_docs, "Deployment", "splattop-prod-fastapi")["spec"]
        self.assertEqual(spec["strategy"], {
            "type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
        })
        self.assertEqual(spec["minReadySeconds"], 10)
        container = spec["template"]["spec"]["containers"][0]
        probe = container["readinessProbe"]
        self.assertEqual(probe["httpGet"], {"path": "/api", "port": "http"})
        self.assertEqual(next(p["containerPort"] for p in container["ports"] if p["name"] == "http"), 8000)
        self.assertEqual(container["resources"]["requests"], {"cpu": "150m", "memory": "256Mi"})

    def test_data_worker_spreads_without_changing_other_worker_placement(self) -> None:
        self.assert_revision_spread_and_budget(self.worker_docs, "agent-workloads")
        for doc in self.worker_docs:
            if doc["kind"] == "Deployment" and doc["metadata"]["name"] != "agent-workloads":
                self.assertNotIn("topologySpreadConstraints", doc["spec"]["template"]["spec"])
        budgets = [d["metadata"]["name"] for d in self.worker_docs if d["kind"] == "PodDisruptionBudget"]
        self.assertEqual(budgets, ["agent-workloads"])

    def test_single_replica_api_has_no_disruption_budget(self) -> None:
        docs = render("splattop", "one", settings=("fastapi.spreadAcrossNodes=true",))
        self.assertFalse(any(d["kind"] == "PodDisruptionBudget" for d in docs))

    def test_default_api_keeps_existing_single_node_behavior(self) -> None:
        docs = render("splattop", "default")
        spec = find(docs, "Deployment", "default-splattop-fastapi")["spec"]
        self.assertNotIn("topologySpreadConstraints", spec["template"]["spec"])
        self.assertNotIn("strategy", spec)
        self.assertFalse(any(d["kind"] == "PodDisruptionBudget" for d in docs))

    def test_production_api_can_explicitly_disable_spreading_and_budget(self) -> None:
        docs = render("splattop", "splattop-prod", "helm/splattop/values-prod.yaml",
                      ("fastapi.spreadAcrossNodes=false",))
        spec = find(docs, "Deployment", "splattop-prod-fastapi")["spec"]
        self.assertNotIn("topologySpreadConstraints", spec["template"]["spec"])
        self.assertFalse(any(d["kind"] == "PodDisruptionBudget" for d in docs))

    def test_worker_single_replica_does_not_block_node_drains(self) -> None:
        docs = render("agent-workloads", "agent-workloads", "apps/agent-workloads/values.yaml",
                      (r"workers.data\.workspace_probe.replicaCount=1",))
        self.assertFalse(any(d["kind"] == "PodDisruptionBudget" for d in docs))

    def test_worker_spreading_can_be_disabled_without_disruption_budget(self) -> None:
        docs = render("agent-workloads", "agent-workloads", "apps/agent-workloads/values.yaml",
                      (r"workers.data\.workspace_probe.spreadAcrossNodes=false",))
        pod = find(docs, "Deployment", "agent-workloads")["spec"]["template"]["spec"]
        self.assertNotIn("topologySpreadConstraints", pod)
        self.assertFalse(any(d["kind"] == "PodDisruptionBudget" for d in docs))


if __name__ == "__main__":
    unittest.main()
