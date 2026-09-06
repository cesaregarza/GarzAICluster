from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "agent-workloads"
PRODUCTION_VALUES_PATH = REPO_ROOT / "apps" / "agent-workloads" / "values.yaml"
YAML_PARSER = YAML(typ="safe")

CURRENT_RELEASE = {
    "codeDigest": "sha256:" + "1" * 64,
    "manifestDigest": "sha256:" + "2" * 64,
    "imageDigest": "sha256:" + "3" * 64,
}
PREVIOUS_RELEASE = {
    **CURRENT_RELEASE,
    "imageDigest": "sha256:" + "4" * 64,
}
CURRENT_SERVICE_ACCOUNT = "agent-workloads-data-workspace-probe-2495342f395034d0f1c9"
PREVIOUS_SERVICE_ACCOUNT = "agent-workloads-data-workspace-probe-6a4b1ee0b9d51c8f72b8"


def _load_yaml(path: Path) -> dict[str, Any]:
    value = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a mapping")
    return value


def _render_process(
    values: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    with tempfile.TemporaryDirectory() as temp_dir:
        if values is None:
            values_path = PRODUCTION_VALUES_PATH
        else:
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


def _render(values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = _render_process(values)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    documents = [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]
    if not documents:
        raise AssertionError("helm rendered no Kubernetes documents")
    return documents


def _find_document(
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


def _container(
    deployment: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        if container["name"] == name:
            return container
    raise AssertionError(f"container {name} not rendered")


def _environment(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _opencode_service_account_name(
    values: dict[str, Any],
    *,
    worker_id: str,
    worker_key: str,
) -> str:
    return _release_service_account_name(
        worker_id,
        values["mandateReleasePins"][worker_id],
        prefix=values[worker_key]["identity"]["serviceAccountNamePrefix"],
    )


def _release_service_account_name(
    worker_id: str,
    release: dict[str, str],
    *,
    prefix: str,
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


class AgentWorkloadsProjectedIdentityChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.production_values = _load_yaml(PRODUCTION_VALUES_PATH)

    def test_retained_hmac_tuple_does_not_render_identity_resources(self) -> None:
        values = copy.deepcopy(self.production_values)
        values["projectedWorkloadIdentity"]["hmacRollbackRelease"] = {
            "codeDigest": "sha256:" + "7" * 64,
            "manifestDigest": "sha256:" + "8" * 64,
            "imageDigest": "sha256:" + "9" * 64,
        }
        self.assertEqual(_render(values), _render(self.production_values))

    def _projected_values(self) -> dict[str, Any]:
        values = copy.deepcopy(self.production_values)
        values["projectedWorkloadIdentity"] = {
            "enabled": True,
            "workerId": "data.workspace_probe",
            "serviceAccountNamePrefix": "agent-workloads",
            "serviceAccount": {
                "create": True,
                "annotations": {},
            },
            "token": {
                "audience": "mandate-api-staging",
                "expirationSeconds": 1800,
                "mountPath": "/var/run/mandate/workload-identity",
                "fileName": "token",
            },
            "previousRelease": copy.deepcopy(PREVIOUS_RELEASE),
        }
        values["mandateReleasePins"]["data.workspace_probe"] = copy.deepcopy(
            CURRENT_RELEASE
        )
        values["image"]["digest"] = CURRENT_RELEASE["imageDigest"]
        values["env"].pop("MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE", None)
        values["extraVolumes"] = []
        values["extraVolumeMounts"] = []
        return values

    def test_production_values_render_workspace_projected_and_opencode_hmac(
        self,
    ) -> None:
        documents = _render()
        worker = _find_document(
            documents,
            kind="Deployment",
            name="agent-workloads",
        )
        worker_pod = worker["spec"]["template"]["spec"]
        worker_volumes = {volume["name"]: volume for volume in worker_pod["volumes"]}
        workspace_account = _release_service_account_name(
            "data.workspace_probe",
            self.production_values["mandateReleasePins"]["data.workspace_probe"],
            prefix=self.production_values["projectedWorkloadIdentity"][
                "serviceAccountNamePrefix"
            ],
        )

        self.assertIs(worker_pod["automountServiceAccountToken"], False)
        self.assertEqual(worker_pod["serviceAccountName"], workspace_account)
        self.assertNotIn("workload-identity-token", worker_volumes)
        self.assertEqual(
            worker_volumes["projected-workload-identity-token"]["projected"],
            {
                "defaultMode": 0o440,
                "sources": [
                    {
                        "serviceAccountToken": {
                            "audience": "mandate-api",
                            "expirationSeconds": 3600,
                            "path": "token",
                        }
                    }
                ],
            },
        )
        self.assertEqual(
            _environment(_container(worker, "worker"))[
                "MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE"
            ]["value"],
            "/var/run/mandate/workload-identity/token",
        )
        self.assertNotIn(
            "checksum.garz.ai/agent-workloads-token-secret",
            worker["spec"]["template"]["metadata"]["annotations"],
        )

        service_accounts = [
            document
            for document in documents
            if document.get("kind") == "ServiceAccount"
        ]
        opencode_accounts = {
            worker_id: _opencode_service_account_name(
                self.production_values,
                worker_id=worker_id,
                worker_key=worker_key,
            )
            for worker_id, worker_key in (
                ("opencode.proposer", "opencodeProposer"),
                ("opencode.apply_executor", "opencodeApplyExecutor"),
            )
        }
        expected_service_accounts = {
            "agent-workloads",
            workspace_account,
            *opencode_accounts.values(),
        }
        previous_release = self.production_values["projectedWorkloadIdentity"][
            "previousRelease"
        ]
        if previous_release is not None:
            expected_service_accounts.add(
                _release_service_account_name(
                    "data.workspace_probe",
                    previous_release,
                    prefix=self.production_values["projectedWorkloadIdentity"][
                        "serviceAccountNamePrefix"
                    ],
                )
            )
        self.assertEqual(
            {account["metadata"]["name"] for account in service_accounts},
            expected_service_accounts,
        )

        for worker_id, deployment_name, container_name in (
            (
                "opencode.proposer",
                "agent-workloads-opencode-proposer",
                "opencode-proposer",
            ),
            (
                "opencode.apply_executor",
                "agent-workloads-opencode-apply-executor",
                "opencode-apply-executor",
            ),
        ):
            opencode = _find_document(
                documents,
                kind="Deployment",
                name=deployment_name,
            )
            opencode_pod = opencode["spec"]["template"]["spec"]
            self.assertIs(opencode_pod["automountServiceAccountToken"], False)
            self.assertEqual(
                opencode_pod["serviceAccountName"],
                opencode_accounts[worker_id],
            )
            self.assertNotIn(
                "projected-workload-identity-token",
                {volume["name"] for volume in opencode_pod["volumes"]},
            )
            self.assertIn(
                "secretKeyRef",
                _environment(_container(opencode, container_name))[
                    "MANDATE_WORKLOAD_IDENTITY_TOKEN"
                ]["valueFrom"],
            )

    def test_projected_current_and_previous_release_identity_render(self) -> None:
        canonical_payload = {
            "schema_version": "workload_identity_bundle.v1",
            "code_digest": CURRENT_RELEASE["codeDigest"],
            "manifest_digest": CURRENT_RELEASE["manifestDigest"],
            "image_digest": CURRENT_RELEASE["imageDigest"],
        }
        expected_suffix = hashlib.sha256(
            json.dumps(
                canonical_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:20]
        self.assertEqual(expected_suffix, "2495342f395034d0f1c9")

        documents = _render(self._projected_values())
        service_accounts = {
            document["metadata"]["name"]: document
            for document in documents
            if document.get("kind") == "ServiceAccount"
        }
        opencode_accounts = {
            worker_id: _opencode_service_account_name(
                self.production_values,
                worker_id=worker_id,
                worker_key=worker_key,
            )
            for worker_id, worker_key in (
                ("opencode.proposer", "opencodeProposer"),
                ("opencode.apply_executor", "opencodeApplyExecutor"),
            )
        }
        self.assertEqual(
            set(service_accounts),
            {
                "agent-workloads",
                CURRENT_SERVICE_ACCOUNT,
                PREVIOUS_SERVICE_ACCOUNT,
                *opencode_accounts.values(),
            },
        )
        self.assertNotEqual(CURRENT_SERVICE_ACCOUNT, PREVIOUS_SERVICE_ACCOUNT)
        for name in (CURRENT_SERVICE_ACCOUNT, PREVIOUS_SERVICE_ACCOUNT):
            self.assertIs(service_accounts[name]["automountServiceAccountToken"], False)

        worker = _find_document(
            documents,
            kind="Deployment",
            name="agent-workloads",
        )
        worker_template = worker["spec"]["template"]
        worker_pod = worker_template["spec"]
        self.assertIs(worker_pod["automountServiceAccountToken"], False)
        self.assertEqual(worker_pod["serviceAccountName"], CURRENT_SERVICE_ACCOUNT)
        self.assertNotIn(
            "checksum.garz.ai/agent-workloads-token-secret",
            worker_template["metadata"]["annotations"],
        )

        worker_volumes = {volume["name"]: volume for volume in worker_pod["volumes"]}
        self.assertNotIn("workload-identity-token", worker_volumes)
        self.assertEqual(
            worker_volumes["projected-workload-identity-token"]["projected"],
            {
                "defaultMode": 0o440,
                "sources": [
                    {
                        "serviceAccountToken": {
                            "audience": "mandate-api-staging",
                            "expirationSeconds": 1800,
                            "path": "token",
                        }
                    }
                ],
            },
        )
        self.assertFalse(
            any("secret" in volume for volume in worker_pod["volumes"]),
            "projected worker must not inject any legacy Secret volume",
        )
        worker_env = _environment(_container(worker, "worker"))
        self.assertEqual(
            worker_env["MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE"]["value"],
            "/var/run/mandate/workload-identity/token",
        )
        self.assertNotIn("MANDATE_WORKLOAD_IDENTITY_TOKEN", worker_env)

        for worker_id, deployment_name, container_name in (
            (
                "opencode.proposer",
                "agent-workloads-opencode-proposer",
                "opencode-proposer",
            ),
            (
                "opencode.apply_executor",
                "agent-workloads-opencode-apply-executor",
                "opencode-apply-executor",
            ),
        ):
            opencode = _find_document(
                documents,
                kind="Deployment",
                name=deployment_name,
            )
            opencode_template = opencode["spec"]["template"]
            opencode_pod = opencode_template["spec"]
            self.assertIs(opencode_pod["automountServiceAccountToken"], False)
            self.assertEqual(
                opencode_pod["serviceAccountName"],
                opencode_accounts[worker_id],
            )
            self.assertNotIn(
                "projected-workload-identity-token",
                {volume["name"] for volume in opencode_pod["volumes"]},
            )
            self.assertIn(
                "checksum.garz.ai/agent-workloads-token-secret",
                opencode_template["metadata"]["annotations"],
            )
            token = _environment(_container(opencode, container_name))[
                "MANDATE_WORKLOAD_IDENTITY_TOKEN"
            ]
            self.assertIn("secretKeyRef", token["valueFrom"])

    def test_projected_worker_is_decoupled_from_legacy_token_checksum(self) -> None:
        values = self._projected_values()
        original_documents = _render(values)
        values["rolloutChecksums"]["workloadIdentityTokenSecret"] = "sha256:" + "9" * 64
        changed_documents = _render(values)

        def annotations(
            documents: list[dict[str, Any]],
            deployment_name: str,
        ) -> dict[str, str]:
            deployment = _find_document(
                documents,
                kind="Deployment",
                name=deployment_name,
            )
            return deployment["spec"]["template"]["metadata"]["annotations"]

        self.assertEqual(
            annotations(original_documents, "agent-workloads"),
            annotations(changed_documents, "agent-workloads"),
        )
        self.assertNotEqual(
            annotations(original_documents, "agent-workloads-opencode-proposer"),
            annotations(changed_documents, "agent-workloads-opencode-proposer"),
        )

    def test_projected_identity_rejects_static_identity_injection(self) -> None:
        def add_static_secret_key(values: dict[str, Any]) -> None:
            values["secretKeys"].append("MANDATE_WORKLOAD_IDENTITY_TOKEN")

        def add_static_secret_env(values: dict[str, Any]) -> None:
            values["secretEnv"]["MANDATE_WORKLOAD_IDENTITY_TOKEN"] = "TOKEN"

        def add_static_env(values: dict[str, Any]) -> None:
            values["env"]["MANDATE_WORKLOAD_IDENTITY_TOKEN"] = "static"

        def add_token_file_env(values: dict[str, Any]) -> None:
            values["env"]["MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE"] = "/tmp/token"

        def add_legacy_secret_volume(values: dict[str, Any]) -> None:
            values["extraVolumes"] = [
                {
                    "name": "legacy-token",
                    "secret": {
                        "secretName": "agent-workloads-workload-identity-tokens"
                    },
                }
            ]

        def claim_projected_mount_path(values: dict[str, Any]) -> None:
            values["extraVolumeMounts"] = [
                {
                    "name": "other-token",
                    "mountPath": "/var/run/mandate/workload-identity",
                }
            ]

        def shadow_projected_token_file(values: dict[str, Any]) -> None:
            values["extraVolumes"] = [
                {
                    "name": "shadow-token",
                    "secret": {"secretName": "unrelated-secret"},
                }
            ]
            values["extraVolumeMounts"] = [
                {
                    "name": "shadow-token",
                    "mountPath": "/var/run/mandate/workload-identity/token",
                    "subPath": "token",
                    "readOnly": True,
                }
            ]

        cases: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
            (
                "secret key",
                add_static_secret_key,
                "must not inject a static workload identity token",
            ),
            (
                "secret env",
                add_static_secret_env,
                "must not inject a static workload identity token",
            ),
            (
                "plain env",
                add_static_env,
                "must not inject a static workload identity token",
            ),
            (
                "token file env",
                add_token_file_env,
                "token file env is chart-owned",
            ),
            (
                "legacy secret volume",
                add_legacy_secret_volume,
                "must not mount the legacy identity Secret",
            ),
            (
                "projected mount path",
                claim_projected_mount_path,
                "must not overlap the projected identity token path",
            ),
            (
                "shadow projected token file",
                shadow_projected_token_file,
                "must not overlap the projected identity token path",
            ),
        )

        for label, mutation, expected_error in cases:
            with self.subTest(label=label):
                values = self._projected_values()
                mutation(values)
                result = _render_process(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

    def test_projected_identity_requires_a_nonempty_audience(self) -> None:
        values = self._projected_values()
        values["projectedWorkloadIdentity"]["token"]["audience"] = ""
        result = _render_process(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "audience",
            result.stderr,
        )

    def test_projected_identity_binds_the_runtime_image_digest(self) -> None:
        cases = (
            (
                "missing runtime digest",
                "",
                "projected identity requires immutable image.digest",
            ),
            (
                "mutable-looking digest",
                "latest",
                "image.digest must be lowercase sha256:<64 hex>",
            ),
            (
                "different immutable digest",
                "sha256:" + "9" * 64,
                "image.digest must equal current release imageDigest",
            ),
        )

        for label, digest, expected_error in cases:
            with self.subTest(label=label):
                values = self._projected_values()
                values["image"]["digest"] = digest
                result = _render_process(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

    def test_projected_identity_rejects_unsafe_token_settings(self) -> None:
        cases: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
            (
                "short expiration",
                lambda values: values["projectedWorkloadIdentity"]["token"].update(
                    {"expirationSeconds": 0}
                ),
                "expirationSeconds",
            ),
            (
                "long expiration",
                lambda values: values["projectedWorkloadIdentity"]["token"].update(
                    {"expirationSeconds": 3601}
                ),
                "expirationSeconds",
            ),
            (
                "whitespace audience",
                lambda values: values["projectedWorkloadIdentity"]["token"].update(
                    {"audience": " mandate-api"}
                ),
                "audience must not have surrounding whitespace",
            ),
            (
                "non-normalized mount path",
                lambda values: values["projectedWorkloadIdentity"]["token"].update(
                    {"mountPath": "/var/run/../workload-identity"}
                ),
                "mountPath must be a normalized absolute path",
            ),
            (
                "nested file name",
                lambda values: values["projectedWorkloadIdentity"]["token"].update(
                    {"fileName": "nested/token"}
                ),
                "fileName must be a normalized basename",
            ),
        )

        for label, mutation, expected_error in cases:
            with self.subTest(label=label):
                values = self._projected_values()
                mutation(values)
                result = _render_process(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

    def test_projected_identity_rejects_service_account_collisions(self) -> None:
        cases: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
            (
                "current equals previous",
                lambda values: values["projectedWorkloadIdentity"].__setitem__(
                    "previousRelease",
                    copy.deepcopy(CURRENT_RELEASE),
                ),
                "projected previous and current release ServiceAccounts must differ",
            ),
            (
                "current equals shared",
                lambda values: values.setdefault("serviceAccount", {}).update(
                    {"create": False, "name": CURRENT_SERVICE_ACCOUNT}
                ),
                (
                    "projected current release ServiceAccount must differ from the "
                    "shared legacy ServiceAccount"
                ),
            ),
            (
                "previous equals shared",
                lambda values: values.setdefault("serviceAccount", {}).update(
                    {"create": False, "name": PREVIOUS_SERVICE_ACCOUNT}
                ),
                (
                    "projected previous release ServiceAccount must differ from the "
                    "shared legacy ServiceAccount"
                ),
            ),
        )

        for label, mutation, expected_error in cases:
            with self.subTest(label=label):
                values = self._projected_values()
                mutation(values)
                result = _render_process(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)


if __name__ == "__main__":
    unittest.main()
