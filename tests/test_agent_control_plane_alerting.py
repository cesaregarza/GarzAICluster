from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def _render_observability_prod() -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    result = subprocess.run(
        [
            "helm",
            "template",
            "garz-observability",
            str(REPO_ROOT / "helm/garz-observability"),
            "-f",
            str(REPO_ROOT / "helm/garz-observability/values-prod.yaml"),
        ],
        check=True,
        cwd=REPO_ROOT,
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
        if doc.get("kind") != kind or metadata.get("name") != name:
            continue
        if namespace is not None and metadata.get("namespace") != namespace:
            continue
        return doc
    raise AssertionError(f"{kind}/{name} not rendered")


class AgentControlPlaneAlertingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = _render_observability_prod()

    def test_prometheus_annotation_scrape_keeps_control_plane_target_labels(self) -> None:
        config_map = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-config",
            namespace="monitoring",
        )
        prometheus_config = YAML_PARSER.load(config_map["data"]["prometheus.yml"])

        scrape_configs = prometheus_config["scrape_configs"]
        pod_job = next(
            config
            for config in scrape_configs
            if config["job_name"] == "kubernetes-pods"
        )
        self.assertNotIn(
            "agent-control-plane",
            {config["job_name"] for config in scrape_configs},
        )
        self.assertNotIn("authorization", pod_job)

        relabel_targets = {
            tuple(config.get("source_labels", [])): config.get("target_label")
            for config in pod_job["relabel_configs"]
        }
        self.assertEqual(
            relabel_targets[("__meta_kubernetes_namespace",)],
            "namespace",
        )
        self.assertEqual(
            relabel_targets[("__meta_kubernetes_pod_name",)],
            "pod",
        )
        self.assertEqual(
            relabel_targets[("__meta_kubernetes_pod_label_app_kubernetes_io_name",)],
            "app_kubernetes_io_name",
        )
        self.assertEqual(
            relabel_targets[
                ("__meta_kubernetes_pod_label_app_kubernetes_io_component",)
            ],
            "app_kubernetes_io_component",
        )
        rendered_config = config_map["data"]["prometheus.yml"]
        self.assertNotIn("AGENT_PLATFORM_AUDIT_READ_TOKEN", rendered_config)
        self.assertNotIn("agent-control-plane-metrics-token", rendered_config)

    def test_prometheus_does_not_mount_control_plane_metrics_token_secret(self) -> None:
        stateful_set = _find_doc(
            self.docs,
            kind="StatefulSet",
            name="splattop-prod-prometheus",
            namespace="monitoring",
        )
        pod_spec = stateful_set["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]

        volume_mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        self.assertNotIn("agent-control-plane-metrics-token", volume_mounts)

        volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
        self.assertNotIn("agent-control-plane-metrics-token", volumes)

    def test_agent_control_plane_alert_rules_render_expected_alerts_and_slos(self) -> None:
        rules_config_map = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-rules",
            namespace="monitoring",
        )
        rule_file = YAML_PARSER.load(
            rules_config_map["data"]["cluster-mandate-alerts.yaml"]
        )
        groups = {group["name"]: group for group in rule_file["groups"]}
        mandate_group = groups["mandate-agent-control-plane"]
        rules = {rule["alert"]: rule for rule in mandate_group["rules"]}

        self.assertEqual(
            set(rules),
            {
                "MandateAgentControlPlaneMetricsDown",
                "MandateCallbackAdapterDown",
                "MandateCallbackOutboxBacklog",
                "MandateOldestReadyJobAgeHigh",
                "MandateDeadLetteredCallbackDeliveries",
                "MandateMigrationJobFailed",
            },
        )
        self.assertEqual(
            rules["MandateAgentControlPlaneMetricsDown"]["labels"]["slo"],
            "metrics-observability",
        )
        self.assertEqual(
            rules["MandateOldestReadyJobAgeHigh"]["labels"]["slo"],
            "time-to-claim",
        )
        self.assertEqual(
            {
                rules["MandateCallbackAdapterDown"]["labels"]["slo"],
                rules["MandateCallbackOutboxBacklog"]["labels"]["slo"],
            },
            {"callback-delivery-latency"},
        )
        self.assertEqual(
            {
                rules["MandateDeadLetteredCallbackDeliveries"]["labels"]["slo"],
                rules["MandateMigrationJobFailed"]["labels"]["slo"],
            },
            {"journey-success-rate"},
        )
        rendered_rules = rules_config_map["data"]["cluster-mandate-alerts.yaml"]
        self.assertIn('job="kubernetes-pods"', rendered_rules)
        self.assertIn('namespace="agent-control-plane"', rendered_rules)
        self.assertIn('app_kubernetes_io_component="api"', rendered_rules)
        self.assertIn("mandate_callback_outbox_events_total", rendered_rules)
        self.assertIn("mandate_control_oldest_ready_job_age_seconds", rendered_rules)
        self.assertIn("kube_deployment_status_replicas_available", rendered_rules)
        self.assertIn("kube_job_status_failed", rendered_rules)

    def test_alertmanager_routes_to_discord_webhook_file(self) -> None:
        alertmanager_config = _find_doc(
            self.docs,
            kind="Secret",
            name="alertmanager-config",
            namespace="monitoring",
        )
        config = YAML_PARSER.load(alertmanager_config["stringData"]["alertmanager.yaml"])
        self.assertEqual(config["route"]["receiver"], "discord-ops")
        self.assertEqual(
            config["receivers"][0]["discord_configs"][0]["webhook_url_file"],
            "/etc/alertmanager/secrets/discord-webhook/webhook-url",
        )

        deployment = _find_doc(
            self.docs,
            kind="Deployment",
            name="splattop-prod-alertmanager",
            namespace="monitoring",
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        volume_mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        self.assertEqual(
            volume_mounts["discord-webhook"]["mountPath"],
            "/etc/alertmanager/secrets/discord-webhook",
        )
        self.assertTrue(volume_mounts["discord-webhook"]["readOnly"])

        volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
        self.assertEqual(
            volumes["discord-webhook"]["secret"]["secretName"],
            "alertmanager-discord-webhook",
        )
        self.assertEqual(
            volumes["discord-webhook"]["secret"]["items"],
            [{"key": "webhook-url", "path": "webhook-url"}],
        )

    def test_prometheus_network_policy_allows_agent_control_plane_egress(self) -> None:
        policy = _find_doc(
            self.docs,
            kind="NetworkPolicy",
            name="prometheus-ingress-egress",
            namespace="monitoring",
        )
        egress_namespaces = {
            selector["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in policy["spec"]["egress"]
            for selector in rule.get("to", [])
            if "namespaceSelector" in selector
        }
        self.assertIn("agent-control-plane", egress_namespaces)

    def test_agent_control_plane_values_allow_prometheus_and_body_limit(self) -> None:
        values = YAML_PARSER.load(
            (REPO_ROOT / "apps/agent-control-plane/values.yaml").read_text()
        )
        self.assertEqual(
            values["ingress"]["annotations"]["nginx.ingress.kubernetes.io/proxy-body-size"],
            "10m",
        )

        ingress_sources = values["networkPolicy"]["ingress"]["sources"]
        self.assertIn(
            {
                "namespaceSelector": {
                    "matchLabels": {
                        "kubernetes.io/metadata.name": "monitoring",
                    },
                },
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/component": "prometheus",
                    },
                },
            },
            ingress_sources,
        )


if __name__ == "__main__":
    unittest.main()
