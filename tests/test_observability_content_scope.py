from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")

CLUSTER_ALERTS = {
    "PrometheusTSDBCapacityHigh",
    "AlertmanagerDown",
    "MandateSyntheticLiveVerifyFailed",
    "MandateAgentControlPlaneMetricsDown",
    "MandateCallbackAdapterDown",
    "MandateCallbackOutboxBacklog",
    "MandateOldestReadyJobAgeHigh",
    "MandateDeadLetteredCallbackDeliveries",
    "MandateMigrationJobFailed",
    "GrafanaDown",
}
SPLATTOP_APP_ALERTS = {
    "FastAPIDown",
    "FastAPIFiveHundreds",
    "FastAPIHighLatencyP95",
    "LookupSQLiteSnapshotStale",
    "CompetitionPlayerSummaryLatencyHigh",
    "CeleryTaskFailures",
    "CeleryWorkersStuck",
    "APIUsageQueueBacklog",
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


class ObservabilityContentScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = _render_observability_prod()
        cls.values = _load_yaml(
            REPO_ROOT / "helm" / "garz-observability" / "values-prod.yaml"
        )
        cls.rules_configmap = _resource(
            cls.docs,
            kind="ConfigMap",
            name="prometheus-rules",
        )
        cls.prometheus_configmap = _resource(
            cls.docs,
            kind="ConfigMap",
            name="prometheus-config",
        )

    def test_alert_rules_are_split_without_dropping_existing_alerts(self) -> None:
        rule_files = self.rules_configmap["data"]

        self.assertIn("cluster-mandate-alerts.yaml", rule_files)
        self.assertIn("splattop-app-alerts.yaml", rule_files)
        self.assertNotIn("critical-alerts.yaml", rule_files)
        self.assertEqual(
            _alert_names(rule_files["cluster-mandate-alerts.yaml"]),
            CLUSTER_ALERTS,
        )
        self.assertEqual(
            _alert_names(rule_files["splattop-app-alerts.yaml"]),
            SPLATTOP_APP_ALERTS,
        )

    def test_scrape_jobs_delineate_cluster_annotation_and_splattop_app_targets(
        self,
    ) -> None:
        prometheus_config = YAML_PARSER.load(
            self.prometheus_configmap["data"]["prometheus.yml"]
        )
        scrape_configs = {
            config["job_name"]: config
            for config in prometheus_config["scrape_configs"]
        }

        self.assertIn("kubernetes-pods", scrape_configs)
        self.assertIn("fastapi", scrape_configs)
        self.assertEqual(
            _relabel_replacement(
                scrape_configs["kubernetes-pods"],
                "observability_scope",
            ),
            "cluster-annotation",
        )
        self.assertEqual(
            _relabel_replacement(scrape_configs["fastapi"], "observability_scope"),
            "splattop-app",
        )

    def test_grafana_canonical_host_is_garzai_with_old_host_redirect(self) -> None:
        grafana_values = self.values["monitoring"]["grafana"]
        self.assertEqual(grafana_values["serverDomain"], "grafana.garz.ai")
        self.assertEqual(grafana_values["serverRootUrl"], "https://grafana.garz.ai/")

        canonical = _resource(self.docs, kind="Ingress", name="grafana-ingress")
        self.assertEqual(
            canonical["spec"]["rules"][0]["host"],
            "grafana.garz.ai",
        )
        self.assertEqual(
            canonical["spec"]["tls"][0]["secretName"],
            "grafana-garz-ai-tls",
        )

        redirect = _resource(
            self.docs,
            kind="Ingress",
            name="grafana-splat-top-redirect",
        )
        self.assertEqual(redirect["spec"]["rules"][0]["host"], "grafana.splat.top")
        annotations = redirect["metadata"]["annotations"]
        self.assertEqual(
            annotations["nginx.ingress.kubernetes.io/permanent-redirect"],
            "https://grafana.garz.ai/",
        )
        self.assertEqual(
            redirect["spec"]["tls"][0]["secretName"],
            "grafana-tls",
        )


def _alert_names(rule_file: str) -> set[str]:
    payload = YAML_PARSER.load(rule_file)
    return {
        rule["alert"]
        for group in payload["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }


def _relabel_replacement(scrape_config: dict[str, Any], target_label: str) -> str:
    for relabel in scrape_config.get("relabel_configs", []):
        if relabel.get("target_label") == target_label:
            return relabel["replacement"]
    raise AssertionError(
        f"{scrape_config['job_name']} does not set relabel target {target_label}"
    )
