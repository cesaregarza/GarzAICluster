from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]


def render(*overrides: str, production: bool = True) -> list[dict]:
    command = ["helm", "template", "splattop-prod", "helm/garz-observability"]
    if production:
        command.extend(["-f", "helm/garz-observability/values-prod.yaml"])
    for override in overrides:
        command.extend(["--set", override])
    result = subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True)
    return [doc for doc in YAML(typ="safe").load_all(result.stdout) if doc]


class PrometheusMetricsEgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production = render()

    def test_pod_metrics_require_both_namespace_and_workload_on_exact_port(self) -> None:
        policy = next(doc for doc in self.production if doc["metadata"]["name"] == "prometheus-ingress-egress")
        actual = {}
        for rule in policy["spec"]["egress"]:
            for peer in rule.get("to", []):
                namespace = peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
                if namespace in ("cert-manager", "agent-control-plane"):
                    self.assertEqual(len(rule["to"]), 1)
                    actual[namespace] = (peer["podSelector"]["matchLabels"], rule["ports"])
        self.assertEqual(actual, {
            "cert-manager": ({
                "app.kubernetes.io/name": "cert-manager",
                "app.kubernetes.io/component": "controller",
            }, [{"protocol": "TCP", "port": 9402}]),
            "agent-control-plane": ({
                "app.kubernetes.io/name": "agent-control-plane",
                "app.kubernetes.io/instance": "agent-control-plane",
                "app.kubernetes.io/component": "api",
            }, [{"protocol": "TCP", "port": 9090}]),
        })

    def test_node_metrics_allow_only_prometheus_to_node_metrics_port(self) -> None:
        policy = next(doc for doc in self.production if doc["metadata"]["name"] == "prometheus-egress-node-metrics")
        self.assertEqual(policy["kind"], "CiliumNetworkPolicy")
        self.assertEqual(policy["spec"], {
            "endpointSelector": {"matchLabels": {
                "app.kubernetes.io/name": "splattop",
                "app.kubernetes.io/instance": "splattop-prod",
                "app.kubernetes.io/component": "prometheus",
            }},
            "egress": [{"toEntities": ["host", "remote-node"], "toPorts": [
                {"ports": [{"port": "9090", "protocol": "TCP"}]},
            ]}],
        })

    def test_new_permissions_are_opt_in_outside_production(self) -> None:
        docs = render("monitoring.prometheus.enabled=true", production=False)
        self.assertNotIn("prometheus-egress-node-metrics", [doc["metadata"]["name"] for doc in docs])
        policy = next(doc for doc in docs if doc["metadata"]["name"] == "prometheus-ingress-egress")
        for rule in policy["spec"]["egress"]:
            for peer in rule.get("to", []):
                namespace = peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
                self.assertNotIn(namespace, ("cert-manager", "agent-control-plane"))

    def test_disabling_prometheus_omits_node_metrics_policy(self) -> None:
        docs = render("monitoring.prometheus.enabled=false")
        self.assertNotIn("prometheus-egress-node-metrics", [doc["metadata"]["name"] for doc in docs])

    def test_empty_workload_selector_is_rejected(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            render("monitoring.networkPolicies.prometheus.metricsEgress[0].namespace=cert-manager",
                   "monitoring.networkPolicies.prometheus.metricsEgress[0].port=9402",
                   "monitoring.prometheus.enabled=true", production=False)


if __name__ == "__main__":
    unittest.main()
