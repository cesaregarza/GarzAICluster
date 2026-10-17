"""citrus-redis admits ingress only from the Citrus pods that use it (CES-1508).

Django's RedisCache unpickles cached values, so any pod that can write to this
Redis can run code in the web pod. These tests model effective admission:
the union of every ingress policy that selects the Redis pod, evaluated for
rendered workloads and for adversarial peers. The expected client set is
derived from each pod's rendered Redis configuration and its actual Cilium
egress, not from a fixed list.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm" / "citrus"
HELM = os.environ.get("HELM_BIN", "helm")
YAML_PARSER = YAML(typ="safe")
REDIS_ENV = ("CACHE_URL", "CELERY_BROKER_URL")
REDIS_PORT = "6379"
POLICY_KINDS = {"CiliumNetworkPolicy", "NetworkPolicy"}
NAMESPACE_LABEL = "io.kubernetes.pod.namespace"
DEPLOYMENT_APPS = ["citrus-web", "citrus-media-worker", "citrus-billing-worker"]
BATCH_COMPONENTS = [
    "migrations",
    "media-requeue",
    "media-gc",
    "recurring-preflight",
    "recurring-tick",
    "recurring-health",
]
REDIS_IMAGE = (
    "redis:7.2.16-alpine@sha256:"
    "29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0"
)


def render(release, namespace, *value_files, extra=()):
    command = [HELM, "template", release, str(CHART), "--namespace", namespace]
    for value_file in value_files:
        command.extend(["-f", str(CHART / value_file)])
    command.extend(extra)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [doc for doc in YAML_PARSER.load_all(result.stdout) if doc]


PROD = (
    "citrus",
    "default",
    "values.yaml",
    "values-payment-prod.yaml",
    "values-payment-prod-legacy.yaml",
)
DEV = (
    "citrus-dev",
    "citrus-dev",
    "values.yaml",
    "values-dev.yaml",
    "values-payment-dev.yaml",
    "values-recurring-dev.yaml",
    "values-stripe-smoke-dev.yaml",
)


def all_renders():
    """Both Argo stacks plus every optional mode that adds a workload."""
    from tests.test_citrus_sms_reconciliation import (
        _render as render_sms,
        _synthetic_render_kwargs,
    )
    from tests.test_citrus_stripe_smoke_runner import _command, _documents

    return {
        "prod": ("default", render(*PROD)),
        "dev": ("citrus-dev", render(*DEV)),
        "prod-sms": ("default", render_sms(**_synthetic_render_kwargs(dev=False))),
        "dev-sms": ("citrus-dev", render_sms(**_synthetic_render_kwargs(dev=True))),
        "dev-smoke": ("citrus-dev", _documents(_command(enabled=True))),
        "dev-smoke-automated": (
            "citrus-dev",
            _documents(_command(enabled=True, automated=True)),
        ),
    }


def pod_templates(documents):
    for doc in documents:
        kind = doc["kind"]
        if kind in {"Deployment", "Job"}:
            template = doc["spec"]["template"]
        elif kind == "CronJob":
            template = doc["spec"]["jobTemplate"]["spec"]["template"]
        else:
            continue
        yield doc["metadata"]["name"], template


def reads_redis_env(template, config_maps):
    containers = template["spec"]["containers"] + template["spec"].get(
        "initContainers", []
    )
    for container in containers:
        if any(env["name"] in REDIS_ENV for env in container.get("env", [])):
            return True
        for source in container.get("envFrom", []):
            ref = source.get("configMapRef")
            data = config_maps.get(ref["name"], {}) if ref else {}
            if any(key in data for key in REDIS_ENV):
                return True
    return False


def selects(selector, labels):
    """Kubernetes label-selector semantics; an empty selector selects all."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values") or []
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and key in labels and labels[key] in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
        if operator not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            raise AssertionError(f"unknown selector operator {operator}")
    return True


def cilium_peer_selects(selector, policy_namespace, labels, namespace):
    """A Cilium endpoint peer; without a namespace key it is policy-local."""

    def strip(key):
        return key.removeprefix("k8s:").removeprefix("any:")

    peer_namespace = policy_namespace
    match_labels = {}
    for key, value in (selector.get("matchLabels") or {}).items():
        if strip(key) == NAMESPACE_LABEL:
            peer_namespace = value
        else:
            match_labels[strip(key)] = value
    expressions = []
    for expression in selector.get("matchExpressions") or []:
        if strip(expression["key"]) == NAMESPACE_LABEL:
            return True  # namespace expressions are treated as broad
        expressions.append({**expression, "key": strip(expression["key"])})
    return namespace == peer_namespace and selects(
        {"matchLabels": match_labels, "matchExpressions": expressions}, labels
    )


def allows_port(rule, port):
    port_rules = rule.get("toPorts")
    if not port_rules:
        return True
    return any(
        str(entry.get("port")) in {port, "0"}
        for port_rule in port_rules
        for entry in port_rule.get("ports", [])
    )


def policy_target(policy):
    if policy["kind"] == "NetworkPolicy":
        return policy["spec"].get("podSelector") or {}
    return policy["spec"].get("endpointSelector") or {}


INGRESS_SPEC_KEYS = {"ingress", "ingressDeny", "enableDefaultDeny"}
SUPPORTED_PORTS = [{"ports": [{"port": REDIS_PORT, "protocol": "TCP"}]}]


def ingress_policies_for(documents, labels):
    """Every policy that selects the pod and says anything about its ingress."""
    return [
        policy
        for policy in documents
        if policy["kind"] in POLICY_KINDS
        and selects(policy_target(policy), labels)
        and (
            bool(INGRESS_SPEC_KEYS & set(policy["spec"]))
            or "Ingress" in policy["spec"].get("policyTypes", [])
            or (policy["kind"] == "NetworkPolicy" and "egress" not in policy["spec"])
        )
    ]


def unsupported_ingress_form(policy):
    """Why a policy selecting Redis falls outside the modelled shape, or None.

    The only modelled shape is a CiliumNetworkPolicy that keeps ingress
    default-deny and whose rules are explicit, non-empty fromEndpoints
    selectors on TCP 6379. Anything else is treated as admitting every peer,
    so an unmodelled allow can never be read as a denial.
    """
    if policy["kind"] != "CiliumNetworkPolicy":
        return f"{policy['kind']} is not modelled"
    spec = policy["spec"]
    if (spec.get("enableDefaultDeny") or {}).get("ingress", True) is not True:
        return "enableDefaultDeny.ingress is not true"
    if "ingressDeny" in spec:
        return "ingressDeny is not modelled"
    rules = spec.get("ingress")
    if not rules:
        return "no ingress rules"
    for rule in rules:
        if set(rule) != {"fromEndpoints", "toPorts"}:
            return f"rule keys {sorted(rule)} are not exactly fromEndpoints+toPorts"
        if rule["toPorts"] != SUPPORTED_PORTS:
            return f"toPorts {rule['toPorts']} is not exactly 6379/TCP"
        if not rule["fromEndpoints"]:
            return "empty fromEndpoints"
        for peer in rule["fromEndpoints"]:
            if not peer or set(peer) - {"matchLabels", "matchExpressions"} or not (
                peer.get("matchLabels") or peer.get("matchExpressions")
            ):
                return f"peer {peer} is not an explicit selector"
    return None


def effectively_admitted(documents, policy_namespace, redis_labels, labels, namespace):
    """Whether a peer may open TCP 6379 to Redis under every applicable policy."""
    policies = ingress_policies_for(documents, redis_labels)
    if not policies:
        return True  # no ingress isolation: everything is admitted
    if any(unsupported_ingress_form(policy) for policy in policies):
        return True  # fail closed on anything the model does not interpret
    return any(
        cilium_peer_selects(peer, policy_namespace, labels, namespace)
        for policy in policies
        for rule in policy["spec"]["ingress"]
        for peer in rule["fromEndpoints"]
    )


def has_redis_egress(documents, policy_namespace, labels, redis_labels):
    """Cilium egress: default allow until a policy selects the pod."""
    policies = [
        policy
        for policy in documents
        if policy["kind"] == "CiliumNetworkPolicy"
        and "egress" in policy["spec"]
        and selects(policy_target(policy), labels)
    ]
    if not policies:
        return True
    for policy in policies:
        for rule in policy["spec"]["egress"]:
            if not allows_port(rule, REDIS_PORT):
                continue
            if set(rule.get("toEntities") or []) & {"all", "cluster"}:
                return True
            for peer in rule.get("toEndpoints") or []:
                if cilium_peer_selects(peer, policy_namespace, redis_labels, policy_namespace):
                    return True
    return False


def redis_labels(documents):
    [redis] = [
        template for name, template in pod_templates(documents)
        if name == "citrus-redis"
    ]
    return redis["metadata"]["labels"]


@unittest.skipUnless(shutil.which(HELM) or Path(HELM).exists(), "helm is required")
class CitrusRedisIngressPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renders = all_renders()

    @staticmethod
    def policy(documents):
        [policy] = [
            doc
            for doc in documents
            if doc["kind"] == "CiliumNetworkPolicy"
            and doc["metadata"]["name"] == "citrus-redis-ingress"
        ]
        return policy

    def expected_and_admitted(self, namespace, documents):
        config_maps = {
            doc["metadata"]["name"]: doc.get("data", {})
            for doc in documents
            if doc["kind"] == "ConfigMap"
        }
        target = redis_labels(documents)
        result = {}
        for name, template in pod_templates(documents):
            if name == "citrus-redis":
                continue
            labels = template["metadata"]["labels"]
            expected = reads_redis_env(template, config_maps) and has_redis_egress(
                documents, namespace, labels, target
            )
            actual = effectively_admitted(documents, namespace, target, labels, namespace)
            result[name] = (expected, actual)
        return result

    def test_effective_admission_is_exactly_the_rendered_redis_clients(self):
        for environment, (namespace, documents) in self.renders.items():
            for name, (expected, actual) in self.expected_and_admitted(
                namespace, documents
            ).items():
                with self.subTest(environment=environment, pod=name):
                    self.assertEqual(actual, expected)

    def test_argo_stacks_admit_the_reviewed_client_sets(self):
        admitted = {
            environment: {
                name
                for name, (_, actual) in self.expected_and_admitted(
                    *self.renders[environment]
                ).items()
                if actual
            }
            for environment in ("prod", "dev")
        }
        self.assertEqual(admitted["prod"], {
            "citrus",
            "citrus-media-worker",
            "citrus-migrate-1",
            "citrus-media-requeue",
            "citrus-media-gc",
        })
        self.assertEqual(admitted["dev"], {
            "citrus-dev",
            "citrus-dev-media-worker",
            "citrus-dev-billing-worker",
            "citrus-dev-migrate-1",
            "citrus-dev-recurring-preflight",
            "citrus-dev-media-requeue",
            "citrus-dev-media-gc",
            "citrus-dev-recurring-tick",
            "citrus-dev-recurring-health",
        })

    def test_optional_modes_admit_no_new_workload(self):
        for environment in ("prod-sms", "dev-sms", "dev-smoke", "dev-smoke-automated"):
            namespace, documents = self.renders[environment]
            optional = {
                name for name, template in pod_templates(documents)
                if template["metadata"]["labels"].get("app.kubernetes.io/component")
                in {"sms-reconciliation", "stripe-smoke-runner"}
            }
            admitted = {
                name
                for name, (_, actual) in self.expected_and_admitted(
                    namespace, documents
                ).items()
                if actual
            }
            with self.subTest(environment=environment):
                self.assertTrue(optional)
                self.assertFalse(optional & admitted)

    def test_adversarial_peers_are_denied(self):
        for environment, (namespace, documents) in self.renders.items():
            release = redis_labels(documents)["app.kubernetes.io/instance"]
            target = redis_labels(documents)
            peers = {
                "unlabelled debug pod": ({}, namespace),
                "unrelated app": ({"app": "splattop"}, namespace),
                "foreign-release migrations": ({
                    "app.kubernetes.io/name": "citrus",
                    "app.kubernetes.io/instance": f"{release}-other",
                    "app.kubernetes.io/component": "migrations",
                }, namespace),
                "release labels without component": ({
                    "app.kubernetes.io/name": "citrus",
                    "app.kubernetes.io/instance": release,
                }, namespace),
                "smoke runner labels": ({
                    "app.kubernetes.io/name": "citrus",
                    "app.kubernetes.io/instance": release,
                    "app.kubernetes.io/component": "stripe-smoke-runner",
                }, namespace),
                "web labels in another namespace": (
                    {"app": "citrus-web"}, f"{namespace}-other",
                ),
                "migrations labels in another namespace": ({
                    "app.kubernetes.io/name": "citrus",
                    "app.kubernetes.io/instance": release,
                    "app.kubernetes.io/component": "migrations",
                }, f"{namespace}-other"),
            }
            for label, (labels, peer_namespace) in peers.items():
                with self.subTest(environment=environment, peer=label):
                    self.assertFalse(effectively_admitted(
                        documents, namespace, target, labels, peer_namespace,
                    ))

    def test_app_label_spoofing_caveat_is_explicit(self):
        # Acknowledged in CES-1508: Deployment pods carry only their app label,
        # so a same-namespace pod that sets one is admitted. Creating or
        # relabelling such a pod needs Kubernetes API write access.
        for environment, (namespace, documents) in self.renders.items():
            target = redis_labels(documents)
            for app in DEPLOYMENT_APPS:
                with self.subTest(environment=environment, app=app):
                    self.assertTrue(effectively_admitted(
                        documents, namespace, target, {"app": app}, namespace,
                    ))

    def test_policy_targets_exactly_the_redis_pod(self):
        for environment, (namespace, documents) in self.renders.items():
            release = redis_labels(documents)["app.kubernetes.io/instance"]
            selector = self.policy(documents)["spec"]["endpointSelector"]
            with self.subTest(environment=environment):
                self.assertEqual(selector, {"matchLabels": {
                    "app.kubernetes.io/name": "citrus",
                    "app.kubernetes.io/instance": release,
                    "app.kubernetes.io/component": "redis",
                }})
                targeted = {
                    name for name, template in pod_templates(documents)
                    if selects(selector, template["metadata"]["labels"])
                }
                self.assertEqual(targeted, {"citrus-redis"})
                for labels in (
                    {},
                    {"app": "citrus-web"},
                    {
                        "app.kubernetes.io/name": "citrus",
                        "app.kubernetes.io/instance": f"{release}-other",
                        "app.kubernetes.io/component": "redis",
                    },
                ):
                    self.assertFalse(selects(selector, labels), labels)

    def test_peer_selectors_are_pinned_to_this_release(self):
        for environment, (namespace, documents) in self.renders.items():
            release = redis_labels(documents)["app.kubernetes.io/instance"]
            [rule] = self.policy(documents)["spec"]["ingress"]
            with self.subTest(environment=environment):
                self.assertEqual(rule, {
                    "fromEndpoints": [
                        {"matchExpressions": [{
                            "key": "app",
                            "operator": "In",
                            "values": DEPLOYMENT_APPS,
                        }]},
                        {
                            "matchLabels": {
                                "app.kubernetes.io/name": "citrus",
                                "app.kubernetes.io/instance": release,
                            },
                            "matchExpressions": [{
                                "key": "app.kubernetes.io/component",
                                "operator": "In",
                                "values": BATCH_COMPONENTS,
                            }],
                        },
                    ],
                    "toPorts": [{"ports": [{"port": REDIS_PORT, "protocol": "TCP"}]}],
                })

    def test_redis_is_in_ingress_default_deny(self):
        # The isolation invariant: at least one policy puts Redis into ingress
        # default-deny, and every policy touching its ingress is in the one
        # shape the admission model interprets.
        for environment, (_, documents) in self.renders.items():
            policies = ingress_policies_for(documents, redis_labels(documents))
            with self.subTest(environment=environment):
                self.assertTrue(policies)
                self.assertEqual(
                    {
                        policy["metadata"]["name"]: unsupported_ingress_form(policy)
                        for policy in policies
                    },
                    {policy["metadata"]["name"]: None for policy in policies},
                )

    def test_no_other_policy_opens_ingress_to_redis(self):
        # Cilium and Kubernetes allows are additive: any other policy that
        # selects Redis with an ingress rule would widen who can connect.
        for environment, (_, documents) in self.renders.items():
            others = {
                policy["metadata"]["name"]
                for policy in ingress_policies_for(documents, redis_labels(documents))
            } - {"citrus-redis-ingress"}
            with self.subTest(environment=environment):
                self.assertEqual(others, set())

    def test_redis_runs_the_patched_image(self):
        # The authenticated start command and memory args are pinned in
        # tests/test_citrus_redis_auth.py.
        for environment, (_, documents) in self.renders.items():
            [container] = [
                template
                for name, template in pod_templates(documents)
                if name == "citrus-redis"
            ][0]["spec"]["containers"]
            with self.subTest(environment=environment):
                self.assertEqual(container["image"], REDIS_IMAGE)


if __name__ == "__main__":
    unittest.main()
