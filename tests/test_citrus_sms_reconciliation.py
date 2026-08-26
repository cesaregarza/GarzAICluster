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
PROD_IMAGE_TAG = "3f68967f777b2665fccb4f0ab423f339b8ea1357"
VERIFIED_IMAGE_TAG = "0e2258bf95c6170895c26780258eb42d5b5c557c"
YAML_PARSER = YAML(typ="safe")


def _helm_command(
    *,
    dev: bool,
    enabled: bool,
    image_tag: str | None = None,
    verified_image_tag: str | None = None,
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


class CitrusSmsReconciliationChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prod_disabled = _render(dev=False, enabled=False)
        cls.dev_disabled = _render(dev=True, enabled=False)
        cls.prod_enabled_synthetic = _render(
            dev=False,
            enabled=True,
            image_tag=VERIFIED_IMAGE_TAG,
            verified_image_tag=VERIFIED_IMAGE_TAG,
        )
        cls.dev_enabled_synthetic = _render(dev=True, enabled=True)

    def test_values_make_both_real_environments_disabled_and_suspended(self) -> None:
        prod = YAML_PARSER.load(VALUES_PATH.read_text(encoding="utf-8"))
        dev = YAML_PARSER.load(DEV_VALUES_PATH.read_text(encoding="utf-8"))

        self.assertFalse(prod["smsReconciliation"]["enabled"])
        self.assertTrue(prod["smsReconciliation"]["suspend"])
        self.assertEqual(prod["smsReconciliation"]["verifiedImageTag"], "")
        self.assertFalse(dev["smsReconciliation"]["enabled"])
        self.assertTrue(dev["smsReconciliation"]["suspend"])
        self.assertEqual(
            dev["smsReconciliation"]["verifiedImageTag"],
            VERIFIED_IMAGE_TAG,
        )

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
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("must exactly match image.tag", failed.stderr)

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

    def test_job_is_bounded_non_root_and_uses_the_verified_immutable_image(self) -> None:
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
                + VERIFIED_IMAGE_TAG,
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

    def test_command_is_bounded_and_provider_network_is_hard_disabled(self) -> None:
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
        self.assertEqual(
            container["env"],
            [
                {"name": "TWILIO_SMS_ENABLED", "value": "False"},
                {"name": "TWILIO_SMS_RECIPIENT_ALLOWLIST", "value": ""},
            ],
        )

        env_from = container["envFrom"]
        self.assertEqual(len(env_from), 5)
        self.assertEqual(env_from[0], {"configMapRef": {"name": "django-config"}})
        self.assertEqual(
            {
                item["secretRef"]["name"]
                for item in env_from[1:]
            },
            {
                "django-secrets",
                "django-email-secrets",
                "django-spaces-secrets",
                "citrus-dev-app-key",
            },
        )

    def test_values_schema_requires_the_fail_closed_scheduler_contract(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        sms = schema["properties"]["smsReconciliation"]
        self.assertFalse(sms["additionalProperties"])
        self.assertEqual(
            sms["properties"]["concurrencyPolicy"]["enum"],
            ["Forbid"],
        )
        self.assertGreaterEqual(
            sms["properties"]["failedJobsHistoryLimit"]["minimum"],
            1,
        )
        self.assertEqual(
            sms["properties"]["commandCompatibleImageTags"]["items"][
                "pattern"
            ],
            "^[0-9a-f]{40}$",
        )
        for gate in (
            "enabled",
            "suspend",
            "verifiedImageTag",
            "commandCompatibleImageTags",
        ):
            with self.subTest(gate=gate):
                self.assertIn(gate, sms["required"])


if __name__ == "__main__":
    unittest.main()
