from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
DEV_VALUES = CHART_PATH / "values-dev.yaml"
OBSERVABILITY_CHART = REPO_ROOT / "helm" / "garz-observability"
OBSERVABILITY_VALUES = OBSERVABILITY_CHART / "values-prod.yaml"
YAML_PARSER = YAML(typ="safe")


def _render(
    *,
    dev: bool,
    activate: bool,
    queue: str | None = None,
) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    release = "citrus-dev" if dev else "citrus"
    namespace = "citrus-dev" if dev else "default"
    command = [
        "helm",
        "template",
        release,
        str(CHART_PATH),
        "--namespace",
        namespace,
    ]
    if dev:
        command.extend(["-f", str(DEV_VALUES)])
    if activate:
        command.extend([
            "--set",
            "billingWorker.enabled=true",
            "--set",
            "recurringRuntime.enabled=true",
        ])
    if queue is not None:
        command.extend([
            "--set-string",
            f"application.configData.RECURRING_BILLING_QUEUE={queue}",
        ])
    result = subprocess.run(
        command,
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _render_recurring_alerts() -> dict[str, dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    result = subprocess.run(
        [
            "helm",
            "template",
            "garz-observability",
            str(OBSERVABILITY_CHART),
            "--namespace",
            "monitoring",
            "-f",
            str(OBSERVABILITY_VALUES),
            "--show-only",
            "templates/monitoring-prometheus-rules-configmap.yaml",
        ],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    documents = [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]
    rules_config = _named(documents, "ConfigMap", "prometheus-rules")
    rules_document = YAML_PARSER.load(rules_config["data"]["critical-alerts.yaml"])
    return {
        rule["alert"]: rule
        for group in rules_document["groups"]
        for rule in group["rules"]
        if str(rule.get("alert", "")).startswith("CitrusRecurring")
    }


def _named(documents, kind, name):
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


class CitrusRecurringRuntimeChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.default = _render(dev=False, activate=False)
        cls.dev = _render(dev=True, activate=False)
        cls.activated = _render(dev=True, activate=True)
        cls.recurring_alerts = _render_recurring_alerts()

    def test_runtime_and_billing_worker_are_default_off_everywhere(self) -> None:
        for environment, documents in (("prod", self.default), ("dev", self.dev)):
            with self.subTest(environment=environment):
                names = {
                    document.get("metadata", {}).get("name")
                    for document in documents
                }
                self.assertNotIn("citrus-billing-worker", names)
                self.assertNotIn("citrus-recurring-tick", names)
                self.assertNotIn("citrus-recurring-health", names)
                self.assertNotIn("citrus-dev-billing-worker", names)
                self.assertNotIn("citrus-dev-recurring-tick", names)
                self.assertNotIn("citrus-dev-recurring-health", names)

    def test_config_map_keeps_every_activation_gate_fail_closed(self) -> None:
        config = _named(self.dev, "ConfigMap", "django-config")["data"]
        self.assertEqual(config["RECURRING_ORDER_ENROLLMENT_MODE"], "off")
        self.assertEqual(config["RECURRING_ORDER_COHORT_MODE"], "off")
        self.assertEqual(config["RECURRING_REMINDERS_ENABLED"], "False")
        self.assertEqual(config["RECURRING_CHARGING_ENABLED"], "False")
        self.assertEqual(config["RECURRING_CHARGE_EMERGENCY_STOP"], "True")
        self.assertEqual(config["RECURRING_BILLING_QUEUE"], "billing")

    def test_activated_worker_is_single_concurrency_and_not_media(self) -> None:
        deployment = _named(
            self.activated,
            "Deployment",
            "citrus-dev-billing-worker",
        )
        self.assertEqual(deployment["spec"]["replicas"], 1)
        pod = deployment["spec"]["template"]["spec"]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        container = pod["containers"][0]
        command = container["args"][0]
        self.assertIn("--queues=billing", command)
        self.assertIn("--concurrency=1", command)
        self.assertNotIn("--queues=media", command)
        self.assertFalse(
            container["securityContext"]["allowPrivilegeEscalation"]
        )
        self.assertEqual(len(container["envFrom"]), 5)
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(annotations["prometheus.io/scrape"], "true")
        self.assertEqual(annotations["prometheus.io/path"], "/metrics")
        metrics = next(
            item for item in pod["containers"] if item["name"] == "recurring-metrics"
        )
        self.assertIn("serve_recurring_metrics", metrics["command"])

    def test_runtime_requires_worker_and_queue_has_one_source_of_truth(self) -> None:
        command = [
            "helm",
            "template",
            "citrus-dev",
            str(CHART_PATH),
            "--namespace",
            "citrus-dev",
            "-f",
            str(DEV_VALUES),
            "--set",
            "recurringRuntime.enabled=true",
            "--set",
            "billingWorker.enabled=false",
        ]
        failed = subprocess.run(
            command,
            check=False,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("requires billingWorker.enabled", failed.stderr)

        metrics_disabled = subprocess.run(
            [
                "helm",
                "template",
                "citrus-dev",
                str(CHART_PATH),
                "--namespace",
                "citrus-dev",
                "-f",
                str(DEV_VALUES),
                "--set",
                "recurringRuntime.enabled=true",
                "--set",
                "billingWorker.enabled=true",
                "--set",
                "billingWorker.metrics.enabled=false",
            ],
            check=False,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(metrics_disabled.returncode, 0)
        self.assertIn(
            "requires billingWorker.metrics.enabled",
            metrics_disabled.stderr,
        )

        documents = _render(
            dev=True,
            activate=True,
            queue="billing-priority",
        )
        config = _named(documents, "ConfigMap", "django-config")["data"]
        deployment = _named(
            documents,
            "Deployment",
            "citrus-dev-billing-worker",
        )
        worker = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            config["RECURRING_BILLING_QUEUE"],
            "billing-priority",
        )
        self.assertIn("--queues=billing-priority", worker["args"][0])

    def test_activated_cronjobs_are_utc_bounded_and_non_root(self) -> None:
        for suffix in ("tick", "health"):
            cronjob = _named(
                self.activated,
                "CronJob",
                f"citrus-dev-recurring-{suffix}",
            )
            spec = cronjob["spec"]
            self.assertEqual(spec["timeZone"], "Etc/UTC")
            self.assertEqual(spec["concurrencyPolicy"], "Forbid")
            self.assertEqual(spec["startingDeadlineSeconds"], 240)
            self.assertEqual(spec["successfulJobsHistoryLimit"], 2)
            self.assertEqual(spec["failedJobsHistoryLimit"], 3)
            job = spec["jobTemplate"]["spec"]
            self.assertEqual(job["activeDeadlineSeconds"], 240)
            self.assertEqual(job["backoffLimit"], 1)
            pod = job["template"]["spec"]
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            self.assertEqual(len(pod["containers"][0]["envFrom"]), 5)

    def test_tick_uses_persisted_due_time_command_on_five_minute_wakeup(self) -> None:
        cronjob = _named(
            self.activated,
            "CronJob",
            "citrus-dev-recurring-tick",
        )
        self.assertEqual(cronjob["spec"]["schedule"], "*/5 * * * *")
        command = (
            cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            ["containers"][0]["command"]
        )
        self.assertEqual(command[:3], ["python", "manage.py", "tick_recurring_orders"])

    def test_rendered_recurring_alert_contract_is_stable_and_exact(self) -> None:
        expected_for = {
            "CitrusRecurringRuntimeHealthFailed": "2m",
            "CitrusRecurringRuntimeTickFailed": "2m",
            "CitrusRecurringRuntimeExporterUnhealthy": "2m",
            "CitrusRecurringRuntimeMetricsMissing": "2m",
            "CitrusRecurringRuntimeWorkOverdue": "5m",
            "CitrusRecurringRuntimeOutboxStale": "5m",
            "CitrusRecurringBillingQueueUnavailable": "5m",
            "CitrusRecurringNoticeFailures": "2m",
            "CitrusRecurringConfirmationNeedsReview": "2m",
            "CitrusRecurringPaymentReconciliationLag": "5m",
            "CitrusRecurringChargeOrOrderStalled": "5m",
        }
        self.assertEqual(set(self.recurring_alerts), set(expected_for))
        for name, duration in expected_for.items():
            with self.subTest(alert=name):
                alert = self.recurring_alerts[name]
                self.assertEqual(alert["for"], duration)
                self.assertEqual(alert["labels"]["severity"], "critical")
                self.assertEqual(alert["labels"]["service"], "citrus")
                self.assertNotIn(
                    'namespace=~"citrus(-dev)?"',
                    alert["expr"],
                )

    def test_rendered_job_and_scrape_alerts_match_both_real_environments(self) -> None:
        for suffix, alert_name in (
            ("health", "CitrusRecurringRuntimeHealthFailed"),
            ("tick", "CitrusRecurringRuntimeTickFailed"),
        ):
            with self.subTest(alert=alert_name):
                expression = self.recurring_alerts[alert_name]["expr"]
                self.assertEqual(
                    expression.count('namespace=~"default|citrus-dev"'),
                    3,
                )
                self.assertEqual(expression.count('job="kube-state-metrics"'), 3)
                self.assertIn('owner_kind="CronJob"', expression)
                self.assertIn(
                    f'owner_name=~"citrus(-dev)?-recurring-{suffix}"',
                    expression,
                )

        missing = self.recurring_alerts[
            "CitrusRecurringRuntimeMetricsMissing"
        ]["expr"]
        self.assertEqual(missing.count('namespace=~"default|citrus-dev"'), 2)
        self.assertIn('job="kube-state-metrics"', missing)
        self.assertIn('deployment=~"citrus(-dev)?-billing-worker"', missing)
        self.assertIn('job="kubernetes-pods"', missing)
        self.assertIn('pod=~"citrus(-dev)?-billing-worker-.*"', missing)

    def test_rendered_runtime_metrics_require_the_citrus_billing_pod(self) -> None:
        expected_metrics = {
            "CitrusRecurringRuntimeExporterUnhealthy": (
                "citrus_recurring_runtime_health",
            ),
            "CitrusRecurringRuntimeWorkOverdue": (
                "citrus_recurring_overdue_due_work",
            ),
            "CitrusRecurringRuntimeOutboxStale": (
                "citrus_recurring_oldest_outbox_age_seconds",
                "citrus_recurring_stale_claims",
            ),
            "CitrusRecurringBillingQueueUnavailable": (
                "citrus_recurring_billing_queue_unavailable",
                "citrus_recurring_billing_queue_depth",
            ),
            "CitrusRecurringNoticeFailures": (
                "citrus_recurring_recent_notice_failures",
            ),
            "CitrusRecurringConfirmationNeedsReview": (
                "citrus_recurring_confirmation_delivery_review_required",
            ),
            "CitrusRecurringPaymentReconciliationLag": (
                "citrus_recurring_webhook_lagged_payments",
            ),
            "CitrusRecurringChargeOrOrderStalled": (
                "citrus_recurring_unclaimed_charge_due",
                "citrus_recurring_stale_paid_cycles_missing_orders",
            ),
        }
        for alert_name, metrics in expected_metrics.items():
            expression = self.recurring_alerts[alert_name]["expr"]
            for metric in metrics:
                with self.subTest(alert=alert_name, metric=metric):
                    selectors = re.findall(
                        rf"{re.escape(metric)}\{{([^}}]+)\}}",
                        expression,
                    )
                    self.assertEqual(len(selectors), 1)
                    selector = " ".join(selectors[0].split())
                    self.assertIn('job="kubernetes-pods"', selector)
                    self.assertIn(
                        'namespace=~"default|citrus-dev"',
                        selector,
                    )
                    self.assertIn(
                        'pod=~"citrus(-dev)?-billing-worker-.*"',
                        selector,
                    )


if __name__ == "__main__":
    unittest.main()
