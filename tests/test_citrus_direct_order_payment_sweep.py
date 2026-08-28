from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
VALUES_PATH = CHART_PATH / "values.yaml"
DEV_VALUES_PATH = CHART_PATH / "values-dev.yaml"
PROD_PAYMENT_VALUES = CHART_PATH / "values-payment-prod.yaml"
DEV_PAYMENT_VALUES = CHART_PATH / "values-payment-dev.yaml"
SCHEMA_PATH = CHART_PATH / "values.schema.json"
TEMPLATE_PATH = (
    CHART_PATH / "templates" / "direct-order-payment-sweep-cronjob.yaml"
)
POLICY_PATH = CHART_PATH / "templates" / "payment-egress-cilium-policy.yaml"
YAML_PARSER = YAML(typ="safe")

PROD_IMAGE_TAG = "3f68967f777b2665fccb4f0ab423f339b8ea1357"
COMMAND_IMAGE_TAG = "4353f11595094bc4893b5799233cfd56c52aed89"
MISMATCH_IMAGE_TAG = "f" * 40
RUNTIME_SECRET_NAME = "citrus-ci-direct-order-runtime"
RUNTIME_SECRET_KEYS = {
    "DJANGO_SECRET_KEY",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
}
PAYMENT_ATTESTATION = {
    "DJANGO_ENV",
    "CITRUS_ENVIRONMENT_OWNER",
    "PAYMENT_NETWORK_MODE",
    "PAYMENT_EGRESS_POLICY_REQUIRED",
    "PAYMENT_EGRESS_POLICY_PROVIDER",
    "PAYMENT_EGRESS_POLICY_REVISION",
}
EXPECTED_COMMAND = [
    "python",
    "manage.py",
    "sweep_direct_order_payment_attempts",
    "--stale-minutes",
    "15",
    "--limit",
    "100",
]


def _base_command(*, dev: bool, chart_path: Path = CHART_PATH) -> list[str]:
    release = "citrus-dev" if dev else "citrus"
    command = [
        "helm",
        "template",
        release,
        str(chart_path),
        "--namespace",
        "citrus-dev" if dev else "default",
        "-f",
        str(chart_path / "values.yaml"),
    ]
    if dev:
        command.extend(["-f", str(chart_path / "values-dev.yaml")])
    return command


def _payment_safety_args(*, dev: bool) -> list[str]:
    environment = "development" if dev else "production"
    owner = "citrus-dev" if dev else "citrus"
    network_mode = "deny" if dev else "allow"
    arguments = [
        "--set",
        "paymentSafety.enabled=true",
        "--set-string",
        f"paymentSafety.environment={environment}",
        "--set-string",
        f"paymentSafety.owner={owner}",
        "--set-string",
        f"paymentSafety.networkMode={network_mode}",
        "--set",
        "paymentSafety.policy.required=true",
        "--set-string",
        "paymentSafety.policy.provider=cilium",
        "--set-string",
        "paymentSafety.policy.revision=ces-807-test",
        "--set",
        "paymentSafety.networkPolicy.enabled=true",
    ]
    if dev:
        arguments.extend(
            [
                "--set-string",
                "paymentSafety.networkPolicy.database.host=db.dev.example",
            ]
        )
    return arguments


def _materialized_command(
    *,
    dev: bool,
    image_tag: str = COMMAND_IMAGE_TAG,
    verified_image_tag: str | None = COMMAND_IMAGE_TAG,
    runtime_secret_name: str | None = RUNTIME_SECRET_NAME,
    include_payment_credentials: bool = True,
    include_payment_safety: bool = True,
    suspend: bool | None = None,
) -> list[str]:
    command = _base_command(dev=dev)
    if include_payment_credentials:
        command.extend(
            [
                "-f",
                str(DEV_PAYMENT_VALUES if dev else PROD_PAYMENT_VALUES),
            ]
        )
    command.extend(
        [
            "--set-string",
            f"image.tag={image_tag}",
            "--set",
            "directOrderPaymentSweep.enabled=true",
        ]
    )
    if suspend is not None:
        command.extend(
            [
                "--set",
                f"directOrderPaymentSweep.suspend={str(suspend).lower()}",
            ]
        )
    if verified_image_tag is not None:
        command.extend(
            [
                "--set-string",
                "directOrderPaymentSweep.verifiedImageTag="
                f"{verified_image_tag}",
            ]
        )
    if runtime_secret_name is not None:
        command.extend(
            [
                "--set-string",
                "directOrderPaymentSweep.runtimeSecretName="
                f"{runtime_secret_name}",
            ]
        )
    if include_payment_safety:
        command.extend(_payment_safety_args(dev=dev))
    return command


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    return subprocess.run(
        command,
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _documents(command: list[str]) -> list[dict[str, Any]]:
    result = _run(command)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _named(
    documents: list[dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    )


def _sweeps(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        document
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "direct-order-payment-sweep"
    ]


def _container(cronjob: dict[str, Any]) -> dict[str, Any]:
    return cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ][0]


def _batch_policy_components(documents: list[dict[str, Any]], release: str) -> set[str]:
    policy = _named(
        documents,
        "CiliumNetworkPolicy",
        f"{release}-payment-egress-batch",
    )
    selector = next(
        expression
        for expression in policy["spec"]["endpointSelector"][
            "matchExpressions"
        ]
        if expression["key"] == "app.kubernetes.io/component"
    )
    return set(selector["values"])


class CitrusDirectOrderPaymentSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.defaults = {
            "prod": _documents(_base_command(dev=False)),
            "dev": _documents(_base_command(dev=True)),
        }
        cls.materialized = {
            "prod": _documents(_materialized_command(dev=False)),
            "dev": _documents(_materialized_command(dev=True)),
        }

    def test_default_values_are_inert_and_leave_no_activation_receipt(self) -> None:
        values = YAML_PARSER.load(VALUES_PATH.read_text(encoding="utf-8"))
        sweep = values["directOrderPaymentSweep"]
        self.assertFalse(sweep["enabled"])
        self.assertTrue(sweep["suspend"])
        self.assertEqual(sweep["runtimeSecretName"], "")
        self.assertEqual(sweep["verifiedImageTag"], "")
        self.assertEqual(sweep["offSessionMode"], "legacy")
        self.assertNotIn("command", sweep)

        for environment, documents in self.defaults.items():
            with self.subTest(environment=environment):
                self.assertEqual(_sweeps(documents), [])
                config = _named(documents, "ConfigMap", "django-config")
                self.assertNotIn(
                    "DIRECT_ORDER_OFF_SESSION_MODE",
                    config["data"],
                )

    def test_disabled_prod_and_dev_renders_are_byte_identical_without_slice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ces807-baseline-") as temp_dir:
            baseline = Path(temp_dir) / "citrus"
            shutil.copytree(CHART_PATH, baseline)
            (
                baseline
                / "templates"
                / "direct-order-payment-sweep-cronjob.yaml"
            ).unlink()

            values_path = baseline / "values.yaml"
            values_text = values_path.read_text(encoding="utf-8")
            start = values_text.index("\ndirectOrderPaymentSweep:\n")
            end = values_text.index("\nterminationGracePeriodSeconds:", start)
            values_path.write_text(
                values_text[:start] + values_text[end:],
                encoding="utf-8",
            )

            schema_path = baseline / "values.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["properties"].pop("directOrderPaymentSweep")
            schema["required"].remove("directOrderPaymentSweep")
            schema_path.write_text(
                json.dumps(schema, indent=2) + "\n",
                encoding="utf-8",
            )

            for dev in (False, True):
                with self.subTest(dev=dev):
                    current = _run(_base_command(dev=dev))
                    stripped = _run(_base_command(dev=dev, chart_path=baseline))
                    self.assertEqual(current.returncode, 0, current.stderr)
                    self.assertEqual(stripped.returncode, 0, stripped.stderr)
                    self.assertEqual(current.stdout, stripped.stdout)

    def test_schema_is_closed_typed_and_bounds_every_scheduler_knob(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertIn("directOrderPaymentSweep", schema["required"])
        sweep = schema["properties"]["directOrderPaymentSweep"]
        self.assertFalse(sweep["additionalProperties"])
        self.assertNotIn("command", sweep["properties"])
        self.assertEqual(sweep["properties"]["enabled"], {"type": "boolean"})
        self.assertEqual(sweep["properties"]["suspend"], {"type": "boolean"})
        self.assertEqual(
            sweep["properties"]["verifiedImageTag"]["pattern"],
            "^$|^[0-9a-f]{40}$",
        )
        for field, value in (
            ("startingDeadlineSeconds", 840),
            ("activeDeadlineSeconds", 600),
            ("backoffLimit", 1),
            ("successfulJobsHistoryLimit", 2),
            ("failedJobsHistoryLimit", 3),
            ("staleMinutes", 15),
            ("limit", 100),
        ):
            with self.subTest(field=field):
                constraint = sweep["properties"][field]
                self.assertEqual(constraint["type"], "integer")
                self.assertEqual(constraint["minimum"], value)
                self.assertEqual(constraint["maximum"], value)
        self.assertEqual(
            sweep["properties"]["schedule"]["const"],
            "3-59/15 * * * *",
        )
        self.assertEqual(
            sweep["properties"]["offSessionMode"]["const"],
            "legacy",
        )
        self.assertEqual(set(sweep["required"]), set(sweep["properties"]))

    def test_template_retains_boolean_type_defenses(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn('kindIs "bool" $sweep.enabled', template)
        self.assertIn('kindIs "bool" $sweep.suspend', template)
        self.assertIn('hasKey $sweep "offSessionMode"', template)

        for field in ("enabled", "suspend"):
            with self.subTest(field=field):
                command = _base_command(dev=False)
                command.extend(
                    [
                        "--set-string",
                        f"directOrderPaymentSweep.{field}=false",
                    ]
                )
                failed = _run(command)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(field, failed.stderr)

    def test_every_materialized_cronjob_requires_payment_and_policy_gates(self) -> None:
        missing_credentials = _materialized_command(
            dev=False,
            include_payment_credentials=False,
            include_payment_safety=True,
        )
        failed = _run(missing_credentials)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("requires paymentCredentials.enabled=true", failed.stderr)

        missing_safety = _materialized_command(
            dev=False,
            include_payment_credentials=True,
            include_payment_safety=False,
        )
        failed = _run(missing_safety)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("requires paymentSafety.enabled=true", failed.stderr)

        for documents in self.materialized.values():
            self.assertTrue(_sweeps(documents)[0]["spec"]["suspend"])

    def test_image_receipt_rejects_empty_mismatch_and_current_production(self) -> None:
        empty = _run(
            _materialized_command(dev=False, verified_image_tag=None)
        )
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("verifiedImageTag", empty.stderr)

        mismatch = _run(
            _materialized_command(
                dev=False,
                verified_image_tag=MISMATCH_IMAGE_TAG,
            )
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("must exactly match image.tag", mismatch.stderr)

        current_prod = _run(
            _materialized_command(
                dev=False,
                image_tag=PROD_IMAGE_TAG,
                verified_image_tag=PROD_IMAGE_TAG,
            )
        )
        self.assertNotEqual(current_prod.returncode, 0)
        self.assertIn("predates sweep_direct_order_payment_attempts", current_prod.stderr)

    def test_off_session_mode_rejects_empty_false_and_zero(self) -> None:
        for value in ("", "false", "0"):
            with self.subTest(value=value):
                command = _base_command(dev=False)
                command.extend(
                    [
                        "--set-string",
                        f"directOrderPaymentSweep.offSessionMode={value}",
                    ]
                )
                failed = _run(command)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("offSessionMode", failed.stderr)

    def test_suspended_manifest_is_safe_against_api_unsuspend(self) -> None:
        for environment, documents in self.materialized.items():
            with self.subTest(environment=environment):
                release = "citrus-dev" if environment == "dev" else "citrus"
                cronjob = _sweeps(documents)[0]
                self.assertTrue(cronjob["spec"]["suspend"])
                self.assertEqual(
                    cronjob["metadata"]["annotations"][
                        "citrus.grace/verified-image-tag"
                    ],
                    COMMAND_IMAGE_TAG,
                )
                self.assertIn(
                    "direct-order-payment-sweep",
                    _batch_policy_components(documents, release),
                )

                patched = copy.deepcopy(cronjob)
                original_template = copy.deepcopy(cronjob["spec"]["jobTemplate"])
                patched["spec"]["suspend"] = False
                self.assertFalse(patched["spec"]["suspend"])
                self.assertEqual(patched["spec"]["jobTemplate"], original_template)
                env = {item["name"]: item for item in _container(patched)["env"]}
                self.assertEqual(env["DIRECT_ORDER_OFF_SESSION_MODE"]["value"], "legacy")
                self.assertIn("STRIPE_SECRET_KEY", env)
                self.assertTrue(RUNTIME_SECRET_KEYS.issubset(env))

    def test_materialized_job_is_bounded_staggered_and_hardcoded(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("directOrderPaymentSweep.command", template)
        values = YAML_PARSER.load(VALUES_PATH.read_text(encoding="utf-8"))
        self.assertNotEqual(
            values["directOrderPaymentSweep"]["schedule"],
            "*/15 * * * *",
            "CES-848 owns the quarter-hour boundary",
        )
        sweep_minutes = set(range(3, 60, 15))
        existing_wakeups = {
            "CES-848 SMS sweep": set(range(0, 60, 15)),
            "recurring tick": set(range(0, 60, 5)),
            "recurring health": set(range(2, 60, 5)),
            "media requeue": set(range(0, 60, 10)),
            "media garbage collection": {22},
        }
        for scheduler, wakeup_minutes in existing_wakeups.items():
            with self.subTest(scheduler=scheduler):
                self.assertTrue(
                    sweep_minutes.isdisjoint(wakeup_minutes),
                    f"direct sweep collides with {scheduler}",
                )

        for documents in self.materialized.values():
            cronjob = _sweeps(documents)[0]
            spec = cronjob["spec"]
            self.assertEqual(spec["schedule"], "3-59/15 * * * *")
            self.assertEqual(spec["timeZone"], "Etc/UTC")
            self.assertEqual(spec["concurrencyPolicy"], "Forbid")
            self.assertEqual(spec["startingDeadlineSeconds"], 840)
            job = spec["jobTemplate"]["spec"]
            self.assertEqual(job["activeDeadlineSeconds"], 600)
            self.assertEqual(job["backoffLimit"], 1)
            pod = job["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["restartPolicy"], "Never")
            container = _container(cronjob)
            self.assertEqual(container["command"], EXPECTED_COMMAND)
            self.assertEqual(
                container["image"],
                "registry.digitalocean.com/sendouq/citrus:" + COMMAND_IMAGE_TAG,
            )

    def test_job_projects_only_exact_nonoptional_runtime_and_payment_keys(self) -> None:
        for environment, documents in self.materialized.items():
            with self.subTest(environment=environment):
                container = _container(_sweeps(documents)[0])
                env = {entry["name"]: entry for entry in container["env"]}
                expected_payment_keys = {"STRIPE_SECRET_KEY"}
                if environment == "dev":
                    expected_payment_keys.update({
                        "STRIPE_PUBLISHABLE_KEY",
                        "STRIPE_WEBHOOK_SECRET_DEV",
                        "STRIPE_WEBHOOK_SECRET_OWNER",
                    })
                self.assertEqual(
                    set(env),
                    PAYMENT_ATTESTATION
                    | RUNTIME_SECRET_KEYS
                    | {"DIRECT_ORDER_OFF_SESSION_MODE"}
                    | expected_payment_keys,
                )
                for key in RUNTIME_SECRET_KEYS:
                    reference = env[key]["valueFrom"]["secretKeyRef"]
                    self.assertEqual(reference["name"], RUNTIME_SECRET_NAME)
                    self.assertEqual(reference["key"], key)
                    self.assertFalse(reference["optional"])

                payment_reference = env["STRIPE_SECRET_KEY"]["valueFrom"][
                    "secretKeyRef"
                ]
                self.assertEqual(
                    payment_reference["name"],
                    (
                        "citrus-dev-payment-credentials"
                        if environment == "dev"
                        else "citrus-prod-payment-credentials"
                    ),
                )
                self.assertEqual(payment_reference["key"], "STRIPE_SECRET_KEY")
                self.assertFalse(payment_reference["optional"])
                if environment == "dev":
                    publishable_reference = env[
                        "STRIPE_PUBLISHABLE_KEY"
                    ]["valueFrom"]["secretKeyRef"]
                    self.assertEqual(
                        publishable_reference,
                        {
                            "name": "citrus-dev-payment-credentials",
                            "key": "STRIPE_PUBLISHABLE_KEY",
                            "optional": False,
                        },
                    )
                    webhook_reference = env[
                        "STRIPE_WEBHOOK_SECRET_DEV"
                    ]["valueFrom"]["secretKeyRef"]
                    self.assertEqual(
                        webhook_reference,
                        {
                            "name": "django-secrets",
                            "key": "STRIPE_WEBHOOK_SECRET_DEV",
                            "optional": False,
                        },
                    )
                    self.assertEqual(
                        env["STRIPE_WEBHOOK_SECRET_OWNER"]["value"],
                        "citrus-dev",
                    )
                self.assertEqual(
                    container["envFrom"],
                    [{"configMapRef": {"name": "django-config"}}],
                )
                self.assertNotIn("STRIPE_WEBHOOK_SECRET", env)
                self.assertNotIn("STRIPE_WEBHOOK_SECRET_PROD", env)
                serialized = str(container)
                forbidden = [
                    "django-email-secrets",
                    "django-spaces-secrets",
                    "citrus-dev-app-key",
                    "citrus-app-key",
                ]
                if environment != "dev":
                    forbidden.extend([
                        "django-secrets",
                        "STRIPE_PUBLISHABLE_KEY",
                    ])
                for forbidden_setting in forbidden:
                    self.assertNotIn(forbidden_setting, serialized)

    def test_explicit_mode_and_payment_key_override_configmap_values(self) -> None:
        command = _materialized_command(dev=True)
        command.extend(
            [
                "--set-string",
                "application.configData.DIRECT_ORDER_OFF_SESSION_MODE=on",
                "--set-string",
                "application.configData.STRIPE_SECRET_KEY=configmap-placeholder",
            ]
        )
        documents = _documents(command)
        config = _named(documents, "ConfigMap", "django-config")["data"]
        self.assertEqual(config["DIRECT_ORDER_OFF_SESSION_MODE"], "on")
        self.assertEqual(config["STRIPE_SECRET_KEY"], "configmap-placeholder")

        env = {
            entry["name"]: entry
            for entry in _container(_sweeps(documents)[0])["env"]
        }
        self.assertEqual(env["DIRECT_ORDER_OFF_SESSION_MODE"]["value"], "legacy")
        self.assertIn("valueFrom", env["STRIPE_SECRET_KEY"])
        self.assertNotIn("value", env["STRIPE_SECRET_KEY"])

    def test_runtime_secret_rejects_missing_broad_and_provider_names(self) -> None:
        missing = _run(
            _materialized_command(dev=True, runtime_secret_name=None)
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("runtimeSecretName", missing.stderr)

        for name in (
            "django-secrets",
            "django-email-secrets",
            "django-spaces-secrets",
            "citrus-dev-app-key",
            "citrus-dev-payment-credentials",
            "citrus-stripe-runtime",
            "citrus-payment-runtime",
        ):
            with self.subTest(name=name):
                failed = _run(
                    _materialized_command(
                        dev=True,
                        runtime_secret_name=name,
                    )
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("runtimeSecretName", failed.stderr)

    def test_combined_ces848_ces850_scheduler_render_preserves_selectors(self) -> None:
        command = _materialized_command(dev=False)
        command.extend(
            [
                "--set",
                "billingWorker.enabled=true",
                "--set-string",
                "billingWorker.topologyRevision=ces-850-test",
                "--set",
                "recurringRuntime.enabled=true",
                "--set-string",
                f"recurringRuntime.expectedSourceRevision={COMMAND_IMAGE_TAG}",
                "--set-string",
                "recurringRuntime.topologyRevision=ces-850-test",
                "--set",
                "recurringRuntime.preflight.enabled=true",
                "--set-string",
                "recurringRuntime.preflight.topologyRevision=ces-850-test",
                "--set-string",
                "recurringRuntime.health.topologyRevision=ces-850-test",
            ]
        )
        documents = _documents(command)
        components = {
            document.get("metadata", {}).get("labels", {}).get(
                "app.kubernetes.io/component"
            )
            for document in documents
        }
        self.assertTrue(
            {
                "billing-worker",
                "recurring-preflight",
                "recurring-tick",
                "recurring-health",
                "direct-order-payment-sweep",
            }.issubset(components)
        )
        selectors = _batch_policy_components(documents, "citrus")
        self.assertTrue(
            {
                "recurring-preflight",
                "recurring-tick",
                "recurring-health",
                "direct-order-payment-sweep",
            }.issubset(selectors)
        )
        policy = POLICY_PATH.read_text(encoding="utf-8")
        for component in (
            "migrations",
            "media-requeue",
            "media-gc",
            "recurring-preflight",
            "recurring-tick",
            "recurring-health",
            "direct-order-payment-sweep",
        ):
            self.assertIn(f"- {component}", policy)


if __name__ == "__main__":
    unittest.main()
