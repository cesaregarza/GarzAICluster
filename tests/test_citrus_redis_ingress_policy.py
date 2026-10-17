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
# Their own egress policies admit no Redis path, so they stay outside.
NO_REDIS_EGRESS = {"stripe-smoke-runner", "sms-reconciliation"}
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
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        assert expression["operator"] == "In", expression
        if labels.get(expression["key"]) not in expression["values"]:
            return False
    return True


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
            "citrus-dev-direct-order-payment-sweep",
            "citrus-dev-media-requeue",
            "citrus-dev-media-gc",
            "citrus-dev-recurring-tick",
            "citrus-dev-recurring-health",
        })

    def test_peers_stay_in_the_policy_namespace(self):
        for documents in (render(*PROD), render(*DEV)):
            for peer in self.policy(documents)["spec"]["ingress"][0]["fromEndpoints"]:
                labels = peer.get("matchLabels") or {}
                keys = {expr["key"] for expr in peer.get("matchExpressions") or []}
                self.assertFalse(
                    any("namespace" in key for key in {*labels, *keys}), peer,
                )

    def test_redis_runs_patched_image_with_memory_cap(self):
        for documents in (render(*PROD), render(*DEV)):
            [container] = [
                template
                for name, template in pod_templates(documents)
                if name == "citrus-redis"
            ][0]["spec"]["containers"]
            self.assertEqual(container["image"], REDIS_IMAGE)
            self.assertEqual(
                container["args"],
                ["--maxmemory", "200mb", "--maxmemory-policy", "volatile-lru"],
            )


if __name__ == "__main__":
    unittest.main()
