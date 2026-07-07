from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def _render_garz_observability_prod() -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    result = subprocess.run(
        [
            "helm",
            "template",
            "garz-observability",
            "helm/garz-observability",
            "--namespace",
            "monitoring",
            "-f",
            "helm/garz-observability/values-prod.yaml",
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
    namespace: str | None = None,
) -> dict[str, Any]:
    for doc in docs:
        metadata = doc.get("metadata", {})
        if (
            doc.get("kind") == kind
            and metadata.get("name") == name
            and (namespace is None or metadata.get("namespace") == namespace)
        ):
            return doc
    namespace_label = f" in {namespace}" if namespace else ""
    raise AssertionError(f"{kind}/{name}{namespace_label} not rendered")


class GarzObservabilityKubeStateMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = _render_garz_observability_prod()

    def test_synthetic_live_verify_alert_uses_kube_job_state_metrics(self) -> None:
        rules = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-rules",
            namespace="monitoring",
        )["data"]["critical-alerts.yaml"]

        self.assertIn("MandateSyntheticLiveVerifyFailed", rules)
        self.assertIn(
            'kube_job_status_failed{namespace="agent-control-plane"}',
            rules,
        )
        self.assertIn("kube_job_owner", rules)
        self.assertIn('owner_kind="CronJob"', rules)
        self.assertIn(
            'owner_name="agent-control-plane-synthetic-live-verify"',
            rules,
        )

    def test_kube_state_metrics_renders_job_source_for_alert(self) -> None:
        deployment = _find_doc(
            self.docs,
            kind="Deployment",
            name="splattop-prod-kube-state-metrics",
            namespace="monitoring",
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["image"],
            "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.19.1",
        )
        self.assertIn("--resources=jobs", container["args"])
        self.assertIn("--namespaces=agent-control-plane", container["args"])
        self.assertEqual(
            deployment["spec"]["template"]["spec"]["serviceAccountName"],
            "splattop-prod-kube-state-metrics",
        )

        service = _find_doc(
            self.docs,
            kind="Service",
            name="splattop-prod-kube-state-metrics",
            namespace="monitoring",
        )
        self.assertEqual(service["spec"]["ports"][0]["port"], 8080)

        role = _find_doc(
            self.docs,
            kind="Role",
            name="splattop-prod-kube-state-metrics",
            namespace="agent-control-plane",
        )
        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": ["batch"],
                    "resources": ["jobs"],
                    "verbs": ["list", "watch"],
                }
            ],
        )

        role_binding = _find_doc(
            self.docs,
            kind="RoleBinding",
            name="splattop-prod-kube-state-metrics",
            namespace="agent-control-plane",
        )
        self.assertEqual(
            role_binding["subjects"],
            [
                {
                    "kind": "ServiceAccount",
                    "name": "splattop-prod-kube-state-metrics",
                    "namespace": "monitoring",
                }
            ],
        )

    def test_prometheus_scrapes_kube_state_metrics(self) -> None:
        prometheus_config = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-config",
            namespace="monitoring",
        )["data"]["prometheus.yml"]

        self.assertIn("job_name: kube-state-metrics", prometheus_config)
        self.assertIn(
            "splattop-prod-kube-state-metrics.monitoring.svc.cluster.local:8080",
            prometheus_config,
        )


if __name__ == "__main__":
    unittest.main()
