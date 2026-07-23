import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


class AgentControlPlanePostgresSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cronjob = _load_yaml(
            REPO_ROOT
            / "apps"
            / "agent-control-plane-runtime-controls"
            / "postgres-sweep-cronjob.yaml"
        )
        cls.values = _load_yaml(
            REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml"
        )
        cls.application = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "agent-control-plane.yaml"
        )
        cls.readme = (
            REPO_ROOT / "apps" / "agent-control-plane" / "README.md"
        ).read_text()

    def test_cronjob_is_scheduled_and_bounded(self) -> None:
        self.assertEqual(self.cronjob["apiVersion"], "batch/v1")
        self.assertEqual(self.cronjob["kind"], "CronJob")
        self.assertEqual(
            self.cronjob["metadata"]["name"],
            "agent-control-plane-postgres-sweep",
        )
        self.assertEqual(
            self.cronjob["metadata"]["namespace"],
            self.application["spec"]["destination"]["namespace"],
        )

        spec = self.cronjob["spec"]
        self.assertEqual(spec["schedule"], "*/10 * * * *")
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        self.assertEqual(spec["startingDeadlineSeconds"], 300)
        self.assertEqual(spec["successfulJobsHistoryLimit"], 1)
        self.assertEqual(spec["failedJobsHistoryLimit"], 3)
        self.assertEqual(spec["jobTemplate"]["spec"]["backoffLimit"], 1)
        self.assertEqual(
            spec["jobTemplate"]["spec"]["activeDeadlineSeconds"],
            600,
        )
        self.assertEqual(
            spec["jobTemplate"]["spec"]["ttlSecondsAfterFinished"],
            600,
        )

    def test_cronjob_uses_the_pinned_control_plane_image_and_secret(self) -> None:
        pod_spec = _pod_spec(self.cronjob)
        container = pod_spec["containers"][0]
        expected_image = (
            f"{self.values['image']['repository']}:{self.values['image']['tag']}"
        )

        self.assertEqual(container["name"], "postgres-sweep")
        self.assertEqual(container["image"], expected_image)
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(container["command"], ["mandate-postgres-sweep"])
        self.assertEqual(
            container["args"],
            [
                "--queued-lease-expiry-limit",
                "100",
                "--approval-expiry-limit",
                "100",
                "--retention-days",
                "30",
                "--batch-size",
                "100",
            ],
        )
        self.assertEqual(
            container["envFrom"],
            [{"secretRef": {"name": "agent-control-plane-secrets"}}],
        )
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "regcred"}])

    def test_cronjob_has_one_connection_budget_and_prod_validation_config(
        self,
    ) -> None:
        env = {
            item["name"]: item["value"]
            for item in _pod_spec(self.cronjob)["containers"][0]["env"]
        }
        self.assertEqual(env["AGENT_PLATFORM_POSTGRES_POOL_MIN_SIZE"], "0")
        self.assertEqual(env["AGENT_PLATFORM_POSTGRES_POOL_MAX_SIZE"], "1")

        for name in (
            "AGENT_PLATFORM_ENVIRONMENT",
            "AGENT_PLATFORM_WORKLOAD_IDENTITY_ISSUER",
            "AGENT_PLATFORM_WORKLOAD_IDENTITY_AUDIENCE",
            "AGENT_PLATFORM_WORKLOAD_IDENTITY_REQUIRED_SCOPES",
            "AGENT_PLATFORM_WORKLOAD_IDENTITY_ALLOWED_SUBJECTS_JSON",
        ):
            self.assertEqual(env[name], self.values["env"][name])
        self.assertEqual(env["AGENT_PLATFORM_WORKLOAD_IDENTITY_MODE"], "hmac")
        self.assertEqual(
            self.values["env"]["AGENT_PLATFORM_WORKLOAD_IDENTITY_MODE"],
            "kubernetes_hybrid",
        )

    def test_cronjob_is_non_root_and_covered_by_control_plane_egress(self) -> None:
        pod_spec = _pod_spec(self.cronjob)
        container = pod_spec["containers"][0]
        labels = (
            self.cronjob["spec"]["jobTemplate"]["spec"]["template"]["metadata"][
                "labels"
            ]
        )

        self.assertEqual(
            {
                "app.kubernetes.io/name": labels["app.kubernetes.io/name"],
                "app.kubernetes.io/instance": labels[
                    "app.kubernetes.io/instance"
                ],
            },
            {
                "app.kubernetes.io/name": "agent-control-plane",
                "app.kubernetes.io/instance": "agent-control-plane",
            },
        )
        self.assertEqual(labels["app.kubernetes.io/component"], "postgres-sweep")
        self.assertEqual(pod_spec["serviceAccountName"], "agent-control-plane")
        self.assertFalse(pod_spec["automountServiceAccountToken"])
        self.assertEqual(pod_spec["restartPolicy"], "Never")
        self.assertEqual(
            pod_spec["securityContext"],
            {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "fsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        self.assertEqual(
            container["securityContext"],
            {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": True,
                "capabilities": {"drop": ["ALL"]},
            },
        )
        self.assertEqual(
            container["volumeMounts"],
            [{"name": "tmp", "mountPath": "/tmp"}],
        )
        self.assertEqual(pod_spec["volumes"], [{"name": "tmp", "emptyDir": {}}])

    def test_raw_cronjob_is_the_only_production_sweep_owner(self) -> None:
        self.assertFalse(self.values["postgresSweep"]["enabled"])
        runtime_sources = [
            source
            for source in self.application["spec"]["sources"]
            if source.get("path") == "apps/agent-control-plane-runtime-controls"
        ]
        self.assertEqual(len(runtime_sources), 1)
        self.assertEqual(runtime_sources[0]["targetRevision"], "main")

    def test_retention_and_connection_decisions_are_documented(self) -> None:
        self.assertIn("single transient", self.readme)
        self.assertIn("terminal-row retention decision is 30 days", self.readme)
        self.assertIn("archives", self.readme)
        self.assertIn("permanent `agent_runs` and `run_events`", self.readme)


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = YAML_PARSER.load(path.read_text())
    if not isinstance(payload, dict):
        raise AssertionError(f"{path} must contain a YAML mapping")
    return payload


def _pod_spec(cronjob: dict[str, Any]) -> dict[str, Any]:
    return cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]


if __name__ == "__main__":
    unittest.main()
