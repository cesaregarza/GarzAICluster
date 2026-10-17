"""citrus-redis admits ingress only from the Citrus pods that use it (CES-1508).

Django's RedisCache unpickles cached values, so any pod that can write to this
Redis can run code in the web pod. The policy must admit exactly the pods
that read CACHE_URL or CELERY_BROKER_URL and whose egress reaches Redis.
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
# Their own egress policies admit no Redis path, so they stay outside. The
# payment sweep uses neither the cache nor Celery and keeps its minimal
# secret projection, so it gets no Redis password either.
NO_REDIS_EGRESS = {"stripe-smoke-runner", "sms-reconciliation", "direct-order-payment-sweep"}
BATCH_COMPONENTS = [
    "migrations",
    "media-requeue",
    "media-gc",
    "recurring-preflight",
    "recurring-tick",
    "recurring-health",
    "direct-order-payment-sweep",
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
    for container in template["spec"]["containers"]:
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


def policy_target(policy):
    """The pod selector a Cilium or Kubernetes network policy applies to."""
    if policy["kind"] == "NetworkPolicy":
        return policy["spec"].get("podSelector") or {}
    return policy["spec"].get("endpointSelector") or {}


def opens_ingress(policy):
    spec = policy["spec"]
    if policy["kind"] == "NetworkPolicy":
        return "Ingress" in spec.get("policyTypes", ["Ingress"]) and bool(
            spec.get("ingress")
        )
    return bool(spec.get("ingress"))


def sms_enabled_renders():
    from tests.test_citrus_sms_reconciliation import (
        _render as render_sms,
        _synthetic_render_kwargs,
    )

    return {
        environment: render_sms(**_synthetic_render_kwargs(dev=dev))
        for environment, dev in (("prod-sms", False), ("dev-sms", True))
    }


@unittest.skipUnless(shutil.which(HELM) or Path(HELM).exists(), "helm is required")
class CitrusRedisIngressPolicyTests(unittest.TestCase):
    def policy(self, documents):
        [policy] = [
            doc
            for doc in documents
            if doc["kind"] == "CiliumNetworkPolicy"
            and doc["metadata"]["name"] == "citrus-redis-ingress"
        ]
        return policy

    def assert_admits_exactly_redis_clients(self, documents):
        policy = self.policy(documents)
        config_maps = {
            doc["metadata"]["name"]: doc.get("data", {})
            for doc in documents
            if doc["kind"] == "ConfigMap"
        }
        [redis] = [
            template
            for name, template in pod_templates(documents)
            if name == "citrus-redis"
        ]
        self.assertTrue(
            selects(policy["spec"]["endpointSelector"], redis["metadata"]["labels"])
        )
        [rule] = policy["spec"]["ingress"]
        self.assertEqual(
            rule["toPorts"], [{"ports": [{"port": "6379", "protocol": "TCP"}]}]
        )
        admitted = set()
        for name, template in pod_templates(documents):
            labels = template["metadata"]["labels"]
            component = labels.get("app.kubernetes.io/component")
            expected = (
                name != "citrus-redis"
                and component not in NO_REDIS_EGRESS
                and reads_redis_env(template, config_maps)
            )
            actual = any(selects(peer, labels) for peer in rule["fromEndpoints"])
            with self.subTest(pod=name):
                self.assertEqual(actual, expected)
            if actual:
                admitted.add(name)
        return admitted

    def test_production_admits_web_media_worker_and_jobs_only(self):
        admitted = self.assert_admits_exactly_redis_clients(render(*PROD))

        self.assertEqual(admitted, {
            "citrus",
            "citrus-media-worker",
            "citrus-migrate-1",
            "citrus-media-requeue",
            "citrus-media-gc",
        })

    def test_development_admits_every_redis_client_only(self):
        admitted = self.assert_admits_exactly_redis_clients(render(*DEV))

        self.assertEqual(admitted, {
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

    def all_renders(self):
        return {"prod": render(*PROD), "dev": render(*DEV), **sms_enabled_renders()}

    @staticmethod
    def redis_labels(documents):
        [redis] = [
            template for name, template in pod_templates(documents)
            if name == "citrus-redis"
        ]
        return redis["metadata"]["labels"]

    def test_policy_targets_exactly_the_redis_pod(self):
        for environment, documents in self.all_renders().items():
            labels = self.redis_labels(documents)
            release = labels["app.kubernetes.io/instance"]
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

    def test_peer_selectors_are_pinned_to_this_release(self):
        for environment, documents in self.all_renders().items():
            release = self.redis_labels(documents)["app.kubernetes.io/instance"]
            [rule] = self.policy(documents)["spec"]["ingress"]
            with self.subTest(environment=environment):
                self.assertEqual(rule["fromEndpoints"], [
                    {"matchExpressions": [{
                        "key": "app",
                        "operator": "In",
                        "values": [
                            "citrus-web",
                            "citrus-media-worker",
                            "citrus-billing-worker",
                        ],
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
                ])
                self.assertEqual(set(rule), {"fromEndpoints", "toPorts"})

    def test_no_other_policy_opens_ingress_to_redis(self):
        # Cilium allows are additive: any other policy selecting Redis with an
        # ingress rule would widen who can connect.
        for environment, documents in self.all_renders().items():
            labels = self.redis_labels(documents)
            for policy in documents:
                if policy["kind"] not in {"CiliumNetworkPolicy", "NetworkPolicy"}:
                    continue
                if policy["metadata"]["name"] == "citrus-redis-ingress":
                    continue
                with self.subTest(environment=environment, policy=policy["metadata"]["name"]):
                    self.assertFalse(
                        selects(policy_target(policy), labels) and opens_ingress(policy)
                    )

    def test_sms_enabled_renders_admit_exactly_redis_clients(self):
        for environment, documents in sms_enabled_renders().items():
            with self.subTest(environment=environment):
                self.assertIn(
                    "sms-reconciliation",
                    {
                        template["metadata"]["labels"].get("app.kubernetes.io/component")
                        for _, template in pod_templates(documents)
                    },
                )
                admitted = self.assert_admits_exactly_redis_clients(documents)
                self.assertFalse(any("sms" in name for name in admitted))

    def test_peers_stay_in_the_policy_namespace(self):
        for documents in (render(*PROD), render(*DEV)):
            for peer in self.policy(documents)["spec"]["ingress"][0]["fromEndpoints"]:
                labels = peer.get("matchLabels") or {}
                keys = {expr["key"] for expr in peer.get("matchExpressions") or []}
                self.assertFalse(
                    any("namespace" in key for key in {*labels, *keys}), peer,
                )

    def test_redis_runs_the_patched_image(self):
        for documents in (render(*PROD), render(*DEV)):
            [container] = [
                template
                for name, template in pod_templates(documents)
                if name == "citrus-redis"
            ][0]["spec"]["containers"]
            self.assertEqual(container["image"], REDIS_IMAGE)


if __name__ == "__main__":
    unittest.main()
