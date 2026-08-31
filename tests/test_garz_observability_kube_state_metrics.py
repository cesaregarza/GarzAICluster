from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")
SMOKE_JOB_LABEL_TO_KSM_LABEL = {
    "app.kubernetes.io/component": "label_app_kubernetes_io_component",
    "citrus.grace/source-revision": "label_citrus_grace_source_revision",
    "citrus.grace/smoke-step": "label_citrus_grace_smoke_step",
}
SMOKE_ALERT_FIXTURE = (
    REPO_ROOT
    / "helm"
    / "garz-observability"
    / "tests"
    / "citrus-stripe-smoke-alerts.test.yaml"
)


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


def _render_citrus_automated_dev() -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    result = subprocess.run(
        [
            "helm",
            "template",
            "citrus-dev",
            "helm/citrus",
            "--namespace",
            "citrus-dev",
            "-f",
            "helm/citrus/values.yaml",
            "-f",
            "helm/citrus/values-dev.yaml",
            "-f",
            "helm/citrus/values-payment-dev.yaml",
            "--set",
            "stripeSmokeRunner.enabled=true",
            "--set",
            "stripeSmokeRunner.automation.enabled=true",
            "--set-string",
            (
                "stripeSmokeRunner.expectedAccountId="
                "acct_0000000000000000"
            ),
        ],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return [
        doc
        for doc in YAML_PARSER.load_all(result.stdout)
        if isinstance(doc, dict) and doc
    ]


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
        cls.citrus_docs = _render_citrus_automated_dev()

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

    def test_cpu_headroom_and_citrus_pending_alerts_are_bounded(self) -> None:
        rendered_rules = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-rules",
            namespace="monitoring",
        )["data"]["critical-alerts.yaml"]
        rules_document = YAML_PARSER.load(rendered_rules)
        alerts = {
            rule["alert"]: rule
            for group in rules_document["groups"]
            for rule in group["rules"]
            if "alert" in rule
        }

        headroom = alerts["KubernetesNodeCPURequestHeadroomLow"]
        headroom_expression = " ".join(headroom["expr"].split())
        self.assertEqual(headroom["for"], "10m")
        self.assertEqual(headroom["labels"]["severity"], "warning")
        self.assertIn("kube_pod_container_resource_requests", headroom_expression)
        self.assertIn("kube_node_status_allocatable", headroom_expression)
        self.assertIn('phase="Running"', headroom_expression)
        self.assertIn("> 0.85", headroom_expression)

        pending = alerts["CitrusPodPending"]
        pending_expression = " ".join(pending["expr"].split())
        self.assertEqual(pending["for"], "1m")
        self.assertEqual(pending["labels"]["service"], "citrus")
        self.assertIn('namespace=~"default|citrus-dev"', pending_expression)
        self.assertIn('pod=~"citrus(-dev)?-.*"', pending_expression)
        self.assertIn('phase="Pending"', pending_expression)
        self.assertNotIn('namespace=~"citrus(-dev)?"', pending_expression)

    def test_citrus_stripe_smoke_alert_is_exact_image_and_job_bounded(self) -> None:
        rendered_rules = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-rules",
            namespace="monitoring",
        )["data"]["critical-alerts.yaml"]
        rules_document = YAML_PARSER.load(rendered_rules)
        alerts = {
            rule["alert"]: rule
            for group in rules_document["groups"]
            for rule in group["rules"]
            if "alert" in rule
        }

        alert = alerts["CitrusStripeSmokeGateFailed"]
        expression = " ".join(alert["expr"].split())
        labels_selector = re.search(
            r"kube_job_labels\{([^}]*)\}",
            alert["expr"],
            flags=re.DOTALL,
        )

        self.assertIsNotNone(labels_selector)
        self.assertEqual(alert["for"], "1m")
        self.assertEqual(
            alert["labels"],
            {
                "severity": "critical",
                "service": "citrus",
                "environment": "development",
            },
        )
        self.assertIn('namespace="citrus-dev"', expression)
        self.assertIn("kube_job_status_failed", expression)
        self.assertIn("kube_job_status_start_time", expression)
        self.assertIn("time() - kube_job_status_start_time", expression)
        self.assertIn("< 3600", expression)
        self.assertIn(
            "max by ( namespace, job_name, label_citrus_grace_source_revision, "
            "label_citrus_grace_smoke_step )",
            expression,
        )
        self.assertIn(
            'label_app_kubernetes_io_component="stripe-smoke-runner"',
            labels_selector.group(1),
        )
        self.assertNotIn(
            "label_citrus_grace_source_revision=",
            labels_selector.group(1),
        )
        self.assertNotIn(
            "label_citrus_grace_smoke_step=",
            labels_selector.group(1),
        )
        self.assertEqual(
            alert["annotations"]["source_revision"],
            "{{ $labels.label_citrus_grace_source_revision }}",
        )
        self.assertEqual(
            alert["annotations"]["smoke_step"],
            "{{ $labels.label_citrus_grace_smoke_step }}",
        )
        self.assertIn(
            "production promotion is blocked for this exact image",
            alert["annotations"]["description"],
        )

    def test_citrus_smoke_job_labels_flow_to_alert_rule_and_fixtures(
        self,
    ) -> None:
        jobs = [
            document
            for document in self.citrus_docs
            if document.get("kind") == "Job"
            and document.get("metadata", {}).get("labels", {}).get(
                "app.kubernetes.io/component"
            )
            == "stripe-smoke-runner"
        ]
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        job_labels = job["metadata"]["labels"]
        self.assertTrue(set(SMOKE_JOB_LABEL_TO_KSM_LABEL).issubset(job_labels))
        source_revision = job_labels["citrus.grace/source-revision"]
        self.assertRegex(source_revision, r"^[0-9a-f]{40}$")
        self.assertEqual(
            job["metadata"]["name"],
            f"citrus-smoke-{source_revision}",
        )
        self.assertEqual(job_labels["citrus.grace/smoke-step"], "claim")
        self.assertEqual(
            job["spec"]["template"]["spec"]["containers"][0]["image"].rsplit(
                ":", 1
            )[1],
            source_revision,
        )

        deployment = _find_doc(
            self.docs,
            kind="Deployment",
            name="splattop-prod-kube-state-metrics",
            namespace="monitoring",
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        allowlist = next(
            argument
            for argument in container["args"]
            if argument.startswith("--metric-labels-allowlist=jobs=[")
        )
        raw_allowlist = set(
            allowlist.removeprefix("--metric-labels-allowlist=jobs=[")
            .removesuffix("]")
            .split(",")
        )
        self.assertEqual(raw_allowlist, set(SMOKE_JOB_LABEL_TO_KSM_LABEL))

        rendered_rules = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-rules",
            namespace="monitoring",
        )["data"]["critical-alerts.yaml"]
        rules_document = YAML_PARSER.load(rendered_rules)
        alert = next(
            rule
            for group in rules_document["groups"]
            for rule in group["rules"]
            if rule.get("alert") == "CitrusStripeSmokeGateFailed"
        )
        for raw_label, metric_label in SMOKE_JOB_LABEL_TO_KSM_LABEL.items():
            expected_metric_label = "label_" + re.sub(
                r"[^a-zA-Z0-9_]", "_", raw_label
            )
            self.assertEqual(metric_label, expected_metric_label)
            self.assertIn(metric_label, alert["expr"])
        self.assertEqual(
            alert["annotations"]["source_revision"],
            "{{ $labels.label_citrus_grace_source_revision }}",
        )
        self.assertEqual(
            alert["annotations"]["smoke_step"],
            "{{ $labels.label_citrus_grace_smoke_step }}",
        )

        fixture = YAML_PARSER.load(
            SMOKE_ALERT_FIXTURE.read_text(encoding="utf-8")
        )
        stripe_label_series = []
        for test_case in fixture["tests"]:
            for sample in test_case.get("input_series", []):
                series = sample["series"]
                if (
                    series.startswith("kube_job_labels{")
                    and 'label_app_kubernetes_io_component="stripe-smoke-runner"'
                    in series
                ):
                    stripe_label_series.append(series)
        self.assertEqual(len(stripe_label_series), 4)
        for series in stripe_label_series:
            emitted_labels = set(
                re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)=", series)
            )
            self.assertTrue(
                set(SMOKE_JOB_LABEL_TO_KSM_LABEL.values()).issubset(
                    emitted_labels
                )
            )
            job_revision = re.search(
                r'job_name="citrus-smoke-([0-9a-f]{40})"', series
            )
            source_label = re.search(
                r'label_citrus_grace_source_revision="([0-9a-f]{40})"',
                series,
            )
            self.assertIsNotNone(job_revision)
            self.assertIsNotNone(source_label)
            self.assertEqual(job_revision.group(1), source_label.group(1))

    def test_kube_state_metrics_renders_cluster_metadata_for_right_sizing(self) -> None:
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
        self.assertIn(
            "--resources=pods,nodes,deployments,replicasets,statefulsets,daemonsets,jobs",
            container["args"],
        )
        label_allowlist_args = [
            arg
            for arg in container["args"]
            if arg.startswith("--metric-labels-allowlist=")
        ]
        self.assertEqual(
            label_allowlist_args,
            [
                "--metric-labels-allowlist=jobs=[app.kubernetes.io/component,"
                "citrus.grace/source-revision,citrus.grace/smoke-step]"
            ],
        )
        self.assertFalse(
            any(arg.startswith("--namespaces=") for arg in container["args"]),
            "kube-state-metrics should watch all namespaces for right-sizing metadata",
        )
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

        cluster_role = _find_doc(
            self.docs,
            kind="ClusterRole",
            name="splattop-prod-kube-state-metrics",
        )
        cluster_resources = {
            resource
            for rule in cluster_role["rules"]
            for resource in rule["resources"]
        }
        self.assertTrue(
            {
                "pods",
                "nodes",
                "namespaces",
                "deployments",
                "replicasets",
                "statefulsets",
                "daemonsets",
                "jobs",
            }.issubset(cluster_resources)
        )

        cluster_role_binding = _find_doc(
            self.docs,
            kind="ClusterRoleBinding",
            name="splattop-prod-kube-state-metrics",
        )
        self.assertEqual(cluster_role_binding["roleRef"]["kind"], "ClusterRole")
        self.assertEqual(
            cluster_role_binding["subjects"],
            [
                {
                    "kind": "ServiceAccount",
                    "name": "splattop-prod-kube-state-metrics",
                    "namespace": "monitoring",
                }
            ],
        )

    def test_alertmanager_egress_allows_only_cluster_dns_and_existing_https(
        self,
    ) -> None:
        policy = _find_doc(
            self.docs,
            kind="NetworkPolicy",
            name="alertmanager-ingress-egress",
            namespace="monitoring",
        )

        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"app.kubernetes.io/component": "alertmanager"},
        )
        self.assertEqual(
            policy["spec"]["egress"],
            [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            },
                            "podSelector": {
                                "matchLabels": {"k8s-app": "kube-dns"}
                            },
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                    "ports": [{"protocol": "TCP", "port": 443}],
                },
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

    def test_prometheus_scrapes_kubelet_usage_for_right_sizing(self) -> None:
        prometheus_config = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-config",
            namespace="monitoring",
        )["data"]["prometheus.yml"]

        self.assertIn("job_name: kubernetes-cadvisor", prometheus_config)
        self.assertIn("job_name: kubernetes-kubelet", prometheus_config)
        self.assertIn(
            "bearer_token_file: /var/run/secrets/kubernetes.io/serviceaccount/token",
            prometheus_config,
        )
        self.assertIn("replacement: kubernetes.default.svc:443", prometheus_config)
        self.assertIn(
            "replacement: /api/v1/nodes/${1}/proxy/metrics/cadvisor",
            prometheus_config,
        )
        self.assertIn(
            "replacement: /api/v1/nodes/${1}/proxy/metrics",
            prometheus_config,
        )

    def test_fastapi_scrape_discovers_default_namespace_with_sample_guardrail(
        self,
    ) -> None:
        prometheus_config = _find_doc(
            self.docs,
            kind="ConfigMap",
            name="prometheus-config",
            namespace="monitoring",
        )["data"]["prometheus.yml"]

        self.assertIn("job_name: fastapi", prometheus_config)
        self.assertIn("sample_limit: 15000", prometheus_config)
        self.assertIn('regex: "default"', prometheus_config)

    def test_prometheus_has_runtime_and_query_memory_guardrails(self) -> None:
        stateful_set = _find_doc(
            self.docs,
            kind="StatefulSet",
            name="splattop-prod-prometheus",
            namespace="monitoring",
        )
        container = stateful_set["spec"]["template"]["spec"]["containers"][0]

        self.assertIn("--query.max-samples=500000", container["args"])
        self.assertIn("--query.max-concurrency=2", container["args"])
        self.assertIn("--query.timeout=30s", container["args"])
        self.assertIn(
            {"name": "GOMEMLIMIT", "value": "1200MiB"},
            container["env"],
        )

        pod_annotations = stateful_set["spec"]["template"]["metadata"][
            "annotations"
        ]
        self.assertTrue(
            re.fullmatch(
                r"[a-f0-9]{64}",
                pod_annotations["checksum/prometheus-config"],
            )
        )
        self.assertTrue(
            re.fullmatch(
                r"[a-f0-9]{64}",
                pod_annotations["checksum/prometheus-rules"],
            )
        )


if __name__ == "__main__":
    unittest.main()
