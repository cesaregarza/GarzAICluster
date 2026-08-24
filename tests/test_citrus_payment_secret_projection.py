from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
DEV_VALUES = CHART_PATH / "values-dev.yaml"
PROD_PAYMENT_VALUES = CHART_PATH / "values-payment-prod.yaml"
DEV_PAYMENT_VALUES = CHART_PATH / "values-payment-dev.yaml"
RUNBOOK_PATH = (
    REPO_ROOT / "docs" / "runbooks" / "citrus-payment-secret-isolation.md"
)
YAML_PARSER = YAML(typ="safe")


def _render(
    *,
    dev: bool,
    payment: bool,
    activate_background_consumers: bool = False,
) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    release = "citrus-dev" if dev else "citrus"
    command = [
        "helm",
        "template",
        release,
        str(CHART_PATH),
        "--namespace",
        "citrus-dev" if dev else "default",
    ]
    if dev:
        command.extend(["-f", str(DEV_VALUES)])
    if payment:
        command.extend([
            "-f",
            str(DEV_PAYMENT_VALUES if dev else PROD_PAYMENT_VALUES),
        ])
    if activate_background_consumers:
        command.extend([
            "--set",
            "billingWorker.enabled=true",
            "--set",
            "recurringRuntime.enabled=true",
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


def _named(
    documents: list[dict[str, Any]],
    kind: str,
    name: str,
) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def _pod_spec(document: dict[str, Any]) -> dict[str, Any]:
    kind = document["kind"]
    if kind == "Deployment":
        return document["spec"]["template"]["spec"]
    if kind == "Job":
        return document["spec"]["template"]["spec"]
    if kind == "CronJob":
        return document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    raise AssertionError(f"unsupported workload kind: {kind}")


def _pod_annotations(document: dict[str, Any]) -> dict[str, Any]:
    if document["kind"] == "CronJob":
        return (
            document["spec"]["jobTemplate"]["spec"]["template"]
            .get("metadata", {})
            .get("annotations", {})
        )
    return document["spec"]["template"].get("metadata", {}).get(
        "annotations", {}
    )


def _container(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(
        container
        for container in _pod_spec(document)["containers"]
        if container["name"] == name
    )


def _payment_refs(container: dict[str, Any], secret_name: str) -> dict[str, str]:
    references: dict[str, str] = {}
    for item in container.get("env", []):
        secret_ref = item.get("valueFrom", {}).get("secretKeyRef", {})
        if secret_ref.get("name") != secret_name:
            continue
        if secret_ref.get("optional") is not False:
            raise AssertionError(f"{item['name']} must be a non-optional reference")
        references[item["name"]] = secret_ref["key"]
    return references


def _all_payment_refs(
    documents: list[dict[str, Any]],
    secret_name: str,
) -> list[tuple[str, str, str]]:
    references: list[tuple[str, str, str]] = []
    for document in documents:
        if document.get("kind") not in {"Deployment", "Job", "CronJob"}:
            continue
        workload_name = document["metadata"]["name"]
        for container in _pod_spec(document).get("containers", []):
            for setting in _payment_refs(container, secret_name):
                references.append((workload_name, container["name"], setting))
            for source in container.get("envFrom", []):
                if source.get("secretRef", {}).get("name") == secret_name:
                    raise AssertionError(
                        f"{workload_name}/{container['name']} imports the payment "
                        "Secret with envFrom"
                    )
    return references


class CitrusPaymentSecretProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.default_prod = _render(dev=False, payment=False)
        cls.default_dev = _render(dev=True, payment=False)
        cls.prepared_prod = _render(
            dev=False,
            payment=True,
            activate_background_consumers=True,
        )
        cls.prepared_dev = _render(
            dev=True,
            payment=True,
            activate_background_consumers=True,
        )

    def test_current_argo_renders_remain_inert(self) -> None:
        for documents in (self.default_prod, self.default_dev):
            serialized = "\n".join(
                str(document) for document in documents
            )
            self.assertNotIn("payment-rollout-revision", serialized)
            self.assertNotIn("citrus-prod-payment-credentials", serialized)
            self.assertNotIn("citrus-dev-payment-credentials", serialized)

        prod_application = (
            REPO_ROOT / "argocd" / "applications" / "citrus.yaml"
        ).read_text(encoding="utf-8")
        dev_application = (
            REPO_ROOT / "argocd" / "applications" / "citrus-dev.yaml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("values-payment-prod.yaml", prod_application)
        self.assertNotIn("values-payment-dev.yaml", dev_application)

    def test_production_projects_exact_keys_only_to_payment_consumers(self) -> None:
        secret_name = "citrus-prod-payment-credentials"
        expected = {
            ("citrus", "django", "STRIPE_SECRET_KEY"),
            ("citrus", "django", "STRIPE_PUBLISHABLE_KEY"),
            ("citrus", "django", "STRIPE_WEBHOOK_SECRET_PROD"),
            ("citrus-billing-worker", "billing-worker", "STRIPE_SECRET_KEY"),
            ("citrus-recurring-tick", "recurring-tick", "STRIPE_SECRET_KEY"),
            ("citrus-recurring-health", "recurring-health", "STRIPE_SECRET_KEY"),
        }
        self.assertEqual(
            set(_all_payment_refs(self.prepared_prod, secret_name)),
            expected,
        )

        web = _named(self.prepared_prod, "Deployment", "citrus")
        self.assertEqual(
            _payment_refs(_container(web, "django"), secret_name),
            {
                "STRIPE_SECRET_KEY": "STRIPE_SECRET_KEY",
                "STRIPE_PUBLISHABLE_KEY": "STRIPE_PUBLISHABLE_KEY",
                "STRIPE_WEBHOOK_SECRET_PROD": "STRIPE_WEBHOOK_SECRET",
            },
        )
        self.assertEqual(
            _pod_annotations(web)["citrus.grace/payment-rollout-revision"],
            "ces-844-prepared",
        )

        billing = _named(
            self.prepared_prod,
            "Deployment",
            "citrus-billing-worker",
        )
        self.assertEqual(
            _payment_refs(_container(billing, "billing-worker"), secret_name),
            {"STRIPE_SECRET_KEY": "STRIPE_SECRET_KEY"},
        )
        self.assertEqual(
            _payment_refs(_container(billing, "recurring-metrics"), secret_name),
            {},
        )

    def test_dev_uses_a_distinct_source_and_dev_webhook_setting(self) -> None:
        secret_name = "citrus-dev-payment-credentials"
        web = _named(self.prepared_dev, "Deployment", "citrus-dev")
        references = _payment_refs(_container(web, "django"), secret_name)
        self.assertEqual(
            references,
            {
                "STRIPE_SECRET_KEY": "STRIPE_SECRET_KEY",
                "STRIPE_PUBLISHABLE_KEY": "STRIPE_PUBLISHABLE_KEY",
                "STRIPE_WEBHOOK_SECRET_DEV": "STRIPE_WEBHOOK_SECRET",
            },
        )
        self.assertNotIn("STRIPE_WEBHOOK_SECRET_PROD", references)
        self.assertNotIn("STRIPE_WEBHOOK_SECRET", references)
        self.assertTrue(_all_payment_refs(self.prepared_dev, secret_name))
        self.assertFalse(
            _all_payment_refs(
                self.prepared_dev,
                "citrus-prod-payment-credentials",
            )
        )

    def test_enabled_projection_fails_closed_on_invalid_contract(self) -> None:
        cases = (
            (
                ["--set", "paymentCredentials.enabled=true"],
                "paymentCredentials.secretName is required",
            ),
            (
                [
                    "-f",
                    str(PROD_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.webhookEnvironmentVariable=STRIPE_WEBHOOK_SECRET",
                ],
                "must select the dev or production environment-specific webhook setting",
            ),
            (
                [
                    "-f",
                    str(PROD_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.rolloutRevision=not safe",
                ],
                "must be a semantic revision",
            ),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                result = subprocess.run(
                    ["helm", "template", "citrus", str(CHART_PATH), *arguments],
                    check=False,
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_runbook_rejects_shared_repo_server_as_isolation(self) -> None:
        runbook = " ".join(
            RUNBOOK_PATH.read_text(encoding="utf-8").split()
        )
        self.assertIn(
            "Adding two KSOPS sidecars to that same pod would not satisfy isolation",
            runbook,
        )
        self.assertIn(
            "Do not add recipients or CMP manifests until that decision is approved",
            runbook,
        )


if __name__ == "__main__":
    unittest.main()
