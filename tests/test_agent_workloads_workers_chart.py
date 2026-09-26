from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm" / "agent-workloads"
VALUES = ROOT / "apps" / "agent-workloads" / "values.yaml"
YAML_SAFE = YAML(typ="safe")


def _load_values() -> dict[str, Any]:
    values = YAML_SAFE.load(VALUES.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise AssertionError("production values must be a mapping")
    return values


def _helm() -> str:
    helm = shutil.which("helm")
    if helm is None:
        raise unittest.SkipTest("Helm is required for chart rendering")
    return helm


def _render(values: dict[str, Any]) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as temp_dir:
        values_path = Path(temp_dir) / "values.yaml"
        with values_path.open("w", encoding="utf-8") as stream:
            YAML_SAFE.dump(values, stream)
        result = subprocess.run(
            [
                _helm(),
                "template",
                "agent-workloads",
                str(CHART),
                "-f",
                str(values_path),
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
    if result.returncode:
        raise AssertionError(result.stderr)
    return [
        document
        for document in YAML_SAFE.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _find(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for document in documents:
        if (
            document.get("kind") == kind
            and document.get("metadata", {}).get("name") == name
        ):
            return document
    raise AssertionError(f"{kind}/{name} was not rendered")


class AgentWorkloadsWorkersChartTests(unittest.TestCase):
    def test_production_is_one_projected_workers_map(self) -> None:
        values = _load_values()
        self.assertEqual(
            set(values["workers"]),
            {"data.workspace_probe", "opencode.apply_executor", "opencode.proposer"},
        )
        self.assertNotIn("projectedWorkloadIdentity", values)
        self.assertNotIn("opencodeProposer", values)
        self.assertNotIn("opencodeApplyExecutor", values)
        for worker_id, worker in values["workers"].items():
            self.assertEqual(worker["identity"]["workerId"], worker_id)
            self.assertEqual(worker["identity"]["mode"], "projected")
            self.assertIn("hmacRollbackRelease", worker["identity"])
            self.assertIn("hmacRollbackTokenKey", worker["identity"])

        documents = _render(values)
        deployments = [d for d in documents if d.get("kind") == "Deployment"]
        self.assertEqual(len(deployments), 3)
        for deployment in deployments:
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            names = {entry["name"] for entry in container.get("env", [])}
            self.assertNotIn("OPENAI_SQL_BROKER_MODEL", names)
            self.assertNotIn("OPENAI_SQL_BROKER_REASONING_EFFORT", names)
            self.assertNotIn("OPENAI_SQL_BROKER_TIMEOUT_SECONDS", names)
            self.assertIn("MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE", names)

    def test_canonical_environment_bridge_matches_legacy_values(self) -> None:
        values = _load_values()
        documents = _render(values)
        for worker_id, worker in values["workers"].items():
            with self.subTest(worker_id=worker_id):
                deployment = _find(documents, "Deployment", worker["metadataName"])
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                env = {entry["name"]: entry for entry in container.get("env", [])}
                legacy_names = [
                    name for name in worker["env"] if name.startswith("AGENT_WORKLOADS_")
                ]
                self.assertTrue(legacy_names)
                for legacy_name in legacy_names:
                    canonical_name = (
                        "MANDATE_WORKER_ID"
                        if legacy_name == "AGENT_WORKLOADS_WORKER_ID"
                        else legacy_name.replace("AGENT_WORKLOADS_", "MANDATE_WORKER_", 1)
                    )
                    self.assertEqual(
                        env[canonical_name]["value"], env[legacy_name]["value"]
                    )
                for env_name, secret_key in worker["secretEnv"].items():
                    secret_ref = env[env_name]["valueFrom"]["secretKeyRef"]
                    self.assertEqual(secret_ref["key"], secret_key)
                    self.assertEqual(
                        secret_ref["name"],
                        env["AGENT_WORKLOADS_DATABASE_URL"]["valueFrom"]
                        ["secretKeyRef"]["name"],
                    )
                if worker["handoffMode"]:
                    self.assertEqual(
                        env["MANDATE_WORKER_OPENCODE_ARTIFACT_HANDOFF_MODE"]["value"],
                        worker["handoffMode"],
                    )
                    self.assertEqual(
                        env["AGENT_WORKLOADS_OPENCODE_ARTIFACT_HANDOFF_MODE"]["value"],
                        worker["handoffMode"],
                    )

    def test_present_canonical_worker_id_must_match_map_key(self) -> None:
        baseline = _load_values()
        for canonical_id in ("opencode.apply_executor", ""):
            with self.subTest(canonical_id=canonical_id):
                values = copy.deepcopy(baseline)
                values["workers"]["opencode.proposer"]["env"]["MANDATE_WORKER_ID"] = canonical_id
                with self.assertRaisesRegex(AssertionError, "MANDATE_WORKER_ID"):
                    _render(values)

    def test_worker_identity_and_governed_credential_guards_remain_enforced(
        self,
    ) -> None:
        baseline = _load_values()
        _render(baseline)
        for mutation, expected in (
            (
                lambda worker: worker["env"].update(
                    AGENT_WORKLOADS_WORKER_ID="opencode.apply_executor"
                ),
                "AGENT_WORKLOADS_WORKER_ID",
            ),
            (
                lambda worker: worker["env"].update(
                    MANDATE_WORKER_OPENCODE_ARTIFACT_HANDOFF_MODE="shadowed"
                ),
                "MANDATE_WORKER_OPENCODE_ARTIFACT_HANDOFF_MODE",
            ),
            (
                lambda worker: worker["secretEnv"].update(
                    OPENAI_API_KEY="provider-key"
                ),
                "static credentials",
            ),
            (
                lambda worker: worker.update(
                    metadataName="agent-workloads-opencode-apply-executor"
                ),
                "duplicates another worker",
            ),
        ):
            with self.subTest(expected=expected):
                values = copy.deepcopy(baseline)
                mutation(values["workers"]["opencode.proposer"])
                with self.assertRaisesRegex(AssertionError, expected):
                    _render(values)

    def test_fourth_projected_worker_requires_one_entry_and_pin(self) -> None:
        values = _load_values()
        before = _render(values)
        fourth_id = "fourth.worker"
        fourth = copy.deepcopy(values["workers"]["data.workspace_probe"])
        fourth["identity"]["workerId"] = fourth_id
        fourth["identity"]["previousRelease"] = None
        fourth["identity"].pop("hmacRollbackRelease")
        fourth["identity"].pop("hmacRollbackTokenKey")
        fourth["metadataName"] = "agent-workloads-fourth-worker"
        fourth["selectorLabels"]["app.kubernetes.io/name"] = "fourth-worker"
        fourth["env"]["AGENT_WORKLOADS_WORKER_ID"] = fourth_id
        fourth["env"]["MANDATE_WORKER_ID"] = fourth_id
        fourth["image"]["digest"] = "sha256:" + "4" * 64
        values["workers"][fourth_id] = fourth
        values["mandateReleasePins"][fourth_id] = {
            "codeDigest": "sha256:" + "5" * 64,
            "manifestDigest": "sha256:" + "6" * 64,
            "imageDigest": fourth["image"]["digest"],
        }

        documents = _render(values)
        from scripts.check_worker_chart_migration import canonical

        for old in before:
            same = _find(documents, old["kind"], old["metadata"]["name"])
            if old["kind"] == "Deployment":
                key = "checksum.garz.ai/agent-workloads-release-pins"
                self.assertNotEqual(
                    old["spec"]["template"]["metadata"]["annotations"][key],
                    same["spec"]["template"]["metadata"]["annotations"][key],
                )
                old["spec"]["template"]["metadata"]["annotations"][key] = same["spec"][
                    "template"
                ]["metadata"]["annotations"][key]
            self.assertEqual(canonical(old), canonical(same))
        deployment = _find(documents, "Deployment", fourth["metadataName"])
        pod = deployment["spec"]["template"]["spec"]
        self.assertIn(
            "projected-workload-identity-token",
            {volume["name"] for volume in pod["volumes"]},
        )
        self.assertTrue(
            any(
                entry["name"] == "MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE"
                for entry in pod["containers"][0]["env"]
            )
        )


if __name__ == "__main__":
    unittest.main()
