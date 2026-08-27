from __future__ import annotations

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
DEV_PAYMENT_VALUES_PATH = CHART_PATH / "values-payment-dev.yaml"
SCHEMA_PATH = CHART_PATH / "values.schema.json"
TEMPLATE_PATH = CHART_PATH / "templates" / "sms-reconciliation-cronjob.yaml"
TRUSTED_IMAGE_REPOSITORY = "registry.digitalocean.com/sendouq/citrus"
PROD_IMAGE_TAG = "3f68967f777b2665fccb4f0ab423f339b8ea1357"
COMMAND_BEARING_IMAGE_TAG = "0e2258bf95c6170895c26780258eb42d5b5c557c"
CANONICAL_MIGRATION_COMMAND = ["python", "manage.py", "migrate", "--noinput"]
SMS_POD_SECURITY_CONTEXT = {
    "fsGroup": 10001,
    "fsGroupChangePolicy": "OnRootMismatch",
    "runAsGroup": 10001,
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}
SMS_CONTAINER_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "runAsGroup": 10001,
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "capabilities": {"drop": ["ALL"]},
}
RUNTIME_SECRET_NAMES = {
    False: "citrus-sms-reconciliation-runtime",
    True: "citrus-dev-sms-reconciliation-runtime",
}
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
    chart_path: Path = CHART_PATH,
    release_name: str | None = None,
    namespace: str | None = None,
    image_repository: str | None = None,
    image_tag: str | None = None,
    verified_image_tag: str | None = None,
    compatible_image_tag: str | None = None,
    secret_name: str | None = None,
    migrations_enabled: bool | None = None,
    config_map_name: str | None = None,
    suspend: bool | None = None,
    schedule: str | None = None,
    time_zone: str | None = None,
    concurrency_policy: str | None = None,
    starting_deadline_seconds: int | None = None,
    active_deadline_seconds: int | None = None,
    backoff_limit: int | None = None,
    successful_jobs_history_limit: int | None = None,
    failed_jobs_history_limit: int | None = None,
    config_sync_wave: str | None = None,
    migrations_sync_wave: str | None = None,
    sms_sync_wave: str | None = None,
    network_policy_enabled: bool | None = None,
    network_policy_provider: str | None = None,
    network_policy_revision: str | None = None,
    network_policy_sync_wave: str | None = None,
    network_policy_database_host: str | None = None,
    network_policy_database_port: int | None = None,
    stale_minutes: int | None = None,
    limit: int | None = None,
    include_payment_credentials: bool = False,
    extra_set: tuple[str, ...] = (),
    extra_set_json: tuple[str, ...] = (),
    extra_set_string: tuple[str, ...] = (),
) -> list[str]:
    release = release_name or ("citrus-dev" if dev else "citrus")
    release_namespace = namespace or ("citrus-dev" if dev else "default")
    command = [
        "helm",
        "template",
        release,
        str(chart_path),
        "--namespace",
        release_namespace,
        "-f",
        str(chart_path / "values.yaml"),
    ]
    if dev:
        command.extend(["-f", str(chart_path / "values-dev.yaml")])
        if include_payment_credentials:
            command.extend([
                "-f",
                str(chart_path / DEV_PAYMENT_VALUES_PATH.name),
            ])
    string_overrides = (
        ("image.repository", image_repository),
        ("application.configMapName", config_map_name),
        ("smsReconciliation.schedule", schedule),
        ("smsReconciliation.timeZone", time_zone),
        ("smsReconciliation.concurrencyPolicy", concurrency_policy),
        ("syncWaves.config", config_sync_wave),
        ("syncWaves.migrations", migrations_sync_wave),
        ("syncWaves.smsReconciliation", sms_sync_wave),
        ("smsReconciliation.networkPolicy.provider", network_policy_provider),
        ("smsReconciliation.networkPolicy.revision", network_policy_revision),
        ("smsReconciliation.networkPolicy.syncWave", network_policy_sync_wave),
        (
            "smsReconciliation.networkPolicy.database.host",
            network_policy_database_host,
        ),
    )
    for key, value in string_overrides:
        if value is not None:
            command.extend(["--set-string", f"{key}={value}"])
    if image_tag is not None:
        command.extend(["--set-string", f"image.tag={image_tag}"])
    if enabled:
        command.extend(["--set", "smsReconciliation.enabled=true"])
    if migrations_enabled is not None:
        command.extend([
            "--set",
            f"migrations.enabled={str(migrations_enabled).lower()}",
        ])
    if suspend is not None:
        command.extend([
            "--set",
            f"smsReconciliation.suspend={str(suspend).lower()}",
        ])
    if network_policy_enabled is not None:
        command.extend([
            "--set",
            "smsReconciliation.networkPolicy.enabled="
            f"{str(network_policy_enabled).lower()}",
        ])
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
    integer_overrides = (
        ("smsReconciliation.startingDeadlineSeconds", starting_deadline_seconds),
        ("smsReconciliation.activeDeadlineSeconds", active_deadline_seconds),
        ("smsReconciliation.backoffLimit", backoff_limit),
        (
            "smsReconciliation.successfulJobsHistoryLimit",
            successful_jobs_history_limit,
        ),
        ("smsReconciliation.failedJobsHistoryLimit", failed_jobs_history_limit),
        (
            "smsReconciliation.networkPolicy.database.port",
            network_policy_database_port,
        ),
    )
    for key, value in integer_overrides:
        if value is not None:
            command.extend(["--set", f"{key}={value}"])
    if stale_minutes is not None:
        command.extend([
            "--set",
            f"smsReconciliation.staleMinutes={stale_minutes}",
        ])
    if limit is not None:
        command.extend(["--set", f"smsReconciliation.limit={limit}"])
    for assignment in extra_set:
        command.extend(["--set", assignment])
    for assignment in extra_set_json:
        command.extend(["--set-json", assignment])
    for assignment in extra_set_string:
        command.extend(["--set-string", assignment])
    return command


def _run_helm(
    *,
    validate_schema: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    if validate_schema:
        return subprocess.run(
            _helm_command(**kwargs),
            check=False,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    with tempfile.TemporaryDirectory() as temporary_directory:
        chart_path = Path(temporary_directory) / "citrus"
        shutil.copytree(CHART_PATH, chart_path)
        (chart_path / "values.schema.json").unlink()
        return subprocess.run(
            _helm_command(chart_path=chart_path, **kwargs),
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


def _sms_network_policy(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == "CiliumNetworkPolicy"
        and document.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/component"
        ) == "sms-reconciliation-egress"
    )


def _migration_job(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == "Job"
        and document.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/component"
        ) == "migrations"
    )


def _application_config_map(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("name") == "django-config"
    )


def _synthetic_render_kwargs(*, dev: bool) -> dict[str, Any]:
    return {
        "dev": dev,
        "enabled": True,
        "image_tag": COMMAND_BEARING_IMAGE_TAG,
        "verified_image_tag": COMMAND_BEARING_IMAGE_TAG,
        "compatible_image_tag": COMMAND_BEARING_IMAGE_TAG,
        "secret_name": RUNTIME_SECRET_NAMES[dev],
        "network_policy_enabled": True,
        "network_policy_provider": "cilium",
        "network_policy_revision": "ces-848-test",
        "network_policy_database_host": "db.sms-reconciliation.example",
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

    def _assert_template_rejects(
        self,
        expected: str,
        **overrides: Any,
    ) -> None:
        kwargs = _synthetic_render_kwargs(dev=True)
        kwargs.update(overrides)
        failed = _run_helm(validate_schema=False, **kwargs)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(expected, failed.stderr)

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
                self.assertFalse(sms["networkPolicy"]["enabled"])
                self.assertEqual(sms["networkPolicy"]["provider"], "")
                self.assertEqual(sms["networkPolicy"]["revision"], "")
                self.assertEqual(sms["networkPolicy"]["database"]["host"], "")

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
                self.assertNotIn("sms-reconciliation-egress", components)

    def test_current_production_image_cannot_render_an_enabled_scheduler(self) -> None:
        network_contract = {
            "network_policy_enabled": True,
            "network_policy_provider": "cilium",
            "network_policy_revision": "ces-848-test",
            "network_policy_database_host": "db.sms-reconciliation.example",
        }
        failed = _run_helm(dev=False, enabled=True, **network_contract)
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
            secret_name=RUNTIME_SECRET_NAMES[False],
            **network_contract,
        )
        self.assertNotEqual(forged_receipt.returncode, 0)
        self.assertIn(
            "is not listed in smsReconciliation.commandCompatibleImageTags",
            forged_receipt.stderr,
        )

    def test_image_receipt_must_exactly_match_the_rendered_image(self) -> None:
        kwargs = _synthetic_render_kwargs(dev=True)
        kwargs["image_tag"] = "ffffffffffffffffffffffffffffffffffffffff"
        failed = _run_helm(**kwargs)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("must exactly match image.tag", failed.stderr)

    def test_image_tag_format_is_enforced_with_and_without_schema(self) -> None:
        invalid_tags = (
            "latest",
            "a" * 39,
            "a" * 41,
            "a" * 64,
            "A" * 40,
            "not-a-source-sha",
        )
        expected = (
            "image.tag to be exactly 40 lowercase hexadecimal characters"
        )
        for validate_schema in (True, False):
            for image_tag in invalid_tags:
                with self.subTest(
                    validate_schema=validate_schema,
                    image_tag=image_tag,
                ):
                    kwargs = _synthetic_render_kwargs(dev=True)
                    kwargs["image_tag"] = image_tag
                    kwargs["verified_image_tag"] = image_tag
                    failed = _run_helm(
                        validate_schema=validate_schema,
                        **kwargs,
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn(expected, failed.stderr)

            valid = _run_helm(
                validate_schema=validate_schema,
                **_synthetic_render_kwargs(dev=True),
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)

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

    def test_runtime_secret_name_is_bound_to_the_exact_release(self) -> None:
        for dev, wrong_name, expected_name in (
            (
                False,
                "citrus-dev-sms-reconciliation-runtime",
                "citrus-sms-reconciliation-runtime",
            ),
            (
                True,
                "citrus-sms-reconciliation-runtime",
                "citrus-dev-sms-reconciliation-runtime",
            ),
        ):
            with self.subTest(dev=dev):
                kwargs = _synthetic_render_kwargs(dev=dev)
                kwargs["secret_name"] = wrong_name
                failed = _run_helm(**kwargs)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(
                    f"must remain exactly {expected_name}",
                    failed.stderr,
                )

        for former_broad_name, mutation in (
            (
                "citrus-dev-app-key",
                "application.generatedSecretName=renamed-app-key",
            ),
            (
                "django-secrets",
                "application.secretName=renamed-django-secrets",
            ),
        ):
            with self.subTest(former_broad_name=former_broad_name):
                kwargs = _synthetic_render_kwargs(dev=True)
                kwargs["secret_name"] = former_broad_name
                kwargs["extra_set_string"] = (mutation,)
                failed = _run_helm(**kwargs)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn(
                    "must remain exactly citrus-dev-sms-reconciliation-runtime",
                    failed.stderr,
                )

    def test_enabled_render_requires_trusted_release_inputs(self) -> None:
        cases = (
            (
                {"image_repository": "attacker.example/citrus"},
                "requires image.repository registry.digitalocean.com/sendouq/citrus",
            ),
            (
                {"migrations_enabled": False},
                "requires migrations.enabled=true",
            ),
            (
                {"config_map_name": "attacker-config"},
                "requires application.configMapName django-config",
            ),
            (
                {"release_name": "citrus-copy"},
                "requires release/namespace citrus/default or citrus-dev/citrus-dev",
            ),
            (
                {"namespace": "other"},
                "requires release/namespace citrus/default or citrus-dev/citrus-dev",
            ),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                kwargs = _synthetic_render_kwargs(dev=False)
                kwargs.update(overrides)
                failed = _run_helm(**kwargs)
                self.assertNotEqual(failed.returncode, 0)
                self._assert_template_rejects(expected, **overrides)

    def test_reproduced_immediate_run_payload_fails_closed(self) -> None:
        overrides = {
            "suspend": False,
            "schedule": "* * * * *",
            "sms_sync_wave": "0",
        }
        for validate_schema in (True, False):
            with self.subTest(validate_schema=validate_schema):
                kwargs = _synthetic_render_kwargs(dev=True)
                kwargs.update(overrides)
                failed = _run_helm(
                    validate_schema=validate_schema,
                    **kwargs,
                )
                self.assertNotEqual(failed.returncode, 0)

    def test_operational_envelope_rejects_every_drift(self) -> None:
        cases = (
            (
                {"suspend": False},
                "requires smsReconciliation.suspend=true",
            ),
            (
                {"schedule": "* * * * *"},
                "schedule must remain exactly */15 * * * *",
            ),
            (
                {"schedule": ""},
                "schedule must remain exactly */15 * * * *",
            ),
            (
                {"schedule": "0-59/15 * * * *"},
                "schedule must remain exactly */15 * * * *",
            ),
            (
                {"time_zone": "UTC"},
                "timeZone must remain exactly Etc/UTC",
            ),
            (
                {"concurrency_policy": "Allow"},
                "concurrencyPolicy must remain exactly Forbid",
            ),
            (
                {"starting_deadline_seconds": 239},
                "startingDeadlineSeconds must remain exactly 240",
            ),
            (
                {"starting_deadline_seconds": 241},
                "startingDeadlineSeconds must remain exactly 240",
            ),
            (
                {"active_deadline_seconds": 239},
                "activeDeadlineSeconds must remain exactly 240",
            ),
            (
                {"active_deadline_seconds": 241},
                "activeDeadlineSeconds must remain exactly 240",
            ),
            (
                {"backoff_limit": 0},
                "backoffLimit must remain exactly 1",
            ),
            (
                {"backoff_limit": 2},
                "backoffLimit must remain exactly 1",
            ),
            (
                {"successful_jobs_history_limit": 1},
                "successfulJobsHistoryLimit must remain exactly 2",
            ),
            (
                {"successful_jobs_history_limit": 3},
                "successfulJobsHistoryLimit must remain exactly 2",
            ),
            (
                {"failed_jobs_history_limit": 2},
                "failedJobsHistoryLimit must remain exactly 3",
            ),
            (
                {"failed_jobs_history_limit": 4},
                "failedJobsHistoryLimit must remain exactly 3",
            ),
            (
                {"stale_minutes": 14},
                "staleMinutes must remain exactly 15",
            ),
            (
                {"stale_minutes": 16},
                "staleMinutes must remain exactly 15",
            ),
            (
                {"limit": 99},
                "limit must remain exactly 100",
            ),
            (
                {"limit": 101},
                "limit must remain exactly 100",
            ),
            (
                {
                    "extra_set_string": (
                        "smsReconciliation.resources.limits.cpu=1000m",
                    )
                },
                "resources must remain requests 50m/128Mi and limits 250m/256Mi",
            ),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                kwargs = _synthetic_render_kwargs(dev=True)
                kwargs.update(overrides)
                schema_failed = _run_helm(**kwargs)
                self.assertNotEqual(schema_failed.returncode, 0)
                self._assert_template_rejects(expected, **overrides)

    def test_sync_wave_contract_rejects_unsafe_types_and_ordering(self) -> None:
        cases = (
            (
                {"config_sync_wave": "-1"},
                "syncWaves.config must remain exactly 0",
            ),
            (
                {"config_sync_wave": "1"},
                "syncWaves.config must remain exactly 0",
            ),
            (
                {"config_sync_wave": "4"},
                "syncWaves.config must remain exactly 0",
            ),
            (
                {"config_sync_wave": "00"},
                "syncWaves.config must be an integer string",
            ),
            (
                {"config_sync_wave": "not-an-integer"},
                "syncWaves.config must be an integer string",
            ),
            (
                {"extra_set": ("syncWaves.config=0",)},
                "syncWaves.config must be an integer string",
            ),
            (
                {"extra_set_json": ("syncWaves.config=null",)},
                "syncWaves.config must be an integer string",
            ),
            (
                {"migrations_sync_wave": "-1"},
                "syncWaves.migrations must remain exactly 1",
            ),
            (
                {"migrations_sync_wave": "0"},
                "syncWaves.migrations must remain exactly 1",
            ),
            (
                {"migrations_sync_wave": "2"},
                "syncWaves.migrations must remain exactly 1",
            ),
            (
                {"migrations_sync_wave": "3"},
                "syncWaves.migrations must remain exactly 1",
            ),
            (
                {"migrations_sync_wave": "01"},
                "migrations must be an integer string",
            ),
            (
                {"migrations_sync_wave": "not-an-integer"},
                "migrations must be an integer string",
            ),
            (
                {"extra_set": ("syncWaves.migrations=1",)},
                "migrations must be an integer string",
            ),
            (
                {"extra_set_json": ("syncWaves.migrations=null",)},
                "migrations must be an integer string",
            ),
            (
                {"sms_sync_wave": "0"},
                "syncWaves.smsReconciliation must remain exactly 3",
            ),
            (
                {"sms_sync_wave": "1"},
                "syncWaves.smsReconciliation must remain exactly 3",
            ),
            (
                {"sms_sync_wave": "4"},
                "syncWaves.smsReconciliation must remain exactly 3",
            ),
            (
                {"sms_sync_wave": "03"},
                "smsReconciliation must be an integer string",
            ),
            (
                {"sms_sync_wave": "not-an-integer"},
                "smsReconciliation must be an integer string",
            ),
            (
                {"extra_set": ("syncWaves.smsReconciliation=3",)},
                "smsReconciliation must be an integer string",
            ),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                kwargs = _synthetic_render_kwargs(dev=True)
                kwargs.update(overrides)
                schema_failed = _run_helm(**kwargs)
                self.assertNotEqual(schema_failed.returncode, 0)
                self._assert_template_rejects(expected, **overrides)

    def test_sync_wave_sequence_is_exactly_config_migration_policy_job(self) -> None:
        for documents in (
            self.prod_enabled_synthetic,
            self.dev_enabled_synthetic,
        ):
            waves = [
                _application_config_map(documents)["metadata"]["annotations"]
                ["argocd.argoproj.io/sync-wave"],
                _migration_job(documents)["metadata"]["annotations"]
                ["argocd.argoproj.io/sync-wave"],
                _sms_network_policy(documents)["metadata"]["annotations"]
                ["argocd.argoproj.io/sync-wave"],
                _sms_cronjob(documents)["metadata"]["annotations"]
                ["argocd.argoproj.io/sync-wave"],
            ]
            self.assertEqual(waves, ["0", "1", "2", "3"])
            self.assertEqual([int(wave) for wave in waves], sorted(map(int, waves)))

    def test_migration_command_contract_rejects_every_bypass(self) -> None:
        bypasses: tuple[Any, ...] = (
            ["true", "noop", "noop", "noop"],
            ["python", "manage.py", "makemigrations", "--noinput"],
            ["python", "manage.py", "migrate"],
            ["python", "manage.py", "migrate", "--fake"],
            ["sh", "-c", "python manage.py migrate --noinput"],
            ["python", "manage.py", "migrate", "--noinput", "--fake"],
            "python manage.py migrate --noinput",
            {"command": CANONICAL_MIGRATION_COMMAND},
            None,
        )
        expected = "requires migrations.command to remain exactly"
        for validate_schema in (True, False):
            for migration_command in bypasses:
                with self.subTest(
                    validate_schema=validate_schema,
                    migration_command=migration_command,
                ):
                    kwargs = _synthetic_render_kwargs(dev=True)
                    kwargs["extra_set_json"] = (
                        "migrations.command=" + json.dumps(migration_command),
                    )
                    failed = _run_helm(
                        validate_schema=validate_schema,
                        **kwargs,
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    if not validate_schema:
                        self.assertIn(expected, failed.stderr)

        migration = _migration_job(self.dev_enabled_synthetic)
        container = migration["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], CANONICAL_MIGRATION_COMMAND)
        self.assertEqual(
            container["image"],
            TRUSTED_IMAGE_REPOSITORY + ":" + COMMAND_BEARING_IMAGE_TAG,
        )

    def test_network_policy_contract_rejects_every_egress_widening(self) -> None:
        too_long_database_host = ".".join(
            ("a" * 63, "b" * 63, "c" * 63, "d" * 62)
        )
        self.assertEqual(len(too_long_database_host), 254)
        cases = (
            (
                {"network_policy_enabled": False},
                "networkPolicy.enabled=true",
            ),
            (
                {"network_policy_provider": "other"},
                "networkPolicy.provider=cilium",
            ),
            (
                {"network_policy_revision": ""},
                "requires smsReconciliation.networkPolicy.revision",
            ),
            (
                {"network_policy_sync_wave": "0"},
                "networkPolicy.syncWave must remain exactly 2",
            ),
            (
                {"network_policy_sync_wave": "3"},
                "networkPolicy.syncWave must remain exactly 2",
            ),
            (
                {"network_policy_sync_wave": "02"},
                "networkPolicy.syncWave must be an integer string",
            ),
            (
                {"network_policy_sync_wave": "not-an-integer"},
                "networkPolicy.syncWave must be an integer string",
            ),
            (
                {
                    "extra_set": (
                        "smsReconciliation.networkPolicy.syncWave=2",
                    )
                },
                "networkPolicy.syncWave must be an integer string",
            ),
            (
                {"migrations_sync_wave": "2"},
                "syncWaves.migrations must remain exactly 1",
            ),
            (
                {"network_policy_database_host": ""},
                "requires an exact smsReconciliation.networkPolicy.database.host",
            ),
            (
                {"network_policy_database_host": "*.example.com"},
                "must be one exact lower-case DNS FQDN without wildcards",
            ),
            (
                {"network_policy_database_host": "DB.EXAMPLE.COM"},
                "must be one exact lower-case DNS FQDN without wildcards",
            ),
            (
                {"network_policy_database_host": "192.0.2.10"},
                "database.host must not be an IP address",
            ),
            (
                {"network_policy_database_host": "db.stripe.example"},
                "must not name a payment, messaging, or email provider",
            ),
            (
                {"network_policy_database_host": "api.twilio.com"},
                "must not name a payment, messaging, or email provider",
            ),
            (
                {"network_policy_database_host": "localhost"},
                "must be one exact lower-case DNS FQDN without wildcards",
            ),
            (
                {"network_policy_database_host": too_long_database_host},
                "database.host must not exceed 253 characters",
            ),
            (
                {"network_policy_database_port": 0},
                "database.port must remain exactly 25060",
            ),
            (
                {"network_policy_database_port": 443},
                "database.port must remain exactly 25060",
            ),
            (
                {"network_policy_database_port": 65535},
                "database.port must remain exactly 25060",
            ),
            (
                {"network_policy_database_port": 65536},
                "database.port must remain exactly 25060",
            ),
            (
                {
                    "extra_set_string": (
                        "smsReconciliation.networkPolicy.dns.namespace=other",
                    )
                },
                "networkPolicy.dns.namespace must remain kube-system",
            ),
            (
                {
                    "extra_set_string": (
                        "smsReconciliation.networkPolicy.dns.podLabels.k8s-app=other",
                    )
                },
                "networkPolicy.dns.podLabels must remain exactly k8s-app=kube-dns",
            ),
            (
                {
                    "extra_set_string": (
                        "smsReconciliation.networkPolicy.additionalExternalEgress[0].host=evil.example",
                    )
                },
                "networkPolicy must not configure additionalExternalEgress",
            ),
            (
                {
                    "extra_set_string": (
                        "smsReconciliation.networkPolicy.toEntities[0]=all",
                    )
                },
                "networkPolicy must not configure toEntities",
            ),
            (
                {
                    "extra_set": (
                        "smsReconciliation.networkPolicy.world=true",
                    )
                },
                "networkPolicy must not configure world",
            ),
            (
                {
                    "extra_set": (
                        "smsReconciliation.networkPolicy.all=true",
                    )
                },
                "networkPolicy must not configure all",
            ),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                kwargs = _synthetic_render_kwargs(dev=True)
                kwargs.update(overrides)
                schema_failed = _run_helm(**kwargs)
                self.assertNotEqual(schema_failed.returncode, 0)
                self._assert_template_rejects(expected, **overrides)

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

    def test_enabled_render_has_one_dedicated_default_deny_egress_policy(self) -> None:
        for environment, documents, release in (
            ("prod", self.prod_enabled_synthetic, "citrus"),
            ("dev", self.dev_enabled_synthetic, "citrus-dev"),
        ):
            with self.subTest(environment=environment):
                policy = _sms_network_policy(documents)
                self.assertEqual(
                    policy["metadata"]["name"],
                    f"{release}-sms-reconciliation-egress",
                )
                self.assertEqual(
                    policy["metadata"]["annotations"],
                    {
                        "argocd.argoproj.io/sync-wave": "2",
                        "citrus.grace/sms-reconciliation-egress-policy-revision": (
                            "ces-848-test"
                        ),
                    },
                )
                spec = policy["spec"]
                self.assertEqual(
                    spec["endpointSelector"]["matchLabels"],
                    {
                        "app.kubernetes.io/instance": release,
                        "citrus.grace/sms-reconciliation-egress": "restricted",
                    },
                )
                self.assertEqual(spec["enableDefaultDeny"], {"egress": True})
                self.assertEqual(len(spec["egress"]), 2)
                dns_egress, database_egress = spec["egress"]
                self.assertEqual(
                    dns_egress["toEndpoints"],
                    [
                        {
                            "matchLabels": {
                                "k8s:io.kubernetes.pod.namespace": "kube-system",
                                "k8s:k8s-app": "kube-dns",
                            }
                        }
                    ],
                )
                self.assertEqual(
                    dns_egress["toPorts"][0]["rules"]["dns"],
                    [{"matchName": "db.sms-reconciliation.example"}],
                )
                self.assertEqual(
                    database_egress,
                    {
                        "toFQDNs": [
                            {"matchName": "db.sms-reconciliation.example"}
                        ],
                        "toPorts": [
                            {
                                "ports": [
                                    {"port": "25060", "protocol": "TCP"}
                                ]
                            }
                        ],
                    },
                )
                serialized_policy = json.dumps(policy, sort_keys=True)
                for forbidden in (
                    "matchPattern",
                    "toEntities",
                    "additionalExternalEgress",
                    '"world"',
                    '"all"',
                    "stripe",
                    "twilio",
                ):
                    with self.subTest(environment=environment, forbidden=forbidden):
                        self.assertNotIn(forbidden.lower(), serialized_policy.lower())

                cronjob = _sms_cronjob(documents)
                pod_labels = cronjob["spec"]["jobTemplate"]["spec"][
                    "template"
                ]["metadata"]["labels"]
                self.assertEqual(
                    pod_labels["citrus.grace/sms-reconciliation-egress"],
                    "restricted",
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
            self.assertEqual(
                pod["securityContext"],
                SMS_POD_SECURITY_CONTEXT,
            )
            container = pod["containers"][0]
            self.assertEqual(
                container["image"],
                "registry.digitalocean.com/sendouq/citrus:"
                + COMMAND_BEARING_IMAGE_TAG,
            )
            self.assertEqual(
                container["securityContext"],
                SMS_CONTAINER_SECURITY_CONTEXT,
            )
            self.assertEqual(
                container["resources"],
                {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "250m", "memory": "256Mi"},
                },
            )

    def test_sms_security_context_is_literal_in_both_schema_modes(self) -> None:
        malicious_global_context = (
            "podSecurityContext.fsGroup=0",
            "podSecurityContext.runAsGroup=0",
            "podSecurityContext.runAsNonRoot=false",
            "podSecurityContext.runAsUser=0",
            "podSecurityContext.seccompProfile.type=Unconfined",
            "containerSecurityContext.allowPrivilegeEscalation=true",
            "containerSecurityContext.readOnlyRootFilesystem=false",
            "containerSecurityContext.runAsGroup=0",
            "containerSecurityContext.runAsNonRoot=false",
            "containerSecurityContext.runAsUser=0",
            "containerSecurityContext.capabilities.drop[0]=NET_ADMIN",
        )
        for validate_schema in (True, False):
            with self.subTest(validate_schema=validate_schema):
                kwargs = _synthetic_render_kwargs(dev=True)
                kwargs["extra_set"] = malicious_global_context
                documents = _render(
                    validate_schema=validate_schema,
                    **kwargs,
                )
                pod = _sms_cronjob(documents)["spec"]["jobTemplate"]["spec"]
                pod = pod["template"]["spec"]
                self.assertEqual(
                    pod["securityContext"],
                    SMS_POD_SECURITY_CONTEXT,
                )
                self.assertEqual(
                    pod["containers"][0]["securityContext"],
                    SMS_CONTAINER_SECURITY_CONTEXT,
                )

        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn(".Values.podSecurityContext", template)
        self.assertNotIn(".Values.containerSecurityContext", template)

    def test_command_argv_and_numeric_inputs_are_exactly_pinned(self) -> None:
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

    def test_former_numeric_boundaries_are_no_longer_configurable(self) -> None:
        for field, value in (
            ("stale_minutes", 5),
            ("stale_minutes", 4),
            ("stale_minutes", 14),
            ("stale_minutes", 16),
            ("stale_minutes", 1440),
            ("stale_minutes", 1441),
            ("limit", 1),
            ("limit", 0),
            ("limit", 99),
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
                self.assertEqual(reference["name"], RUNTIME_SECRET_NAMES[True])
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

    def test_dev_scheduler_receives_the_dedicated_test_payment_set(self) -> None:
        documents = _render(
            **_synthetic_render_kwargs(dev=True),
            include_payment_credentials=True,
        )
        container = (
            _sms_cronjob(documents)["spec"]["jobTemplate"]
            ["spec"]["template"]["spec"]["containers"][0]
        )
        env = {entry["name"]: entry for entry in container["env"]}
        self.assertEqual(
            env["STRIPE_SECRET_KEY"]["valueFrom"]["secretKeyRef"],
            {
                "name": "citrus-dev-payment-credentials",
                "key": "STRIPE_SECRET_KEY",
                "optional": False,
            },
        )
        self.assertEqual(
            env["STRIPE_PUBLISHABLE_KEY"]["valueFrom"]["secretKeyRef"],
            {
                "name": "citrus-dev-payment-credentials",
                "key": "STRIPE_PUBLISHABLE_KEY",
                "optional": False,
            },
        )
        self.assertEqual(
            env["STRIPE_WEBHOOK_SECRET_DEV"]["valueFrom"]["secretKeyRef"],
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
        self.assertNotIn("STRIPE_WEBHOOK_SECRET", env)
        self.assertNotIn("STRIPE_WEBHOOK_SECRET_PROD", env)

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
            {"type": "integer", "const": 15},
        )
        self.assertEqual(
            sms["properties"]["limit"],
            {"type": "integer", "const": 100},
        )
        for field, expected in (
            ("suspend", True),
            ("schedule", "*/15 * * * *"),
            ("timeZone", "Etc/UTC"),
            ("startingDeadlineSeconds", 240),
            ("activeDeadlineSeconds", 240),
            ("backoffLimit", 1),
            ("successfulJobsHistoryLimit", 2),
            ("failedJobsHistoryLimit", 3),
        ):
            with self.subTest(field=field):
                self.assertEqual(sms["properties"][field]["const"], expected)
        sync_waves = schema["properties"]["syncWaves"]
        self.assertEqual(
            sync_waves["required"],
            ["config", "migrations", "smsReconciliation"],
        )
        self.assertEqual(
            sync_waves["properties"]["config"],
            {"type": "string", "pattern": "^-?(0|[1-9][0-9]*)$"},
        )
        self.assertEqual(
            sync_waves["properties"]["smsReconciliation"],
            {"type": "string", "pattern": "^[0-9]+$", "const": "3"},
        )
        network_policy = sms["properties"]["networkPolicy"]
        self.assertFalse(network_policy["additionalProperties"])
        self.assertEqual(
            network_policy["properties"]["syncWave"],
            {"type": "string", "pattern": "^[0-9]+$", "const": "2"},
        )
        self.assertEqual(
            network_policy["properties"]["dns"]["properties"]["podLabels"]
            ["const"],
            {"k8s-app": "kube-dns"},
        )
        self.assertEqual(
            network_policy["properties"]["database"]["properties"]["host"]
            ["maxLength"],
            253,
        )
        self.assertEqual(
            network_policy["properties"]["database"]["properties"]["port"],
            {"type": "integer", "const": 25060},
        )
        self.assertEqual(
            sms["properties"]["resources"]["const"],
            {
                "requests": {"cpu": "50m", "memory": "128Mi"},
                "limits": {"cpu": "250m", "memory": "256Mi"},
            },
        )
        serialized_root_guards = json.dumps(schema["allOf"], sort_keys=True)
        for exact_guard in (
            TRUSTED_IMAGE_REPOSITORY,
            "migrations",
            "django-config",
        ):
            with self.subTest(exact_guard=exact_guard):
                self.assertIn(exact_guard, serialized_root_guards)
        enabled_guards = schema["allOf"][0]["then"]["properties"]
        self.assertEqual(
            enabled_guards["syncWaves"]["properties"],
            {
                "config": {"const": "0"},
                "migrations": {"const": "1"},
                "smsReconciliation": {"const": "3"},
            },
        )
        self.assertEqual(
            enabled_guards["migrations"]["properties"]["command"]["const"],
            CANONICAL_MIGRATION_COMMAND,
        )
        self.assertIn(
            "command",
            enabled_guards["migrations"]["required"],
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
            "networkPolicy",
        ):
            with self.subTest(gate=gate):
                self.assertIn(gate, sms["required"])


if __name__ == "__main__":
    unittest.main()
