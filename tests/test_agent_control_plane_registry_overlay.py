from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text())
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded


class AgentControlPlaneRegistryOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configmap = _load_yaml(
            REPO_ROOT / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
        )
        cls.data = configmap["data"]
        cls.control_plane_values = _load_yaml(
            REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml"
        )

    def test_opencode_proposer_import_is_overlay_pinned_and_proposal_only(self) -> None:
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}

        opencode = imports_by_id["opencode.proposer"]
        self.assertEqual(
            opencode["manifest_path"],
            "registries/imports/agent-opencode.proposer.json",
        )
        self.assertEqual(
            opencode["manifest_digest"],
            "sha256:a9cb5036952c2e2928fce6e2ef867fb56e7b45a9708a850ab57d4b41dbf529b0",
        )
        self.assertEqual(
            opencode["image_digest"],
            "sha256:afcdd7b36aff7bca91d23f90683fb2b3db5226b2a6c8b1616f345db2d1f55579",
        )
        self.assertEqual(opencode["agent"]["execution_posture"], "hosted_harness")
        self.assertIs(opencode["agent"]["model_gateway_token"], True)
        self.assertEqual(opencode["agent"]["network_access"], "broker_only")

        capability = opencode["capabilities"]["agent_workloads.opencode_propose"]
        self.assertEqual(capability["model_lease"]["allowed_tier"], "fast")
        self.assertEqual(
            capability["model_lease"]["allowed_profiles"],
            ["openai.gpt-5.3-codex-spark"],
        )
        self.assertEqual(capability["model_lease"]["max_cost_usd"], 0.25)
        self.assertEqual(capability["session_authority_budget"]["max_operations"], 1)
        self.assertEqual(
            capability["disclosure"]["artifact_classes_allowed"],
            ["opencode_proposal"],
        )
        self.assertEqual(
            capability["artifacts"],
            {"allowed": True, "broker_required": False},
        )

        manifest = json.loads(self.data["agent-opencode.proposer.json"])
        self.assertEqual(manifest["id"], "opencode.proposer")
        self.assertEqual(
            manifest["digest"],
            "sha256:a9cb5036952c2e2928fce6e2ef867fb56e7b45a9708a850ab57d4b41dbf529b0",
        )
        self.assertEqual(
            manifest["code_digest"],
            "sha256:d20f282c159db6a5a9039c50f745825ee58e27836e35637e87fd6edfa9db7e20",
        )
        self.assertEqual(
            manifest["capability_metadata"]["agent_workloads.opencode_propose"],
            {"consequence_class": "reversible_staging"},
        )

    def test_opencode_proposer_policy_and_eval_overlay_are_mounted(self) -> None:
        policy = YAML_PARSER.load(self.data["policy.prod.yaml"])
        binding = next(
            item
            for item in policy["bindings"]
            if item["id"] == "private-admin-controlled-capabilities"
        )
        self.assertIn(
            "agent_workloads.opencode_propose",
            binding["capabilities"]["allow"],
        )
        self.assertEqual(policy["defaults"]["max_cost_usd_per_job"], 0.25)
        self.assertEqual(
            policy["defaults"]["aggregate_budget"]["per_capability_daily_usd"][
                "agent_workloads.opencode_propose"
            ],
            1.0,
        )

        evals = YAML_PARSER.load(self.data["evals.yaml"])
        evals_by_id = {entry["id"]: entry for entry in evals["eval_suites"]}
        self.assertIn("eval.task_echo_smoke", evals_by_id)
        self.assertEqual(
            evals_by_id["eval.opencode_proposer_smoke"]["dataset"],
            "registries/imports/opencode_proposer_smoke.jsonl",
        )
        self.assertIn("opencode_proposer_smoke.jsonl", self.data)

        mounts = {
            mount["mountPath"]: mount
            for mount in self.control_plane_values["extraVolumeMounts"]
        }
        self.assertEqual(
            mounts["/app/registries/evals.yaml"]["subPath"],
            "evals.yaml",
        )

    def test_hosted_harness_safe_floor_and_token_handoff_are_configured(self) -> None:
        values = self.control_plane_values
        self.assertIn(
            "AGENT_PLATFORM_MODEL_GATEWAY_TOKEN_SECRET",
            values["secretKeys"],
        )
        subjects = json.loads(
            values["env"]["AGENT_PLATFORM_WORKLOAD_IDENTITY_ALLOWED_SUBJECTS_JSON"]
        )
        self.assertIn("opencode.proposer", subjects["worker_service"])

        env = values["env"]
        self.assertEqual(
            env["AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_EVIDENCE"],
            "deployment_attestation",
        )
        for key in (
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_EGRESS_JAIL",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_NO_AMBIENT_CREDENTIALS",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_MODEL_GATEWAY",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_COST_CUTOFF",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_CONSEQUENCE_ENFORCEMENT",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_AUDIT",
        ):
            self.assertEqual(env[key], "true")


if __name__ == "__main__":
    unittest.main()
