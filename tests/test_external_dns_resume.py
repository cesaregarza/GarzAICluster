import unittest
from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def load_documents(path: Path):
    return [item for item in YAML_PARSER.load_all(path.read_text(encoding="utf-8")) if item]


class ExternalDnsResumeTests(unittest.TestCase):
    def test_source_ip_payload_has_service_only_and_preserves_identity(self):
        documents = load_documents(ROOT / "infra/ingress-nginx/citrus-source-ip-load-balancer.yaml")
        self.assertEqual([(item["kind"], item["metadata"]["name"]) for item in documents], [("Service", "ingress-nginx-controller-source-ip")])
        service = documents[0]
        self.assertEqual(service["metadata"]["namespace"], "ingress-nginx")
        self.assertEqual(service["spec"]["selector"], {
            "app.kubernetes.io/name": "gaic-traefik-ingress",
            "app.kubernetes.io/instance": "gaic-traefik-ingress",
        })
        self.assertEqual(service["spec"]["type"], "LoadBalancer")

    def test_runbook_requires_all_external_dns_resume_gates(self):
        runbook = (ROOT / "infra/external-dns/README.md").read_text(encoding="utf-8")
        for phrase in ("129.212.154.58", "temporary canary Ingress", "15-record A-content move", "missing `cegarza.com` TXT", "server dry-run", "automated sync"):
            self.assertIn(phrase, runbook)


if __name__ == "__main__":
    unittest.main()
