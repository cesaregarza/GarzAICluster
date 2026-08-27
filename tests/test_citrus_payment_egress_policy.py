from __future__ import annotations

import json
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
PROJECT_PATH = REPO_ROOT / "argocd" / "projects" / "splattop-project.yaml"
RUNBOOK_PATH = (
    REPO_ROOT / "docs" / "runbooks" / "citrus-payment-egress-safety.md"
)
YAML_PARSER = YAML(typ="safe")
APP_IMAGE = "registry.digitalocean.com/sendouq/citrus:"
ATTESTATION = {
    "DJANGO_ENV",
    "CITRUS_ENVIRONMENT_OWNER",
    "PAYMENT_NETWORK_MODE",
    "PAYMENT_EGRESS_POLICY_REQUIRED",
    "PAYMENT_EGRESS_POLICY_PROVIDER",
    "PAYMENT_EGRESS_POLICY_REVISION",
}
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


def _helm_command(*, development: bool, enabled: bool) -> list[str]:
    command = [
        "helm",
        "template",
        "citrus-dev" if development else "citrus",
        str(CHART_PATH),
        "--namespace",
        "citrus-dev" if development else "default",
    ]
    if development:
        command.extend(["-f", str(DEV_VALUES)])
    if not enabled:
        return command

    environment = "development" if development else "production"
    owner = "citrus-dev" if development else "citrus"
    mode = "deny" if development else "allow"
    command.extend(
        [
            "--set",
            "paymentSafety.enabled=true",
            "--set-string",
            f"paymentSafety.environment={environment}",
            "--set-string",
            f"paymentSafety.owner={owner}",
            "--set-string",
            f"paymentSafety.networkMode={mode}",
            "--set",
            "paymentSafety.policy.required=true",
            "--set-string",
            "paymentSafety.policy.provider=cilium",
            "--set-string",
            "paymentSafety.policy.revision=ces-845-test",
            "--set",
            "paymentSafety.networkPolicy.enabled=true",
        ]
    )
    if development:
        command.extend(
            [
                "--set-string",
                "paymentSafety.networkPolicy.database.host=db.dev.example",
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


def _replace_flag_value(
    command: list[str], flag: str, value: str
) -> list[str]:
    replaced = list(command)
    replaced[replaced.index(flag) + 1] = value
    return replaced


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
    kind = document["kind"]
    if kind == "Deployment":
        return document["spec"]["template"]
    if kind == "Job":
        return document["spec"]["template"]
    if kind == "CronJob":
        return document["spec"]["jobTemplate"]["spec"]["template"]
    raise AssertionError(f"unsupported workload kind: {kind}")


def _app_workloads(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    workloads = []
    for document in documents:
        if document.get("kind") not in {"Deployment", "Job", "CronJob"}:
            continue
        containers = _pod_template(document)["spec"].get("containers", [])
        if any(
            str(container.get("image", "")).startswith(APP_IMAGE)
            for container in containers
        ):
            workloads.append(document)
    return workloads


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {
        entry["name"]: str(entry.get("value", ""))
        for entry in container.get("env", [])
        if "name" in entry and "value" in entry
    }


def _selector_matches(labels: dict[str, str], selector: dict[str, Any]) -> bool:
    if any(
        labels.get(key) != value
        for key, value in selector.get("matchLabels", {}).items()
    ):
        return False
    for expression in selector.get("matchExpressions", []):
        if expression.get("operator") != "In":
            raise AssertionError(
                f"unsupported selector operator: {expression.get('operator')}"
            )
        if labels.get(expression["key"]) not in expression.get("values", []):
            return False
    return True


class CitrusPaymentEgressPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.default_prod = _documents(
            _helm_command(development=False, enabled=False)
        )
        cls.default_dev = _documents(
            _helm_command(development=True, enabled=False)
        )

        legacy_dev_command = _helm_command(development=True, enabled=False)
        legacy_dev_command.extend(
            [
                "--set",
                "billingWorker.enabled=true",
                "--set",
                "recurringRuntime.enabled=true",
            ]
        )
        cls.legacy_dev = _documents(legacy_dev_command)

        dev_command = _helm_command(development=True, enabled=True)
        dev_command.extend(
            [
                "--set",
                "billingWorker.enabled=true",
                "--set",
                "recurringRuntime.enabled=true",
                "--set-json",
                (
                    "paymentSafety.networkPolicy.additionalExternalEgress="
                    '[{"name":"captcha","host":"captcha.internal.example",'
                    '"ports":[443]},{"name":"notify",'
                    '"host":"notify.internal.example","ports":[443,8443]}]'
                ),
            ]
        )
        cls.enforced_dev = _documents(dev_command)

        prod_command = _helm_command(development=False, enabled=True)
        prod_command.extend(
            [
                "--set",
                "billingWorker.enabled=true",
                "--set",
                "recurringRuntime.enabled=true",
            ]
        )
        cls.explicit_prod = _documents(prod_command)

    def test_current_argo_renders_remain_inert(self) -> None:
        for documents in (self.default_prod, self.default_dev):
            self.assertFalse(
                any(
                    document.get("kind") == "CiliumNetworkPolicy"
                    for document in documents
                )
            )
            for workload in _app_workloads(documents):
                template = _pod_template(workload)
                self.assertNotIn(
                    "citrus.grace/payment-egress-boundary",
                    template.get("metadata", {}).get("labels", {}),
                )
                for container in template["spec"]["containers"]:
                    if not str(container.get("image", "")).startswith(APP_IMAGE):
                        continue
                    self.assertTrue(
                        ATTESTATION.isdisjoint(_env(container)),
                        f"{workload['metadata']['name']}/{container['name']}",
                    )

        for application in ("citrus.yaml", "citrus-dev.yaml"):
            contents = (
                REPO_ROOT / "argocd" / "applications" / application
            ).read_text(encoding="utf-8")
            self.assertNotIn("payment-safety", contents)

    def test_development_attestation_covers_every_citrus_process(self) -> None:
        expected_workloads = {
            "citrus-dev",
            "citrus-dev-media-worker",
            "citrus-dev-billing-worker",
            "citrus-dev-migrate-1",
            "citrus-dev-media-requeue",
            "citrus-dev-media-gc",
            "citrus-dev-recurring-tick",
            "citrus-dev-recurring-health",
        }
        workloads = _app_workloads(self.enforced_dev)
        self.assertEqual(
            {workload["metadata"]["name"] for workload in workloads},
            expected_workloads,
        )

        expected_env = {
            "DJANGO_ENV": "development",
            "CITRUS_ENVIRONMENT_OWNER": "citrus-dev",
            "PAYMENT_NETWORK_MODE": "deny",
            "PAYMENT_EGRESS_POLICY_REQUIRED": "true",
            "PAYMENT_EGRESS_POLICY_PROVIDER": "cilium",
            "PAYMENT_EGRESS_POLICY_REVISION": "ces-845-test",
        }
        container_count = 0
        for workload in workloads:
            template = _pod_template(workload)
            self.assertEqual(
                template["metadata"]["labels"].get(
                    "citrus.grace/payment-egress-boundary"
                ),
                "enabled",
            )
            for container in template["spec"]["containers"]:
                if not str(container.get("image", "")).startswith(APP_IMAGE):
                    continue
                container_count += 1
                actual_env = _env(container)
                self.assertEqual(
                    {name: actual_env.get(name) for name in ATTESTATION},
                    expected_env,
                    f"{workload['metadata']['name']}/{container['name']}",
                )
        self.assertEqual(container_count, 9)

        redis = _named(self.enforced_dev, "Deployment", "citrus-redis")
        self.assertNotIn(
            "citrus.grace/payment-egress-boundary",
            redis["spec"]["template"]["metadata"]["labels"],
        )

    def test_policy_selectors_cover_pre_activation_and_enabled_pods(self) -> None:
        policies = [
            document
            for document in self.enforced_dev
            if document.get("kind") == "CiliumNetworkPolicy"
        ]
        self.assertEqual(
            {policy["metadata"]["name"] for policy in policies},
            {
                "citrus-dev-payment-egress",
                "citrus-dev-payment-egress-batch",
            },
        )
        selectors = [policy["spec"]["endpointSelector"] for policy in policies]

        for documents, phase in (
            (self.legacy_dev, "pre-activation"),
            (self.enforced_dev, "enabled"),
        ):
            for workload in _app_workloads(documents):
                labels = _pod_template(workload)["metadata"]["labels"]
                self.assertTrue(
                    any(
                        _selector_matches(labels, selector)
                        for selector in selectors
                    ),
                    f"{phase} {workload['metadata']['name']} is unselected",
                )

            redis = _named(documents, "Deployment", "citrus-redis")
            redis_labels = redis["spec"]["template"]["metadata"]["labels"]
            self.assertFalse(
                any(
                    _selector_matches(redis_labels, selector)
                    for selector in selectors
                ),
                f"{phase} Redis must remain outside the payment boundary",
            )

    def test_development_policy_allows_only_declared_non_stripe_dependencies(
        self,
    ) -> None:
        policy = _named(
            self.enforced_dev,
            "CiliumNetworkPolicy",
            "citrus-dev-payment-egress",
        )
        self.assertEqual(
            policy["metadata"]["annotations"][
                "argocd.argoproj.io/sync-wave"
            ],
            "-1",
        )
        self.assertEqual(
            policy["metadata"]["annotations"][
                "citrus.grace/payment-egress-policy-revision"
            ],
            "ces-845-test",
        )
        self.assertEqual(
            policy["spec"]["endpointSelector"],
            {
                "matchExpressions": [
                    {
                        "key": "app",
                        "operator": "In",
                        "values": [
                            "citrus-web",
                            "citrus-media-worker",
                            "citrus-billing-worker",
                        ],
                    }
                ]
            },
        )
        self.assertNotIn("ingress", policy["spec"])
        self.assertNotIn("egressDeny", policy["spec"])

        egress = policy["spec"]["egress"]
        self.assertFalse(any("toEntities" in rule for rule in egress))
        fqdn_hosts = {
            target["matchName"]
            for rule in egress
            for target in rule.get("toFQDNs", [])
        }
        self.assertEqual(
            fqdn_hosts,
            {
                "db.dev.example",
                "nyc3.digitaloceanspaces.com",
                "citrus-media-dev.nyc3.digitaloceanspaces.com",
                "citrus-media-dev.nyc3.cdn.digitaloceanspaces.com",
                "captcha.internal.example",
                "notify.internal.example",
            },
        )
        self.assertFalse(
            any(
                host == "stripe.com"
                or host.endswith(".stripe.com")
                or host == "stripe.network"
                or host.endswith(".stripe.network")
                for host in fqdn_hosts
            )
        )

        dns_rule = next(
            rule
            for rule in egress
            if rule.get("toPorts", [{}])[0].get("rules", {}).get("dns")
        )
        self.assertEqual(
            dns_rule["toEndpoints"][0]["matchLabels"],
            {
                "k8s:io.kubernetes.pod.namespace": "kube-system",
                "k8s:k8s-app": "kube-dns",
            },
        )
        self.assertEqual(
            dns_rule["toPorts"][0]["rules"]["dns"],
            [{"matchPattern": "*"}],
        )

        redis_rule = next(
            rule
            for rule in egress
            if rule.get("toEndpoints", [{}])[0]
            .get("matchLabels", {})
            .get("k8s:app.kubernetes.io/instance")
            == "citrus-dev"
        )
        redis = _named(self.enforced_dev, "Deployment", "citrus-redis")
        redis_labels = redis["spec"]["template"]["metadata"]["labels"]
        redis_destination_labels = redis_rule["toEndpoints"][0]["matchLabels"]
        self.assertEqual(
            redis_destination_labels["k8s:io.kubernetes.pod.namespace"],
            "citrus-dev",
        )
        for label in (
            "app.kubernetes.io/name",
            "app.kubernetes.io/instance",
            "app.kubernetes.io/component",
        ):
            with self.subTest(redis_label=label):
                self.assertEqual(
                    redis_destination_labels[f"k8s:{label}"],
                    redis_labels[label],
                )
        self.assertEqual(
            redis_rule["toPorts"][0]["ports"],
            [{"port": "6379", "protocol": "TCP"}],
        )

    def test_production_requires_and_renders_explicit_allow_mode(self) -> None:
        policies = [
            document
            for document in self.explicit_prod
            if document.get("kind") == "CiliumNetworkPolicy"
        ]
        self.assertEqual(len(policies), 2)
        for policy in policies:
            self.assertEqual(
                policy["spec"]["egress"],
                [{"toEntities": ["all"]}],
            )
        for workload in _app_workloads(self.explicit_prod):
            for container in _pod_template(workload)["spec"]["containers"]:
                if not str(container.get("image", "")).startswith(APP_IMAGE):
                    continue
                actual_env = _env(container)
                self.assertEqual(actual_env["DJANGO_ENV"], "production")
                self.assertEqual(
                    actual_env["CITRUS_ENVIRONMENT_OWNER"], "citrus"
                )
                self.assertEqual(actual_env["PAYMENT_NETWORK_MODE"], "allow")

    def test_enabled_contract_fails_closed_on_unsafe_values(self) -> None:
        dev = _helm_command(development=True, enabled=True)
        prod = _helm_command(development=False, enabled=True)
        cases = (
            (
                [*dev[:2], "citrus", *dev[3:]],
                "development payment safety requires release citrus-dev",
            ),
            (
                _replace_flag_value(dev, "--namespace", "default"),
                "development payment safety requires release citrus-dev",
            ),
            (
                [*prod[:2], "citrus-dev", *prod[3:]],
                "production payment safety requires release citrus",
            ),
            (
                _replace_flag_value(prod, "--namespace", "citrus-dev"),
                "production payment safety requires release citrus",
            ),
            (
                [*dev, "--set-string", "paymentSafety.owner=citrus"],
                "paymentSafety.owner",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.networkMode=allow",
                ],
                "paymentSafety.networkMode",
            ),
            (
                [*prod, "--set-string", "paymentSafety.networkMode=deny"],
                "paymentSafety.networkMode",
            ),
            (
                [
                    *dev,
                    "--set",
                    "paymentSafety.networkPolicy.enabled=false",
                ],
                "paymentSafety.networkPolicy.enabled",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.policy.provider=",
                ],
                "paymentSafety.policy.provider",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.policy.revision=not safe",
                ],
                "paymentSafety.policy.revision",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.networkPolicy.database.host=",
                ],
                "paymentSafety.networkPolicy.database.host",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.networkPolicy.database.host=*.example.com",
                ],
                "paymentSafety.networkPolicy.database.host",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.networkPolicy.database.host=203.0.113.10",
                ],
                "not an IP address or wildcard",
            ),
            (
                [
                    *dev,
                    "--set-json",
                    (
                        "paymentSafety.networkPolicy.additionalExternalEgress="
                        '[{"name":"forbidden","host":"api.stripe.com",'
                        '"ports":[443]}]'
                    ),
                ],
                "must not authorize a Stripe destination",
            ),
            (
                [*dev, "--set", "redis.enabled=false"],
                "redis.enabled must be true",
            ),
            (
                [*dev, "--set", "redis.service.port=0"],
                "redis.service.port",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.networkPolicy.syncWave=0",
                ],
                "paymentSafety.networkPolicy.syncWave",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "application.configData.AWS_S3_ENDPOINT_URL=http://storage.example",
                ],
                "application.configData.AWS_S3_ENDPOINT_URL",
            ),
            (
                [
                    *dev,
                    "--set-string",
                    "paymentSafety.unexpected=value",
                ],
                "unexpected",
            ),
        )
        for command, expected in cases:
            with self.subTest(expected=expected):
                result = _run(command)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(expected, _normalize_helm_error(result.stderr))

    def test_development_rejects_ip_literals_and_invalid_dns_labels(
        self,
    ) -> None:
        dev = _helm_command(development=True, enabled=True)
        for host in (
            "203.0.113.10",
            "127.1",
            "2130706433",
            "0127.0.0.1",
            "0x7f000001",
            "0x7f.0.0.1",
            "foo.-bar.example",
            "foo-.bar.example",
            f"{'a' * 64}.example",
        ):
            with self.subTest(host=host):
                result = _run([
                    *dev,
                    "--set-string",
                    f"paymentSafety.networkPolicy.database.host={host}",
                ])
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(
                    "lowercase exact DNS hostname with valid labels",
                    _normalize_helm_error(result.stderr),
                )

    def test_helm_schema_paths_are_normalized_without_changing_messages(
        self,
    ) -> None:
        hosted = (
            "- at '/paymentSafety/networkPolicy/database/host': "
            "minLength: got 0, want 1"
        )
        local = "paymentSafety.networkPolicy.syncWave must precede config"
        self.assertIn(
            "paymentSafety.networkPolicy.database.host",
            _normalize_helm_error(hosted),
        )
        self.assertEqual(_normalize_helm_error(local), local)

    def test_schema_and_argo_project_cover_the_namespaced_crd(self) -> None:
        schema = json.loads(
            (CHART_PATH / "values.schema.json").read_text(encoding="utf-8")
        )
        payment_safety = schema["properties"]["paymentSafety"]
        self.assertFalse(payment_safety["additionalProperties"])
        self.assertEqual(
            payment_safety["properties"]["networkMode"]["enum"],
            ["", "deny", "allow"],
        )

        project = YAML_PARSER.load(PROJECT_PATH.read_text(encoding="utf-8"))
        self.assertIn(
            {"group": "cilium.io", "kind": "CiliumNetworkPolicy"},
            project["spec"]["namespaceResourceWhitelist"],
        )
        self.assertNotIn(
            {"group": "cilium.io", "kind": "CiliumNetworkPolicy"},
            project["spec"]["clusterResourceWhitelist"],
        )

    def test_runbook_records_activation_and_zero_provider_boundaries(self) -> None:
        runbook = " ".join(
            RUNBOOK_PATH.read_text(encoding="utf-8").split()
        )
        for required in (
            "disabled by default",
            "Never use a Stripe request to validate this policy",
            "attestation alone does not prove that the live Cilium policy",
            "cannot read the secret-backed `DB_HOST`",
            "Do not activate it until an operator supplies the exact database FQDN",
            "local fake endpoint/FQDN omitted from the allowlist",
            "Do not remove the Cilium boundary while payment credentials remain available",
        ):
            with self.subTest(required=required):
                self.assertIn(required, runbook)


if __name__ == "__main__":
    unittest.main()
