from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_OVERLAY_DIR = REPO_ROOT / "apps" / "agent-control-plane-registry-overlay"
REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
SKILL_BUNDLE_DIR = REPO_ROOT / "apps" / "agent-control-plane-skills"
YAML_PARSER = YAML(typ="safe")


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text())
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded


def _load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    loaded = [
        document
        for document in YAML_PARSER.load_all(path.read_text())
        if isinstance(document, dict)
    ]
    if not loaded:
        raise AssertionError(f"YAML documents expected: {path}")
    return loaded


def _load_registry_overlay_data() -> dict[str, str]:
    legacy_configmap_path = REGISTRY_OVERLAY_DIR / "configmap.yaml"
    if legacy_configmap_path.exists():
        configmap = _load_yaml(legacy_configmap_path)
        return configmap["data"]

    kustomization = _load_yaml(REGISTRY_OVERLAY_DIR / "kustomization.yaml")
    generators = kustomization.get("configMapGenerator") or []
    generator = next(
        item
        for item in generators
        if isinstance(item, dict)
        and item.get("name") == REGISTRY_OVERLAY_CONFIGMAP_NAME
    )
    data: dict[str, str] = {}
    for file_spec in generator["files"]:
        key, relative_path = file_spec.split("=", 1)
        data[key] = (REGISTRY_OVERLAY_DIR / relative_path).read_text()
    return data


class AgentControlPlaneRegistryOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _load_registry_overlay_data()
        cls.control_plane_values = _load_yaml(
            REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml"
        )
        cls.control_plane_secret = _load_yaml(
            REPO_ROOT
            / "secrets"
            / "agent-control-plane"
            / "runtime-secret.enc.yaml"
        )
        cls.agent_workloads_values = _load_yaml(
            REPO_ROOT / "apps" / "agent-workloads" / "values.yaml"
        )
        cls.control_plane_application = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "agent-control-plane.yaml"
        )
        cls.registry_overlay_application = _load_yaml(
            REPO_ROOT
            / "argocd"
            / "applications"
            / "agent-control-plane-registry-overlay.yaml"
        )
        cls.control_plane_skills_application = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "agent-control-plane-skills.yaml"
        )
        cls.control_plane_skills_kustomization = _load_yaml(
            SKILL_BUNDLE_DIR / "kustomization.yaml"
        )
        cls.control_plane_skills_configmap = _load_yaml(
            SKILL_BUNDLE_DIR / "configmap.yaml"
        )
        cls.control_plane_skills_rbac = _load_yaml_documents(
            SKILL_BUNDLE_DIR / "materialize-rbac.yaml"
        )
        cls.control_plane_skills_job = _load_yaml(
            SKILL_BUNDLE_DIR / "materialize-job.yaml"
        )
        cls.control_plane_skills_cronjob = _load_yaml(
            SKILL_BUNDLE_DIR / "materialize-cronjob.yaml"
        )
        cls.splattop_project = _load_yaml(
            REPO_ROOT / "argocd" / "projects" / "splattop-project.yaml"
        )
        cls.registry_overlay_restart_hook = _load_yaml(
            REGISTRY_OVERLAY_DIR / "restart-hook.yaml"
        )
        cls.model_gateway_controls = _load_yaml(
            REPO_ROOT
            / "apps"
            / "agent-control-plane-runtime-controls"
            / "configmap.yaml"
        )

    def test_registry_overlay_is_authored_as_kustomize_file_generator(self) -> None:
        kustomization = _load_yaml(REGISTRY_OVERLAY_DIR / "kustomization.yaml")
        self.assertFalse((REGISTRY_OVERLAY_DIR / "configmap.yaml").exists())
        self.assertEqual(kustomization["kind"], "Kustomization")
        self.assertIn("restart-rbac.yaml", kustomization["resources"])
        self.assertIn("restart-hook.yaml", kustomization["resources"])
        self.assertTrue(kustomization["generatorOptions"]["disableNameSuffixHash"])

        generator = next(
            item
            for item in kustomization["configMapGenerator"]
            if item["name"] == REGISTRY_OVERLAY_CONFIGMAP_NAME
        )
        self.assertEqual(
            set(self.data),
            {
                "workload_imports.yaml",
                "policy.prod.yaml",
                "evals.yaml",
                "agent-data.workspace_probe.json",
                "agent-opencode.proposer.json",
                "agent-opencode.apply_executor.json",
                "opencode_proposer_smoke.jsonl",
                "opencode_apply_smoke.jsonl",
            },
        )
        self.assertEqual(len(generator["files"]), len(self.data))

    def test_readonly_query_skills_sync_from_published_bundle_consumer(self) -> None:
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
        readonly_query = imports_by_id["data.workspace_probe"]["capabilities"][
            "agent_workloads.readonly_query"
        ]
        self.assertEqual(
            readonly_query["skills"],
            ["xscraper-schema", "xscraper-glossary"],
        )

        skills = self.control_plane_values["skills"]
        self.assertEqual(
            skills,
            {
                "enabled": True,
                "configMapName": "mandate-skill-packs",
                "mountPath": "/var/lib/mandate/skills",
            },
        )

        self.assertFalse(
            (
                REPO_ROOT / "argocd" / "applications" / "agent-workloads-skills.yaml"
            ).exists()
        )
        skills_app = self.control_plane_skills_application
        self.assertEqual(skills_app["spec"]["project"], "splattop")
        self.assertEqual(
            skills_app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
            "8",
        )
        self.assertEqual(
            skills_app["spec"]["source"],
            {
                "repoURL": "https://github.com/cesaregarza/GarzAICluster",
                "targetRevision": "main",
                "path": "apps/agent-control-plane-skills",
            },
        )
        self.assertEqual(
            skills_app["spec"]["destination"]["namespace"],
            "agent-control-plane",
        )
        self.assertEqual(
            skills_app["spec"]["syncPolicy"]["automated"],
            {"prune": True, "selfHeal": True},
        )
        self.assertEqual(
            skills_app["spec"]["ignoreDifferences"],
            [
                {
                    "group": "",
                    "kind": "ConfigMap",
                    "name": "mandate-skill-packs",
                    "namespace": "agent-control-plane",
                    "jsonPointers": ["/data"],
                }
            ],
        )
        self.assertNotIn(
            "https://github.com/cesaregarza/agent-workloads",
            self.splattop_project["spec"]["sourceRepos"],
        )

        self.assertEqual(
            set(self.control_plane_skills_kustomization["resources"]),
            {
                "configmap.yaml",
                "materialize-rbac.yaml",
                "materialize-job.yaml",
                "materialize-cronjob.yaml",
            },
        )
        self.assertEqual(
            self.control_plane_skills_configmap["metadata"]["name"],
            "mandate-skill-packs",
        )
        self.assertEqual(
            self.control_plane_skills_configmap["data"],
            {},
        )
        self.assertEqual(
            self.control_plane_skills_configmap["metadata"]["labels"]["garz.ai/source"],
            "ci-published-skill-bundle",
        )
        self.assertFalse(any((SKILL_BUNDLE_DIR / "bundle").glob("*")))

        role = next(
            document
            for document in self.control_plane_skills_rbac
            if document["kind"] == "Role"
        )
        self.assertEqual(role["rules"], [
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "resourceNames": ["mandate-skill-packs"],
                "verbs": ["get", "patch", "update"],
            }
        ])
        self.assertNotIn("create", role["rules"][0]["verbs"])
        self.assertNotIn("delete", role["rules"][0]["verbs"])

        self._assert_skill_bundle_materializer_pod(
            self.control_plane_skills_job["spec"]["template"]["spec"]
        )
        cron_spec = self.control_plane_skills_cronjob["spec"]
        self.assertEqual(cron_spec["schedule"], "*/5 * * * *")
        self.assertEqual(cron_spec["concurrencyPolicy"], "Forbid")
        self._assert_skill_bundle_materializer_pod(
            cron_spec["jobTemplate"]["spec"]["template"]["spec"]
        )

    def _assert_skill_bundle_materializer_pod(self, pod_spec: dict[str, Any]) -> None:
        self.assertEqual(pod_spec["serviceAccountName"], "mandate-skill-bundle-sync")
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "regcred"}])
        init_container = pod_spec["initContainers"][0]
        self.assertEqual(init_container["name"], "bundle")
        self.assertEqual(
            init_container["image"],
            "registry.digitalocean.com/sendouq/agent-workloads-skills:main",
        )
        self.assertEqual(init_container["imagePullPolicy"], "Always")

        materializer = pod_spec["containers"][0]
        script = materializer["command"][-1]
        self.assertIn("sha256sum -c SHA256SUMS", script)
        self.assertIn("agent-control-plane-skill-bundle.v1", script)
        self.assertIn("create configmap mandate-skill-packs", script)
        self.assertIn("--server-side", script)
        self.assertIn("--field-manager=mandate-skill-bundle-sync", script)
        self.assertNotIn("kubectl create -f", script)

    def test_registry_overlay_restart_hook_runs_without_selective_sync(self) -> None:
        annotations = self.registry_overlay_restart_hook["metadata"]["annotations"]
        self.assertEqual(annotations["argocd.argoproj.io/hook"], "PostSync")
        self.assertEqual(
            annotations["argocd.argoproj.io/hook-delete-policy"],
            "BeforeHookCreation",
        )
        self.assertEqual(
            self.registry_overlay_restart_hook["metadata"]["name"],
            "agent-control-plane-registry-overlay-restart",
        )
        self.assertNotIn("generateName", self.registry_overlay_restart_hook["metadata"])

        sync_options = set(
            self.registry_overlay_application["spec"]["syncPolicy"].get(
                "syncOptions", []
            )
        )
        self.assertIn("CreateNamespace=true", sync_options)
        self.assertNotIn("ApplyOutOfSyncOnly=true", sync_options)

    def test_ci_runs_real_registry_overlay_kustomize_render_gate(self) -> None:
        workflow = _load_yaml(REPO_ROOT / ".github" / "workflows" / "ci.yaml")
        job = workflow["jobs"]["agent-control-plane-registry-overlay-render"]
        run_steps = [
            step.get("run", "")
            for step in job["steps"]
            if isinstance(step, dict) and "run" in step
        ]

        self.assertTrue(
            any("kustomize version" in step for step in run_steps),
            "registry overlay render job must install standalone kustomize",
        )
        self.assertTrue(
            any(
                "scripts/check_agent_control_plane_registry_overlay_render.py" in step
                for step in run_steps
            ),
            "registry overlay render job must run the real kustomize render gate",
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

        orchestrate = opencode["capabilities"]["agent_workloads.opencode_orchestrate"]
        self.assertEqual(
            orchestrate["result_contract"]["output_schema"],
            "agent_workloads_opencode_orchestrate_result_v1",
        )
        released_fields = set(orchestrate["result_contract"]["released_result_fields"])
        self.assertIn("delegated_capability_id", released_fields)
        self.assertNotIn("capability_id", released_fields)

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
            binding["surface_identifiers"],
            {
                "guild_id": "1523242748822425750",
                "channel_id": "1523242750043226234",
            },
        )
        self.assertEqual(
            synthetic_binding["users"]["authorized"],
            ["mandate-live-probe"],
        )
        self.assertEqual(
            synthetic_binding["principal"],
            {
                "issuer": "synthetic",
                "subject_kind": "service",
                "tenant_id": "garzai-prod",
            },
        )
        self.assertEqual(
            synthetic_binding["capabilities"]["allow"],
            [
                "mandate.deploy.smoke",
                "agent_workloads.readonly_query",
                "agent_workloads.opencode_task",
            ],
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
        self.assertEqual(policy["defaults"]["max_cost_usd_per_job"], 10.0)
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
            evals_by_id["eval.readonly_sql_safety"]["applies_to"],
            ["data.readonly_sql"],
        )
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

    def test_synthetic_live_verify_runs_internal_deployment_and_readonly_probes(
        self,
    ) -> None:
        synthetic = self.control_plane_values["syntheticLiveVerify"]

        self.assertTrue(synthetic["enabled"])
        self.assertEqual(synthetic["schedule"], "*/5 * * * *")
        self.assertEqual(synthetic["baseUrl"], "http://agent-control-plane:80")
        self.assertEqual(
            synthetic["principal"],
            {
                "issuer": "synthetic",
                "subject_id": "mandate-live-probe",
                "subject_kind": "service",
                "tenant_id": "garzai-prod",
                "roles": ["synthetic_probe"],
                "provenance": {
                    "source": "synthetic-live-verify",
                    "identifiers": {
                        "tenant_ref": "garzai-prod",
                        "subject_ref": "mandate-live-probe",
                    },
                },
            },
        )
        self.assertEqual(
            synthetic["deliveryTarget"],
            {
                "kind": "internal",
                "target_ref": "synthetic-live-verify",
                "message_ref": "scheduled-synthetic-live-verify",
            },
        )
        self.assertEqual(
            synthetic["surfaceContext"],
            {
                "source": "synthetic-live-verify",
                "surface_ref": "synthetic-live-verify",
                "adapter_provenance": {
                    "source": "synthetic-live-verify",
                    "identifiers": {"probe": "scheduled"},
                },
            },
        )
        self.assertEqual(
            synthetic["trustedContext"],
            {"entitlements": [], "attachment_authorities": []},
        )
        journeys = {journey["id"]: journey for journey in synthetic["journeys"]}
        self.assertEqual(
            set(journeys),
            {"deployment-smoke", "readonly-query-skill-digests"},
        )
        self.assertEqual(
            journeys["deployment-smoke"]["required_result_fields"],
            ["output_text", "schema_version"],
        )
        readonly_query = journeys["readonly-query-skill-digests"]
        self.assertEqual(
            readonly_query["capability_id"],
            "agent_workloads.readonly_query",
        )
        self.assertEqual(readonly_query["required_result_fields"], ["output_text"])
        self.assertEqual(
            readonly_query["required_skill_ids"],
            ["xscraper-schema", "xscraper-glossary"],
        )
        self.assertIn("tool.started", readonly_query["required_event_types"])

    def test_prometheus_alerts_on_failed_synthetic_live_verify_job(self) -> None:
        rules_template = (
            REPO_ROOT
            / "helm"
            / "garz-observability"
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
        self.assertIn("kube_job_owner", rules_template)

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

    def test_trusted_edge_token_is_wired_with_legacy_alias(self) -> None:
        values = self.control_plane_values
        secret_data = self.control_plane_secret["stringData"]

        self.assertIn("AGENT_PLATFORM_TRUSTED_EDGE_TOKEN", values["secretKeys"])
        self.assertIn("AGENT_PLATFORM_OPENCLAW_TOKEN", values["secretKeys"])
        self.assertIn("AGENT_PLATFORM_TRUSTED_EDGE_TOKEN", secret_data)
        self.assertIn("AGENT_PLATFORM_OPENCLAW_TOKEN", secret_data)

    def test_git_deliverer_is_configured_for_mandate_sandbox_only(self) -> None:
        values = self.control_plane_values
        deliverer = values["gitDeliverer"]

        self.assertTrue(deliverer["enabled"])
        self.assertEqual(deliverer["replicaCount"], 1)
        self.assertEqual(
            deliverer["secretKeys"],
            ["AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN"],
        )
        self.assertNotIn(
            "AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN",
            values["secretKeys"],
        )
        self.assertEqual(deliverer["targetRepo"], "cesaregarza/mandate-sandbox")
        self.assertEqual(
            deliverer["remoteUrl"],
            "https://github.com/cesaregarza/mandate-sandbox",
        )
        self.assertEqual(deliverer["baseRef"], "main")
        self.assertEqual(
            deliverer["allowedBaseRepos"],
            ["https://github.com/cesaregarza/mandate-sandbox"],
        )
        self.assertIn(
            "AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN",
            self.control_plane_secret["stringData"],
        )

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
            "https://github.com/cesaregarza/GarzAICluster",
        )

    def test_model_gateway_provider_pins_are_scoped_to_gateway_process(self) -> None:
        values = self.control_plane_values
        api_pins = json.loads(values["env"]["AGENT_PLATFORM_PROVIDER_DIGEST_PINS_JSON"])
        gateway_pins = json.loads(
            values["modelGateway"]["env"]["AGENT_PLATFORM_PROVIDER_DIGEST_PINS_JSON"]
        )

        self.assertEqual(
            set(api_pins),
            {"model_gateway", "readonly-sql-broker"},
        )
        self.assertEqual(
            set(gateway_pins),
            {"model_gateway"},
        )
        self.assertEqual(
            api_pins["model_gateway"]["digest"],
            "sha256:c17d395b7bb81e69e91fe20ed90c3859349f19b084f76a4e8d89efb27848cc43",
        )
        self.assertEqual(
            api_pins["readonly-sql-broker"]["digest"],
            "sha256:73203a3ff8309cb762966f7559abf871f46a239c3136e7a5658eb069f52066c1",
        )
        self.assertEqual(
            gateway_pins["model_gateway"],
            {
                "digest": (
                    "sha256:"
                    "2a3b6afc068849e07f65badc6a2ac83f1062ba062b3c8085d6188c2b28a45d64"
                ),
                "protocol": "model_gateway",
            },
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
