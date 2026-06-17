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
        cls.agent_workloads_values = _load_yaml(
            REPO_ROOT / "apps" / "agent-workloads" / "values.yaml"
        )
        cls.control_plane_application = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "agent-control-plane.yaml"
        )
        cls.model_gateway_controls = _load_yaml(
            REPO_ROOT
            / "apps"
            / "agent-control-plane-runtime-controls"
            / "configmap.yaml"
        )

    def test_opencode_proposer_import_is_overlay_pinned_and_proposal_only(self) -> None:
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
        release_pin = self.agent_workloads_values["mandateReleasePins"][
            "opencode.proposer"
        ]

        opencode = imports_by_id["opencode.proposer"]
        self.assertEqual(
            opencode["manifest_path"],
            "registries/imports/agent-opencode.proposer.json",
        )
        self.assertEqual(opencode["manifest_digest"], release_pin["manifestDigest"])
        self.assertEqual(opencode["image_digest"], release_pin["imageDigest"])
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
        self.assertEqual(capability["session_authority_budget"]["max_operations"], 100)
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
        self.assertEqual(manifest["digest"], release_pin["manifestDigest"])
        self.assertEqual(manifest["code_digest"], release_pin["codeDigest"])
        self.assertEqual(manifest["image"]["digest"], release_pin["imageDigest"])
        self.assertEqual(
            manifest["capability_metadata"]["agent_workloads.opencode_propose"],
            {"consequence_class": "reversible_staging"},
        )

    def test_opencode_apply_import_is_executor_only_and_admin_confirmed(self) -> None:
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
        release_pin = self.agent_workloads_values["mandateReleasePins"][
            "opencode.apply_executor"
        ]

        opencode_apply = imports_by_id["opencode.apply_executor"]
        self.assertEqual(
            opencode_apply["manifest_path"],
            "registries/imports/agent-opencode.apply_executor.json",
        )
        self.assertEqual(
            opencode_apply["manifest_digest"], release_pin["manifestDigest"]
        )
        self.assertEqual(opencode_apply["image_digest"], release_pin["imageDigest"])
        self.assertEqual(
            opencode_apply["agent"]["execution_posture"],
            "capability_worker",
        )
        self.assertEqual(opencode_apply["agent"]["network_access"], "broker_only")
        self.assertIs(opencode_apply["agent"]["executor"], True)
        self.assertNotIn("model_gateway_token", opencode_apply["agent"])

        capability = opencode_apply["capabilities"]["agent_workloads.opencode_apply"]
        self.assertEqual(capability["approval_mode"], "admin_confirm")
        self.assertEqual(
            capability["output_schema"],
            "agent_workloads_opencode_apply_result_v1",
        )
        self.assertEqual(capability["session_authority_budget"]["max_operations"], 1)
        self.assertEqual(
            capability["session_authority_budget"]["session_taint"],
            "prod_authority",
        )
        self.assertEqual(
            capability["artifacts"],
            {"allowed": True, "broker_required": False},
        )
        self.assertEqual(
            capability["disclosure"]["artifact_classes_allowed"],
            ["opencode_apply_result"],
        )
        self.assertEqual(
            capability["disclosure"]["max_confidentiality_level_out"],
            "customer_visible",
        )
        self.assertIs(
            capability["disclosure"]["require_output_redaction_pass"],
            True,
        )
        self.assertIs(capability["disclosure"]["require_result_schema"], True)
        self.assertEqual(
            capability["result_contract"]["output_schema"],
            "agent_workloads_opencode_apply_result_v1",
        )
        released_fields = set(capability["result_contract"]["released_result_fields"])
        self.assertGreaterEqual(
            released_fields,
            {
                "output_text",
                "operation_status",
                "action_id",
                "branch",
                "commit_sha",
                "applied_diff_sha256",
                "proposal_diff_sha256",
                "changed_files",
                "base_repo",
                "base_ref_name",
                "base_commit_sha",
                "base_tree_sha",
            },
        )
        self.assertTrue(
            released_fields.isdisjoint(
                {
                    "diff",
                    "patch",
                    "unified_diff",
                    "remote_ref",
                    "pull_request_url",
                    "pr_url",
                    "delivery_status",
                }
            )
        )
        self.assertEqual(
            capability["disclosure_summary"]["artifact_classes_allowed"],
            ["opencode_apply_result"],
        )
        self.assertIn(
            "Remote ref and PR URL arrive only through the deliverer callback after "
            "confirmed write.",
            capability["negative_affordances"],
        )

        manifest = json.loads(self.data["agent-opencode.apply_executor.json"])
        self.assertEqual(manifest["id"], "opencode.apply_executor")
        self.assertEqual(manifest["digest"], release_pin["manifestDigest"])
        self.assertEqual(manifest["code_digest"], release_pin["codeDigest"])
        self.assertEqual(manifest["image"]["digest"], release_pin["imageDigest"])
        self.assertEqual(
            manifest["capability_metadata"]["agent_workloads.opencode_apply"],
            {"consequence_class": "consequential"},
        )
        self.assertEqual(manifest["evals"]["required"], ["eval.opencode_apply_smoke"])

    def test_opencode_policy_and_eval_overlay_are_mounted(self) -> None:
        policy = YAML_PARSER.load(self.data["policy.prod.yaml"])
        bindings_by_id = {item["id"]: item for item in policy["bindings"]}
        binding = bindings_by_id["private-admin-controlled-capabilities"]
        synthetic_binding = bindings_by_id["synthetic-live-verify-probe"]

        self.assertEqual(
            synthetic_binding["users"]["authorized"],
            ["mandate-live-probe"],
        )
        self.assertEqual(
            synthetic_binding["capabilities"]["allow"],
            ["mandate.deploy.smoke"],
        )
        self.assertEqual(synthetic_binding["users"].get("admins"), [])
        self.assertNotIn("approval_overrides", synthetic_binding["capabilities"])

        self.assertIn(
            "agent_workloads.opencode_propose",
            binding["capabilities"]["allow"],
        )
        self.assertIn(
            "agent_workloads.opencode_apply",
            binding["capabilities"]["allow"],
        )
        self.assertEqual(
            binding["capabilities"]["approval_overrides"][
                "agent_workloads.opencode_apply"
            ],
            "admin_confirm",
        )
        self.assertEqual(policy["defaults"]["max_cost_usd_per_job"], 0.25)
        self.assertEqual(
            policy["defaults"]["aggregate_budget"]["per_capability_daily_usd"][
                "agent_workloads.opencode_propose"
            ],
            1.0,
        )
        self.assertEqual(
            policy["defaults"]["aggregate_budget"]["per_capability_daily_usd"][
                "agent_workloads.opencode_apply"
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
        self.assertEqual(
            evals_by_id["eval.opencode_apply_smoke"]["dataset"],
            "registries/imports/opencode_apply_smoke.jsonl",
        )
        self.assertIn("opencode_proposer_smoke.jsonl", self.data)
        self.assertIn("opencode_apply_smoke.jsonl", self.data)

        mounts = {
            mount["mountPath"]: mount
            for mount in self.control_plane_values["extraVolumeMounts"]
        }
        self.assertEqual(
            mounts["/app/registries/evals.yaml"]["subPath"],
            "evals.yaml",
        )

    def test_synthetic_live_verify_uses_dedicated_probe_actor(self) -> None:
        synthetic = self.control_plane_values["syntheticLiveVerify"]

        self.assertTrue(synthetic["enabled"])
        self.assertEqual(synthetic["schedule"], "*/5 * * * *")
        self.assertEqual(synthetic["baseUrl"], "http://agent-control-plane:80")
        self.assertEqual(
            synthetic["actor"],
            {
                "platform": "synthetic",
                "guildId": "614277943706910722",
                "channelId": "1480483954694819940",
                "userId": "mandate-live-probe",
                "roles": ["synthetic_probe"],
            },
        )
        self.assertEqual(
            synthetic["replyTarget"],
            {
                "channelId": "1480483954694819940",
                "messageId": "scheduled-synthetic-live-verify",
            },
        )

    def test_prometheus_alerts_on_failed_synthetic_live_verify_job(self) -> None:
        rules_template = (
            REPO_ROOT
            / "helm"
            / "splattop"
            / "templates"
            / "monitoring-prometheus-rules-configmap.yaml"
        ).read_text()

        self.assertIn("MandateSyntheticLiveVerifyFailed", rules_template)
        self.assertIn(
            'owner_name="agent-control-plane-synthetic-live-verify"',
            rules_template,
        )
        self.assertIn(
            'kube_job_status_failed{namespace="agent-control-plane"}',
            rules_template,
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
        self.assertIn("opencode.apply_executor", subjects["worker_service"])

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

    def test_model_gateway_kill_switch_and_revocation_files_are_wired(self) -> None:
        values = self.control_plane_values
        env = values["env"]
        self.assertEqual(
            env["AGENT_PLATFORM_MODEL_GATEWAY_KILL_SWITCH_FILE"],
            "/app/model-gateway-controls/kill-switch",
        )
        self.assertEqual(
            env["AGENT_PLATFORM_MODEL_GATEWAY_REVOCATION_FILE"],
            "/app/model-gateway-controls/revocations.txt",
        )

        volumes = {volume["name"]: volume for volume in values["extraVolumes"]}
        self.assertEqual(
            volumes["model-gateway-controls"]["configMap"]["name"],
            "agent-control-plane-model-gateway-controls",
        )

        mounts = {mount["name"]: mount for mount in values["extraVolumeMounts"]}
        controls_mount = mounts["model-gateway-controls"]
        self.assertEqual(controls_mount["mountPath"], "/app/model-gateway-controls")
        self.assertTrue(controls_mount["readOnly"])
        self.assertNotIn("subPath", controls_mount)

        configmap = self.model_gateway_controls
        self.assertEqual(configmap["kind"], "ConfigMap")
        self.assertEqual(
            configmap["metadata"]["name"],
            "agent-control-plane-model-gateway-controls",
        )
        self.assertEqual(configmap["metadata"]["namespace"], "agent-control-plane")
        self.assertEqual(
            configmap["metadata"]["annotations"][
                "mandate.cesaregarza.io/operator-editable"
            ],
            "true",
        )
        self.assertNotIn("kill-switch", configmap["data"])
        self.assertIn("revocations.txt", configmap["data"])

        raw_sources = [
            source
            for source in self.control_plane_application["spec"]["sources"]
            if source.get("path") == "apps/agent-control-plane-runtime-controls"
        ]
        self.assertEqual(len(raw_sources), 1)
        self.assertEqual(
            raw_sources[0]["repoURL"],
            "https://github.com/cesaregarza/SplatTopConfig",
        )

    def test_control_plane_pin_understands_opencode_executor_imports(self) -> None:
        sources = self.control_plane_application["spec"]["sources"]
        mandate_source = next(
            source
            for source in sources
            if source["repoURL"] == "git@github.com:cesaregarza/agent-platform.git"
        )
        target_revision = mandate_source["targetRevision"]
        self.assertEqual(
            self.control_plane_values["image"]["tag"],
            f"sha-{target_revision[:12]}",
        )


if __name__ == "__main__":
    unittest.main()
