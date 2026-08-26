from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
VALUES_PATH = CHART_PATH / "values.yaml"
DEV_VALUES_PATH = CHART_PATH / "values-dev.yaml"
SCHEMA_PATH = CHART_PATH / "values.schema.json"
TEMPLATE_PATH = CHART_PATH / "templates" / "sms-reconciliation-cronjob.yaml"
PROD_IMAGE_TAG = "3f68967f777b2665fccb4f0ab423f339b8ea1357"
COMMAND_BEARING_IMAGE_TAG = "0e2258bf95c6170895c26780258eb42d5b5c557c"
SYNTHETIC_SECRET_NAME = "citrus-ci-sms-reconciliation-runtime"
RUNTIME_SECRET_KEYS = {
    "DJANGO_SECRET_KEY",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
}
YAML_PARSER = YAML(typ="safe")


def _helm_command(
    *,
    dev: bool,
    enabled: bool,
    image_tag: str | None = None,
    verified_image_tag: str | None = None,
    compatible_image_tag: str | None = None,
    secret_name: str | None = None,
    stale_minutes: int | None = None,
    limit: int | None = None,
) -> list[str]:
    release = "citrus-dev" if dev else "citrus"
    command = [
        "helm",
        "template",
        release,
        str(CHART_PATH),
        "--namespace",
        release if dev else "default",
        "-f",
        str(VALUES_PATH),
    ]
    if dev:
        command.extend(["-f", str(DEV_VALUES_PATH)])
    if image_tag is not None:
        command.extend(["--set-string", f"image.tag={image_tag}"])
    if enabled:
        command.extend(["--set", "smsReconciliation.enabled=true"])
    if verified_image_tag is not None:
        command.extend([
            "--set-string",
            f"smsReconciliation.verifiedImageTag={verified_image_tag}",
        ])
    if compatible_image_tag is not None:
        command.extend([
            "--set-string",
            "smsReconciliation.commandCompatibleImageTags[0]="
            f"{compatible_image_tag}",
        ])
    if secret_name is not None:
        command.extend([
            "--set-string",
            f"smsReconciliation.secretName={secret_name}",
        ])
    if stale_minutes is not None:
        command.extend([
            "--set",
            f"smsReconciliation.staleMinutes={stale_minutes}",
        ])
    if limit is not None:
        command.extend(["--set", f"smsReconciliation.limit={limit}"])
    return command


def _run_helm(**kwargs) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    return subprocess.run(
        _helm_command(**kwargs),
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _render(**kwargs) -> list[dict[str, Any]]:
    result = _run_helm(**kwargs)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _sms_cronjob(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/component"
        ) == "sms-reconciliation"
    )


def _synthetic_render_kwargs(*, dev: bool) -> dict[str, Any]:
    return {
        "dev": dev,
        "enabled": True,
        "image_tag": COMMAND_BEARING_IMAGE_TAG,
        "verified_image_tag": COMMAND_BEARING_IMAGE_TAG,
        "compatible_image_tag": COMMAND_BEARING_IMAGE_TAG,
        "secret_name": SYNTHETIC_SECRET_NAME,
    }


class CitrusSmsReconciliationChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prod_disabled = _render(dev=False, enabled=False)
        cls.dev_disabled = _render(dev=True, enabled=False)
        cls.prod_enabled_synthetic = _render(
            **_synthetic_render_kwargs(dev=False)
        )
        cls.dev_enabled_synthetic = _render(
            **_synthetic_render_kwargs(dev=True)
        )

    def test_values_leave_every_real_activation_input_empty(self) -> None:
        prod = YAML_PARSER.load(VALUES_PATH.read_text(encoding="utf-8"))
        dev = YAML_PARSER.load(DEV_VALUES_PATH.read_text(encoding="utf-8"))

        for environment, values in (("prod", prod), ("dev", dev)):
            sms = values["smsReconciliation"]
            with self.subTest(environment=environment):
                self.assertFalse(sms["enabled"])
                self.assertTrue(sms["suspend"])
                self.assertEqual(sms["secretName"], "")
                self.assertEqual(sms["verifiedImageTag"], "")
                self.assertEqual(sms["commandCompatibleImageTags"], [])

        self.assertNotIn("command", prod["smsReconciliation"])
        self.assertEqual(prod["smsReconciliation"]["staleMinutes"], 15)
        self.assertEqual(prod["smsReconciliation"]["limit"], 100)

    def test_disabled_renders_omit_scheduler_in_prod_and_dev(self) -> None:
        for environment, documents in (
            ("prod", self.prod_disabled),
            ("dev", self.dev_disabled),
        ):
            with self.subTest(environment=environment):
                components = {
                    document.get("metadata", {}).get("labels", {}).get(
                        "app.kubernetes.io/component"
                    )
                    for document in documents
                }
                self.assertNotIn("sms-reconciliation", components)

    def test_current_production_image_cannot_render_an_enabled_scheduler(self) -> None:
        failed = _run_helm(dev=False, enabled=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(
            "requires smsReconciliation.verifiedImageTag",
            failed.stderr,
        )
        self.assertIn(PROD_IMAGE_TAG, VALUES_PATH.read_text(encoding="utf-8"))

        forged_receipt = _run_helm(
            dev=False,
            enabled=True,
            verified_image_tag=PROD_IMAGE_TAG,
            secret_name=SYNTHETIC_SECRET_NAME,
        )
        self.assertNotEqual(forged_receipt.returncode, 0)
        self.assertIn(
            "is not listed in smsReconciliation.commandCompatibleImageTags",
            forged_receipt.stderr,
        )

    def test_image_receipt_must_exactly_match_the_rendered_image(self) -> None:
        failed = _run_helm(
            dev=True,
            enabled=True,
            image_tag="ffffffffffffffffffffffffffffffffffffffff",
            verified_image_tag=COMMAND_BEARING_IMAGE_TAG,
            compatible_image_tag=COMMAND_BEARING_IMAGE_TAG,
            secret_name=SYNTHETIC_SECRET_NAME,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("must exactly match image.tag", failed.stderr)

    def test_enabled_render_requires_a_dedicated_runtime_secret(self) -> None:
        common = _synthetic_render_kwargs(dev=True)
        common["secret_name"] = None
        missing = _run_helm(**common)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(
            "requires a dedicated smsReconciliation.secretName",
            missing.stderr,
        )

        for forbidden in (
            "django-secrets",
            "django-email-secrets",
            "django-spaces-secrets",
            "citrus-dev-app-key",
            "citrus-prod-payment-credentials",
            "citrus-stripe-runtime",
            "citrus-twilio-runtime",
        ):
            with self.subTest(secret_name=forbidden):
                common["secret_name"] = forbidden
                rejected = _run_helm(**common)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("smsReconciliation.secretName", rejected.stderr)

    def test_enabled_prod_and_dev_synthetic_renders_stay_suspended(self) -> None:
        for environment, documents, expected_name in (
            ("prod", self.prod_enabled_synthetic, "citrus-sms-reconciliation"),
            (
                "dev",
                self.dev_enabled_synthetic,
                "citrus-dev-sms-reconciliation",
            ),
        ):
            with self.subTest(environment=environment):
                cronjob = _sms_cronjob(documents)
                self.assertEqual(cronjob["metadata"]["name"], expected_name)
                self.assertEqual(
                    cronjob["metadata"]["annotations"][
                        "argocd.argoproj.io/sync-wave"
                    ],
                    "3",
                )
                spec = cronjob["spec"]
                self.assertTrue(spec["suspend"])
                self.assertEqual(spec["schedule"], "*/15 * * * *")
                self.assertEqual(spec["timeZone"], "Etc/UTC")
                self.assertEqual(spec["concurrencyPolicy"], "Forbid")
                self.assertEqual(spec["startingDeadlineSeconds"], 240)
                self.assertEqual(spec["successfulJobsHistoryLimit"], 2)
                self.assertEqual(spec["failedJobsHistoryLimit"], 3)
                self.assertEqual(
                    spec["jobTemplate"]["metadata"]["labels"][
                        "app.kubernetes.io/component"
                    ],
                    "sms-reconciliation",
                )

    def test_job_is_bounded_non_root_and_uses_an_immutable_image(self) -> None:
        for documents in (
            self.prod_enabled_synthetic,
            self.dev_enabled_synthetic,
        ):
            cronjob = _sms_cronjob(documents)
            job = cronjob["spec"]["jobTemplate"]["spec"]
            self.assertEqual(job["activeDeadlineSeconds"], 240)
            self.assertEqual(job["backoffLimit"], 1)
            pod = job["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["restartPolicy"], "Never")
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            container = pod["containers"][0]
            self.assertEqual(
                container["image"],
                "registry.digitalocean.com/sendouq/citrus:"
                + COMMAND_BEARING_IMAGE_TAG,
            )
            self.assertFalse(
                container["securityContext"]["allowPrivilegeEscalation"]
            )
            self.assertEqual(
                container["securityContext"]["capabilities"]["drop"],
                ["ALL"],
            )
            self.assertEqual(
                container["resources"],
                {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "250m", "memory": "256Mi"},
                },
            )

    def test_command_argv_is_hardcoded_except_for_bounded_numbers(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(
            template,
            r"\.Values\.smsReconciliation\.command(?!CompatibleImageTags)",
        )
        self.assertIn("- sweep_manual_order_sms_attempts", template)

        container = (
            _sms_cronjob(self.dev_enabled_synthetic)["spec"]["jobTemplate"]
            ["spec"]["template"]["spec"]["containers"][0]
        )
        self.assertEqual(
            container["command"],
            [
                "python",
                "manage.py",
                "sweep_manual_order_sms_attempts",
                "--stale-minutes",
                "15",
                "--limit",
                "100",
            ],
        )

        tuned = _render(
            **_synthetic_render_kwargs(dev=True),
            stale_minutes=30,
            limit=25,
        )
        tuned_command = (
            _sms_cronjob(tuned)["spec"]["jobTemplate"]["spec"]["template"]
            ["spec"]["containers"][0]["command"]
        )
        self.assertEqual(tuned_command[:4], container["command"][:4])
        self.assertEqual(tuned_command[4:], ["30", "--limit", "25"])

    def test_numeric_knobs_reject_out_of_bounds_values(self) -> None:
        for field, value in (
            ("stale_minutes", 4),
            ("stale_minutes", 1441),
            ("limit", 0),
            ("limit", 101),
        ):
            with self.subTest(field=field, value=value):
                kwargs = _synthetic_render_kwargs(dev=True)
                kwargs[field] = value
                failed = _run_helm(**kwargs)
                self.assertNotEqual(failed.returncode, 0)
                schema_field = (
                    "staleMinutes" if field == "stale_minutes" else "limit"
                )
                self.assertIn(schema_field, failed.stderr)

    def test_manifest_projects_only_named_db_and_app_secret_keys(self) -> None:
        container = (
            _sms_cronjob(self.dev_enabled_synthetic)["spec"]["jobTemplate"]
            ["spec"]["template"]["spec"]["containers"][0]
        )
        env = {entry["name"]: entry for entry in container["env"]}
        self.assertEqual(env["TWILIO_SMS_ENABLED"]["value"], "False")
        self.assertEqual(env["TWILIO_SMS_RECIPIENT_ALLOWLIST"]["value"], "")

        self.assertEqual(set(env) - {
            "TWILIO_SMS_ENABLED",
            "TWILIO_SMS_RECIPIENT_ALLOWLIST",
        }, RUNTIME_SECRET_KEYS)
        for key in RUNTIME_SECRET_KEYS:
            with self.subTest(key=key):
                reference = env[key]["valueFrom"]["secretKeyRef"]
                self.assertEqual(reference["name"], SYNTHETIC_SECRET_NAME)
                self.assertEqual(reference["key"], key)
                self.assertFalse(reference["optional"])

        self.assertEqual(
            container["envFrom"],
            [{"configMapRef": {"name": "django-config"}}],
        )
        serialized = str(container)
        for forbidden in (
            "django-secrets",
            "django-email-secrets",
            "django-spaces-secrets",
            "citrus-dev-app-key",
            "STRIPE_SECRET_KEY",
            "STRIPE_PUBLISHABLE_KEY",
            "TWILIO_AUTH_TOKEN",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_values_schema_requires_the_fail_closed_scheduler_contract(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        sms = schema["properties"]["smsReconciliation"]
        self.assertFalse(sms["additionalProperties"])
        self.assertNotIn("command", sms["properties"])
        self.assertEqual(
            sms["properties"]["concurrencyPolicy"]["enum"],
            ["Forbid"],
        )
        self.assertEqual(
            sms["properties"]["staleMinutes"],
            {"type": "integer", "minimum": 5, "maximum": 1440},
        )
        self.assertEqual(
            sms["properties"]["limit"],
            {"type": "integer", "minimum": 1, "maximum": 100},
        )
        self.assertEqual(
            sms["properties"]["commandCompatibleImageTags"]["items"]
            ["pattern"],
            "^[0-9a-f]{40}$",
        )
        for gate in (
            "enabled",
            "suspend",
            "secretName",
            "verifiedImageTag",
            "commandCompatibleImageTags",
            "staleMinutes",
            "limit",
        ):
            with self.subTest(gate=gate):
                self.assertIn(gate, sms["required"])


if __name__ == "__main__":
    unittest.main()
