from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA_DIR = REPO_ROOT / "infra" / "ingress-nginx"
MANIFEST = INFRA_DIR / "citrus-source-ip-load-balancer.yaml"
RUNBOOK = INFRA_DIR / "README.md"
YAML_PARSER = YAML(typ="safe")
CONTROLLER_SELECTOR = {
    "app.kubernetes.io/component": "controller",
    "app.kubernetes.io/instance": "ingress-nginx",
    "app.kubernetes.io/name": "ingress-nginx",
}


class CitrusSourceIpLoadBalancerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = [
            document
            for document in YAML_PARSER.load_all(MANIFEST.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document
        ]
        cls.by_kind = {document["kind"]: document for document in cls.documents}
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")

    def test_payload_contains_only_pdb_and_parallel_service(self) -> None:
        self.assertEqual(
            [document["kind"] for document in self.documents],
            ["PodDisruptionBudget", "Service"],
        )
        for document in self.documents:
            metadata = document["metadata"]
            self.assertEqual(metadata["namespace"], "ingress-nginx")
            for server_field in ("creationTimestamp", "finalizers", "resourceVersion", "uid"):
                self.assertNotIn(server_field, metadata)
            self.assertNotIn("status", document)

    def test_service_creates_a_distinct_source_ip_preserving_load_balancer(self) -> None:
        service = self.by_kind["Service"]
        self.assertEqual(service["metadata"]["name"], "ingress-nginx-controller-source-ip")
        self.assertNotIn("kubernetes.digitalocean.com/load-balancer-id", service["metadata"]["annotations"])
        self.assertEqual(
            service["metadata"]["annotations"],
            {
                "service.beta.kubernetes.io/do-loadbalancer-name": "citrus-source-ip",
                "service.beta.kubernetes.io/do-loadbalancer-type": "REGIONAL_NETWORK",
            },
        )
        spec = service["spec"]
        self.assertEqual(spec["type"], "LoadBalancer")
        self.assertEqual(spec["externalTrafficPolicy"], "Local")
        self.assertEqual(spec["selector"], CONTROLLER_SELECTOR)
        self.assertEqual(
            [(port["protocol"], port["port"], port["targetPort"]) for port in spec["ports"]],
            [("TCP", 80, 80), ("TCP", 443, 443)],
        )
        self.assertNotIn("clusterIP", spec)
        self.assertNotIn("nodePort", str(spec))

    def test_pdb_preserves_a_controller_during_cutover(self) -> None:
        pdb = self.by_kind["PodDisruptionBudget"]
        self.assertEqual(pdb["metadata"]["name"], "ingress-nginx-controller-source-ip-cutover")
        self.assertEqual(pdb["spec"]["minAvailable"], 1)
        self.assertEqual(pdb["spec"]["selector"]["matchLabels"], CONTROLLER_SELECTOR)

    def test_payload_is_not_wired_to_automatic_gitops_reconciliation(self) -> None:
        self.assertFalse(list(INFRA_DIR.glob("kustomization.y*ml")))
        manifest_reference = MANIFEST.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "argocd").rglob("*.yaml"):
            self.assertNotIn(manifest_reference, path.read_text(encoding="utf-8"))

    def test_runbook_freezes_blue_green_gates_and_rollback(self) -> None:
        normalized_runbook = " ".join(self.runbook.split())
        required_phrases = (
            "Merging it is inert",
            "public preview",
            "no AAAA record",
            "two `READY=true` rows on different nodes",
            "at least seven days before",
            "only new public worker rule",
            "kubectl apply -f infra/ingress-nginx/citrus-source-ip-load-balancer.yaml",
            "80→80 and 443→443",
            "known public IPv4 address",
            "get_client_ip",
            "forged `X-Forwarded-For`",
            "DIRECT_ORDER_PAYMENT_SETUP_ENABLED",
            "Keep the old Service and load balancer intact",
            "Restore the three DNS A records to `143.244.222.41`",
            "obtain explicit cleanup approval",
            "removed the network load balancer's public port-10256",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, normalized_runbook)


if __name__ == "__main__":
    unittest.main()
