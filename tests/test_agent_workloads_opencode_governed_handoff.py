from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "agent-workloads"
PRODUCTION_VALUES_PATH = REPO_ROOT / "apps" / "agent-workloads" / "values.yaml"
YAML_PARSER = YAML(typ="safe")


def _load_values() -> dict[str, Any]:
    value = YAML_PARSER.load(PRODUCTION_VALUES_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("production values must contain a mapping")
    return value


def _render_process(values: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    with tempfile.TemporaryDirectory() as temp_dir:
        values_path = Path(temp_dir) / "values.yaml"
        with values_path.open("w", encoding="utf-8") as values_file:
            YAML_PARSER.dump(values, values_file)
        return subprocess.run(
            [
                "helm",
                "template",
                "agent-workloads",
                str(CHART_PATH),
                "-f",
                str(values_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )


def _render(values: dict[str, Any]) -> list[dict[str, Any]]:
    result = _render_process(values)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _find(
    documents: list[dict[str, Any]],
    *,
    kind: str,
    name: str,
) -> dict[str, Any]:
    for document in documents:
        if (
            document.get("kind") == kind
            and document.get("metadata", {}).get("name") == name
        ):
            return document
    raise AssertionError(f"{kind}/{name} not rendered")


def _container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        if container["name"] == name:
            return container
    raise AssertionError(f"container {name} not rendered")


def _environment(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _ports(policy: dict[str, Any]) -> set[tuple[int, str]]:
    return {
        (port["port"], port.get("protocol", "TCP"))
        for rule in policy["spec"]["egress"]
        for port in rule.get("ports", [])
    }


def _component_egress_rules(
    policy: dict[str, Any],
    component: str,
) -> list[dict[str, Any]]:
    return [
        rule
        for rule in policy["spec"]["egress"]
        if any(
            destination.get("podSelector", {})
            .get("matchLabels", {})
            .get("app.kubernetes.io/component")
            == component
            for destination in rule.get("to", [])
        )
    ]


def _service_account_name(
    *,
    worker_id: str,
    release: dict[str, str],
    prefix: str = "agent-workloads",
) -> str:
    payload = {
        "schema_version": "workload_identity_bundle.v1",
        "code_digest": release["codeDigest"],
        "manifest_digest": release["manifestDigest"],
        "image_digest": release["imageDigest"],
    }
    suffix = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    normalized_worker = worker_id.replace(".", "-").replace("_", "-")
    return f"{prefix}-{normalized_worker}-{suffix}"


class AgentWorkloadsOpenCodeGovernedHandoffTests(unittest.TestCase):
    def _governed_values(self) -> dict[str, Any]:
        values = copy.deepcopy(_load_values())
        values["opencodeArtifactHandoff"] = {"mode": "governedCore"}
        return values

    def test_production_remains_explicit_legacy_rollback_mode(self) -> None:
        values = _load_values()
        documents = _render(values)
        opencode_deployments = [
            document
            for document in documents
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name", "").startswith(
                "agent-workloads-opencode"
            )
        ]

        self.assertEqual(
            values["opencodeArtifactHandoff"]["mode"],
            "legacySharedVolume",
        )
        self.assertEqual(
            [document["metadata"]["name"] for document in opencode_deployments],
            ["agent-workloads-opencode-proposer"],
        )
        pod = opencode_deployments[0]["spec"]["template"]["spec"]
        self.assertEqual(
            {container["name"] for container in pod["containers"]},
            {"opencode-proposer", "opencode-apply-executor"},
        )
        self.assertIn("proposals", {volume["name"] for volume in pod["volumes"]})

    def test_governed_hmac_mode_splits_identity_storage_and_egress(self) -> None:
        values = self._governed_values()
        documents = _render(values)
        proposer = _find(
            documents,
            kind="Deployment",
            name="agent-workloads-opencode-proposer",
        )
        apply = _find(
            documents,
            kind="Deployment",
            name="agent-workloads-opencode-apply-executor",
        )
        proposer_pod = proposer["spec"]["template"]["spec"]
        apply_pod = apply["spec"]["template"]["spec"]

        self.assertEqual(
            [container["name"] for container in proposer_pod["containers"]],
            ["opencode-proposer"],
        )
        self.assertEqual(
            [container["name"] for container in apply_pod["containers"]],
            ["opencode-apply-executor"],
        )
        self.assertIs(proposer_pod["automountServiceAccountToken"], False)
        self.assertIs(apply_pod["automountServiceAccountToken"], False)
        self.assertNotEqual(
            proposer_pod["serviceAccountName"],
            apply_pod["serviceAccountName"],
        )

        accounts = {
            document["metadata"]["name"]: document
            for document in documents
            if document.get("kind") == "ServiceAccount"
        }
        for worker_id, pod in (
            ("opencode.proposer", proposer_pod),
            ("opencode.apply_executor", apply_pod),
        ):
            account = accounts[pod["serviceAccountName"]]
            self.assertEqual(
                account["metadata"]["labels"]["mandate.cesaregarza.io/worker-id"],
                worker_id,
            )
            self.assertIs(account["automountServiceAccountToken"], False)

        proposer_env = _environment(_container(proposer, "opencode-proposer"))
        apply_env = _environment(_container(apply, "opencode-apply-executor"))
        self.assertEqual(
            proposer_env["AGENT_WORKLOADS_OPENCODE_ARTIFACT_HANDOFF_MODE"]["value"],
            "governed_core",
        )
        self.assertEqual(
            apply_env["AGENT_WORKLOADS_OPENCODE_ARTIFACT_HANDOFF_MODE"]["value"],
            "governed_core",
        )
        self.assertEqual(
            proposer_env["MANDATE_WORKLOAD_IDENTITY_TOKEN"]["valueFrom"][
                "secretKeyRef"
            ]["key"],
            "OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN",
        )
        self.assertEqual(
            apply_env["MANDATE_WORKLOAD_IDENTITY_TOKEN"]["valueFrom"]["secretKeyRef"][
                "key"
            ],
            "OPENCODE_APPLY_EXECUTOR_WORKLOAD_IDENTITY_TOKEN",
        )
        self.assertNotIn("MANDATE_MODEL_GATEWAY_BASE_URL", apply_env)
        self.assertNotIn("AGENT_WORKLOADS_OPENCODE_APPLY_PROPOSALS_DIR", apply_env)

        proposer_mounts = {
            mount["mountPath"]
            for mount in _container(proposer, "opencode-proposer")["volumeMounts"]
        }
        apply_mounts = {
            mount["mountPath"]
            for mount in _container(apply, "opencode-apply-executor")["volumeMounts"]
        }
        self.assertIn("/workspace/proposals", proposer_mounts)
        self.assertNotIn("/workspace/proposals", apply_mounts)
        self.assertNotIn(
            "proposer-artifacts",
            {volume["name"] for volume in apply_pod["volumes"]},
        )
        self.assertFalse(
            any("secret" in volume for volume in proposer_pod["volumes"])
        )
        self.assertFalse(any("secret" in volume for volume in apply_pod["volumes"]))

        proposer_policy = _find(
            documents,
            kind="NetworkPolicy",
            name="agent-workloads-opencode-proposer",
        )
        apply_policy = _find(
            documents,
            kind="NetworkPolicy",
            name="agent-workloads-opencode-apply-executor",
        )
        self.assertIn((8000, "TCP"), _ports(proposer_policy))
        self.assertIn((8000, "TCP"), _ports(apply_policy))
        self.assertIn((80, "TCP"), _ports(proposer_policy))
        self.assertIn((80, "TCP"), _ports(apply_policy))
        self.assertEqual(
            len(_component_egress_rules(proposer_policy, "api")),
            1,
        )
        self.assertEqual(
            len(_component_egress_rules(proposer_policy, "model-gateway")),
            1,
        )
        apply_api_rules = _component_egress_rules(apply_policy, "api")
        self.assertEqual(len(apply_api_rules), 1)
        self.assertEqual(
            {
                (port["port"], port.get("protocol", "TCP"))
                for port in apply_api_rules[0]["ports"]
            },
            {(80, "TCP"), (8000, "TCP")},
        )
        self.assertEqual(
            _component_egress_rules(apply_policy, "model-gateway"),
            [],
        )

    def test_governed_projected_mode_mounts_only_each_pods_token(self) -> None:
        values = self._governed_values()
        for worker_key in ("opencodeProposer", "opencodeApplyExecutor"):
            values[worker_key]["identity"] = {"mode": "projected"}
            values[worker_key]["secretEnv"] = {}

        documents = _render(values)
        for deployment_name, container_name in (
            ("agent-workloads-opencode-proposer", "opencode-proposer"),
            (
                "agent-workloads-opencode-apply-executor",
                "opencode-apply-executor",
            ),
        ):
            deployment = _find(
                documents,
                kind="Deployment",
                name=deployment_name,
            )
            template = deployment["spec"]["template"]
            pod = template["spec"]
            volumes = {volume["name"]: volume for volume in pod["volumes"]}
            env = _environment(_container(deployment, container_name))

            self.assertNotIn(
                "checksum.garz.ai/agent-workloads-token-secret",
                template["metadata"]["annotations"],
            )
            self.assertIn("projected-workload-identity-token", volumes)
            self.assertEqual(
                volumes["projected-workload-identity-token"]["projected"][
                    "sources"
                ][0]["serviceAccountToken"]["audience"],
                "mandate-api",
            )
            self.assertNotIn("MANDATE_WORKLOAD_IDENTITY_TOKEN", env)
            self.assertEqual(
                env["MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE"]["value"],
                "/var/run/mandate/workload-identity/token",
            )
            self.assertFalse(any("secret" in volume for volume in pod["volumes"]))

    def test_governed_release_overlap_renders_distinct_tuple_subjects(self) -> None:
        values = self._governed_values()
        previous_by_worker = {
            "opencode.proposer": {
                "codeDigest": "sha256:" + "1" * 64,
                "manifestDigest": "sha256:" + "2" * 64,
                "imageDigest": "sha256:" + "3" * 64,
            },
            "opencode.apply_executor": {
                "codeDigest": "sha256:" + "4" * 64,
                "manifestDigest": "sha256:" + "5" * 64,
                "imageDigest": "sha256:" + "6" * 64,
            },
        }
        values["opencodeProposer"]["identity"] = {
            "previousRelease": previous_by_worker["opencode.proposer"]
        }
        values["opencodeApplyExecutor"]["identity"] = {
            "previousRelease": previous_by_worker["opencode.apply_executor"]
        }

        documents = _render(values)
        accounts = {
            document["metadata"]["name"]: document
            for document in documents
            if document.get("kind") == "ServiceAccount"
        }
        for worker_id, worker_key in (
            ("opencode.proposer", "opencodeProposer"),
            ("opencode.apply_executor", "opencodeApplyExecutor"),
        ):
            current_name = _service_account_name(
                worker_id=worker_id,
                release=values["mandateReleasePins"][worker_id],
            )
            previous_name = _service_account_name(
                worker_id=worker_id,
                release=previous_by_worker[worker_id],
            )
            self.assertNotEqual(current_name, previous_name)
            self.assertIn(current_name, accounts)
            self.assertIn(previous_name, accounts)
            self.assertEqual(
                accounts[previous_name]["metadata"]["labels"][
                    "mandate.cesaregarza.io/worker-id"
                ],
                worker_id,
            )
            deployment_name = (
                "agent-workloads-opencode-proposer"
                if worker_key == "opencodeProposer"
                else "agent-workloads-opencode-apply-executor"
            )
            deployment = _find(
                documents,
                kind="Deployment",
                name=deployment_name,
            )
            self.assertEqual(
                deployment["spec"]["template"]["spec"]["serviceAccountName"],
                current_name,
            )

    def test_governed_mode_rejects_cross_worker_hmac_key_reuse(self) -> None:
        values = self._governed_values()
        values["opencodeApplyExecutor"]["secretEnv"][
            "MANDATE_WORKLOAD_IDENTITY_TOKEN"
        ] = "OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN"

        result = _render_process(values)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "proposer and apply HMAC secret keys must differ",
            result.stderr,
        )

    def test_governed_mode_requires_both_network_policies(self) -> None:
        for worker_key in ("opencodeProposer", "opencodeApplyExecutor"):
            with self.subTest(worker_key=worker_key):
                values = self._governed_values()
                values[worker_key]["networkPolicy"]["enabled"] = False

                result = _render_process(values)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"governedCore requires {worker_key}.networkPolicy.enabled",
                    result.stderr,
                )

    def test_governed_mode_rejects_runtime_image_tuple_drift(self) -> None:
        values = self._governed_values()
        values["opencodeApplyExecutor"]["image"]["digest"] = "sha256:" + "9" * 64

        result = _render_process(values)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "image.digest must equal mandateReleasePins",
            result.stderr,
        )

    def test_handoff_mode_schema_rejects_unknown_values(self) -> None:
        values = self._governed_values()
        values["opencodeArtifactHandoff"]["mode"] = "sharedMaybe"

        result = _render_process(values)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("opencodeArtifactHandoff", result.stderr)
        self.assertIn("mode", result.stderr)
        self.assertIn("legacySharedVolume", result.stderr)
        self.assertIn("governedCore", result.stderr)


if __name__ == "__main__":
    unittest.main()
