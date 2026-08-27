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
PROD_PAYMENT_VALUES = CHART_PATH / "values-payment-prod.yaml"
DEV_PAYMENT_VALUES = CHART_PATH / "values-payment-dev.yaml"
RUNBOOK_PATH = (
    REPO_ROOT / "docs" / "runbooks" / "citrus-payment-secret-isolation.md"
)
DEV_PAYMENT_SECRET_PATH = (
    REPO_ROOT
    / "secrets"
    / "citrus-dev"
    / "citrus-dev-payment-credentials.enc.yaml"
)
DEV_KSOPS_PATH = REPO_ROOT / "secrets" / "citrus-dev" / "ksops.yaml"
YAML_PARSER = YAML(typ="safe")
HELM_JSON_POINTER = re.compile(
    r"(?P<quote>['\"])/(?P<path>[A-Za-z0-9_./-]+)(?P=quote)"
)


def _normalize_helm_error(stderr: str) -> str:
    """Normalize version-dependent quoted JSON Pointer schema paths."""

    return HELM_JSON_POINTER.sub(
        lambda match: (
            f"{match.group('quote')}"
            f"{match.group('path').replace('/', '.')}"
            f"{match.group('quote')}"
        ),
        stderr,
    )


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


def _plain_env(container: dict[str, Any], name: str) -> str | None:
    matches = [
        item
        for item in container.get("env", [])
        if item.get("name") == name
    ]
    if len(matches) > 1:
        raise AssertionError(f"{name} is projected more than once")
    if not matches:
        return None
    if "valueFrom" in matches[0]:
        raise AssertionError(f"{name} must be a plain ownership attestation")
    return matches[0].get("value")


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

    def test_default_chart_renders_remain_inert(self) -> None:
        for documents in (self.default_prod, self.default_dev):
            serialized = "\n".join(
                str(document) for document in documents
            )
            self.assertNotIn("payment-rollout-revision", serialized)
            self.assertNotIn("citrus-prod-payment-credentials", serialized)
            self.assertNotIn("citrus-dev-payment-credentials", serialized)
            self.assertNotIn("STRIPE_WEBHOOK_SECRET_OWNER", serialized)

        prod_application = (
            REPO_ROOT / "argocd" / "applications" / "citrus.yaml"
        ).read_text(encoding="utf-8")
        dev_application = (
            REPO_ROOT / "argocd" / "applications" / "citrus-dev.yaml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("values-payment-prod.yaml", prod_application)
        self.assertIn("values-payment-dev.yaml", dev_application)

    def test_dev_encrypted_source_contains_only_test_api_roles(self) -> None:
        document = YAML_PARSER.load(
            DEV_PAYMENT_SECRET_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(document["kind"], "Secret")
        self.assertEqual(
            document["metadata"]["name"],
            "citrus-dev-payment-credentials",
        )
        self.assertEqual(document["metadata"]["namespace"], "citrus-dev")
        self.assertEqual(
            set(document.get("data", {})),
            {"STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY"},
        )
        self.assertIn("sops", document)
        self.assertIn(
            "citrus-dev-payment-credentials.enc.yaml",
            DEV_KSOPS_PATH.read_text(encoding="utf-8"),
        )

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
            _plain_env(
                _container(web, "django"),
                "STRIPE_WEBHOOK_SECRET_OWNER",
            ),
            "citrus",
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

    def test_dev_projects_one_test_set_to_every_django_runtime(self) -> None:
        secret_name = "citrus-dev-payment-credentials"
        expected_runtimes = {
            ("citrus-dev", "django"),
            ("citrus-dev-migrate-1", "migrate"),
            ("citrus-dev-media-worker", "media-worker"),
            ("citrus-dev-media-requeue", "media-requeue"),
            ("citrus-dev-media-gc", "media-gc"),
            ("citrus-dev-billing-worker", "billing-worker"),
            ("citrus-dev-billing-worker", "recurring-metrics"),
            ("citrus-dev-recurring-tick", "recurring-tick"),
            ("citrus-dev-recurring-health", "recurring-health"),
        }
        observed_runtimes = set()
        for document in self.prepared_dev:
            if document.get("kind") not in {"Deployment", "Job", "CronJob"}:
                continue
            for container in _pod_spec(document).get("containers", []):
                if not any(
                    source.get("configMapRef", {}).get("name")
                    == "django-config"
                    for source in container.get("envFrom", [])
                ):
                    continue
                runtime = (document["metadata"]["name"], container["name"])
                observed_runtimes.add(runtime)
                references = _payment_refs(container, secret_name)
                self.assertEqual(
                    references,
                    {
                        "STRIPE_SECRET_KEY": "STRIPE_SECRET_KEY",
                        "STRIPE_PUBLISHABLE_KEY": "STRIPE_PUBLISHABLE_KEY",
                    },
                    runtime,
                )
                webhook_refs = [
                    item["valueFrom"]["secretKeyRef"]
                    for item in container["env"]
                    if item["name"] == "STRIPE_WEBHOOK_SECRET_DEV"
                ]
                self.assertEqual(
                    webhook_refs,
                    [{
                        "name": "django-secrets",
                        "key": "STRIPE_WEBHOOK_SECRET_DEV",
                        "optional": False,
                    }],
                    runtime,
                )
                self.assertEqual(
                    _plain_env(container, "STRIPE_WEBHOOK_SECRET_OWNER"),
                    "citrus-dev",
                    runtime,
                )
                env_names = {item["name"] for item in container["env"]}
                self.assertNotIn("STRIPE_WEBHOOK_SECRET_PROD", env_names)
                self.assertNotIn("STRIPE_WEBHOOK_SECRET", env_names)

        self.assertEqual(observed_runtimes, expected_runtimes)
        self.assertTrue(_all_payment_refs(self.prepared_dev, secret_name))
        self.assertFalse(
            _all_payment_refs(
                self.prepared_dev,
                "citrus-prod-payment-credentials",
            )
        )

    def test_dev_activation_never_imports_the_legacy_secret_broadly(self) -> None:
        allowed_legacy_keys = {
            "DB_HOST",
            "DB_PORT",
            "DB_NAME",
            "DB_USER",
            "DB_PASSWORD",
        }
        for document in self.prepared_dev:
            if document.get("kind") not in {"Deployment", "Job", "CronJob"}:
                continue
            for container in _pod_spec(document).get("containers", []):
                for source in container.get("envFrom", []):
                    self.assertNotEqual(
                        source.get("secretRef", {}).get("name"),
                        "django-secrets",
                    )
                legacy_keys = {
                    item.get("valueFrom", {})
                    .get("secretKeyRef", {})
                    .get("key")
                    for item in container.get("env", [])
                    if item.get("valueFrom", {})
                    .get("secretKeyRef", {})
                    .get("name") == "django-secrets"
                }
                uses_application_config = any(
                    source.get("configMapRef", {}).get("name")
                    == "django-config"
                    for source in container.get("envFrom", [])
                )
                if not uses_application_config:
                    self.assertFalse(legacy_keys)
                    continue
                expected = set(allowed_legacy_keys)
                expected.add("STRIPE_WEBHOOK_SECRET_DEV")
                self.assertEqual(legacy_keys, expected)

    def test_enabled_projection_fails_closed_on_invalid_contract(self) -> None:
        cases = (
            (
                False,
                ["--set", "paymentCredentials.enabled=true"],
                "paymentCredentials.secretName",
            ),
            (
                False,
                [
                    "-f",
                    str(PROD_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.webhookEnvironmentVariable=STRIPE_WEBHOOK_SECRET",
                ],
                "paymentCredentials.webhookEnvironmentVariable",
            ),
            (
                False,
                [
                    "-f",
                    str(PROD_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.rolloutRevision=not safe",
                ],
                "paymentCredentials.rolloutRevision",
            ),
            (
                False,
                [
                    "-f",
                    str(PROD_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.owner=citrus-dev",
                ],
                "paymentCredentials.owner",
            ),
            (
                False,
                [
                    "-f",
                    str(DEV_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.secretName=citrus-prod-payment-credentials",
                ],
                "paymentCredentials.secretName",
            ),
            (
                False,
                [
                    "-f", str(DEV_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.webhookSecretKey=STRIPE_WEBHOOK_SECRET_PROD",
                ],
                "paymentCredentials.webhookSecretKey",
            ),
            (
                False,
                [
                    "-f", str(PROD_PAYMENT_VALUES),
                    "--set-string",
                    "paymentCredentials.webhookSecretName=django-secrets",
                ],
                "paymentCredentials.webhookSecretName",
            ),
        )
        for development, arguments, message in cases:
            with self.subTest(message=message):
                release_name = "citrus-dev" if development else "citrus"
                namespace = "citrus-dev" if development else "default"
                result = subprocess.run(
                    [
                        "helm",
                        "template",
                        release_name,
                        str(CHART_PATH),
                        "--namespace",
                        namespace,
                        *arguments,
                    ],
                    check=False,
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, _normalize_helm_error(result.stderr))

    def test_helm_schema_paths_are_normalized_across_ci_versions(self) -> None:
        hosted = (
            "- at '/paymentCredentials/secretName': "
            "minLength: got 0, want 1"
        )
        local = "paymentCredentials.secretName: Must have at least 1 characters"
        self.assertIn(
            "paymentCredentials.secretName",
            _normalize_helm_error(hosted),
        )
        self.assertEqual(_normalize_helm_error(local), local)

    def test_runbook_accepts_shared_trusted_gitops_control_plane(self) -> None:
        runbook = " ".join(
            RUNBOOK_PATH.read_text(encoding="utf-8").split()
        )
        self.assertIn(
            "A shared Argo CD instance and shared SOPS recipient are acceptable",
            runbook,
        )
        self.assertIn(
            "No production replacement, rotation, or revocation is required",
            runbook,
        )


if __name__ == "__main__":
    unittest.main()
