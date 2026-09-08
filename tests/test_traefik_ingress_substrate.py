from __future__ import annotations

import unittest
from pathlib import Path
import subprocess
import sys
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA_DIR = REPO_ROOT / "infra" / "traefik-ingress"
CANARY_FILE = INFRA_DIR / "canary" / "citrus.yaml"
CANARY_GENERATOR = REPO_ROOT / "scripts" / "generate_ingress_canary.py"
MANDATE_VALUES = REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml"
PARSER = YAML(typ="safe")
TARGET_IMAGE = (
    "docker.io/library/traefik@"
    "sha256:f86a2cab1b5c649070c49f883c743dd32d8485a56e3368c5f93b9e91f1e91259"
)
SELECTOR = {
    "app.kubernetes.io/name": "gaic-traefik-ingress",
    "app.kubernetes.io/instance": "gaic-traefik-ingress",
}


def _documents(path: Path) -> list[dict[str, Any]]:
    return [
        document
        for document in PARSER.load_all(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document
    ]


def _documents_from_text(text: str) -> list[dict[str, Any]]:
    return [
        document
        for document in PARSER.load_all(text)
        if isinstance(document, dict) and document
    ]


class TraefikIngressSubstrateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = []
        for path in sorted(INFRA_DIR.glob("*.yaml")):
            if path.name == "kustomization.yaml":
                continue
            cls.docs.extend(_documents(path))
        cls.by_kind = {document["kind"]: document for document in cls.docs}
        cls.runbook = (INFRA_DIR / "README.md").read_text(encoding="utf-8")
        cls.canary = _documents(CANARY_FILE)[0]

    def test_payload_is_small_and_has_no_public_service_or_secret(self) -> None:
        self.assertEqual(
            [document["kind"] for document in self.docs],
            [
                "Deployment",
                "IngressClass",
                "PodDisruptionBudget",
                "ClusterRole",
                "ClusterRoleBinding",
                "Service",
                "ServiceAccount",
            ],
        )
        self.assertNotIn("Secret", self.by_kind)
        self.assertNotIn("LoadBalancer", str(self.by_kind["Service"]["spec"]))
        self.assertEqual(self.by_kind["Service"]["spec"]["type"], "ClusterIP")
        root = (REPO_ROOT / "argocd" / "applications" / "root.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("infra/traefik-ingress", root)

    def test_image_and_controller_class_are_immutable_and_isolated(self) -> None:
        deployment = self.by_kind["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], TARGET_IMAGE)
        self.assertEqual(deployment["spec"]["replicas"], 2)
        self.assertEqual(
            deployment["spec"]["strategy"],
            {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
            },
        )
        self.assertEqual(
            self.by_kind["IngressClass"]["spec"],
            {"controller": "traefik.io/ingress-controller"},
        )
        args = set(container["args"])
        self.assertIn(
            "--providers.kubernetesingressnginx.ingressclass=traefik-nginx", args
        )
        self.assertIn(
            "--providers.kubernetesingressnginx.controllerclass=traefik.io/ingress-controller",
            args,
        )
        self.assertFalse(
            any(
                argument.startswith("--providers.kubernetesingressnginx.publishservice")
                for argument in args
            )
        )
        self.assertIn(
            "--providers.kubernetesingressnginx.allowsnippetannotations=false", args
        )
        self.assertIn(
            "--providers.kubernetesingressnginx.watchingresswithoutclass=false", args
        )
        self.assertIn("--providers.kubernetescrd=false", args)
        self.assertIn("--providers.kubernetesgateway=false", args)

    def test_scheduler_budget_ports_and_pdb_match_canary_contract(self) -> None:
        deployment = self.by_kind["Deployment"]
        pod = deployment["spec"]["template"]
        self.assertEqual(pod["spec"]["topologySpreadConstraints"][0]["maxSkew"], 1)
        self.assertEqual(pod["spec"]["topologySpreadConstraints"][0]["minDomains"], 2)
        self.assertEqual(
            pod["spec"]["topologySpreadConstraints"][0]["whenUnsatisfiable"],
            "DoNotSchedule",
        )
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["resources"]["requests"], {"cpu": "50m", "memory": "128Mi"})
        self.assertEqual(
            [(port["name"], port["containerPort"]) for port in container["ports"]],
            [("traefik", 8080), ("web", 80), ("websecure", 443)],
        )
        service = self.by_kind["Service"]
        self.assertEqual(service["spec"]["selector"], SELECTOR)
        self.assertEqual(
            [(port["port"], port["targetPort"]) for port in service["spec"]["ports"]],
            [(80, "web"), (443, "websecure")],
        )
        self.assertEqual(self.by_kind["PodDisruptionBudget"]["spec"]["minAvailable"], 1)

    def test_rbac_is_read_only_until_status_publication_is_reviewed(self) -> None:
        role = self.by_kind["ClusterRole"]
        self.assertEqual(
            {
                (rule["apiGroups"][0], resource, tuple(rule["verbs"]))
                for rule in role["rules"]
                for resource in rule["resources"]
            },
            {
                ("", "configmaps", ("get", "list", "watch")),
                ("", "nodes", ("get", "list", "watch")),
                ("", "services", ("get", "list", "watch")),
                ("", "namespaces", ("list", "watch")),
                ("", "pods", ("get",)),
                ("", "secrets", ("get", "list", "watch")),
                ("discovery.k8s.io", "endpointslices", ("list", "watch")),
                ("networking.k8s.io", "ingressclasses", ("get", "list", "watch")),
                ("networking.k8s.io", "ingresses", ("get", "list", "watch")),
            },
        )

    def test_canary_preserves_tls_backend_and_has_no_ownership_annotations(self) -> None:
        self.assertEqual(self.canary["metadata"]["namespace"], "default")
        self.assertEqual(self.canary["spec"]["ingressClassName"], "traefik-nginx")
        self.assertNotIn("annotations", self.canary["metadata"])
        self.assertEqual(
            self.canary["spec"]["tls"],
            [
                {
                    "hosts": ["citrus-grace.com", "www.citrus-grace.com"],
                    "secretName": "citrus-grace-tls",
                }
            ],
        )
        for rule in self.canary["spec"]["rules"]:
            path = rule["http"]["paths"][0]
            self.assertEqual(
                path["backend"]["service"],
                {"name": "citrus-service", "port": {"number": 80}},
            )

        generated = subprocess.run(
            [
                sys.executable,
                str(CANARY_GENERATOR),
                "--host",
                "citrus-grace.com",
                "--host",
                "www.citrus-grace.com",
                "--service",
                "citrus-service",
                "--service-port",
                "80",
                "--tls-secret",
                "citrus-grace-tls",
                "--name",
                "citrus-traefik-canary",
                "--namespace",
                "default",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(_documents_from_text(generated.stdout)[0], self.canary)

        for invalid_host in ("bad_host.example.com", "*.example.com", "a..example.com"):
            rejected = subprocess.run(
                [
                    sys.executable,
                    str(CANARY_GENERATOR),
                    "--host",
                    invalid_host,
                    "--service",
                    "citrus-service",
                    "--tls-secret",
                    "citrus-grace-tls",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected.returncode, 0, invalid_host)

    def test_mandate_allows_both_current_and_traefik_ingress_sources(self) -> None:
        values = PARSER.load(MANDATE_VALUES.read_text(encoding="utf-8"))
        sources = values["networkPolicy"]["ingress"]["sources"]
        self.assertIn(
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}
                },
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "ingress-nginx"}},
            },
            sources,
        )
        self.assertIn(
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}
                },
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "gaic-traefik-ingress",
                        "app.kubernetes.io/instance": "gaic-traefik-ingress",
                    }
                },
            },
            sources,
        )
        self.assertEqual(
            values["networkPolicy"]["ingress"]["ports"],
            [{"port": 8000, "protocol": "TCP"}, {"port": 9090, "protocol": "TCP"}],
        )

    def test_runbook_protects_status_dns_acme_and_selector_rollback(self) -> None:
        normalized_runbook = " ".join(self.runbook.split())
        for phrase in (
            "creates no `LoadBalancer` Service",
            "string-valued `publishservice` option and `publishstatusaddress` are omitted",
            "same-host canary Ingress",
            "does not publish status",
            "patch only the existing source-IP Service",
            "exact saved source-IP Service selector",
            "`X-Forwarded-For`; the forged",
            "Do not copy `cert-manager.io/cluster-issuer`",
            "do not start a renewal",
            "`nginx.org/websocket-services`",
            "Do not delete the old Service or either load balancer",
        ):
            self.assertIn(phrase, normalized_runbook)


if __name__ == "__main__":
    unittest.main()
