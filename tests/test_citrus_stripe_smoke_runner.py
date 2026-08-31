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
TEMPLATE_PATH = CHART_PATH / "templates" / "stripe-smoke-runner.yaml"
POLICY_REVISION = "ces-883-stripe-smoke-v1"
AUTOMATION_POLICY_REVISION = "ces-881-stripe-smoke-gate-v1"
SENTINEL_ACCOUNT_ID = "acct_0000000000000000"
YAML_PARSER = YAML(typ="safe")


def _command(
    *,
    enabled: bool,
    dev: bool = True,
    automated: bool = False,
    expected_account_id: str | None = SENTINEL_ACCOUNT_ID,
) -> list[str]:
    command = [
        "helm",
        "template",
        "citrus-dev" if dev else "citrus",
        str(CHART_PATH),
        "--namespace",
        "citrus-dev" if dev else "default",
        "-f",
        str(CHART_PATH / "values.yaml"),
    ]
    if dev:
        command.extend(
            [
                "-f",
                str(CHART_PATH / "values-dev.yaml"),
                "-f",
                str(CHART_PATH / "values-payment-dev.yaml"),
            ]
        )
    if enabled:
        command.extend(["--set", "stripeSmokeRunner.enabled=true"])
        if automated:
            command.extend(
                ["--set", "stripeSmokeRunner.automation.enabled=true"]
            )
        if expected_account_id is not None:
            command.extend(
                [
                    "--set-string",
                    (
                        "stripeSmokeRunner.expectedAccountId="
                        f"{expected_account_id}"
                    ),
                ]
            )
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


def _pod_template(document: dict[str, Any]) -> dict[str, Any]:
    if document["kind"] == "CronJob":
        return document["spec"]["jobTemplate"]["spec"]["template"]
    return document["spec"]["template"]


def _plain_env(container: dict[str, Any]) -> dict[str, str]:
    return {
        entry["name"]: str(entry["value"])
        for entry in container.get("env", [])
        if "value" in entry
    }


class CitrusStripeSmokeRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prod_off = _documents(_command(enabled=False, dev=False))
        cls.dev_off = _documents(_command(enabled=False))
        cls.enabled_dev = _documents(_command(enabled=True))
        cls.automated_dev = _documents(
            _command(enabled=True, automated=True)
        )

    def test_runner_is_default_off_in_dev_and_absent_from_prod(self) -> None:
        for documents in (self.dev_off, self.prod_off):
            names = {
                document.get("metadata", {}).get("name")
                for document in documents
            }
            self.assertNotIn("citrus-dev-stripe-smoke-runner", names)
            self.assertNotIn("citrus-stripe-smoke-runner", names)
            self.assertFalse(
                any(
                    "CITRUS_STRIPE_SMOKE_"
                    in json.dumps(document, sort_keys=True)
                    for document in documents
                )
            )
            self.assertFalse(
                any(
                    target.get("matchName") == "api.stripe.com"
                    for document in documents
                    if document.get("kind") == "CiliumNetworkPolicy"
                    for rule in document["spec"].get("egress", [])
                    for target in rule.get("toFQDNs", [])
                )
            )

    def test_enabled_runner_is_permanently_suspended_and_bounded(self) -> None:
        runner = _named(
            self.enabled_dev,
            "CronJob",
            "citrus-dev-stripe-smoke-runner",
        )
        self.assertEqual(runner["spec"]["schedule"], "0 0 31 2 *")
        self.assertEqual(runner["spec"]["timeZone"], "Etc/UTC")
        self.assertIs(runner["spec"]["suspend"], True)
        self.assertEqual(runner["spec"]["concurrencyPolicy"], "Forbid")

        job = runner["spec"]["jobTemplate"]["spec"]
        self.assertEqual(job["activeDeadlineSeconds"], 900)
        self.assertEqual(job["backoffLimit"], 0)
        template = job["template"]
        self.assertIs(
            template["spec"]["automountServiceAccountToken"], False
        )
        self.assertEqual(template["spec"]["restartPolicy"], "Never")
        self.assertNotIn("serviceAccountName", template["spec"])

        container = template["spec"]["containers"][0]
        self.assertRegex(
            container["image"],
            r"^registry\.digitalocean\.com/sendouq/citrus:[0-9a-f]{40}$",
        )
        image_tag = container["image"].rsplit(":", 1)[1]
        self.assertEqual(
            runner["metadata"]["annotations"][
                "citrus.grace/verified-image-tag"
            ],
            image_tag,
        )
        self.assertEqual(
            container["command"],
            [
                "python",
                "manage.py",
                "run_stripe_smoke",
                "--receipt-path",
                "/tmp/citrus-stripe-smoke-receipt.json",
            ],
        )
        self.assertNotIn("envFrom", container)

    def test_runner_projects_only_named_runtime_and_dev_payment_roles(self) -> None:
        runner = _named(
            self.enabled_dev,
            "CronJob",
            "citrus-dev-stripe-smoke-runner",
        )
        container = _pod_template(runner)["spec"]["containers"][0]
        plain = _plain_env(container)
        image_tag = container["image"].rsplit(":", 1)[1]
        self.assertEqual(
            {
                key: plain[key]
                for key in (
                    "DJANGO_ENV",
                    "CITRUS_ENVIRONMENT_OWNER",
                    "CITRUS_STRIPE_SMOKE_RUNNER",
                    "CITRUS_STRIPE_SMOKE_EXPECTED_ACCOUNT_ID",
                    "CITRUS_EXPECTED_SOURCE_REVISION",
                    "PAYMENT_NETWORK_MODE",
                    "PAYMENT_EGRESS_POLICY_REQUIRED",
                    "PAYMENT_EGRESS_POLICY_PROVIDER",
                    "PAYMENT_EGRESS_POLICY_REVISION",
                )
            },
            {
                "DJANGO_ENV": "development",
                "CITRUS_ENVIRONMENT_OWNER": "citrus-dev",
                "CITRUS_STRIPE_SMOKE_RUNNER": "true",
                "CITRUS_STRIPE_SMOKE_EXPECTED_ACCOUNT_ID": (
                    SENTINEL_ACCOUNT_ID
                ),
                "CITRUS_EXPECTED_SOURCE_REVISION": image_tag,
                "PAYMENT_NETWORK_MODE": "allow",
                "PAYMENT_EGRESS_POLICY_REQUIRED": "true",
                "PAYMENT_EGRESS_POLICY_PROVIDER": "cilium",
                "PAYMENT_EGRESS_POLICY_REVISION": POLICY_REVISION,
            },
        )
        self.assertEqual(
            {
                key: plain[key]
                for key in (
                    "EMAIL_BACKEND",
                    "EMAIL_USE_AUTH",
                    "EMAIL_HOST",
                    "EMAIL_HOST_USER",
                    "EMAIL_HOST_PASSWORD",
                    "TWILIO_SMS_ENABLED",
                    "NTFY_NOTIFICATION_URL",
                    "NTFY_AUTH_TOKEN",
                    "DIRECT_ORDER_PAYMENT_SETUP_ENABLED",
                    "RECURRING_ORDER_ENROLLMENT_MODE",
                    "RECURRING_ORDER_ENROLLMENT_ALLOWLIST",
                )
            },
            {
                "EMAIL_BACKEND": (
                    "django.core.mail.backends.locmem.EmailBackend"
                ),
                "EMAIL_USE_AUTH": "false",
                "EMAIL_HOST": "",
                "EMAIL_HOST_USER": "",
                "EMAIL_HOST_PASSWORD": "",
                "TWILIO_SMS_ENABLED": "false",
                "NTFY_NOTIFICATION_URL": "",
                "NTFY_AUTH_TOKEN": "",
                "DIRECT_ORDER_PAYMENT_SETUP_ENABLED": "false",
                "RECURRING_ORDER_ENROLLMENT_MODE": "off",
                "RECURRING_ORDER_ENROLLMENT_ALLOWLIST": "",
            },
        )

        projected = {
            entry["name"]: entry["valueFrom"]["secretKeyRef"]
            for entry in container["env"]
            if "secretKeyRef" in entry.get("valueFrom", {})
        }
        self.assertEqual(
            set(projected),
            {
                "DJANGO_SECRET_KEY",
                "DB_HOST",
                "DB_PORT",
                "DB_NAME",
                "DB_USER",
                "DB_PASSWORD",
                "STRIPE_SECRET_KEY",
                "STRIPE_PUBLISHABLE_KEY",
                "STRIPE_WEBHOOK_SECRET_DEV",
            },
        )
        for name in ("STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY"):
            self.assertEqual(
                projected[name],
                {
                    "name": "citrus-dev-payment-credentials",
                    "key": name,
                    "optional": False,
                },
            )
        self.assertEqual(
            projected["STRIPE_WEBHOOK_SECRET_DEV"]["key"],
            "STRIPE_WEBHOOK_SECRET_DEV",
        )
        self.assertNotIn("STRIPE_WEBHOOK_SECRET_PROD", projected)
        config_projected = {
            entry["name"]: entry["valueFrom"]["configMapKeyRef"]
            for entry in container["env"]
            if "configMapKeyRef" in entry.get("valueFrom", {})
        }
        self.assertEqual(
            config_projected,
            {
                "DB_SCHEMA": {
                    "name": "django-config",
                    "key": "DB_SCHEMA",
                    "optional": False,
                }
            },
        )
        self.assertFalse(
            any(
                name.startswith("TWILIO_")
                and name != "TWILIO_SMS_ENABLED"
                for name in plain | projected
            )
        )

    def test_only_runner_pod_receives_smoke_marker_and_allow_mode(self) -> None:
        image_tag = YAML_PARSER.load(
            (CHART_PATH / "values-dev.yaml").read_text(encoding="utf-8")
        )["image"]["tag"]
        cases = (
            (
                "manual",
                self.enabled_dev,
                "citrus-dev-stripe-smoke-runner",
            ),
            (
                "automated",
                self.automated_dev,
                f"citrus-smoke-{image_tag}",
            ),
        )
        for name, documents, expected_name in cases:
            with self.subTest(mode=name):
                marked: list[tuple[str, str]] = []
                account_bound: list[tuple[str, str]] = []
                allow_mode: list[tuple[str, str]] = []
                for workload in documents:
                    if workload.get("kind") not in {
                        "Deployment",
                        "Job",
                        "CronJob",
                    }:
                        continue
                    for container in _pod_template(workload)["spec"].get(
                        "containers", []
                    ):
                        plain_env = _plain_env(container)
                        if plain_env.get("CITRUS_STRIPE_SMOKE_RUNNER") == "true":
                            marked.append(
                                (
                                    workload["metadata"]["name"],
                                    container["name"],
                                )
                            )
                        if "CITRUS_STRIPE_SMOKE_EXPECTED_ACCOUNT_ID" in plain_env:
                            self.assertEqual(
                                plain_env[
                                    "CITRUS_STRIPE_SMOKE_EXPECTED_ACCOUNT_ID"
                                ],
                                SENTINEL_ACCOUNT_ID,
                            )
                            account_bound.append(
                                (
                                    workload["metadata"]["name"],
                                    container["name"],
                                )
                            )
                        if plain_env.get("PAYMENT_NETWORK_MODE") == "allow":
                            allow_mode.append(
                                (
                                    workload["metadata"]["name"],
                                    container["name"],
                                )
                            )
                self.assertEqual(
                    marked,
                    [(expected_name, "stripe-smoke-runner")],
                )
                self.assertEqual(account_bound, marked)
                self.assertEqual(allow_mode, marked)

    def test_account_identity_gate_is_empty_by_default_and_mutation_honest(
        self,
    ) -> None:
        dev_values = YAML_PARSER.load(
            (CHART_PATH / "values-dev.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            dev_values["stripeSmokeRunner"]["expectedAccountId"],
            "",
        )
        self.assertIs(
            dev_values["stripeSmokeRunner"]["automation"]["enabled"],
            False,
        )

        missing = _run(_command(enabled=True, expected_account_id=None))
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(
            "stripeSmokeRunner.expectedAccountId is required when enabled",
            missing.stderr,
        )

        for invalid in (
            "acct_short",
            "acct_contains-hyphen",
            "acct_contains/slash",
            "cus_0000000000000000",
        ):
            with self.subTest(invalid=invalid):
                result = _run(
                    _command(enabled=True, expected_account_id=invalid)
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "stripeSmokeRunner.expectedAccountId",
                    result.stderr,
                )

        schema = json.loads(
            (CHART_PATH / "values.schema.json").read_text(encoding="utf-8")
        )
        runner_schema = schema["properties"]["stripeSmokeRunner"]
        self.assertIn("expectedAccountId", runner_schema["required"])
        self.assertEqual(
            runner_schema["properties"]["expectedAccountId"],
            {
                "type": "string",
                "pattern": "^$|^acct_[A-Za-z0-9]{8,64}$",
            },
        )

    def test_one_stripe_policy_selects_only_the_runner(self) -> None:
        dev_values = YAML_PARSER.load(
            (CHART_PATH / "values-dev.yaml").read_text(encoding="utf-8")
        )
        expected_database = dev_values["paymentSafety"]["networkPolicy"][
            "database"
        ]
        policies = [
            document
            for document in self.enabled_dev
            if document.get("kind") == "CiliumNetworkPolicy"
        ]
        stripe_policies = []
        for policy in policies:
            fqdn_hosts = {
                target["matchName"]
                for rule in policy["spec"].get("egress", [])
                for target in rule.get("toFQDNs", [])
            }
            if any(host.endswith(".stripe.com") for host in fqdn_hosts):
                stripe_policies.append((policy, fqdn_hosts))
        self.assertEqual(len(stripe_policies), 1)
        policy, fqdn_hosts = stripe_policies[0]
        self.assertEqual(
            policy["metadata"]["name"],
            "citrus-dev-stripe-smoke-runner-egress",
        )
        self.assertEqual(
            policy["metadata"]["annotations"][
                "citrus.grace/payment-egress-policy-revision"
            ],
            POLICY_REVISION,
        )
        self.assertEqual(
            policy["spec"]["endpointSelector"],
            {
                "matchLabels": {
                    "app.kubernetes.io/name": "citrus",
                    "app.kubernetes.io/instance": "citrus-dev",
                    "app.kubernetes.io/component": "stripe-smoke-runner",
                }
            },
        )
        egress = policy["spec"]["egress"]
        self.assertEqual(len(egress), 4)
        self.assertEqual(
            [set(rule) for rule in egress],
            [
                {"toEndpoints", "toPorts"},
                {"toFQDNs", "toPorts"},
                {"toServices", "toPorts"},
                {"toFQDNs", "toPorts"},
            ],
        )
        self.assertEqual(
            fqdn_hosts,
            {
                "api.stripe.com",
                expected_database["host"],
            },
        )
        database_rule = next(
            rule
            for rule in policy["spec"]["egress"]
            if {
                target["matchName"]
                for target in rule.get("toFQDNs", [])
            }
            == {expected_database["host"]}
        )
        self.assertEqual(
            database_rule["toPorts"],
            [
                {
                    "ports": [
                        {
                            "port": str(expected_database["port"]),
                            "protocol": "TCP",
                        }
                    ]
                }
            ],
        )
        dns_rule = next(
            rule
            for rule in policy["spec"]["egress"]
            if rule.get("toPorts", [{}])[0].get("rules", {}).get("dns")
        )
        self.assertEqual(
            dns_rule["toEndpoints"],
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
            dns_rule["toPorts"],
            [
                {
                    "ports": [
                        {"port": "53", "protocol": "UDP"},
                        {"port": "53", "protocol": "TCP"},
                    ],
                    "rules": {"dns": [{"matchPattern": "*"}]},
                }
            ],
        )
        stripe_rule = next(
            rule
            for rule in policy["spec"]["egress"]
            if {target["matchName"] for target in rule.get("toFQDNs", [])}
            == {"api.stripe.com"}
        )
        self.assertEqual(
            stripe_rule["toPorts"],
            [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        )
        service_rule = next(
            rule for rule in policy["spec"]["egress"] if "toServices" in rule
        )
        self.assertEqual(
            service_rule["toServices"],
            [
                {
                    "k8sService": {
                        "serviceName": "citrus-service",
                        "namespace": "citrus-dev",
                    }
                }
            ],
        )
        self.assertEqual(
            service_rule["toPorts"],
            [{"ports": [{"port": "80", "protocol": "TCP"}]}],
        )
        self.assertFalse(
            any("toEntities" in rule for rule in policy["spec"]["egress"])
        )

        for general in policies:
            if general is policy:
                continue
            self.assertNotIn(
                "stripe-smoke-runner",
                json.dumps(general["spec"]["endpointSelector"], sort_keys=True),
            )
            self.assertFalse(
                any(
                    target.get("matchName", "").endswith(".stripe.com")
                    for rule in general["spec"].get("egress", [])
                    for target in rule.get("toFQDNs", [])
                )
            )

    def test_enabled_runner_hard_fails_outside_dev_or_when_unsuspended(self) -> None:
        wrong_release = _command(enabled=True)
        wrong_release[wrong_release.index("citrus-dev")] = "citrus"
        result = _run(wrong_release)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "may be enabled only for release citrus-dev",
            result.stderr,
        )

        unsuspended = _command(enabled=True)
        unsuspended.extend(["--set", "stripeSmokeRunner.suspend=false"])
        result = _run(unsuspended)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stripeSmokeRunner.suspend", result.stderr)

        automation_only = _command(enabled=False)
        automation_only.extend(
            ["--set", "stripeSmokeRunner.automation.enabled=true"]
        )
        result = _run(automation_only)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "automation.enabled requires stripeSmokeRunner.enabled=true",
            result.stderr,
        )

    def test_manual_runner_keeps_stage_two_resources_absent(self) -> None:
        self.assertFalse(
            any(
                document.get("kind")
                in {"ServiceAccount", "Role", "RoleBinding", "ConfigMap"}
                and "stripe-smoke" in document.get("metadata", {}).get("name", "")
                for document in self.enabled_dev
            )
        )
        runner = _named(
            self.enabled_dev,
            "CronJob",
            "citrus-dev-stripe-smoke-runner",
        )
        self.assertNotIn("argocd.argoproj.io/hook", runner["metadata"]["annotations"])
        pod_spec = _pod_template(runner)["spec"]
        self.assertNotIn("serviceAccountName", pod_spec)
        self.assertIs(pod_spec["automountServiceAccountToken"], False)

    def test_automated_runner_is_exact_image_postsync_hook(self) -> None:
        image_tag = YAML_PARSER.load(
            (CHART_PATH / "values-dev.yaml").read_text(encoding="utf-8")
        )["image"]["tag"]
        job_name = f"citrus-smoke-{image_tag}"
        runner = _named(self.automated_dev, "Job", job_name)

        self.assertEqual(
            runner["metadata"]["annotations"]["argocd.argoproj.io/hook"],
            "PostSync",
        )
        self.assertEqual(
            runner["metadata"]["annotations"][
                "argocd.argoproj.io/hook-delete-policy"
            ],
            "HookSucceeded",
        )
        self.assertNotIn("ttlSecondsAfterFinished", runner["spec"])
        self.assertEqual(runner["spec"]["backoffLimit"], 0)
        self.assertEqual(runner["spec"]["activeDeadlineSeconds"], 900)
        self.assertEqual(
            runner["metadata"]["labels"]["citrus.grace/source-revision"],
            image_tag,
        )
        self.assertEqual(
            runner["metadata"]["labels"]["citrus.grace/smoke-step"],
            "claim",
        )
        self.assertEqual(
            runner["metadata"]["annotations"][
                "citrus.grace/payment-egress-policy-revision"
            ],
            AUTOMATION_POLICY_REVISION,
        )

        pod_spec = runner["spec"]["template"]["spec"]
        self.assertEqual(
            pod_spec["serviceAccountName"],
            "citrus-dev-stripe-smoke-runner",
        )
        self.assertIs(pod_spec["automountServiceAccountToken"], True)
        self.assertEqual(pod_spec["restartPolicy"], "Never")
        container = pod_spec["containers"][0]
        self.assertEqual(container["image"].rsplit(":", 1)[1], image_tag)
        plain = _plain_env(container)
        self.assertEqual(
            plain["CITRUS_STRIPE_SMOKE_RECEIPT_NAMESPACE"],
            "citrus-dev",
        )
        self.assertEqual(
            plain["CITRUS_STRIPE_SMOKE_RECEIPT_CONFIG_MAP"],
            "citrus-dev-stripe-smoke-receipts",
        )
        self.assertEqual(plain["CITRUS_STRIPE_SMOKE_JOB_NAME"], job_name)
        self.assertEqual(plain["CITRUS_EXPECTED_SOURCE_REVISION"], image_tag)
        self.assertEqual(
            plain["PAYMENT_EGRESS_POLICY_REVISION"],
            AUTOMATION_POLICY_REVISION,
        )

    def test_manual_and_automated_runtime_revisions_are_distinct(self) -> None:
        manual = _named(
            self.enabled_dev,
            "CronJob",
            "citrus-dev-stripe-smoke-runner",
        )
        image_tag = YAML_PARSER.load(
            (CHART_PATH / "values-dev.yaml").read_text(encoding="utf-8")
        )["image"]["tag"]
        automated = _named(
            self.automated_dev,
            "Job",
            f"citrus-smoke-{image_tag}",
        )

        manual_revision = _plain_env(
            _pod_template(manual)["spec"]["containers"][0]
        )["PAYMENT_EGRESS_POLICY_REVISION"]
        automated_revision = _plain_env(
            _pod_template(automated)["spec"]["containers"][0]
        )["PAYMENT_EGRESS_POLICY_REVISION"]
        self.assertEqual(manual_revision, POLICY_REVISION)
        self.assertEqual(automated_revision, AUTOMATION_POLICY_REVISION)
        self.assertNotEqual(manual_revision, automated_revision)

    def test_automated_runner_rbac_is_narrow_and_exact_sha_bound(self) -> None:
        image_tag = YAML_PARSER.load(
            (CHART_PATH / "values-dev.yaml").read_text(encoding="utf-8")
        )["image"]["tag"]
        job_name = f"citrus-smoke-{image_tag}"
        service_account = _named(
            self.automated_dev,
            "ServiceAccount",
            "citrus-dev-stripe-smoke-runner",
        )
        self.assertIs(service_account["automountServiceAccountToken"], False)

        role = _named(
            self.automated_dev,
            "Role",
            "citrus-dev-stripe-smoke-runner",
        )
        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "verbs": ["create"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": [
                        "citrus-dev-stripe-smoke-receipts"
                    ],
                    "verbs": ["get", "patch"],
                },
                {
                    "apiGroups": ["batch"],
                    "resources": ["jobs"],
                    "resourceNames": [job_name],
                    "verbs": ["patch"],
                },
            ],
        )
        binding = _named(
            self.automated_dev,
            "RoleBinding",
            "citrus-dev-stripe-smoke-runner",
        )
        self.assertEqual(
            binding["subjects"],
            [
                {
                    "kind": "ServiceAccount",
                    "name": "citrus-dev-stripe-smoke-runner",
                    "namespace": "citrus-dev",
                }
            ],
        )

    def test_automated_runner_adds_only_kube_apiserver_egress(self) -> None:
        policy = _named(
            self.automated_dev,
            "CiliumNetworkPolicy",
            "citrus-dev-stripe-smoke-runner-egress",
        )
        egress = policy["spec"]["egress"]
        self.assertEqual(len(egress), 5)
        kubernetes_rule = next(
            rule
            for rule in egress
            if rule.get("toEntities") == ["kube-apiserver"]
        )
        self.assertEqual(
            kubernetes_rule,
            {
                "toEntities": ["kube-apiserver"],
                "toPorts": [
                    {"ports": [{"port": "443", "protocol": "TCP"}]}
                ],
            },
        )
        entities = [
            entity
            for rule in egress
            for entity in rule.get("toEntities", [])
        ]
        self.assertEqual(entities, ["kube-apiserver"])
        self.assertTrue(
            {"all", "world", "cluster", "host", "remote-node"}.isdisjoint(
                entities
            )
        )

    def test_only_runner_template_names_the_stripe_api_hostname(self) -> None:
        occurrences = []
        for template in (CHART_PATH / "templates").glob("*.yaml"):
            if "api.stripe.com" in template.read_text(encoding="utf-8"):
                occurrences.append(template.name)
        self.assertEqual(occurrences, [TEMPLATE_PATH.name])


if __name__ == "__main__":
    unittest.main()
