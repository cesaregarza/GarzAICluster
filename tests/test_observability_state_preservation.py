from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")

PROMETHEUS_STATEFULSET = "splattop-prod-prometheus"
PROMETHEUS_TSDB_CLAIM_TEMPLATE = "prometheus-data"
PROMETHEUS_TSDB_PVC = "prometheus-data-splattop-prod-prometheus-0"
GRAFANA_DEPLOYMENT = "splattop-prod-grafana"
GRAFANA_STORAGE_PVC = "splattop-prod-grafana-storage"
LEGACY_SELECTOR_LABELS = {
    "app.kubernetes.io/name": "splattop",
    "app.kubernetes.io/instance": "splattop-prod",
}


def _render_observability_prod() -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    result = subprocess.run(
        [
            "helm",
            "template",
            "garz-observability",
            "helm/garz-observability",
            "-n",
            "monitoring",
            "-f",
            "helm/garz-observability/values-prod.yaml",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document.get("kind")
    ]


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text())
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded


def _resource(
    docs: list[dict[str, Any]],
    *,
    kind: str,
    name: str,
    namespace: str = "monitoring",
) -> dict[str, Any]:
    for document in docs:
        metadata = document.get("metadata") or {}
        if (
            document.get("kind") == kind
            and metadata.get("name") == name
            and metadata.get("namespace") == namespace
        ):
            return document
    raise AssertionError(f"{kind}/{namespace}/{name} not rendered")


class ObservabilityStatePreservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = _render_observability_prod()
        cls.values = _load_yaml(
            REPO_ROOT / "helm" / "garz-observability" / "values-prod.yaml"
        )
        cls.readme = (
            REPO_ROOT / "docs" / "observability-state-preservation.md"
        ).read_text()

    def test_prod_values_record_stable_identity_decision(self) -> None:
        self.assertEqual(self.values["nameOverride"], "splattop")
        self.assertEqual(self.values["fullnameOverride"], "splattop-prod")
        self.assertEqual(
            self.values["legacySelectorLabels"],
            {"name": "splattop", "instance": "splattop-prod"},
        )

    def test_prometheus_tsdb_claim_identity_is_preserved(self) -> None:
        statefulset = _resource(
            self.docs,
            kind="StatefulSet",
            name=PROMETHEUS_STATEFULSET,
        )
        self.assertEqual(statefulset["spec"]["serviceName"], PROMETHEUS_STATEFULSET)
        self.assertEqual(
            statefulset["spec"]["selector"]["matchLabels"],
            LEGACY_SELECTOR_LABELS | {"app.kubernetes.io/component": "prometheus"},
        )
        self.assertEqual(
            statefulset["spec"]["template"]["metadata"]["labels"],
            LEGACY_SELECTOR_LABELS | {"app.kubernetes.io/component": "prometheus"},
        )

        claim_templates = {
            template["metadata"]["name"]: template
            for template in statefulset["spec"]["volumeClaimTemplates"]
        }
        prometheus_claim = claim_templates[PROMETHEUS_TSDB_CLAIM_TEMPLATE]
        self.assertEqual(
            _statefulset_pvc_name(
                statefulset_name=PROMETHEUS_STATEFULSET,
                claim_template=PROMETHEUS_TSDB_CLAIM_TEMPLATE,
                ordinal=0,
            ),
            PROMETHEUS_TSDB_PVC,
        )
        self.assertEqual(prometheus_claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertNotIn("storageClassName", prometheus_claim["spec"])
        self.assertEqual(
            prometheus_claim["spec"]["resources"]["requests"]["storage"],
            "20Gi",
        )

    def test_grafana_runtime_claim_identity_and_dashboards_are_preserved(self) -> None:
        grafana = _resource(self.docs, kind="Deployment", name=GRAFANA_DEPLOYMENT)
        self.assertEqual(
            grafana["spec"]["selector"]["matchLabels"],
            LEGACY_SELECTOR_LABELS | {"app.kubernetes.io/component": "grafana"},
        )
        storage_volume = _volume(grafana, "grafana-storage")
        self.assertEqual(
            storage_volume["persistentVolumeClaim"]["claimName"],
            GRAFANA_STORAGE_PVC,
        )

        pvc = _resource(self.docs, kind="PersistentVolumeClaim", name=GRAFANA_STORAGE_PVC)
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertNotIn("storageClassName", pvc["spec"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "5Gi")

        configmaps = {
            document["metadata"]["name"]
            for document in self.docs
            if document.get("kind") == "ConfigMap"
        }
        self.assertGreaterEqual(
            configmaps,
            {
                "grafana-datasources",
                "grafana-dashboard-providers",
                "grafana-dashboard-core",
                "grafana-dashboard-observability",
            },
        )

    def test_runbook_names_the_cutover_bindings_to_verify(self) -> None:
        self.assertIn(PROMETHEUS_TSDB_PVC, self.readme)
        self.assertIn(GRAFANA_STORAGE_PVC, self.readme)
        self.assertIn(".spec.volumeName", self.readme)
        self.assertIn("no replacement monitoring volumes", self.readme)


def _statefulset_pvc_name(
    *,
    statefulset_name: str,
    claim_template: str,
    ordinal: int,
) -> str:
    return f"{claim_template}-{statefulset_name}-{ordinal}"


def _volume(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    for volume in deployment["spec"]["template"]["spec"]["volumes"]:
        if volume.get("name") == name:
            return volume
    raise AssertionError(f"volume {name} not rendered")
