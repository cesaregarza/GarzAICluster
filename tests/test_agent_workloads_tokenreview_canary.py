from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def _load_yaml(path: Path) -> dict[str, Any]:
    value = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a mapping")
    return value


def _workspace_service_account_subject(
    values: dict[str, Any],
    *,
    release: dict[str, str] | None = None,
) -> str:
    worker_id = "data.workspace_probe"
    if release is None:
        release = values["mandateReleasePins"][worker_id]
    payload = {
        "schema_version": "workload_identity_bundle.v1",
        "code_digest": release["codeDigest"],
        "manifest_digest": release["manifestDigest"],
        "image_digest": release["imageDigest"],
    }
    suffix = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return (
        "system:serviceaccount:agent-workloads:"
        f"agent-workloads-data-workspace-probe-{suffix}"
    )


class AgentWorkloadsTokenReviewCanaryTests(unittest.TestCase):
    def test_core_tokenreview_substrate_is_api_only_and_fail_closed(self) -> None:
        values = _load_yaml(
            REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml"
        )
        reviewer = values["workloadIdentity"]["kubernetesTokenReview"]
        api_account = values["apiServiceAccount"]

        self.assertEqual(
            values["env"]["AGENT_PLATFORM_WORKLOAD_IDENTITY_MODE"],
            "kubernetes_hybrid",
        )
        self.assertEqual(
            reviewer,
            {
                "enabled": True,
                "apiServerUrl": "https://kubernetes.default.svc",
                "reviewerTokenAudience": "",
                "tokenMountPath": "/var/run/mandate/tokenreview/token",
                "caMountPath": "/var/run/mandate/tokenreview/ca.crt",
                "tokenExpirationSeconds": 3600,
                "timeoutSeconds": 5.0,
            },
        )
        self.assertEqual(
            api_account,
            {
                "create": True,
                "name": "agent-control-plane-tokenreview",
                "automountServiceAccountToken": False,
                "annotations": {},
            },
        )

    def test_tokenreview_rbac_grants_only_create_to_api_service_account(
        self,
    ) -> None:
        documents = [
            document
            for document in YAML_PARSER.load_all(
                (
                    REPO_ROOT
                    / "apps"
                    / "agent-control-plane-runtime-controls"
                    / "tokenreview-rbac.yaml"
                ).read_text(encoding="utf-8")
            )
            if isinstance(document, dict)
        ]
        role = next(
            document for document in documents if document["kind"] == "ClusterRole"
        )
        binding = next(
            document
            for document in documents
            if document["kind"] == "ClusterRoleBinding"
        )

        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": ["authentication.k8s.io"],
                    "resources": ["tokenreviews"],
                    "verbs": ["create"],
                }
            ],
        )
        self.assertEqual(
            binding["roleRef"],
            {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "agent-control-plane-tokenreview",
            },
        )
        self.assertEqual(
            binding["subjects"],
            [
                {
                    "kind": "ServiceAccount",
                    "name": "agent-control-plane-tokenreview",
                    "namespace": "agent-control-plane",
                }
            ],
        )

    def test_workspace_subject_matches_release_and_retains_hmac_rollback(
        self,
    ) -> None:
        values = _load_yaml(
            REPO_ROOT / "apps" / "agent-workloads" / "values.yaml"
        )
        imports = _load_yaml(
            REPO_ROOT
            / "apps"
            / "agent-control-plane-registry-overlay"
            / "registry"
            / "workload_imports.yaml"
        )
        workspace = next(
            entry
            for entry in imports["imports"]
            if entry["id"] == "data.workspace_probe"
        )
        projected = values["projectedWorkloadIdentity"]

        self.assertIs(projected["enabled"], True)
        self.assertEqual(projected["workerId"], "data.workspace_probe")
        self.assertEqual(projected["token"]["audience"], "mandate-api")
        self.assertEqual(
            workspace["agent"]["service_account_subject"],
            _workspace_service_account_subject(values),
        )
        self.assertEqual(
            workspace["agent"]["identity_audience"],
            "mandate-api",
        )
        previous_release = projected["previousRelease"]
        if previous_release is None:
            self.assertNotIn("previous_release", workspace["agent"])
        else:
            self.assertEqual(
                workspace["agent"]["previous_release"],
                {
                    "service_account_subject": _workspace_service_account_subject(
                        values,
                        release=previous_release,
                    ),
                    "code_digest": previous_release["codeDigest"],
                    "manifest_digest": previous_release["manifestDigest"],
                    "image_digest": previous_release["imageDigest"],
                },
            )
            self.assertNotEqual(
                workspace["agent"]["service_account_subject"],
                workspace["agent"]["previous_release"]["service_account_subject"],
            )

        self.assertNotIn("MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE", values["env"])
        self.assertNotIn("extraVolumes", values)
        self.assertNotIn("extraVolumeMounts", values)
        self.assertTrue(
            values["rolloutChecksums"]["workloadIdentityTokenSecret"].startswith(
                "sha256:"
            )
        )

        for key in ("opencodeProposer", "opencodeApplyExecutor"):
            self.assertEqual(values[key]["identity"]["mode"], "hmac")
            self.assertTrue(
                values[key]["secretEnv"]["MANDATE_WORKLOAD_IDENTITY_TOKEN"]
            )

    def test_registry_overlay_auto_syncs_before_manual_workload_activation(
        self,
    ) -> None:
        for application_name in ("agent-control-plane", "agent-workloads"):
            application = _load_yaml(
                REPO_ROOT / "argocd" / "applications" / f"{application_name}.yaml"
            )
            self.assertNotIn(
                "automated",
                application["spec"].get("syncPolicy", {}),
            )

        overlay = _load_yaml(
            REPO_ROOT
            / "argocd"
            / "applications"
            / "agent-control-plane-registry-overlay.yaml"
        )
        self.assertEqual(
            overlay["spec"]["syncPolicy"]["automated"],
            {"prune": True, "selfHeal": True},
        )
        self.assertNotIn(
            "ApplyOutOfSyncOnly=true",
            overlay["spec"]["syncPolicy"]["syncOptions"],
        )
