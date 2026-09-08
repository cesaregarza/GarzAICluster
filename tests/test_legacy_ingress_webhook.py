from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ruamel.yaml import YAML

from scripts import patch_legacy_ingress_webhook as helper


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "helm" / "splattop" / "files" / "ingress-nginx" / "controller-v1.0.0.yaml"
YAML_PARSER = YAML(typ="safe")


def target_webhook() -> dict:
    for document in YAML_PARSER.load_all(MANIFEST.read_text(encoding="utf-8")):
        if document and document.get("kind") == "ValidatingWebhookConfiguration":
            return next(webhook for webhook in document["webhooks"] if webhook["name"] == helper.WEBHOOK_NAME)
    raise AssertionError("validating webhook not found")


def ingress(*, class_name: str | None, annotation: str | None = None) -> dict:
    metadata = {}
    if annotation is not None:
        metadata["annotations"] = {helper.ANNOTATION_KEY: annotation}
    result = {"metadata": metadata, "spec": {}}
    if class_name is not None:
        result["spec"]["ingressClassName"] = class_name
    return result


class LegacyIngressWebhookTests(unittest.TestCase):
    def test_condition_truth_table_is_narrow(self) -> None:
        cases = (
            (ingress(class_name="traefik-nginx"), True),
            (ingress(class_name="traefik-nginx", annotation="traefik-nginx"), True),
            (ingress(class_name="traefik-nginx", annotation="nginx"), False),
            (ingress(class_name="legacy-nginx"), False),
            (ingress(class_name="nginx"), False),
            (ingress(class_name=None), False),
        )
        for candidate, expected_skip in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(helper.condition_skips(candidate), expected_skip)

    def test_source_has_only_the_target_condition_and_preserves_webhook_contract(self) -> None:
        webhook = target_webhook()
        self.assertEqual(webhook["failurePolicy"], "Fail")
        self.assertEqual(webhook["matchPolicy"], "Equivalent")
        self.assertEqual(webhook["rules"], [{
            "apiGroups": ["networking.k8s.io"], "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE"], "resources": ["ingresses"],
        }])
        self.assertEqual(webhook["clientConfig"]["service"], {
            "namespace": helper.WEBHOOK_NAMESPACE,
            "name": helper.WEBHOOK_SERVICE,
            "path": "/networking/v1/ingresses",
        })
        self.assertEqual(webhook["matchConditions"], [{
            "name": helper.CONDITION_NAME,
            "expression": helper.CONDITION_EXPRESSION,
        }])
        self.assertNotIn("namespaceSelector", webhook)
        self.assertNotIn("objectSelector", webhook)

    def test_patch_tests_identity_and_changes_only_match_conditions(self) -> None:
        old = target_webhook()
        operations = helper.build_patch(
            uid="webhook-uid", resource_version="7", index=0,
            old_conditions=[],
            new_conditions=[{"name": helper.CONDITION_NAME, "expression": helper.CONDITION_EXPRESSION}],
            has_conditions=False,
            rules=target_webhook()["rules"],
        )
        self.assertEqual([operation["op"] for operation in operations[:6]], ["test"] * 6)
        self.assertEqual(
            {operation["path"] for operation in operations if operation["op"] in {"add", "replace"}},
            {"/webhooks/0/matchConditions"},
        )
        self.assertEqual(old["name"], helper.WEBHOOK_NAME)
        self.assertNotIn("caBundle", str(operations))

    def test_validation_rejects_rules_or_client_service_drift(self) -> None:
        config = {
            "metadata": {"name": helper.CONFIG_NAME},
            "webhooks": [target_webhook()],
        }
        # The checked-in manifest has no metadata identity fields; the helper's
        # live validation must require them before any patch is built.
        with self.assertRaisesRegex(helper.GuardError, "UID or resourceVersion"):
            helper.validate_configuration(config)
        config["metadata"].update({"uid": "uid", "resourceVersion": "8"})
        config["webhooks"][0]["rules"][0]["resources"] = ["services"]
        with self.assertRaisesRegex(helper.GuardError, "rules changed"):
            helper.validate_configuration(config)

    def test_default_path_can_server_dry_run_without_ca_or_mutation(self) -> None:
        config = {
            "metadata": {"name": helper.CONFIG_NAME, "uid": "uid", "resourceVersion": "8"},
            "webhooks": [copy.deepcopy(target_webhook())],
        }
        config["webhooks"][0].pop("matchConditions")
        calls: list[bool] = []

        class FakeKubectl:
            def __init__(self, *_args: object) -> None:
                pass

            def current_context(self) -> str:
                return helper.CONTEXT

            def get(self) -> dict:
                return copy.deepcopy(config)

            def patch(self, operations: list[dict], *, dry_run: bool) -> dict:
                calls.append(dry_run)
                self.operations = operations
                admitted = copy.deepcopy(config)
                admitted["webhooks"][0]["matchConditions"] = [{"name": helper.CONDITION_NAME, "expression": helper.CONDITION_EXPRESSION}]
                return admitted

        original = helper.Kubectl
        helper.Kubectl = FakeKubectl  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as directory:
                result = helper._forward(SimpleNamespace(
                    kubectl="fake", context=helper.CONTEXT, apply=False,
                    receipt=Path(directory) / "receipt.json",
                ))
                self.assertEqual(result, 0)
                self.assertEqual(calls, [True])
        finally:
            helper.Kubectl = original  # type: ignore[assignment]

    def test_kubectl_server_dry_run_uses_self_contained_flags(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps({}), stderr="")

        original = helper.subprocess.run
        helper.subprocess.run = fake_run  # type: ignore[assignment]
        try:
            helper.Kubectl("kubectl", helper.CONTEXT).patch([], dry_run=True)
        finally:
            helper.subprocess.run = original  # type: ignore[assignment]
        command = calls[0]
        self.assertIn("--dry-run=server", command)
        self.assertIn("--output=json", command)
        self.assertNotIn("-o", command)
        self.assertIn("--patch=[]", command)
        self.assertIn("has(object.spec.ingressClassName)", helper.CONDITION_EXPRESSION)


if __name__ == "__main__":
    unittest.main()
